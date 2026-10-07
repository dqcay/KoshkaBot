import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive import Archive
from storage import prepare_storage, load_settings


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings_patch = patch("storage.app_folder", return_value=self.root / "settings")
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.directory.cleanup()

    def test_custom_destinations_and_preferences_are_saved(self):
        database, media = prepare_storage(123, self.root / "Чаты", str(self.root / "Файлы"))
        self.assertEqual(database, self.root / "Чаты" / "123" / "archive.sqlite3")
        self.assertEqual(media, self.root / "Файлы" / "123")
        settings = load_settings()
        self.assertEqual(settings["archive_root"], str(self.root / "Чаты"))
        self.assertEqual(settings["media_root"], str(self.root / "Файлы"))
        self.assertNotIn("token", json.dumps(settings))

    def test_empty_media_destination_keeps_media_with_archive(self):
        database, media = prepare_storage(123, self.root / "Чаты")
        self.assertEqual(media, database.parent / "media")

    def seed(self):
        database, media = prepare_storage(123, self.root / "Старые")
        file = media / "photo.jpg"
        file.write_bytes(b"photo")
        archive = Archive(database, object())
        message = {"_local_media": str(file), "text": str(file), "chat": {"id": 2}, "message_id": 3}
        with archive.db:
            archive.db.execute("INSERT INTO messages VALUES ('c',2,3,?)", (json.dumps(message),))
            archive.db.execute("INSERT INTO history(event,data) VALUES ('edit',?)",
                               (json.dumps({"before": message, "after": message}),))
            archive.enqueue(100, "sendPhoto", photo="id", _fallback_media=["photo", str(file)])
            archive.db.execute("INSERT INTO state VALUES ('offset','99')")
        archive.db.close()
        return database, media

    def test_changing_folders_copies_archive_media_and_pending_notifications(self):
        source, old_media = self.seed()
        target, new_media = prepare_storage(123, self.root / "Новые", str(self.root / "Медиа"))
        archive = Archive(target, object())
        try:
            self.assertEqual(archive.offset, 99)
            message = json.loads(archive.db.execute("SELECT data FROM messages").fetchone()[0])
            self.assertEqual(message["_local_media"], str(new_media / "photo.jpg"))
            self.assertEqual(message["text"], str(old_media / "photo.jpg"))
            self.assertEqual((new_media / "photo.jpg").read_bytes(), b"photo")
            pending = json.loads(archive.db.execute("SELECT parameters FROM outbox").fetchone()[0])
            self.assertEqual(pending["_fallback_media"][1], str(new_media / "photo.jpg"))
            self.assertTrue(source.exists())
            self.assertTrue((old_media / "photo.jpg").exists())
        finally:
            archive.db.close()

    def test_changing_only_media_folder_preserves_existing_database(self):
        source, _ = self.seed()
        target, media = prepare_storage(123, self.root / "Старые", str(self.root / "Файлы"))
        self.assertEqual(source, target)
        archive = Archive(target, object())
        try:
            self.assertEqual(archive.offset, 99)
            message = json.loads(archive.db.execute("SELECT data FROM messages").fetchone()[0])
            self.assertEqual(message["_local_media"], str(media / "photo.jpg"))
        finally:
            archive.db.close()

    def test_failed_migration_does_not_publish_incomplete_database(self):
        source, _ = self.seed()
        with patch("storage.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                prepare_storage(123, self.root / "Новые")
        self.assertFalse((self.root / "Новые" / "123" / "archive.sqlite3").exists())
        self.assertTrue(source.exists())
        self.assertEqual(load_settings()["archive_root"], str(self.root / "Старые"))

    def test_media_cannot_be_nested_inside_previous_media(self):
        _, old_media = self.seed()
        with self.assertRaises(ValueError):
                prepare_storage(123, self.root / "Новые", str(old_media / "Вложенная"))

    def test_changing_folders_preserves_imported_chats_and_rebases_readable_files(self):
        from export_import import import_export
        import threading
        source = self.root / "export"
        source.mkdir()
        (source / "photo.jpg").write_bytes(b"photo bytes")
        (source / "result.json").write_text(json.dumps({"personal_information": {"user_id": 100},
            "chats": {"list": [{"name": "Друг", "type": "personal_chat", "id": 200,
            "messages": [{"id": 5, "from_id": "user200", "text": "Привет", "photo": "photo.jpg"}]}]}}), encoding="utf-8")
        original, old_media = prepare_storage(123, self.root / "Старые")
        archive = Archive(original, object(), media_folder=old_media)
        imported = import_export(source / "result.json", archive, threading.Event(), lambda _: None)
        archive.db.close()
        new_database, new_media = prepare_storage(123, self.root / "Новые", str(self.root / "Файлы"))
        archive = Archive(new_database, object(), media_folder=new_media)
        try:
            message = json.loads(archive.db.execute("SELECT data FROM imported_messages").fetchone()[0])
            self.assertTrue(Path(message["_local_media"]).is_relative_to(new_media))
            self.assertTrue(Path(message["_local_media"]).exists())
            new_export = new_database.parent / "exports" / Path(imported["folder"]).name
            self.assertIn(str(new_media), (new_export / "personal_chat_200.txt").read_text(encoding="utf-8"))
            self.assertNotIn(str(old_media), (new_export / "personal_chat_200.txt").read_text(encoding="utf-8"))
        finally:
            archive.db.close()


if __name__ == "__main__":
    unittest.main()
