"""Durable snapshots and notification queue, isolated by bot and connection."""
import json
import sqlite3
import time
from pathlib import Path


def media(message):
    if message.get("photo"):
        return "photo", message["photo"][-1]["file_id"]
    # Telegram animations also have a document field; animation must come first.
    for kind in ("animation", "document", "video", "voice", "audio", "sticker", "video_note"):
        if message.get(kind):
            return kind, message[kind]["file_id"]
    return None, None


def content(message):
    kind, file_id = media(message)
    return (message.get("text"), message.get("caption"), kind, file_id,
            message.get("entities"), message.get("caption_entities"))


def name(person):
    label = " ".join(filter(None, (person.get("first_name"), person.get("last_name"))))
    if person.get("username"):
        label += f" (@{person['username']})"
    return label or str(person.get("id", "неизвестно"))


def chunks(value, size=3500):
    # Telegram limits are measured in UTF-16 code units.
    chunk, units = [], 0
    for char in value:
        count = 2 if ord(char) > 0xffff else 1
        if units + count > size:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += count
    if chunk:
        yield "".join(chunk)


class Archive:
    def __init__(self, path, api, media_folder=None):
        self.api = api
        self.media_folder = Path(media_folder) if media_folder else Path(path).parent / "media"
        self.media_folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                connection TEXT, chat INTEGER, id INTEGER, data TEXT NOT NULL,
                PRIMARY KEY(connection, chat, id));
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, recipient INTEGER NOT NULL, method TEXT NOT NULL,
                parameters TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, event TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (
                id TEXT PRIMARY KEY, owner INTEGER, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imported_messages (
                source TEXT, chat INTEGER, type TEXT, id INTEGER, data TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source,chat,type,id));
            CREATE INDEX IF NOT EXISTS imported_lookup ON imported_messages(chat,id,type);
        ''')

    def imported_snapshot(self, connection, chat_id, message_id):
        row = self.db.execute('''SELECT m.data FROM imported_messages m
            JOIN imports i ON i.id=m.source WHERE i.owner=? AND m.chat=? AND m.id=?
            AND m.type='personal_chat' AND m.deleted=0 ORDER BY i.rowid DESC LIMIT 1''',
                              (connection["user"]["id"], chat_id, message_id)).fetchone()
        return json.loads(row[0]) if row else None

    def mark_imported_deleted(self, connection, chat_id, message_id):
        self.db.execute('''UPDATE imported_messages SET deleted=1 WHERE chat=? AND id=?
            AND type='personal_chat' AND source IN (SELECT id FROM imports WHERE owner=?)''',
                        (chat_id, message_id, connection["user"]["id"]))

    @property
    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def connection(self, connection_id):
        row = self.db.execute("SELECT data FROM connections WHERE id=?", (connection_id,)).fetchone()
        if row:
            return json.loads(row[0])
        result = self.api.call("getBusinessConnection", business_connection_id=connection_id)
        self.db.execute("INSERT INTO connections VALUES (?,?)", (connection_id, json.dumps(result)))
        return result

    def enqueue(self, recipient, method, **parameters):
        parameters["chat_id"] = recipient
        self.db.execute("INSERT INTO outbox(recipient,method,parameters) VALUES (?,?,?)",
                        (recipient, method, json.dumps(parameters)))

    def text(self, recipient, text):
        for part in chunks(text):
            self.enqueue(recipient, "sendMessage", text=part)

    def snapshot(self, recipient, label, message):
        self.text(recipient, label + "\n" + (message.get("text") or message.get("caption") or "[Медиа / сообщение без текста]"))
        kind, file_id = media(message)
        if kind and (file_id or message.get("_local_media")):
            methods = {"photo": "sendPhoto", "animation": "sendAnimation", "document": "sendDocument",
                       "video": "sendVideo", "voice": "sendVoice", "audio": "sendAudio",
                       "sticker": "sendSticker", "video_note": "sendVideoNote"}
            parameters = {kind: file_id} if file_id else {"_local_file": [kind, message["_local_media"]]}
            self.enqueue(recipient, methods[kind], **parameters)
            if file_id and message.get("_local_media"):
                row_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
                row = self.db.execute("SELECT parameters FROM outbox WHERE id=?", (row_id,)).fetchone()
                parameters = json.loads(row[0])
                parameters["_fallback_media"] = [kind, message["_local_media"]]
                self.db.execute("UPDATE outbox SET parameters=? WHERE id=?", (json.dumps(parameters), row_id))

    def process(self, update):
        # Saving content, queueing notifications, and checkpointing are atomic.
        with self.db:
            if update["update_id"] < self.offset:
                return
            if "business_connection" in update:
                connection = update["business_connection"]
                self.db.execute("INSERT OR REPLACE INTO connections VALUES (?,?)",
                                (connection["id"], json.dumps(connection)))
                if connection["is_enabled"]:
                    self.text(connection["user_chat_id"],
                              "Бот подключён. Удалённые и изменённые сообщения будут приходить сюда. "
                              "Программа на компьютере должна оставаться открытой.")
            for event in ("business_message", "edited_business_message"):
                if event not in update:
                    continue
                message = update[event]
                connection = self.connection(message["business_connection_id"])
                if not connection["is_enabled"]:
                    continue
                outgoing = message.get("from", {}).get("id") == connection["user"]["id"]
                message = dict(message)
                kind, file_id = media(message)
                if file_id and hasattr(self.api, "cache_media"):
                    message["_local_media"] = self.api.cache_media(file_id, self.media_folder / kind)
                key = (connection["id"], message["chat"]["id"], message["message_id"])
                previous = self.db.execute("SELECT data FROM messages WHERE connection=? AND chat=? AND id=?", key).fetchone()
                if event == "edited_business_message":
                    old = json.loads(previous[0]) if previous else self.imported_snapshot(connection, key[1], key[2])
                    if old is None or content(old) != content(message):
                        self.db.execute("INSERT INTO history(event,data) VALUES ('edit',?)",
                                        (json.dumps({"before": old, "after": message}),))
                        if not outgoing:
                            recipient = connection["user_chat_id"]
                            self.text(recipient, "✏️ Сообщение изменено\nОт: " + name(message.get("from", {}))
                                      + f"\nЧат: {name(message['chat'])}\nID: {message['message_id']}")
                            if old:
                                self.snapshot(recipient, "Было:", old)
                            else:
                                self.text(recipient, "Старая версия не сохранена: бот ещё не видел это сообщение.")
                            self.snapshot(recipient, "Стало:", message)
                self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?)", (*key, json.dumps(message)))
            if "deleted_business_messages" in update:
                event = update["deleted_business_messages"]
                connection = self.connection(event["business_connection_id"])
                if connection["is_enabled"]:
                    for message_id in event["message_ids"]:
                        key = (connection["id"], event["chat"]["id"], message_id)
                        row = self.db.execute("SELECT data FROM messages WHERE connection=? AND chat=? AND id=?", key).fetchone()
                        recipient = connection["user_chat_id"]
                        message = json.loads(row[0]) if row else self.imported_snapshot(connection, key[1], key[2])
                        if message:
                            self.db.execute("INSERT INTO history(event,data) VALUES ('delete',?)", (json.dumps(message),))
                            if message.get("from", {}).get("id") != connection["user"]["id"]:
                                self.snapshot(recipient,
                                              "🗑 Сообщение удалено\nОт: " + name(message.get("from", {}))
                                              + f"\nЧат: {name(event['chat'])}\nID: {message_id}", message)
                            self.mark_imported_deleted(connection, key[1], key[2])
                        # Unseen/outgoing deletions have no saved content to restore.
                        self.db.execute("DELETE FROM messages WHERE connection=? AND chat=? AND id=?", key)
            message = update.get("message", {})
            if message.get("chat", {}).get("type") == "private" and message.get("text", "").split(" ")[0] == "/start":
                self.text(message["chat"]["id"], "Подключи этого бота в настройках Telegram Business → Чат-боты. "
                          "После подключения я буду присылать сюда удалённые и изменённые сообщения.")
                # Explicit /start retries notifications if the owner previously blocked the bot.
                self.db.execute("UPDATE outbox SET next_attempt=0 WHERE recipient=?", (message["chat"]["id"],))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset',?)", (str(update["update_id"] + 1),))

    def deliver(self, stop, report):
        # One blocked owner must not stop archival or deliveries to another owner.
        rows = self.db.execute("SELECT id,recipient,method,parameters FROM outbox WHERE next_attempt<=? ORDER BY id LIMIT 30",
                               (time.time(),)).fetchall()
        blocked = set()
        for job_id, recipient, method, parameters in rows:
            if stop.is_set():
                return
            if recipient in blocked:
                continue
            try:
                data = json.loads(parameters)
                fallback = data.pop("_fallback_media", None)
                from telegram_api import ApiError
                try:
                    self.api.call(method, **data)
                except ApiError as exc:
                    if exc.code == 400 and fallback and Path(fallback[1]).is_file():
                        self.api.call(method, _local_file=fallback, **data)
                    else:
                        raise
            except Exception as exc:
                from telegram_api import ApiError
                if not isinstance(exc, ApiError):
                    raise
                if exc.code in (400, 413) and method != "sendMessage":
                    with self.db:
                        self.db.execute("DELETE FROM outbox WHERE id=?", (job_id,))
                        self.text(recipient, "Telegram не разрешил переслать это медиа. "
                                  "Текст уведомления сохранён; локальная копия, если доступна, находится на компьютере.")
                    continue
                blocked.add(recipient)
                delay = max(5, exc.retry_after) if exc.code != 403 else 300
                with self.db:
                    self.db.execute("UPDATE outbox SET next_attempt=? WHERE recipient=?", (time.time() + delay, recipient))
                report("Отправка отложена. Откройте бота в Telegram и нажмите Start." if exc.code == 403
                       else "Telegram временно не принял уведомление. Повторим отправку.")
                if exc.code == 429:
                    return
            else:
                with self.db:
                    self.db.execute("DELETE FROM outbox WHERE id=?", (job_id,))
