# Runbook

## How DataFleet behaves

The findings below lean on these facts:

- **Processing date = UTC date of the run - the job's date offset.** A job at 23:00 New York runs on the next UTC
  day, so its offset decides which date it processes.
- **Synchronize Once.** Scheduled jobs sync with "Once": a run skips its sync when a run of the same **chain name**
  already published that processing date. That is why a healthy redundancy shows `Skipped`.
- **Manual Run on an offset-1 job.** Never pick today's date during the day: it uses up the date of tonight's
  scheduled run, which then comes out `Skipped`.
- **Chain steps are separate rows.** Each step of a job chain is its own run row. A trivial step ("sleep 1s") can be
  `Complete` while the real step `Failed`.
- **Run history.** The run list keeps 14 days, but log objects expire after about 7.
- **Each node writes a sync log.** Its `ShouldSync` line holds the decision: "No prior successful sync" (first
  publication), "will be synced" (the date was synced before, but this is a new turn of the syncing job's cron, so
  it gets published again), or "will not be synced" (Skipped, with the time of the prior sync). A log without a
  `ShouldSync` line comes from a Manual Run with Synchronize Always. "Successfully synced" is when the data
  reached `/Data`.
- **No files, no sync.** A run that writes no files skips the sync and still reports `Complete`.

## Findings

| Kind | Severity | Meaning | What to do |
|---|---|---|---|
| `date_failed` | critical | Every run for the date failed and none is still due. | Read the cause and the probes. If the source answers now, Manual Run for that date. If it fails everywhere, wait for the source. If it is a code error, fix it, update the Commit URL, Force Build, then Manual Run. |
| `date_failed` on a holiday or weekend | warning | The source had nothing to publish, so the processor failed. | Nothing to recover. Make the processor skip non-publication days. |
| `date_failed_pending` | warning | Failed so far; a run for the date is still scheduled. | Wait for it and re-check. |
| `date_not_run` | critical | The date was due and nothing ran. | The scheduler did not start the job: check that it is Enabled and its cron, then Manual Run. |
| `run_stuck` | critical | In progress beyond 1.5x its timeout (or `stuck_hours`). | Stop it in the panel, then Manual Run. |
| `run_missed` | warning | One scheduled run did not start, but the date got published anyway. | Watch for a repeat. A recent cron change can produce one of these. |
| `date_recovered` | info | The first run failed and a later run published. | Nothing to publish. If it repeats every day, fix the cause or schedule the primary later. |
| `date_empty` | info | It completed without publishing files. | Compare with the source. |
| `redundancy_disabled` | warning | The redundancy exists but is Disabled. | Ask the data team to enable it, with the same processing date as the primary. |
| `offset_mismatch` | warning | Redundancy runs after the primary process a different date. | Align the offsets with the UTC run times. |
| `commit_mismatch` | warning | Jobs of one pipeline run different commits. | Point them at the same commit. |
| `primary_disabled` | warning | Only the redundancy is enabled. | Confirm it is on purpose. |
| `no_redundancy` | info | The pipeline has no redundancy job. | Consider adding one. |
| `sync_failed` | warning | A node failed the sync and no other run of the date covered it. | Ask for a re-sync of that node. |
| `sync_diverged` | warning | One node synced the run and another skipped it. | Check that the node that skipped has the files. |
| `published_again` | info | Dates synchronized more than once ("Every date..." means the redundancy always re-publishes). | Harmless if both runs produce the same data; otherwise the later run silently revises the data. |
| `skipped_unseen` | info | Skipped because of a sync that no run of this pipeline made. | Find the job with the same chain name, or the manual run. |
| `run_off_schedule` | info | The date was processed at a time the current cron does not produce. | Nothing to do if the cron change or the manual run was intended. |
| `completed_with_errors` | info | Complete, but the log shows source errors. | Check that nothing was skipped. |
| `duration_anomaly` | info | Under 20% or over 5x the job's median duration. | Read the log. |

## Failure causes

| Category | Typical log | Real or false positive? |
|---|---|---|
| `source_unavailable` | 5xx, timeouts, DNS | Usually transient. The probe tells. |
| `source_access_denied` | 401/403, Access Denied | If the probe works from here, the job node is blocked: IP, User-Agent policy or rate limit. If it fails everywhere, the access rules changed. |
| `source_tls` | certificate mismatch | The provider's certificate; it needs the provider or a host change. |
| `source_not_published` | 404, FTP 550, missing from an index | Often a holiday or a late publication; check the date. |
| `source_bad_content` | null bytes, empty page, bad zip or JSON | Often a bad edge or CDN response. It is intermittent if the probe works. |
| `missing_input` | missing directory or file, "no SPY constituents" | Look at the dependency's status for the same date. |
| `infra` | OOM, killed, no disk | Data team. |
| `code_error` | any other exception | A processor bug: reproduce with `QC_DATAFLEET_DEPLOYMENT_DATE`. |

## Content findings (`python run.py content`)

| Kind | Severity | Meaning | What to do |
|---|---|---|---|
| `content_too_old` | critical | Newest date is older than the directory's limit (cadence plus source lag). | Compare with the source's newest period; if the source has it, the processor is not writing it. |
| `content_stale` | critical | The newest date did not move across snapshots for several cadences. | Same as above. |
| `content_missing_dates` | warning | A weekday the directory always writes has no file. | Check the job for that date; a Manual Run fills it. |
| `content_thin_date` | warning | A date with under 25% of the rows around it. | Read that run's log; re-run the date. |
| `content_identity_collision` | warning | One ticker under two security IDs on one date. | Check the map files; usually a stale mapping. |
| `content_unsorted` | warning | Rows going back in time inside a file; LEAN skips them. | Fix the merge to sort, republish those files whole. |
| `content_dups_grew`, `content_bad_grew` | warning | Duplicate dates or malformed rows grew since the last snapshot. | Diff the worst files with their previous version. |
| `reconcile_mismatch` | critical | Published values differ from the source beyond its revision tolerance. | Re-download those dates and diff; a processor bug unless the source restated. |
| `reconcile_revision` | info | The source revised values after we published. | Expected for point-in-time data. |
