"""Claude CLI backend: one non-interactive `claude -p` session per case folder.

The session runs with the case folder as its working directory (so the repository's CLAUDE.md applies), the skill
as an appended system prompt and the case as the prompt (stdin). Tools are an allow-list: reading files and the
web, and read-only `gh` calls to look at the dataset's repository. It cannot edit, push, or open issues; its answer
is written to `analysis.md` in the case. Publishing (issues, then PRs from forks) is a separate, explicit step.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / "skills"

READ_ONLY_TOOLS = [
    "Read", "Grep", "Glob", "WebFetch",
    "Bash(gh repo view:*)", "Bash(gh api repos/*/contents/*)", "Bash(gh api repos/*/commits*)",
    "Bash(gh issue list:*)", "Bash(gh pr list:*)", "Bash(gh search:*)",
]


def claude_path():
    return shutil.which("claude")


def logged_in():
    """(ok, message): whether the claude CLI on this machine has a usable login."""
    path = claude_path()
    if not path:
        return False, "claude CLI not found on PATH"
    try:
        done = subprocess.run([path, "auth", "status"], capture_output=True, text=True, encoding="utf-8", timeout=60)
        status = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return False, f"could not read `claude auth status`: {error}"
    if not status.get("loggedIn"):
        return False, "claude CLI is not logged in: run `claude auth login`"
    return True, f"claude CLI logged in ({status.get('authMethod', '?')})"


def run_claude(prompt, system_prompt, cwd, tools, timeout_minutes=20):
    """Run one print-mode session; returns (exit code, stdout, stderr)."""
    command = [claude_path() or "claude", "-p", "--append-system-prompt", system_prompt,
               "--output-format", "text", "--allowedTools", *tools]
    completed = subprocess.run(command, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               timeout=timeout_minutes * 60)
    return completed.returncode, completed.stdout, completed.stderr


def triage(case_dir, timeout_minutes=20):
    """Read-only triage of a case. Writes analysis.md next to case.md and returns its path. A session that fails
    writes analysis-error.log instead, so the case is tried again on the next cycle."""
    case_dir = Path(case_dir)
    skill = (SKILLS / "triage" / "SKILL.md").read_text(encoding="utf-8")
    prompt = ("Triage the case in the current directory: read case.md and case.json, follow the triage skill, and "
              "answer with the analysis only.")
    started = datetime.now(timezone.utc)
    try:
        code, out, err = run_claude(prompt, skill, case_dir, READ_ONLY_TOOLS, timeout_minutes)
    except subprocess.TimeoutExpired:
        code, out, err = -1, "", f"timed out after {timeout_minutes} minutes"
    if code != 0 or not out.strip():
        path = case_dir / "analysis-error.log"
        path.write_text(f"{started:%Y-%m-%d %H:%M} UTC, exit {code}\n{out.strip()}\n{err.strip()[:2000]}\n",
                        encoding="utf-8")
        return path
    path = case_dir / "analysis.md"
    path.write_text(f"<!-- claude triage {started:%Y-%m-%d %H:%M} UTC -->\n{out.strip()}\n", encoding="utf-8")
    (case_dir / "analysis-error.log").unlink(missing_ok=True)
    return path


def pending_cases(cases_folder):
    """Open cases without an analysis yet: critical first, then the most recently updated."""
    found = []
    for case_json in Path(cases_folder).glob("*/*/case.json"):
        try:
            case = json.loads(case_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if case.get("status", "open") == "open" and not (case_json.parent / "analysis.md").exists():
            found.append((case.get("level") == "critical", case.get("updated", ""), case_json.parent))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in found]
