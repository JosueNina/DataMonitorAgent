"""Render a CheckResult as Markdown (people) or JSON (tools)."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime

_ICON = {"critical": "CRITICAL", "warning": "WARNING", "info": "info"}
_STATE_ORDER = ["failed", "stuck", "not_run", "failed_pending", "running", "pending", "empty", "recovered", "published"]


def to_markdown(result, inventory, include_info=True, max_detail=8):
    names = {d.id: d.name for d in inventory.datasets.values()}
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in ("critical", "warning", "info")}
    lines = [f"# Dataset monitor: {result.checked_at:%Y-%m-%d %H:%M} UTC",
             "",
             f"Window: last {result.hours:g} h. Datasets: {len(result.dataset_ids)}"
             + (f" (+ dependencies {', '.join(map(str, result.dependency_ids))})" if result.dependency_ids else "")
             + f". Findings: {counts['critical']} critical, {counts['warning']} warning, {counts['info']} info.",
             ""]
    if result.previous_check_at:
        runs = result.runs_since_previous
        failed = [r for r in runs if r.status == "Failed"]
        lines.append(f"Since the previous check ({result.previous_check_at:%Y-%m-%d %H:%M} UTC): {len(runs)} runs started, "
                     f"{sum(1 for r in runs if r.status == 'Complete')} complete, {len(failed)} failed, "
                     f"{sum(1 for r in runs if r.status in (None, 'In Progress'))} running.")
        for run in failed[:10]:
            lines.append(f"- failed: job {run.job_id} run {run.id} at {run.started:%m-%d %H:%M} UTC, date {run.processing_date}")
        lines.append("")
    lines += ["## Pipelines", "",
              "| Pipeline | Datasets | State | Dates in window | Last published (UTC, minutes after the run started) |",
              "|---|---|---|---|---|"]
    for status in sorted(result.pipelines, key=lambda s: ({"failing": 0, "degraded": 1, "ok": 2, "disabled": 3}[s.state], s.key)):
        if status.state == "disabled":
            continue
        tally = {}
        for day in status.dates:
            tally[day.state] = tally.get(day.state, 0) + 1
        dates = ", ".join(f"{tally[s]} {s}" for s in _STATE_ORDER if s in tally) or "none"
        datasets = ", ".join(names.get(d, str(d)) for d in status.dataset_ids)
        published = [d for d in status.dates if d.published_at]
        last = max(published, key=lambda d: d.processing_date, default=None)
        when = (f"{last.processing_date} at {last.published_at:%m-%d %H:%M} (+{last.publish_lag_minutes:g})"
                if last else "-")
        lines.append(f"| {status.key} | {datasets} | {status.state} | {dates} | {when} |")
    lines.append("")

    shown = [f for f in result.findings if include_info or f.severity != "info"]
    if shown:
        lines += ["## Findings", ""]
    for item in shown:
        datasets = ", ".join(names.get(d, str(d)) for d in item.dataset_ids)
        dependency_of = item.context.get("dependency_of")
        if dependency_of:
            datasets += " (input of " + ", ".join(names.get(d, str(d)) for d in dependency_of) + ")"
        lines.append(f"### [{_ICON[item.severity]}] {datasets}: {item.title}")
        lines.append("")
        meta = [f"pipeline `{item.pipeline}`"]
        if item.job_ids:
            meta.append("jobs " + ", ".join(map(str, item.job_ids)))
        if item.run_ids:
            meta.append("runs " + ", ".join(map(str, item.run_ids)))
        lines.append("_" + "; ".join(meta) + "_")
        lines.append("")
        for text in item.detail[:max_detail]:
            lines.append(f"- {text}")
        for result_probe in item.probes:
            lines.append(f"- Probe {'OK' if result_probe.ok else 'FAILED'}: {result_probe.describe()}")
        if item.actions:
            lines.append("")
            lines.append("**Actions**")
            lines.extend(f"{i}. {a}" for i, a in enumerate(item.actions, 1))
        lines.append("")
    if result.capped_jobs:
        lines.append(f"_Jobs at the 250-row API cap (oldest runs not visible): {', '.join(map(str, result.capped_jobs))}_")
    return "\n".join(lines).rstrip() + "\n"


def content_markdown(result, inventory, include_info=True):
    names = {d.id: d.name for d in inventory.datasets.values()}
    datasets = result.payload.get("datasets", [])
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in ("critical", "warning", "info")}
    lines = [f"# Published data check: {result.sensed_at:%Y-%m-%d %H:%M} UTC", "",
             f"Sensor read {len(datasets)} directories in {result.payload.get('seconds', '?')} s; compared with "
             f"{result.snapshots_before} earlier snapshot(s). Findings: {counts['critical']} critical, "
             f"{counts['warning']} warning, {counts['info']} info.", ""]
    if result.notes:
        lines += ["Source comparisons: " + "; ".join(result.notes), ""]
    unmapped = [f for f in result.findings if not f.dataset_ids and f.context.get("content_path")]
    if unmapped:
        by_vendor = {}
        for item in unmapped:
            vendor = "/".join(item.pipeline.split("/")[:2])
            by_vendor.setdefault(vendor, []).append(item)
        lines.append("## Not mapped to a dataset (summary)")
        lines.append("")
        for vendor, items in sorted(by_vendor.items(), key=lambda kv: -len(kv[1])):
            kinds = {}
            for item in items:
                kinds[item.kind] = kinds.get(item.kind, 0) + 1
            lines.append(f"- {vendor}: " + ", ".join(f"{n} {k.replace('content_', '')}" for k, n in sorted(kinds.items())))
        lines.append("")
    for item in result.findings:
        if item.severity == "info" and not include_info:
            continue
        if item in unmapped:
            continue
        label = ", ".join(names.get(d, str(d)) for d in item.dataset_ids) or "unmapped"
        lines.append(f"### [{item.severity.upper() if item.severity != 'info' else 'info'}] {item.pipeline} ({label}): "
                     f"{item.title}")
        lines.append("")
        lines.extend(f"- {d}" for d in item.detail[:8])
        if item.actions:
            lines += ["", "**Actions**"] + [f"{i}. {a}" for i, a in enumerate(item.actions, 1)]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_markdown(check, content, inventory, only_keys=None, sections_out=None):
    """The report of `dsmon run`, grouped by dataset. Every dataset gets its section with everything the checks
    found about it: state, the processing dates in the window, each finding with its full detail, evidence and
    actions, and its published folders. Datasets with problems come first. `sections_out`, when given, receives
    (label, dataset ids, level, findings, section markdown) per dataset, for the case folders."""
    def keep(findings):
        return [f for f in findings if only_keys is None or f.key in only_keys]

    rank = {"critical": 3, "warning": 2, "info": 1}
    names = {d.id: d.name for d in inventory.datasets.values()}
    findings = keep(check.findings) + (keep(content.findings) if content else [])
    status_of = _dataset_status(check, content, inventory)

    # Groups: one per set of datasets a pipeline serves, plus any set a finding names that no pipeline does.
    groups = {}
    for status in check.pipelines:
        if status.state != "disabled":
            groups.setdefault(tuple(sorted(status.dataset_ids)), {"pipelines": [], "findings": []})["pipelines"].append(status)
    unmapped = []
    for item in findings:
        if not item.dataset_ids:
            unmapped.append(item)
            continue
        groups.setdefault(tuple(sorted(item.dataset_ids)), {"pipelines": [], "findings": []})["findings"].append(item)
    if only_keys is not None:  # an alert: only the datasets with something new
        groups = {ids: g for ids, g in groups.items() if g["findings"]}

    # A dataset with a section of its own is left out of the label of a pipeline it only shares
    # (FDIC failure jobs also hang off Bank Financials): that section is "US Bank Failures".
    solo = {ids[0] for ids in groups if len(ids) == 1}
    sections = []
    for ids, group in groups.items():
        items = sorted(group["findings"], key=lambda f: (f.kind != "recurring_failure", -rank[f.severity]))
        worst = max((f.severity for f in items), key=lambda s: rank[s], default="ok")
        level = worst if worst in ("critical", "warning") else "ok"
        order = {"critical": 0, "warning": 1, "ok": 2}[level]
        shown = tuple(d for d in ids if d not in solo) if len(ids) > 1 else ids
        sections.append((order, not any(f.kind == "recurring_failure" for f in items),
                         _label(shown or ids, names, items),
                         ids, level, items, group["pipelines"]))
    sections.sort(key=lambda s: s[:3])

    counts = {level: sum(1 for s in sections if s[4] == level) for level in ("critical", "warning", "ok")}
    lines = [f"# DataMonitorAgent: {check.checked_at:%Y-%m-%d %H:%M} UTC", "",
             f"{len(sections)} datasets: {counts['critical']} critical, {counts['warning']} warning, "
             f"{counts['ok']} ok. Window: last {check.hours:g} h."]
    if check.previous_check_at:
        runs = check.runs_since_previous
        failed = [r for r in runs if r.status == "Failed"]
        lines.append(f"Since the previous check ({check.previous_check_at:%Y-%m-%d %H:%M} UTC): {len(runs)} runs "
                     f"started, {sum(1 for r in runs if r.status == 'Complete')} complete, {len(failed)} failed, "
                     f"{sum(1 for r in runs if r.status in (None, 'In Progress'))} running.")
        lines.extend(f"- failed: job {r.job_id} run {r.id} at {r.started:%m-%d %H:%M} UTC, date {r.processing_date}"
                     for r in failed)
    lines += ["", "| Dataset | State |", "|---|---|"]
    lines += [f"| {s[2]} | {s[4]} |" for s in sections]
    lines.append("")

    # The datasets that need someone, each with what is wrong and the first thing to do; the sections below
    # hold the evidence and the full list of actions.
    to_act = [s for s in sections if s[4] in ("critical", "warning")]
    if to_act:
        lines += ["## Take action", ""]
        for _, _, label, _, level, items, _ in to_act:
            headline = next(f for f in items if f.severity != "info")
            first = next((a for f in items if f.severity != "info" for a in (f.actions or [])
                          if not (headline.kind == "recurring_failure" and "failure was transient" in a)), None)
            others = sum(1 for f in items if f.severity != "info") - 1
            lines.append(f"- **{label}** ({level.upper()}): {headline.title}"
                         + (f" (+{others} more)" if others > 0 else ""))
            if first:
                lines.append(f"  - Do: {first}")
        lines.append("")
    else:
        lines += ["Nothing needs action.", ""]

    for _, _, label, ids, level, items, pipelines in sections:
        start = len(lines)
        lines.append(f"## {label}: {level.upper()}")
        lines.append("")
        state = max(filter(None, (status_of.get(d) for d in ids)), key=len, default="")
        if state:
            lines += [state, ""]
        for status in pipelines:
            if status.dates:
                lines += [f"Pipeline `{status.key}`: {status.state}", "",
                          "| Processing date | State | Published (UTC, minutes after the run started) | Runs |",
                          "|---|---|---|---|"]
                for day in sorted(status.dates, key=lambda d: d.processing_date):
                    published = (f"{day.published_at:%m-%d %H:%M} (+{day.publish_lag_minutes:g})"
                                 if day.published_at else "-")
                    runs = ", ".join(f"{r.id} {r.status}" for r in day.runs) or "-"
                    lines.append(f"| {day.processing_date} | {day.state} | {published} | {runs} |")
                lines.append("")
        if items:
            lines += ["### Findings", ""]
        for item in items:
            lines.append(f"**[{item.severity}] {item.title}**")
            lines.append("")
            meta = []
            if item.job_ids:
                meta.append("jobs " + ", ".join(map(str, item.job_ids)))
            if item.run_ids:
                meta.append("runs " + ", ".join(map(str, item.run_ids)))
            if meta:
                lines += ["_" + "; ".join(meta) + "_", ""]
            lines.extend(f"- {text}" for text in item.detail)
            lines.extend(f"- Probe {'OK' if p.ok else 'FAILED'}: {p.describe()}" for p in item.probes)
            if item.actions:
                lines += ["", "Actions:"] + [f"{i}. {a}" for i, a in enumerate(item.actions, 1)]
            lines.append("")
        folders = _folders(content, ids)
        if folders:
            lines += ["### Published folders", "", "| Folder | Files | Newest date |", "|---|---|---|"]
            lines.extend(folders)
            lines.append("")
        if sections_out is not None:
            sections_out.append((label, ids, level, items, "\n".join(lines[start:]).rstrip() + "\n"))

    if unmapped and content is not None:
        by_vendor = {}
        for item in unmapped:
            by_vendor.setdefault("/".join(item.pipeline.split("/")[:2]), []).append(item)
        lines += ["## Published folders no dataset maps", ""]
        for vendor, items in sorted(by_vendor.items(), key=lambda kv: -len(kv[1])):
            kinds = {}
            for item in items:
                kinds[item.kind.replace("content_", "")] = kinds.get(item.kind.replace("content_", ""), 0) + 1
            lines.append(f"- {vendor}: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
        lines.append("")
    if content is not None:
        lines.append(f"Published data read by the sensor at {content.sensed_at:%Y-%m-%d %H:%M} UTC "
                     f"({len(content.payload.get('datasets', []))} folders, compared with {content.snapshots_before} "
                     "earlier snapshots).")
        if content.notes:
            lines.append("Source comparisons: " + "; ".join(content.notes))
    if check.capped_jobs:
        lines.append(f"Jobs at the 250-row API cap (oldest runs not visible): {', '.join(map(str, check.capped_jobs))}")
    return "\n".join(lines).rstrip() + "\n"


def _folders(content, ids):
    if content is None:
        return []
    prefixes = [p for d in ids for p in (content.mapping or {}).get(d, [])]
    rows = []
    for facts in sorted(content.payload.get("datasets", []), key=lambda f: f.get("p", "")):
        path = facts.get("p", "")
        if prefixes and any(path.startswith(p) for p in prefixes):
            newest = (facts.get("dated") or {}).get("newest") or (facts.get("files") or {}).get("newest_last") or "-"
            rows.append(f"| {path} | {facts.get('n', '-')} | {newest} |")
    return rows


def _label(ids, names, items):
    label = ", ".join(names.get(d, str(d)) for d in ids)
    dependency_of = sorted({d for f in items for d in f.context.get("dependency_of", [])})
    if dependency_of:
        label += " (input of " + ", ".join(names.get(d, str(d)) for d in dependency_of) + ")"
    return label


def _dataset_status(check, content, inventory):
    """One line per dataset: when its newest date was published and what the newest data in the folder is."""
    out = {}
    # A job linked to several datasets (FDIC failures also hangs off Bank Financials) must not stand for a dataset
    # that has its own pipeline: the most specific pipeline wins.
    for status in sorted(check.pipelines, key=lambda s: len(s.dataset_ids)):
        if status.state == "disabled":
            continue
        published = [d for d in status.dates if d.published_at]
        latest = max(published, key=lambda d: d.processing_date, default=None)
        parts = []
        if status.last_published:
            when = f" (in /Data {latest.published_at:%m-%d %H:%M} UTC)" if latest and \
                latest.processing_date == status.last_published else ""
            parts.append(f"last published date {status.last_published}{when}")
        jobs = [inventory.jobs[j] for j in status.job_ids if j in inventory.jobs]
        disabled = [str(j.id) for j in jobs if not j.enabled]
        parts.append("jobs " + ", ".join(str(j.id) for j in jobs if j.enabled) +
                     (f" ({', '.join(disabled)} disabled)" if disabled else ""))
        for dataset_id in status.dataset_ids:
            out.setdefault(dataset_id, "; ".join(parts))
    if content is not None:
        # Newest data only where an age limit says what "current" means for the dataset; for per-filer or
        # event-driven folders the newest sampled row is not a freshness signal.
        limited = tuple((getattr(content, "settings", None) or {}).get("max_age_days", {}))
        newest = {}
        for facts in content.payload.get("datasets", []):
            day = (facts.get("dated") or {}).get("newest") or (facts.get("files") or {}).get("newest_last")
            if not day:
                continue
            for dataset_id, prefixes in (content.mapping or {}).items():
                path = facts.get("p", "")
                if limited and not path.startswith(limited):
                    continue
                if any(path.startswith(p) for p in prefixes) and day > newest.get(dataset_id, ""):
                    newest[dataset_id] = day
        for dataset_id, day in newest.items():
            text = f"newest data {day[:4]}-{day[4:6]}-{day[6:]}"
            out[dataset_id] = f"{out[dataset_id]}; {text}" if out.get(dataset_id) else text
    return out


def to_json(result):
    def convert(value):
        if is_dataclass(value):
            return {k: convert(v) for k, v in asdict(value).items()}
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    pipelines = []
    for status in result.pipelines:
        pipelines.append({
            "key": status.key, "dataset_ids": status.dataset_ids, "job_ids": status.job_ids, "state": status.state,
            "dates": [{"processing_date": d.processing_date.isoformat(), "state": d.state,
                       "published_at": d.published_at.isoformat() if d.published_at else None,
                       "publish_lag_minutes": d.publish_lag_minutes,
                       "runs": [{"id": r.id, "job_id": r.job_id, "started": r.started.isoformat(), "status": r.status,
                                 "published": r.published, "sync_skipped": r.sync_skipped,
                                 "sync": [{"node": s.name, "status": s.status,
                                           "decision": s.info.decision if s.info else None,
                                           "synced_at": s.info.synced_at.isoformat() if s.info and s.info.synced_at else None}
                                          for s in r.sync]} for r in d.runs]}
                      for d in status.dates],
        })
    findings = []
    for item in result.findings:
        entry = convert(item)
        entry["key"] = item.key
        findings.append(entry)
    return {"checked_at": result.checked_at.isoformat(), "hours": result.hours, "dataset_ids": result.dataset_ids,
            "dependency_ids": result.dependency_ids, "worst": result.worst, "capped_jobs": result.capped_jobs,
            "pipelines": pipelines, "findings": findings}


def summary_line(result):
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in ("critical", "warning", "info")}
    failing = [s.key for s in result.pipelines if s.state == "failing"]
    text = f"{counts['critical']} critical, {counts['warning']} warning, {counts['info']} info"
    return text + (f"; failing: {', '.join(failing)}" if failing else "")
