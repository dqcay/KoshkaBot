import json
from pathlib import Path
import tempfile
import threading
import unittest

from archive import Archive
from export_import import ImportCancelled, import_export, parse_export, text_content
from test_archive import FakeTelegram


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.export_root = self.root / "Исходный экспорт"
        self.export_root.mkdir()
        self.source = self.export_root / "result.json"
        self.api = FakeTelegram()
        self.archive = Archive(self.root / "archive.sqlite3", self.api, media_folder=self.root / "Медиа")
        self.stop = threading.Event()
        self.progress = []

    def tearDown(self):
        self.archive.db.close()
        self.directory.cleanup()

    def message(self, **overrides):
        message = {"id": 5, "type": "message", "from": "Собеседник", "from_id": "user200",
                   "date": "2026-10-07T10:00:00", "text": ["Старый ", {"type": "bold", "text": "текст"}]}
        message.update(overrides)
        return message

    def full_export(self, messages=None, owner=100):
        return {"personal_information": {"user_id": owner}, "chats": {"list": [
            {"name": "Друг", "type": "personal_chat", "id": 200, "messages": messages or [self.message()]},
            {"name": "Группа", "type": "private_group", "id": 200, "messages": [self.message(text="Групповой текст")]},
            {"name": "Канал", "type": "public_channel", "id": 300, "messages": []}]}}

    def write(self, data):
        self.source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def perform(self):
        return import_export(self.source, self.archive, self.stop, self.progress.append)

    def connect(self):
        self.archive.process({"update_id": 1, "business_connection": {"id": "c", "user": {"id": 100},
                               "user_chat_id": 100, "is_enabled": True}})
        self.archive.deliver(self.stop, lambda _: None)
        self.api.sent.clear()

    def delete(self):
        self.archive.process({"update_id": 2, "deleted_business_messages": {
            "business_connection_id": "c", "chat": {"id": 200}, "message_ids": [5]}})
        self.archive.deliver(self.stop, lambda _: None)

    def test_all_chat_types_are_saved_with_readable_text(self):
        self.write(self.full_export())
        result = self.perform()
        self.assertEqual(result["chats"], 3)
        self.assertEqual(result["messages"], 2)
        folder = Path(result["folder"])
        self.assertEqual(len(list(folder.glob("*.txt"))), 3)
        self.assertIn("Старый текст", (folder / "personal_chat_200.txt").read_text(encoding="utf-8"))
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM imported_messages").fetchone()[0], 2)

    def test_media_is_copied_and_imported_photo_can_be_sent_after_deletion(self):
        photos = self.export_root / "photos"
        photos.mkdir()
        (photos / "test.jpg").write_bytes(b"photo bytes")
        self.write(self.full_export([self.message(photo="photos/test.jpg")]))
        result = self.perform()
        self.assertEqual(result["files"], 1)
        self.connect()
        self.delete()
        photo = [parameters for method, parameters in self.api.sent if method == "sendPhoto"][0]
        self.assertEqual(photo["_local_file"][0], "photo")
        self.assertEqual(Path(photo["_local_file"][1]).read_bytes(), b"photo bytes")
        self.assertTrue((photos / "test.jpg").exists())

    def test_animation_and_missing_files_are_reported(self):
        (self.export_root / "gif.mp4").write_bytes(b"gif bytes")
        self.write(self.full_export([self.message(file="gif.mp4", media_type="animation"),
                                   self.message(id=6, photo="photos/missing.jpg")]))
        result = self.perform()
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["missing_files"], 1)
        self.connect()
        self.delete()
        self.assertTrue(any(method == "sendAnimation" for method, _ in self.api.sent))

    def test_another_accounts_export_never_seeds_business_notifications(self):
        self.write(self.full_export(owner=999))
        self.perform()
        self.connect()
        self.delete()
        self.assertEqual(self.api.sent, [])

    def test_imported_outgoing_messages_are_archived_but_not_notified(self):
        self.write(self.full_export([self.message(from_id="user100")]))
        self.perform()
        self.connect()
        self.delete()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM history WHERE event='delete'").fetchone()[0], 1)

    def test_repeated_import_does_not_duplicate_or_resurrect_deleted_records(self):
        self.write(self.full_export())
        self.perform()
        self.connect()
        self.delete()
        self.perform()
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 1)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM imported_messages").fetchone()[0], 2)
        self.assertEqual(self.archive.db.execute("SELECT deleted FROM imported_messages WHERE type='personal_chat'").fetchone()[0], 1)

    def test_single_chat_export_with_bom_is_supported(self):
        self.source.write_text(json.dumps({"name": "Друг", "type": "personal_chat", "id": 200,
                                          "messages": [self.message()]}), encoding="utf-8-sig")
        result = self.perform()
        self.assertEqual(result["chats"], 1)
        self.assertIsNone(result["owner_id"])

    def test_invalid_json_does_not_publish_partial_import(self):
        self.write(self.full_export())
        self.source.write_text(self.source.read_text(encoding="utf-8")[:-8], encoding="utf-8")
        with self.assertRaises(Exception):
            self.perform()
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)
        self.assertFalse(any((self.root / "exports").glob("*/index.json")))

    def test_cancel_does_not_publish_partial_import(self):
        self.write(self.full_export())
        def cancel(text):
            self.stop.set()
        with self.assertRaises(ImportCancelled):
            import_export(self.source, self.archive, self.stop, cancel)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)

    def test_path_traversal_cannot_copy_files_outside_export(self):
        outside = self.root / "private.jpg"
        outside.write_bytes(b"private")
        self.write(self.full_export([self.message(photo="../private.jpg"), self.message(id=6, file=str(outside))]))
        result = self.perform()
        self.assertEqual(result["missing_files"], 2)
        self.assertEqual(result["files"], 0)

    def test_import_does_not_overwrite_newer_live_snapshot(self):
        self.connect()
        message = {"business_connection_id": "c", "chat": {"id": 200}, "message_id": 5,
                   "from": {"id": 200}, "text": "Более новый текст"}
        self.archive.process({"update_id": 2, "business_message": message})
        self.write(self.full_export())
        self.perform()
        self.archive.process({"update_id": 3, "deleted_business_messages": {
            "business_connection_id": "c", "chat": {"id": 200}, "message_ids": [5]}})
        self.archive.deliver(self.stop, lambda _: None)
        text = "\n".join(parameters.get("text", "") for _, parameters in self.api.sent)
        self.assertIn("Более новый текст", text)
        self.assertNotIn("Старый текст", text)

    def test_new_rich_text_and_left_chats_are_supported(self):
        text = {"type": "concat", "text": [{"type": "plain", "text": "Hello "}, {"type": "bold", "text": "world"}]}
        self.write({"left_chats": {"list": [{"name": "Left", "type": "private_supergroup", "id": 55,
                                              "messages": [self.message(text=text)]}]}})
        result = self.perform()
        self.assertEqual(result["chats"], 1)
        self.assertIn("Hello world", (Path(result["folder"]) / "private_supergroup_55.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
