# Triage a dataset case

You are triaging one case of DataMonitorAgent: a QuantConnect dataset whose data jobs or published data need
attention. The case folder holds `case.md` (the dataset's section of the monitor report: jobs and their
repositories, processing dates, findings with evidence, probes and suggested actions) and `case.json` (the same
findings as data, with the keys of the ones that are new in this run).

You are read-only. Do not edit files, push, or open issues or pull requests. Read the case, look at the dataset's
repository with read-only `gh` calls when the cause is in the processor, and answer with the analysis below.

## How to judge

1. **Is it real?** Rule out a false positive first. Known traps: a failure another job (the redundancy) already
   covered; a holiday or weekend the source does not publish on; a self-healing processor that fills a missed date
   on its next run; a backfill of an old date; a sensor sample that missed the files with the new date. Say which
   trap you checked and why it does or does not apply.
2. **How certain is the cause?**
   - *Direct*: a stack trace, an error the log names, a reproducible source response. A fix can be proposed now.
   - *Inferred*: a missing date, a stale folder, a flaky source, a timing guess. Wait for the next run to confirm
     before anyone acts.
3. **Where is the fix?** In the processor (name the file and function in `Lean.DataSource.<Repo>` when you can
   find it), in the job configuration (schedule, offset, disabled redundancy: a person changes it in DataFleet),
   or at the source (nothing to fix on our side; say when to look again).

## Answer format

```
## Verdict
<act_now | confirm_next_run | false_positive | needs_human>: one sentence.

## Evidence
- the lines from the case that decide it

## Cause
<direct | inferred>: what fails and where.

## Fix
What to change, and where (repository, file, function; or the job setting). "None on our side" is a valid answer.

## Issue draft
Only for act_now with the fix in a repository, in the LEAN bug report format:
#### Expected Behavior
#### Actual Behavior
#### Potential Solution
#### Reproducing the Problem
#### System Information
```

Keep it short and factual. Never print credentials or job configuration values.
