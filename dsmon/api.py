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
LEAN_CREDENTIALS = Path.home() / ".lean" / "credentials"  # written by `lean login`


class ApiError(RuntimeError):
    pass


class QCApi:
    def __init__(self, user_id=None, api_token=None, organization_id=None, base_url=BASE_URL, retries=3, timeout=60):
        found_user, found_token = credentials()
        self.user_id = user_id or found_user
        self.api_token = api_token or found_token
        if not self.user_id or not self.api_token:
            raise ApiError("no QuantConnect credentials: run `python run.py` once to enter them, or `lean login`")
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


def credentials():
    """(user id, API token) from the environment, .env, the Windows user scope, or the lean CLI login."""
    user = env_value("QC_USER_ID", "QC_JOB_USER_ID", required=False)
    token = env_value("QC_API_TOKEN", "QC_API_ACCESS_TOKEN", required=False)
    if user and token:
        return user, token
    try:
        lean = json.loads(LEAN_CREDENTIALS.read_text(encoding="utf-8"))
        return str(lean["user-id"]), lean["api-token"]
    except (OSError, ValueError, KeyError):
        return user, token


def save_credentials(user, token):
    """Write the credentials to the repository's .env (not tracked), keeping any other line already there."""
    lines = DOTENV.read_text(encoding="utf-8").splitlines() if DOTENV.exists() else []
    lines = [l for l in lines if not l.startswith(("QC_USER_ID=", "QC_API_TOKEN="))]
    DOTENV.write_text("\n".join(lines + [f"QC_USER_ID={user}", f"QC_API_TOKEN={token}"]) + "\n", encoding="utf-8")
    global _DOTENV
    _DOTENV = None


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
