import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from telegram_api import ApiError, Telegram


class ApiTests(unittest.TestCase):
    def test_http_errors_do_not_expose_token(self):
        token = "123456:do-not-log-this-token"
        api = Telegram(token)
        error = urllib.error.HTTPError(api.base + "getMe", 401, "Unauthorized", {},
                                       io.BytesIO(b'{"ok":false,"error_code":401}'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as caught:
                api.call("getMe")
        self.assertEqual(caught.exception.code, 401)
        self.assertNotIn(token, str(caught.exception))

    def test_rate_limit_preserves_retry_after(self):
        body = io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":12}}')
        with patch("urllib.request.urlopen", return_value=body):
            with self.assertRaises(ApiError) as caught:
                Telegram("test").call("sendMessage", chat_id=1, text="hello")
        self.assertEqual(caught.exception.retry_after, 12)

    def test_multipart_local_media_contains_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"sample-photo-bytes")
            response = io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
            with patch("urllib.request.urlopen", return_value=response) as opener:
                Telegram("test").call("sendPhoto", chat_id=100, photo="expired", _local_file=["photo", str(path)])
            request = opener.call_args.args[0]
            self.assertIn(b"sample-photo-bytes", request.data)
            self.assertIn(b'name="photo"; filename="photo.jpg"', request.data)
            self.assertNotIn(b"expired", request.data)

    def test_cached_media_download_url_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Telegram("test")
            with patch.object(api, "call", return_value={"file_path": "photos/file_1.jpg"}) as getfile:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(b"photo")) as opener:
                    first = api.cache_media("id", directory)
                    second = api.cache_media("id", directory)
            self.assertEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b"photo")
            getfile.assert_called_once()
            self.assertEqual(opener.call_args.args[0], "https://api.telegram.org/file/bottest/photos/file_1.jpg")


if __name__ == "__main__":
    unittest.main()
