"""Local folder preferences and safe copying of an existing bot archive."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from contextlib import closing


def app_folder():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KoshkaBot"


def load_settings():
    try:
        settings = json.loads((app_folder() / "settings.json").read_text(encoding="utf-8"))
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    folder = app_folder()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(settings, stream, ensure_ascii=False, indent=2)
    try:
        temporary.replace(folder / "settings.json")
    finally:
        temporary.unlink(missing_ok=True)


def rewrite_paths(value, old_folder, new_folder):
    if isinstance(value, dict):
        return {key: rewrite_paths(item, old_folder, new_folder) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, old_folder, new_folder) for item in value]
    if isinstance(value, str):
        # Only internal media path fields are rewritten by the caller.
        try:
            return str(new_folder / Path(value).relative_to(old_folder))
        except ValueError:
            pass
    return value


def rebase_message(message, old_folder, new_folder):
    if isinstance(message, dict) and message.get("_local_media"):
        message["_local_media"] = rewrite_paths(message["_local_media"], old_folder, new_folder)
    return message


def rebase_database(connection, old_folder, new_folder):
    for rowid, raw in connection.execute("SELECT rowid,data FROM messages").fetchall():
        value = rebase_message(json.loads(raw), old_folder, new_folder)
        connection.execute("UPDATE messages SET data=? WHERE rowid=?", (json.dumps(value), rowid))
    for rowid, event, raw in connection.execute("SELECT id,event,data FROM history").fetchall():
        value = json.loads(raw)
        if event == "edit":
            rebase_message(value.get("before"), old_folder, new_folder)
            rebase_message(value.get("after"), old_folder, new_folder)
        else:
            rebase_message(value, old_folder, new_folder)
        connection.execute("UPDATE history SET data=? WHERE id=?", (json.dumps(value), rowid))
    for rowid, raw in connection.execute("SELECT id,parameters FROM outbox").fetchall():
        value = json.loads(raw)
        if value.get("_fallback_media"):
            value["_fallback_media"][1] = rewrite_paths(value["_fallback_media"][1], old_folder, new_folder)
        connection.execute("UPDATE outbox SET parameters=? WHERE id=?", (json.dumps(value), rowid))
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "imported_messages" in tables:
        for rowid, raw in connection.execute("SELECT rowid,data FROM imported_messages").fetchall():
            value = rebase_message(json.loads(raw), old_folder, new_folder)
            connection.execute("UPDATE imported_messages SET data=? WHERE rowid=?", (json.dumps(value), rowid))


def copy_folder(source, target):
    if source == target or not source.exists():
        return
    if target.is_relative_to(source):
        raise ValueError("Новая папка не должна находиться внутри переносимой старой папки.")
    shutil.copytree(source, target, dirs_exist_ok=True)


def rebase_exports(exports, old_media, new_media):
    if old_media == new_media or not exports.exists():
        return
    from export_import import plain_message
    for file in exports.rglob("*.jsonl"):
        with file.open(encoding="utf-8") as original, file.with_suffix(".jsonl.tmp").open("w", encoding="utf-8") as copied, file.with_suffix(".txt.tmp").open("w", encoding="utf-8") as plain:
            for line in original:
                value = json.loads(line)
                if "local_files" in value:
                    value["local_files"] = rewrite_paths(value["local_files"], old_media, new_media)
                    plain.write(plain_message(value["message"], value["local_files"]))
                elif "chat" in value:
                    chat = value["chat"]
                    plain.write(f"{chat.get('name') or 'Без названия'} | {chat.get('type')} | ID {chat.get('id')}\n\n")
                copied.write(json.dumps(value, ensure_ascii=False) + "\n")
        file.with_suffix(".jsonl.tmp").replace(file)
        file.with_suffix(".txt.tmp").replace(file.with_suffix(".txt"))


def prepare_storage(bot_id, archive_root, media_root=""):
    bot_id = str(int(bot_id))
    settings = load_settings()
    previous = settings.get("bots", {}).get(bot_id, {})
    old_archive = Path(previous.get("archive", app_folder() / bot_id)).resolve()
    old_media = Path(previous.get("media", old_archive / "media")).resolve()
    archive = (Path(archive_root).expanduser() / bot_id).resolve()
    media = ((Path(media_root).expanduser() / bot_id) if media_root else archive / "media").resolve()
    if media == archive or archive.is_relative_to(media):
        raise ValueError("Папка медиа не должна содержать папку базы архива. Выберите соседние папки.")
    if old_media != media and media.is_relative_to(old_media):
        raise ValueError("Выберите папку медиа вне старой папки медиа, чтобы избежать вложенных копий.")
    if archive != old_archive and (archive / "exports").is_relative_to(old_archive / "exports"):
        raise ValueError("Выберите новую папку архива вне старой папки exports.")
    archive.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    # Confirm both selected destinations are writable before starting polling.
    for folder in (archive, media):
        with tempfile.TemporaryFile(dir=folder) as stream:
            stream.write(b"test")
    target = archive / "archive.sqlite3"
    source = old_archive / "archive.sqlite3"
    if not target.exists() and source.exists() and source != target:
        temporary = archive / "archive.migration.sqlite3"
        temporary.unlink(missing_ok=True)
        try:
            with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(temporary)) as copied:
                original.backup(copied)
                copy_folder(old_media, media)
                copy_folder(old_archive / "exports", archive / "exports")
                rebase_exports(archive / "exports", old_media, media)
                with copied:
                    rebase_database(copied, old_media, media)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    elif source == target and old_media != media and target.exists():
        copy_folder(old_media, media)
        rebase_exports(archive / "exports", old_media, media)
        with closing(sqlite3.connect(target)) as connection:
            with connection:
                rebase_database(connection, old_media, media)
    settings["archive_root"] = str(Path(archive_root).expanduser().resolve())
    settings["media_root"] = str(Path(media_root).expanduser().resolve()) if media_root else ""
    settings.setdefault("bots", {})[bot_id] = {"archive": str(archive), "media": str(media)}
    save_settings(settings)
    return target, media
