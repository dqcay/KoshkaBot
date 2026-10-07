"""Small Bot API client. Never includes the token/URL in errors."""
import json
import hashlib
from pathlib import Path
import urllib.error
import urllib.request
import uuid


class ApiError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API: {code}")


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **parameters):
        local_file = parameters.pop("_local_file", None)
        if local_file:
            field, path = local_file
            boundary = uuid.uuid4().hex
            body = bytearray()
            for key, value in parameters.items():
                if key == field:
                    continue
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{Path(path).name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
            body.extend(Path(path).read_bytes())
            body.extend(f'\r\n--{boundary}--\r\n'.encode())
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            body = json.dumps(parameters).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            self.base + method,
            bytes(body),
            {"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except (ValueError, OSError):
                raise ApiError(exc.code) from None
        except (OSError, ValueError):
            raise ApiError(0) from None
        if not result.get("ok"):
            raise ApiError(result.get("error_code", 0),
                           result.get("parameters", {}).get("retry_after", 5))
        return result["result"]

    def cache_media(self, file_id, folder):
        folder = Path(folder)
        folder.mkdir(exist_ok=True)
        stem = hashlib.sha256(file_id.encode()).hexdigest()
        matches = list(folder.glob(stem + ".*"))
        if matches:
            return str(matches[0])
        try:
            info = self.call("getFile", file_id=file_id)
            suffix = Path(info["file_path"]).suffix
            if len(suffix) > 10 or not suffix[1:].isalnum():
                suffix = ".bin"
            path = folder / (stem + suffix)
            url = self.base.replace("/bot", "/file/bot", 1) + info["file_path"]
            with urllib.request.urlopen(url, timeout=40) as response:
                data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                return None
            # Atomic write: a crash cannot leave a partial file as a valid cache entry.
            temporary = folder / (stem + "_partial")
            temporary.write_bytes(data)
            temporary.replace(path)
            return str(path)
        except (ApiError, OSError, ValueError, KeyError):
            return None
