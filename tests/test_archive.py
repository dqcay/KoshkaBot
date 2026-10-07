import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from archive import Archive, chunks
from telegram_api import ApiError


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.failure = None

    def call(self, method, **parameters):
        if self.failure:
            self.failure(method, parameters)
        self.sent.append((method, parameters))
        return True


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "archive.sqlite3"
        self.api = FakeTelegram()
        self.archive = Archive(self.path, self.api)
        self.sequence = 0
        self.connection("c1", 100)
        self.flush()
        self.api.sent.clear()

    def tearDown(self):
        self.archive.db.close()
        self.directory.cleanup()

    def process(self, kind, payload):
        self.sequence += 1
        update = {"update_id": self.sequence, kind: payload}
        self.archive.process(update)
        return update

    def connection(self, connection, owner):
        self.process("business_connection", {"id": connection, "is_enabled": True,
                                              "user_chat_id": owner, "user": {"id": owner}})

    def message(self, **overrides):
        result = {"business_connection_id": "c1", "chat": {"id": 200, "first_name": "Собеседник"},
                  "from": {"id": 200, "first_name": "Собеседник"}, "message_id": 5, "text": "Первая версия"}
        result.update(overrides)
        return result

    def deleted(self, connection="c1", ids=None):
        return {"business_connection_id": connection, "chat": {"id": 200}, "message_ids": ids or [5]}

    def flush(self):
        self.archive.deliver(threading.Event(), lambda _: None)

    def texts(self):
        return "\n".join(p.get("text", "") for _, p in self.api.sent)

    def test_edit_and_delete_restore_correct_versions(self):
        self.process("business_message", self.message())
        self.process("edited_business_message", self.message(text="Вторая версия"))
        self.flush()
        self.assertIn("Было:\nПервая версия", self.texts())
        self.assertIn("Стало:\nВторая версия", self.texts())
        self.api.sent.clear()
        self.process("deleted_business_messages", self.deleted())
        self.flush()
        self.assertIn("Вторая версия", self.texts())
        self.assertNotIn("Первая версия", self.texts())
        self.assertTrue(all(p["chat_id"] == 100 for _, p in self.api.sent))

    def test_media_photo_and_animation_survive_delete(self):
        for index, kind in enumerate(("photo", "animation"), 5):
            data = [{"file_id": "small"}, {"file_id": "large"}] if kind == "photo" else {"file_id": "gif"}
            overrides = {"text": None, kind: data, "message_id": index, "caption": "Подпись"}
            if kind == "animation":
                overrides["document"] = {"file_id": "gif"}
            self.process("business_message", self.message(**overrides))
        self.process("deleted_business_messages", self.deleted(ids=[5, 6]))
        self.flush()
        self.assertIn(("sendPhoto", {"photo": "large", "chat_id": 100}), self.api.sent)
        self.assertIn(("sendAnimation", {"animation": "gif", "chat_id": 100}), self.api.sent)

    def test_media_edit_delivers_both_photos(self):
        self.process("business_message", self.message(photo=[{"file_id": "old"}]))
        self.process("edited_business_message", self.message(photo=[{"file_id": "new"}]))
        self.flush()
        photos = [p["photo"] for method, p in self.api.sent if method == "sendPhoto"]
        self.assertEqual(photos, ["old", "new"])

    def test_replay_after_restart_does_not_duplicate_notifications(self):
        self.process("business_message", self.message())
        update = self.process("deleted_business_messages", self.deleted())
        pending = self.archive.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.archive.db.close()
        self.archive = Archive(self.path, self.api)
        self.archive.process(update)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], pending)
        self.flush()
        self.assertIn("Первая версия", self.texts())

    def test_two_owners_with_same_message_id_never_mix(self):
        self.connection("c2", 300)
        self.flush()
        self.api.sent.clear()
        self.process("business_message", self.message())
        self.process("business_message", self.message(business_connection_id="c2", text="Чужой текст"))
        self.process("deleted_business_messages", self.deleted())
        self.flush()
        self.assertNotIn("Чужой текст", self.texts())
        self.assertTrue(all(p["chat_id"] == 100 for _, p in self.api.sent))

    def test_blocked_owner_keeps_queue_without_blocking_archival(self):
        self.process("business_message", self.message())
        self.process("deleted_business_messages", self.deleted())
        def fail(method, parameters):
            raise ApiError(403)
        self.api.failure = fail
        self.flush()
        self.assertGreater(self.archive.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.process("business_message", self.message(message_id=6, text="Новый текст"))
        self.assertEqual(self.archive.offset, self.sequence + 1)
        self.api.failure = None
        self.process("message", {"chat": {"id": 100, "type": "private"}, "text": "/start"})
        self.flush()
        self.assertIn("Первая версия", self.texts())

    def test_outgoing_messages_do_not_trigger_notifications(self):
        outgoing = self.message(**{"from": {"id": 100}})
        self.process("business_message", outgoing)
        self.process("edited_business_message", dict(outgoing, text="Отредактировано"))
        self.process("deleted_business_messages", self.deleted())
        self.flush()
        self.assertEqual(self.api.sent, [])

    def test_metadata_only_edit_is_silent(self):
        self.process("business_message", self.message())
        self.process("edited_business_message", self.message(edit_date=42))
        self.flush()
        self.assertEqual(self.api.sent, [])

    def test_failed_update_rolls_back_queue_and_offset(self):
        before = self.archive.offset
        original = self.archive.snapshot
        def fail(*args):
            original(*args)
            raise RuntimeError("simulated crash")
        self.archive.snapshot = fail
        self.process("business_message", self.message())
        before = self.archive.offset
        with self.assertRaises(RuntimeError):
            self.process("deleted_business_messages", self.deleted())
        self.assertEqual(self.archive.offset, before)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_unicode_chunks_fit_telegram_limit(self):
        value = "😀" * 4000
        parts = list(chunks(value))
        self.assertEqual("".join(parts), value)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 3500 for p in parts))

    def test_media_falls_back_to_local_copy(self):
        saved = Path(self.directory.name) / "photo.jpg"
        saved.write_bytes(b"test")
        message = self.message(photo=[{"file_id": "expired"}], _local_media=str(saved))
        self.process("business_message", message)
        self.process("deleted_business_messages", self.deleted())
        def fail(method, parameters):
            if method == "sendPhoto" and "_local_file" not in parameters:
                raise ApiError(400)
        self.api.failure = fail
        self.flush()
        sent = [p for method, p in self.api.sent if method == "sendPhoto"]
        self.assertEqual(sent[0]["_local_file"], ["photo", str(saved)])

    def test_disabled_connection_sends_nothing(self):
        self.process("business_connection", {"id": "c1", "is_enabled": False,
                                              "user_chat_id": 100, "user": {"id": 100}})
        self.process("business_message", self.message())
        self.process("edited_business_message", self.message(text="Изменено"))
        self.flush()
        self.assertEqual(self.api.sent, [])


if __name__ == "__main__":
    unittest.main()
