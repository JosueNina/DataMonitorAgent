"""What to do about a finding. The steps encode how DataFleet behaves (see RUNBOOK.md for the why)."""

CATEGORY_TEXT = {
    "source_bad_content": "The source answered, but with an unusable body (null bytes, empty page, bad zip/JSON).",
    "source_tls": "The TLS handshake with the source failed (expired or mismatched certificate).",
    "source_access_denied": "The source refused the request (401/403, login failure, missing key).",
    "source_not_published": "The file the job asked for does not exist at the source (404, FTP 550, not in the index).",
    "source_unavailable": "The source did not answer properly (5xx, timeout, connection or DNS failure).",
    "missing_input": "An input the job reads from the data store or another job was missing.",
    "infra": "The job ran out of memory, disk, or was killed.",
    "code_error": "The processor raised an exception unrelated to the source: a bug or an unhandled case.",
    "unknown": "The log does not show why the run failed.",
}

CAUSE_SHORT = {
    "source_bad_content": "the source sends an unusable body",
    "source_tls": "TLS errors with the source",
    "source_access_denied": "the source refuses access",
    "source_not_published": "the source has not published the file",
    "source_unavailable": "the source is down or times out",
    "missing_input": "an input from another job is missing",
    "infra": "memory, disk or the job being killed",
    "code_error": "a processor exception",
}

_MANUAL_RUN = "Manual Run of the primary job for processing date {date} with Synchronize Once, once the cause is gone."
_TODAY_WARNING = (" Never pick today's date on a daytime manual run of a job with date offset 1: it uses up the date "
                  "of tonight's scheduled run, which then comes out Skipped.")


class _ManualRun:
    """`_MANUAL_RUN.format(date=...)` that adds the offset warning only where it applies."""

    def __init__(self, finding):
        self.offset = (finding.context or {}).get("offset")

    def format(self, date):
        text = _MANUAL_RUN.format(date=date)
        return text + _TODAY_WARNING if self.offset not in (None, 0) else text


def actions_for(finding):
    kind = finding.kind
    manual = _ManualRun(finding)
    day = finding.processing_date.isoformat() if finding.processing_date else "the missing date"
    if kind == "redundancy_disabled":
        return ["Ask the data team to enable the redundancy job, with the same date offset as the primary "
                "and a run time after the primary's.",
                "Until then a single failed run leaves the date unpublished."]
    if kind == "no_redundancy":
        return ["Consider a redundancy job: same chain name and offset, scheduled after the primary."]
    if kind == "primary_disabled":
        return ["Confirm with the data team that the primary is disabled on purpose."]
    if kind == "offset_mismatch":
        return ["Make every job of the pipeline process the same date: processing date = UTC run date - offset.",
                "A redundancy on a different processing date publishes first and turns the primary into Skipped."]
    if kind == "commit_mismatch":
        return ["Point every job of the pipeline at the same commit (Commit URL + Force Build)."]
    if kind == "run_stuck":
        return ["Check the run in the DataFleet panel and stop it if it is hung.",
                "Then: " + manual.format(date=day)]
    if kind in ("date_not_run", "run_missed"):
        return ["The scheduler did not start the job. Confirm the job is Enabled and its cron/timezone in the panel.",
                "If the date is unpublished: " + manual.format(date=day)]
    if kind == "date_empty":
        return ["Compare with the source: if it published new data for that date, the processor dropped it "
                "(read the log); if not, nothing to do."]
    if kind == "date_failed_pending":
        return ["Wait for the next scheduled run; re-check after it."] + _cause_actions(finding, day, final=False, manual=manual)
    if kind == "date_failed":
        if (finding.context or {}).get("self_healed_by"):
            return ["The processor recovers missed dates on its next run: confirm the date is in the published "
                    "data (content check) before re-running anything."] + \
                _cause_actions(finding, day, final=False, manual=manual)
        return _cause_actions(finding, day, final=True, manual=manual)
    if kind == "date_recovered":
        category = finding.diagnosis.category if finding.diagnosis else "unknown"
        return ["Nothing to publish: a later run covered the date.",
                f"Why the first run failed: {CATEGORY_TEXT.get(category, category)} If this repeats every day, "
                "fix the cause or schedule the primary later, so the date does not depend on the redundancy."]
    if kind == "recurring_failure":
        cause = (finding.context or {}).get("cause", "unknown")
        if cause == "unknown":
            # No rule recognized the logs: nothing says the failures share a cause, so do not claim it.
            steps = ["Read the newest failed log (the `log` command): the monitor did not recognize its cause, so "
                     "it cannot tell whether the failures share one. Add a rule to dsmon/logs.py once it is known."]
            if not (finding.context or {}).get("has_redundancy"):
                steps.append("Ask the data team to enable a redundancy job with the same processing date, so a "
                             "failed run is retried the same day.")
            return steps
        steps = [f"This is not transient: the same cause on several days ({CAUSE_SHORT.get(cause, cause)}). Fix it "
                 "in the processor or in how it reaches the source, rather than re-running dates; until then every "
                 "failure delays or loses a date."]
        if not (finding.context or {}).get("has_redundancy"):
            steps.append("Ask the data team to enable a redundancy job with the same processing date, so a failed "
                         "run is retried the same day.")
        return steps
    if kind == "config_changed":
        return ["Nothing to do if the change was intended. Dates processed under the old configuration are judged "
                "by their runs; a date neither configuration processed shows up as a gap in the content check."]
    if kind == "backfill":
        return ["Nothing to do if someone is re-processing history on purpose."]
    if kind == "stale":
        return ["The dataset stopped advancing: read the latest runs (the `runs` command) and the newest log, and "
                "check the source with the `probe` command. Raise it with the data team if the jobs do not run at all."]
    if kind == "run_off_schedule":
        return ["Nothing to do if the cron was changed on purpose or the run was manual."]
    if kind == "published_again":
        return ["Expected after a manual re-run. Otherwise the redundancy's cron turn re-published: compare the two "
                "tars if the source revises data, since the later one wins."]
    if kind == "sync_failed":
        return ["Ask the data team to re-sync the node (or Manual Run with Synchronize Always for that date)."]
    if kind == "sync_diverged":
        return ["Check the data on the node that skipped; if it lacks the files, re-sync it."]
    if kind == "skipped_unseen":
        return ["Find the job that shares this chain name (the `inventory` command) or the manual run, and confirm it "
                "publishes the same data as this pipeline."]
    if kind == "duration_anomaly":
        return ["Read the log: a run much shorter than usual often processed nothing; much longer, retries or a "
                "bigger backfill."]
    if kind == "completed_with_errors":
        return ["The run completed but logged source errors: check that no part of the data was skipped."]
    return []


def _cause_actions(finding, day, final, manual):
    diagnosis = finding.diagnosis
    category = diagnosis.category if diagnosis else "unknown"
    probes = finding.probes or []
    probed = bool(probes)
    source_up = probed and all(p.ok for p in probes)
    steps = []
    if category.startswith("source_"):
        if not probed:
            steps.append("No probe covers this source yet: check it by hand (or add a [[probe]] to config.toml) to "
                         "see whether it is down for everyone or only for the job node.")
        elif source_up:
            steps.append("The source answers correctly from here now: the failure was transient or specific to "
                         "the job node (IP block, CDN edge, rate limit).")
            if final:
                steps.append(manual.format(date=day))
            steps.append("If it fails again on the next scheduled run, it is node-specific: raise it with the data "
                         "team with the log and this probe.")
        else:
            steps.append("The source fails from here too: it is down or changed for everyone. Not a processor bug; "
                         "wait for it, and check the provider's status page or announcements.")
            if category in ("source_access_denied", "source_tls"):
                steps.append("For 401/403 or certificate errors that last more than a day, the provider changed "
                             "access rules: credentials, User-Agent policy or host. That needs a code or config change.")
        if (finding.context or {}).get("non_business_day"):
            steps = ["The processing date is not a business day: the source had nothing to publish. Nothing to "
                     "recover.",
                     "The processor should skip days the source does not publish instead of failing; otherwise "
                     "every holiday raises an alert. Worth a fix in the dataset repo."]
    elif category == "missing_input":
        steps.append("An upstream input was missing. Check the dependency status below; once it is published: "
                     + manual.format(date=day))
    elif category == "infra":
        steps.append("Infrastructure problem (memory/disk/kill): raise it with the data team; then: "
                     + manual.format(date=day))
    elif category == "code_error":
        steps.append("Processor bug: reproduce locally with QC_DATAFLEET_DEPLOYMENT_DATE set to the processing date, "
                     "fix it in the dataset repo, then update the job's Commit URL and Force Build.")
        if final:
            steps.append("After the fix is deployed: " + manual.format(date=day))
    else:
        steps.append("Read the full log (the `log` command); the cause is not recognized automatically.")
    return steps
