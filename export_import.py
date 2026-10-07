"""Streaming, offline import of Telegram Desktop JSON exports and local media."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

import ijson
from ijson.common import ObjectBuilder


class ImportCancelled(Exception):
    pass


def check_stop(stop):
    if stop.is_set():
        raise ImportCancelled()


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text_content(item) for item in value)
    if isinstance(value, dict):
        return text_content(value.get("text", value.get("children", "")))
    return ""


def json_text(value):
    return json.dumps(value, ensure_ascii=False)


def safe_source(root, relative):
    if not isinstance(relative, str) or not relative or relative.startswith("("):
        return None
    path = Path(relative)
    if path.is_absolute() or path.drive:
        return None
    candidate = (root / path).resolve()
    return candidate if candidate.is_relative_to(root) and candidate.is_file() else None


def file_references(value, prefix=""):
    fields = {"photo", "file", "thumbnail", "video_thumbnail", "cover", "video_file"}
    if isinstance(value, dict):
        for key, item in value.items():
            label = f"{prefix}.{key}" if prefix else key
            if key in fields and isinstance(item, str) and item:
                yield label, item
            elif isinstance(item, (dict, list)):
                yield from file_references(item, label)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from file_references(item, f"{prefix}.{index}")


def normalize(message, chat, files):
    sender = message.get("from_id", "")
    match = re.fullmatch(r"user(\d+)", str(sender))
    result = {
        "message_id": int(message["id"]),
        "chat": {"id": int(chat["id"]), "first_name": chat.get("name") or str(chat["id"])},
        "from": {"id": int(match[1]) if match else None, "first_name": message.get("from") or message.get("actor") or "Неизвестно"},
        "date": message.get("date_unixtime", message.get("date")),
        "text": text_content(message.get("text", "")),
        "_imported": True,
    }
    if message.get("photo"):
        kind, reference = "photo", "photo"
    elif message.get("file"):
        kind = {"animation": "animation", "video_file": "video", "video_message": "video_note",
                "audio_file": "audio", "voice_message": "voice", "sticker": "sticker"}.get(message.get("media_type"), "document")
        reference = "file"
    else:
        return result
    payload = {"file_id": None}
    result[kind] = [payload] if kind == "photo" else payload
    result["_local_media"] = files.get(reference)
    if result["text"]:
        result["caption"] = result.pop("text")
    return result


def plain_message(value, files):
    text = f"[{value.get('date', '')}] {value.get('from') or value.get('actor') or ''} | ID {value.get('id', '')}\n"
    text += text_content(value.get("text", "")) + "\n"
    if value.get("action"):
        text += f"[Событие: {value['action']}]\n"
    for file_path in files.values():
        text += f"[Файл: {file_path}]\n"
    return text + "\n"


def parse_export(path, stop):
    """Yield one message at a time, also covering single-chat and left-chat exports."""
    chat_prefixes = {"chats.list.item", "left_chats.list.item", "chats.item"}
    chat = None
    chat_prefix = None
    owner = None
    builder = None
    build_prefix = None
    depth = 0
    single = {}
    single_started = False
    recognized = False
    with path.open("rb") as stream:
        if stream.read(3) != b"\xef\xbb\xbf":
            stream.seek(0)
        # ijson defaults to Decimal for fractional values; use floats for JSON serializability.
        for prefix, event, value in ijson.parse(stream, use_float=True):
            check_stop(stop)
            if builder is not None:
                builder.event(event, value)
                if event in ("start_map", "start_array"):
                    depth += 1
                elif event in ("end_map", "end_array"):
                    depth -= 1
                if depth == 0:
                    yield "message", chat, builder.value
                    builder = None
                continue
            if prefix in ("chats.list", "left_chats.list", "chats") and event == "start_array":
                recognized = True
            if prefix == "personal_information.user_id" and event in ("number", "string"):
                owner = int(value)
                yield "owner", None, owner
            elif prefix in chat_prefixes and event == "start_map":
                chat, chat_prefix = {}, prefix
                recognized = True
            elif chat is not None and prefix == chat_prefix and event == "end_map":
                if "id" not in chat:
                    raise ValueError("В экспорте найден чат без ID.")
                yield "chat_end", chat, None
                chat, chat_prefix = None, None
            elif chat is not None and prefix in {chat_prefix + ".id", chat_prefix + ".name", chat_prefix + ".type"} and event in ("number", "string", "null"):
                chat[prefix.rsplit(".", 1)[1]] = value
            elif chat is not None and prefix == chat_prefix + ".messages" and event == "start_array":
                if "id" not in chat:
                    raise ValueError("Неверная структура экспорта: ID чата должен предшествовать сообщениям.")
                yield "chat_start", chat, None
            elif chat is not None and prefix == chat_prefix + ".messages.item" and event == "start_map":
                builder = ObjectBuilder()
                builder.event(event, value)
                depth = 1
            elif prefix in ("id", "name", "type") and event in ("number", "string", "null"):
                single[prefix] = value
            elif prefix == "messages" and event == "start_array":
                if "id" not in single:
                    raise ValueError("В JSON не найден ID чата.")
                recognized, single_started = True, True
                chat, chat_prefix = single, ""
                yield "chat_start", single, None
            elif single_started and prefix == "messages.item" and event == "start_map":
                builder = ObjectBuilder()
                builder.event(event, value)
                depth = 1
            elif single_started and prefix == "messages" and event == "end_array":
                yield "chat_end", single, None
                chat, chat_prefix, single_started = None, None, False
    if not recognized:
        raise ValueError("Это не JSON-экспорт чатов Telegram Desktop. Выберите result.json, а не HTML.")


def import_export(source, archive, stop, report):
    source = Path(source).resolve()
    root = source.parent
    report("Чтение экспорта…")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            check_stop(stop)
            digest.update(block)
    import_id = digest.hexdigest()
    exports = Path(archive.db.execute("PRAGMA database_list").fetchone()[2]).parent / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    final_chats = exports / import_id[:24]
    media_parent = archive.media_folder / "imports"
    media_parent.mkdir(parents=True, exist_ok=True)
    final_media = media_parent / import_id[:24]
    counts = {"chats": 0, "messages": 0, "files": 0, "missing_files": 0}
    owner = None
    copied_files = {}
    chat_stream = None
    text_stream = None
    with tempfile.TemporaryDirectory(prefix=".import-", dir=exports) as stage_name, tempfile.TemporaryDirectory(prefix=".import-", dir=media_parent) as media_name:
        stage, stage_media = Path(stage_name).resolve(), Path(media_name).resolve()
        # Temp cleanup is confined to these newly created directories.
        assert stage.parent == exports.resolve() and stage_media.parent == media_parent.resolve()
        with closing(sqlite3.connect(stage / "staging.sqlite3")) as staging:
            staging.executescript('''
                CREATE TABLE chats (key TEXT PRIMARY KEY, id INTEGER, type TEXT, name TEXT);
                CREATE TABLE messages (key TEXT, id INTEGER, data TEXT, PRIMARY KEY(key,id));
            ''')
            try:
                for event, chat, value in parse_export(source, stop):
                    if event == "owner":
                        owner = value
                        continue
                    chat_type = chat.get("type") or "unknown"
                    chat_key = f"{chat_type}_{int(chat['id'])}"
                    # Filenames only use numeric IDs and sanitized Telegram chat types.
                    filename = re.sub(r"[^a-zA-Z0-9_-]", "_", chat_key)
                    if event == "chat_start":
                        counts["chats"] += 1
                        staging.execute("INSERT OR REPLACE INTO chats VALUES (?,?,?,?)",
                                        (chat_key, int(chat["id"]), chat_type, chat.get("name") or "Без названия"))
                        chat_stream = (stage / (filename + ".jsonl")).open("w", encoding="utf-8")
                        text_stream = (stage / (filename + ".txt")).open("w", encoding="utf-8")
                        chat_stream.write(json_text({"chat": chat}) + "\n")
                        text_stream.write(f"{chat.get('name') or 'Без названия'} | {chat_type} | ID {chat['id']}\n\n")
                        continue
                    if event == "chat_end":
                        if chat_stream:
                            chat_stream.close()
                            text_stream.close()
                            chat_stream = text_stream = None
                        continue
                    if event != "message" or "id" not in value:
                        continue
                    files = {}
                    for label, relative in file_references(value):
                        check_stop(stop)
                        if relative not in copied_files:
                            original = safe_source(root, relative)
                            if original is None:
                                copied_files[relative] = None
                                counts["missing_files"] += 1
                            else:
                                # No user-supplied path is used as a destination path.
                                suffix = original.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", original.suffix) else ".bin"
                                name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + suffix
                                temporary = stage_media / name
                                with original.open("rb") as reader, temporary.open("wb") as writer:
                                    while block := reader.read(1024 * 1024):
                                        check_stop(stop)
                                        writer.write(block)
                                copied_files[relative] = str(final_media / name)
                                counts["files"] += 1
                        if copied_files[relative]:
                            files[label] = copied_files[relative]
                    normalized = normalize(value, chat, files)
                    staging.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?)", (chat_key, int(value["id"]), json_text(normalized)))
                    chat_stream.write(json_text({"message": value, "local_files": files}) + "\n")
                    text_stream.write(plain_message(value, files))
                    counts["messages"] += 1
                    if counts["messages"] % 100 == 0:
                        report(f"Импорт: {counts['chats']} чатов, {counts['messages']} сообщений, {counts['files']} файлов…")
                staging.commit()
            finally:
                if chat_stream:
                    chat_stream.close()
                    text_stream.close()
            check_stop(stop)
            report("Сохранение импортированного архива…")
            # Publish data only after the complete JSON has been validated and parsed.
            final_media.mkdir(parents=True, exist_ok=True)
            for file in stage_media.iterdir():
                check_stop(stop)
                file.replace(final_media / file.name)
            final_chats.mkdir(parents=True, exist_ok=True)
            for file in stage.iterdir():
                if file.suffix in (".txt", ".jsonl"):
                    file.replace(final_chats / file.name)
            manifest = {"owner_id": owner, "source": source.name, "chat_count": counts["chats"],
                        "messages": counts["messages"], "files": counts["files"], "missing_files": counts["missing_files"],
                        "chats": [dict(zip(("key", "id", "type", "name"), row)) for row in staging.execute("SELECT * FROM chats")]}
            (final_chats / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            # Import tables never overwrite the current business-message snapshots.
            with archive.db:
                archive.db.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?)", (import_id, owner, json_text(manifest)))
                for chat_key, chat_id, chat_type, chat_name in staging.execute("SELECT * FROM chats"):
                    rows = staging.execute("SELECT id,data FROM messages WHERE key=?", (chat_key,))
                    archive.db.executemany('''INSERT INTO imported_messages VALUES (?,?,?,?,?,0)
                        ON CONFLICT(source,chat,type,id) DO UPDATE SET data=excluded.data''',
                                          ((import_id, chat_id, chat_type, message_id, raw) for message_id, raw in rows))
    return {**counts, "folder": str(final_chats), "owner_id": owner}
