"""Claude CLI backend: one non-interactive `claude -p` session per case folder.

The session runs with the case folder as its working directory, the skill as an appended system prompt and the
case as the prompt (stdin). Tools are an allow-list: reading files and the web, and read-only `gh` calls to look at
the dataset's repository. It cannot edit, push, or open issues; its answer is written to `analysis.md` in the case.
Publishing (issues, then PRs from forks) comes later and goes through its own, explicit step.
"""
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


def run_claude(prompt, system_prompt, cwd, tools, timeout_minutes=20):
    """Run one print-mode session; returns (exit code, stdout, stderr)."""
    command = [claude_path() or "claude", "-p", "--append-system-prompt", system_prompt,
               "--output-format", "text", "--allowedTools", *tools]
    completed = subprocess.run(command, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               timeout=timeout_minutes * 60)
    return completed.returncode, completed.stdout, completed.stderr


def triage(case_dir, timeout_minutes=20):
    """Read-only triage of a case. Writes analysis.md next to case.md and returns its path."""
    case_dir = Path(case_dir)
    skill = (SKILLS / "triage" / "SKILL.md").read_text(encoding="utf-8")
    prompt = ("Triage the case in the current directory: read case.md and case.json, follow the triage skill, and "
              "answer with the analysis only.")
    started = datetime.now(timezone.utc)
    try:
        code, out, err = run_claude(prompt, skill, case_dir, READ_ONLY_TOOLS, timeout_minutes)
    except subprocess.TimeoutExpired:
        code, out, err = -1, "", f"timed out after {timeout_minutes} minutes"
    path = case_dir / "analysis.md"
    header = f"<!-- claude triage {started:%Y-%m-%d %H:%M} UTC, exit {code} -->\n"
    body = out.strip() or f"The triage session produced no answer.\n\n```\n{err.strip()[:2000]}\n```"
    path.write_text(header + body + "\n", encoding="utf-8")
    return path
