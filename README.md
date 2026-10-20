# DataMonitorAgent

Watches every QuantConnect dataset: whether its DataFleet jobs published each processing date, why they did not,
and whether the published files are sound. Each dataset that needs action gets a case folder with the evidence, and
a Claude CLI session that triages it.

## Run it

Requirements: Python 3.11+, no packages.

```
python run.py
```

That is all. QuantConnect credentials are taken from the machine: the `lean` CLI login (`lean login`), the
environment (`QC_USER_ID`, `QC_API_TOKEN`) or `.env`. When none is found, the first run asks for the user id and
API token, checks them against DataFleet and keeps them in `.env` (not tracked). The agent uses the `claude` CLI as it
is logged in on the machine; when it is not, cases still get written and wait for triage. `python run.py doctor`
checks all of it: Python, credentials, DataFleet access, the reports folder, `claude`, `gh` and `git`.

`python run.py` keeps going until stopped:

- every hour, a job check of every dataset with jobs (about a minute)
- once a day, after 06:00 UTC, the DatasetWatch sensor: a cloud backtest that reads the published data folder
- after each cycle, one report and a case folder per dataset that needs action, and a Claude triage of up to three
  cases that have no analysis yet

The first cycle downloads about a week of job and sync logs for every job and runs the sensor right away: expect
10 to 20 minutes and a few hundred MB in `reports/`. Later cycles reuse that copy and take about a minute. The loop
runs in the foreground; stop it with Ctrl+C, and start it again with the same command (nothing is lost).

Other forms:

```
python run.py --once                 one cycle, then exit
python run.py --once 88 "fama"       only these datasets (ids or name fragments)
python run.py --once --agent         one cycle with triage even when [agent] enabled is false
python run.py triage reports/cases/2026-10-19/us-bureau-of-labor-statistics-bls
python run.py check 88 --hours 72    the monitor's commands: run, check, content, inventory, runs, log, probe
```

## What it checks

Jobs, per dataset and processing date, from the run history and every node's sync log:

- published, recovered by the redundancy, failed, never run, stuck, or complete without publishing
- the cause of each failed log (source down, access denied, TLS, not yet published, bad content, missing input,
  infrastructure, processor exception), with a probe of the source from here
- datasets that fail repeatedly, disabled redundancies, redundancies that process another date, stale pipelines
- false positives kept out: failures the redundancy covered, holidays and days a source never publishes, processors
  that refill a missed date, the cron and offset in force on each day, backfills

Published data, from the sensor: missing or thin dates, rows out of date order, one ticker under two security
ids, folders older than their cadence, and a few ECB and FDIC series compared with their source. Folders are
mapped to datasets from the inventory (job repository, vendor and name); `[content.paths]` in `config.toml`
overrides the guess.

## The reports folder

`reports/` is not tracked. Everything the monitor writes goes there:

| Path | What | Kept |
|---|---|---|
| `runs/` | one Markdown report per cycle, `latest.md` | 90 days |
| `cases/<date>/<dataset>/` | `case.md`, `case.json`, `analysis.md` from the agent | 90 days once closed; open cases stay |
| `logs/` | copies of job and sync logs (DataFleet deletes them after about 7 days) | 30 days, failed runs 180 |
| `content/` | sensor snapshots | last 30 |
| `inventory.json`, `dsmon.sqlite`, `alerts.log` | inventory cache, finding history, alert log | |

A case is `open` until its `case.json` says otherwise. A finding starts a case once: later cycles refresh the same
case and do not mark it new again.

## The agent

With `[agent] enabled = true` (the default) or `--agent`, each open case without an analysis gets one `claude -p`
session, critical cases first, at most `max_sessions_per_cycle` per cycle. The session follows `skills/triage/SKILL.md`: rule out a false positive, rate
the cause as direct (a stack trace, a reproducible response: act now) or inferred (a missing date, a flaky source:
confirm on the next run), locate the fix, and draft an issue in the LEAN format. It is read-only: its tools are
reading files and the web and read-only `gh` calls, and its answer goes to `analysis.md`.

Next steps, in order: issues in the dataset repositories (tested first on personal forks), then pull requests with
the fix from a fork, citing the issue.

## Safety

- Read-only on DataFleet: it never edits, enables or re-runs a job. It says which date to re-run and why.
- Job configurations hold plaintext secrets: only their keys and alert emails are stored, never values.
- Credentials are never printed or written to the reports folder.

## Layout

| Path | What |
|---|---|
| `run.py` | the entry point |
| `monitor_datasets.py` | the service loop |
| `agent_backend.py` | Claude CLI sessions |
| `doctor.py` | environment checks |
| `skills/` | the agent's instructions |
| `dsmon/` | the monitor: API client, inventory, runs, judging, logs, sensor client, report, cases |
| `sensor/` | DatasetWatch, uploaded to the QuantConnect project `DatasetWatchSensor` |
| `config.toml` | probes, self-healing datasets, folder mapping, limits, retention |
| `RUNBOOK.md` | what each finding means and what to do |
| `tests/` | `pip install -r requirements-dev.txt`, then `python -m pytest` |
