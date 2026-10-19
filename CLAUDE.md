# CLAUDE.md

Instructions for Claude sessions in this repository: the agent's own triage sessions (`python run.py --agent` runs
them inside `reports/cases/...`, so this file applies to them) and anyone working on the code.

## What this is

DataMonitorAgent watches every QuantConnect dataset: the DataFleet job runs and sync logs (`dsmon/`), the published
data folder (the DatasetWatch sensor, `sensor/`, run as a cloud backtest), and the sources. Each dataset that needs
action gets a case folder in `reports/cases/<date>/<dataset>/` with `case.md` and `case.json`. See `README.md` for
the commands and `RUNBOOK.md` for what each finding means.

## Safety rules

- **Read-only on DataFleet.** Never call a route that creates, edits, enables, disables, runs or deletes a job,
  and never change a dataset listing. The monitor reads `data-job/list`, `data-processing/list`,
  `market/data/list` and `market/sections/read` only. Recommend a re-run; never perform it.
- **No publishing from a triage session.** Do not open issues or pull requests, push, comment, or send messages.
  Write the analysis to the case folder. Publishing is a separate, explicit step.
- **Publishing, when enabled, goes to forks first.** Issues and pull requests go to the operator's personal forks
  of the `Lean.DataSource.*` repositories until the operator says otherwise; never create branches in QuantConnect
  repositories. A pull request cites its issue under "Related Issue".
- **Secrets.** Job configurations carry plaintext credentials of the data vendors. Never print, copy or store a
  configuration value; only keys and alert emails are kept. Never print the QuantConnect user id or token, or
  write them to the reports folder.
- **The reports folder is local.** `reports/` is not tracked. Do not commit anything from it, and do not move case
  contents to any external service.

## Triage

Follow `skills/triage/SKILL.md`. In short: rule out a false positive first (a failure the redundancy covered, a
holiday, a self-healing processor, a backfill, a sensor sample), then rate the cause as direct (stack trace,
reproducible response: act now) or inferred (missing date, stale folder, flaky source: confirm on the next run).

## Working on the code

- Python 3.11+, standard library only at runtime. `python -m pytest` runs the tests (`requirements-dev.txt`).
- `sensor/watch_core.py` runs inside a QuantConnect backtest: plain Python, and it must not import `zipfile`,
  `zlib`, `base64` or other compression or encoding packages (QuantConnect rejects such project files).
- Tests use real job and sync logs in `tests/fixtures/`; a new failure pattern gets a fixture and a test.
- Keep `config.toml` the only place for dataset-specific knowledge (probes, folder overrides, limits).
