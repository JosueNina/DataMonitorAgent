"""`python run.py doctor`: checks that this machine can run the monitor, with the credentials already on it."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _run(command):
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        return done.returncode, (done.stdout + done.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def check_python():
    ok = sys.version_info >= (3, 11)
    return ok, f"Python {sys.version.split()[0]}" + ("" if ok else " (3.11 or newer is required)")


def check_qc_credentials():
    from dsmon.api import credentials
    user, token = credentials()
    if not user or not token:
        return False, "no QuantConnect credentials: run `python run.py` to enter them, or `lean login`"
    return True, "QuantConnect credentials found"


def ensure_credentials():
    """First run: when no credentials are found anywhere, ask for them once, check them, and keep them in .env."""
    import getpass
    from dsmon.api import QCApi, credentials, save_credentials
    if all(credentials()):
        return True
    if not sys.stdin.isatty():
        print("No QuantConnect credentials found. Run `python run.py` in a terminal once to enter them.",
              file=sys.stderr)
        return False
    print("QuantConnect API credentials (Account page, API Access). They are checked, then kept in .env here.")
    for _ in range(3):
        user = input("User id: ").strip()
        token = getpass.getpass("API token (hidden): ").strip()
        try:
            jobs = QCApi(user, token, retries=1).post_form("/market/dashboard/data-job/list/").get("list", [])
        except Exception as error:
            print(f"Those credentials do not open the DataFleet job list: {str(error)[:200]}")
            continue
        save_credentials(user, token)
        print(f"Saved. DataFleet job list readable ({len(jobs)} jobs).")
        return True
    return False


def check_qc_admin():
    from dsmon.api import QCApi
    try:
        body = QCApi(retries=1).post_form("/market/dashboard/data-job/list/")
    except Exception as error:
        return False, f"DataFleet admin routes refused: {str(error)[:200]}"
    return True, f"DataFleet job list readable ({len(body.get('list', []))} jobs)"


def check_claude():
    from agent_backend import logged_in
    ok, message = logged_in()
    return ok, message + ("" if ok else " (needed only for --agent)")


def check_gh():
    path = shutil.which("gh")
    if not path:
        return False, "gh not found on PATH (needed only to publish issues and PRs, a later step)"
    code, out = _run([path, "api", "user", "--jq", ".login"])
    return code == 0, f"gh logged in as {out}" if code == 0 else f"gh is not logged in: run `gh auth login` ({out[:120]})"


def check_git():
    path = shutil.which("git")
    if not path:
        return False, "git not found on PATH"
    code, out = _run([path, "--version"])
    return code == 0, out


def check_reports():
    folder = ROOT / "reports"
    try:
        folder.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder):
            pass
    except OSError as error:
        return False, f"cannot write {folder}: {error}"
    return True, f"reports folder writable ({folder})"


# (name, check, required): optional checks only matter for the agent steps.
CHECKS = [("python", check_python, True), ("credentials", check_qc_credentials, True),
          ("datafleet", check_qc_admin, True), ("reports", check_reports, True),
          ("claude", check_claude, False), ("gh", check_gh, False), ("git", check_git, False)]


def main():
    failed = False
    for name, check, required in CHECKS:
        try:
            ok, message = check()
        except Exception as error:
            ok, message = False, f"{type(error).__name__}: {error}"
        mark = "OK  " if ok else ("FAIL" if required else "WARN")
        print(f"{mark} {name:<12} {message}")
        failed |= required and not ok
        if name == "credentials" and not ok:
            print("     skipping the DataFleet check: no credentials")
            break
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
