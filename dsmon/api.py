"""Signed client for the QuantConnect API v2, including the internal DataFleet admin routes."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from hashlib import sha256
from pathlib import Path

BASE_URL = "https://www.quantconnect.com/api/v2"
DOTENV = Path(__file__).resolve().parent.parent / ".env"


class ApiError(RuntimeError):
    pass


class QCApi:
    def __init__(self, user_id=None, api_token=None, organization_id=None, base_url=BASE_URL, retries=3, timeout=60):
        self.user_id = user_id or env_value("QC_USER_ID", "QC_JOB_USER_ID")
        self.api_token = api_token or env_value("QC_API_TOKEN", "QC_API_ACCESS_TOKEN")
        self.organization_id = organization_id or env_value("QC_ORGANIZATION_ID", "QC_JOB_ORGANIZATION_ID", required=False)
        self.base_url = base_url
        self.retries = retries
        self.timeout = timeout

    def _headers(self, content_type):
        timestamp = str(int(time.time()))
        digest = sha256(f"{self.api_token}:{timestamp}".encode()).hexdigest()
        auth = b64encode(f"{self.user_id}:{digest}".encode()).decode()
        return {"Authorization": f"Basic {auth}", "Timestamp": timestamp, "Content-Type": content_type}

    def post_form(self, path, fields=None):
        """Admin routes take form-encoded bodies, the way the web app sends them."""
        data = urllib.parse.urlencode(fields or {}, doseq=True).encode()
        return self._post(path, data, "application/x-www-form-urlencoded")

    def post_json(self, path, payload=None):
        return self._post(path, json.dumps(payload or {}).encode(), "application/json")

    def _post(self, path, data, content_type):
        last_error = None
        for attempt in range(self.retries):
            request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=self._headers(content_type))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                time.sleep(2 ** attempt)
                continue
            if not body.get("success", False):
                raise ApiError(f"{path}: {body.get('errors') or body}")
            return body
        raise ApiError(f"{path}: {last_error}")


def fetch_text(url, timeout=60):
    """GET a presigned S3 URL (job and sync logs)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def env_value(*names, required=True):
    """The process environment, then the repository's .env, then (Windows) the user scope `setx` writes to."""
    for name in names:
        value = os.environ.get(name) or _dotenv().get(name)
        if not value and os.name == "nt":
            value = _windows_user_env(name)
        if value:
            return value
    if required:
        raise ApiError(f"missing credentials: set one of {', '.join(names)}")
    return None


_DOTENV = None


def _dotenv():
    global _DOTENV
    if _DOTENV is None:
        _DOTENV = {}
        if DOTENV.exists():
            for line in DOTENV.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    _DOTENV[key.strip()] = value.strip().strip('"').strip("'")
    return _DOTENV


def _windows_user_env(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
