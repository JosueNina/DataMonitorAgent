"""Deliver new findings. Every channel is optional and configured through environment variables, so no secret
ever lives in the repository: email (SMTP) and a Slack-compatible webhook. A local alert log always gets a line."""
import json
import smtplib
import urllib.request
from email.message import EmailMessage

from .api import env_value
from .report import to_markdown
from .store import Store


def notify_new(monitor, result):
    """Record the check, then send the findings not yet notified. Returns the channels used."""
    return _deliver(monitor, [result], lambda pending: _message(monitor, result, pending))


def notify_run(monitor, check, content=None):
    """One message for a whole `dsmon run`: new job findings and new content findings together."""
    results = [check] + ([content_as_check(monitor, content)] if content else [])

    def message(pending):
        from .report import run_markdown
        keys = {f.key for f in pending}
        subject = _subject(monitor, pending)
        return subject, run_markdown(check, content, monitor.inventory(), only_keys=keys)

    return _deliver(monitor, results, message)


def content_as_check(monitor, result):
    """Content findings in the shape the store records; each published directory is one scope."""
    from .health import PipelineStatus
    from .monitor import CheckResult
    scopes = [PipelineStatus(d["p"], [], [], "ok") for d in result.payload.get("datasets", []) if "p" in d]
    scopes += [PipelineStatus(s["file"], [], [], "ok") for s in monitor.config.get("reconcile", [])]
    return CheckResult(result.sensed_at, 0, sorted({d for f in result.findings for d in f.dataset_ids}),
                       scopes, result.findings)


def _deliver(monitor, results, build_message):
    store = Store(monitor.var / "dsmon.sqlite")
    try:
        pending = []
        min_severity = monitor.config.get("notify", {}).get("min_severity", "warning")
        for result in results:
            store.record(result)
            pending.extend(store.to_notify(result.findings, min_severity))
        if not pending:
            return []
        subject, body = build_message(pending)
        when = results[0].checked_at
        used = []
        for name, channel in (("email", _email), ("webhook", _webhook)):
            try:
                if channel(subject, body):
                    used.append(name)
            except Exception as error:  # a broken channel must not hide the alert from the others
                used.append(f"{name} failed: {error}")
        with open(monitor.var / "alerts.log", "a", encoding="utf-8") as log:
            log.write(f"{when.isoformat(timespec='seconds')} {subject} [{', '.join(used) or 'log only'}]\n")
        delivered = [u for u in used if "failed" not in u]
        # Retry next check only when every configured channel failed; log-only setups count as delivered.
        if delivered or len(delivered) == len(used):
            store.mark_notified(pending)
        return used
    finally:
        store.close()


def notify_content(monitor, result):
    return notify_new(monitor, content_as_check(monitor, result))


def _subject(monitor, pending):
    critical = sum(1 for f in pending if f.severity == "critical")
    names = {d.id: d.name for d in monitor.inventory().datasets.values()}
    affected = sorted({names.get(d, str(d)) for f in pending for d in f.dataset_ids}) or \
        sorted({f.pipeline for f in pending})
    level = "CRITICAL" if critical else "WARNING"
    return f"[DataMonitorAgent] {level}: {len(pending)} new finding(s): {', '.join(affected)[:120]}"


def _message(monitor, result, pending):
    subject = _subject(monitor, pending)
    focused = type(result)(result.checked_at, result.hours, result.dataset_ids, result.pipelines, pending,
                           result.capped_jobs, result.dependency_ids)
    return subject, to_markdown(focused, monitor.inventory())


def _email(subject, body):
    host, to = _get("DSMON_SMTP_HOST"), _get("DSMON_EMAIL_TO")
    if not host or not to:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _get("DSMON_EMAIL_FROM") or _get("DSMON_SMTP_USER") or "DataMonitorAgent"
    message["To"] = to
    message.set_content(body)
    with smtplib.SMTP(host, int(_get("DSMON_SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        if _get("DSMON_SMTP_USER"):
            smtp.login(_get("DSMON_SMTP_USER"), _get("DSMON_SMTP_PASSWORD", ""))
        smtp.send_message(message)
    return True


def _webhook(subject, body):
    url = _get("DSMON_WEBHOOK_URL")
    if not url:
        return False
    data = json.dumps({"text": f"*{subject}*\n{body[:3500]}"}).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30):
        pass
    return True


def _get(name, default=None):
    """Environment first, then the Windows user scope, so a scheduled task sees what `setx` stored."""
    return env_value(name, required=False) or default
