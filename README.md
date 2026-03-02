# Activity Reporter

A one-shot, read-only CLI tool that audits file system activity and produces reproducible evidence reports.

This tool scans a directory, analyzes file metadata within a given time window,
and generates human-readable and machine-readable reports without modifying any data.

---

## Design Philosophy

Activity Reporter follows a strict audit-oriented design:

- One-shot execution (no background service)
- Read-only (no modification or deletion of user files)
- Human-readable output (Markdown report)
- Machine-readable output (JSON)
- Append-only execution log
- No automatic judgment or decision-making

The tool provides observable facts.  
Interpretation remains the responsibility of the user.

---

## What It Does

Given a directory and a time window, Activity Reporter:

- Scans file metadata (size, mtime, ctime)
- Identifies files modified within the time window
- Provides a platform-dependent creation signal (ctime)
- Generates:
  - Markdown report
  - JSON report
  - Append-only execution log

---

## Important Notes About Time Fields

File system metadata differs by operating system:

- **Windows**: `ctime` usually represents creation time.
- **Linux/Unix**: `ctime` usually represents metadata change time (NOT creation time).

For this reason, `ctime` is treated as a *signal*, not definitive proof of file creation.

---

## Installation

No external dependencies required.

Requires:

- Python 3.8+

Clone the repository:

```bash
git clone https://github.com/0745vipno-png/activity-reporter.git
cd activity-reporter

---------------------------------------------------------------------------------------------------------------

Usage

Basic example:

python activity_report.py --path . --since 3d

Scan last 48 hours:

python activity_report.py --path . --since 48h

Specify file extensions:

python activity_report.py --path . --since 7d --include-ext .py,.md

Exclude directories:

python activity_report.py --path . --since 7d --exclude-glob .git,node_modules,reports

Increase top results:

python activity_report.py --path . --since 7d --top 30
Output

Reports are written to:

./reports/

Each execution produces:

activity_report_<run_id>.md

activity_report_<run_id>.json

activity_reporter.log (append-only)

The report includes:

Execution metadata

Scan window

Summary statistics

Most recently modified files

Largest modified files

Extension-based modification counts

Example Output Summary
[OK] run_id: 20260302_085610_xxxxx
[OK] scanned: 7 | kept: 7
[OK] modified_in_window: 6
[OK] created_signal_in_window: 7
Non-Goals

This tool intentionally does NOT:

Delete files

Modify files

Monitor continuously

Detect anomalies automatically

Determine whether a change is "good" or "bad"

It generates evidence — not conclusions.