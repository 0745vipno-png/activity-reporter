#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Activity Reporter (one-shot audit CLI)

設計目標
- 一次性執行 (one-shot CLI)
- 只讀，不修改/刪除任何檔案
- 產出「人類可讀報告」(Markdown) + 「機器可讀」(JSON)
- 行為可回溯：append-only log
- 不自動判斷對錯，只提供可觀測事實（檔案系統 metadata）

重要注意
- ctime 在不同 OS 意義不同：
  - Windows: 通常是 Creation Time（建立時間）
  - Linux/Unix: 多半是 metadata change time（不是建立時間）
  因此本工具把 ctime 作為 "created_signal"（平台相依訊號）而非確定建立時間。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict, Any


# -----------------------------
# Data models
# -----------------------------

@dataclass(frozen=True)
class FileRecord:
    rel_path: str
    abs_path: str
    size: int
    mtime: float
    ctime: float


@dataclass(frozen=True)
class Event:
    rel_path: str
    size: int
    mtime: float
    ctime: float
    modified_in_window: bool
    created_signal_in_window: bool  # platform-dependent signal


@dataclass(frozen=True)
class Summary:
    run_id: str
    root: str
    window_start: str
    window_end: str
    include_ext: List[str]
    exclude_glob: List[str]
    follow_symlinks: bool
    top_n: int
    total_scanned: int
    total_records: int
    modified_count: int
    created_signal_count: int
    most_recent_modified: List[Event]
    largest_modified: List[Event]
    by_extension_modified: List[Tuple[str, int]]
    notes: List[str]


# -----------------------------
# Helpers: time parsing
# -----------------------------

def parse_since(value: str, now: dt.datetime) -> dt.datetime:
    """
    Parse --since value into an absolute datetime.

    Supported:
    - "3d" "48h" "30m" "15s"
    - ISO-ish: "2026-03-01T00:00" or "2026-03-01 00:00"
    - Date only: "2026-03-01" (interpreted as 00:00)
    """
    v = value.strip()

    # Relative format
    if len(v) >= 2 and v[-1].lower() in ("d", "h", "m", "s"):
        unit = v[-1].lower()
        num_str = v[:-1].strip()
        if not num_str.isdigit():
            raise ValueError(f"Invalid --since relative value: {value}")
        num = int(num_str)
        if unit == "d":
            return now - dt.timedelta(days=num)
        if unit == "h":
            return now - dt.timedelta(hours=num)
        if unit == "m":
            return now - dt.timedelta(minutes=num)
        if unit == "s":
            return now - dt.timedelta(seconds=num)

    # Absolute datetime
    # Accept "YYYY-MM-DDTHH:MM" / "YYYY-MM-DD HH:MM" / "YYYY-MM-DD"
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(v, fmt)
            return parsed
        except ValueError:
            pass

    raise ValueError(f"Unsupported --since format: {value}")


def fmt_dt_local(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fmt_ts_local(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# Helpers: logging (append-only)
# -----------------------------

def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = fmt_dt_local(dt.datetime.now())
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


# -----------------------------
# Helpers: filters
# -----------------------------

def normalize_ext_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    norm = []
    for p in parts:
        if not p:
            continue
        if not p.startswith("."):
            p = "." + p
        norm.append(p.lower())
    # de-dup keep order
    out = []
    seen = set()
    for e in norm:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def normalize_glob_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    # de-dup keep order
    out = []
    seen = set()
    for g in parts:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def should_exclude(rel_path: str, exclude_globs: List[str]) -> bool:
    # Apply patterns against rel_path and also each path segment
    if not exclude_globs:
        return False
    rel_norm = rel_path.replace("\\", "/")
    segments = rel_norm.split("/")
    for pat in exclude_globs:
        pat_norm = pat.replace("\\", "/")
        if fnmatch.fnmatch(rel_norm, pat_norm):
            return True
        for seg in segments:
            if fnmatch.fnmatch(seg, pat_norm):
                return True
    return False


def ext_of(rel_path: str) -> str:
    return Path(rel_path).suffix.lower()


# -----------------------------
# Scanner
# -----------------------------

def scan_records(
    root: Path,
    include_ext: List[str],
    exclude_glob: List[str],
    follow_symlinks: bool,
) -> Tuple[List[FileRecord], int]:
    """
    Walk filesystem and collect FileRecord for files that pass filters.

    Returns:
      (records, total_scanned_files)
      - total_scanned_files counts files encountered (before include_ext / exclude filtering)
      - records counts files that pass filters and successfully stat'd
    """
    records: List[FileRecord] = []
    total_scanned = 0

    # Use os.walk for speed and control, but keep rel_path via Path
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=follow_symlinks):
        # optionally exclude directories early
        # (dirnames is mutable; pruning reduces cost)
        pruned = []
        for d in list(dirnames):
            rel_dir = os.path.relpath(os.path.join(dirpath, d), str(root))
            rel_dir = rel_dir.replace("\\", "/")
            if should_exclude(rel_dir, exclude_glob):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for name in filenames:
            total_scanned += 1
            abs_path = Path(dirpath) / name
            try:
                rel_path = abs_path.relative_to(root).as_posix()
            except Exception:
                # fallback
                rel_path = os.path.relpath(str(abs_path), str(root)).replace("\\", "/")

            if should_exclude(rel_path, exclude_glob):
                continue

            if include_ext:
                if ext_of(rel_path) not in include_ext:
                    continue

            try:
                st = abs_path.stat()
            except (OSError, PermissionError):
                # skip unreadable entries; do not fail whole run
                continue

            records.append(
                FileRecord(
                    rel_path=rel_path,
                    abs_path=str(abs_path),
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    ctime=float(st.st_ctime),
                )
            )

    return records, total_scanned


# -----------------------------
# Event Builder & Aggregator
# -----------------------------

def build_events(records: List[FileRecord], window_start_ts: float, window_end_ts: float) -> List[Event]:
    events: List[Event] = []
    for r in records:
        modified = (r.mtime >= window_start_ts) and (r.mtime <= window_end_ts)
        created_signal = (r.ctime >= window_start_ts) and (r.ctime <= window_end_ts)
        events.append(
            Event(
                rel_path=r.rel_path,
                size=r.size,
                mtime=r.mtime,
                ctime=r.ctime,
                modified_in_window=modified,
                created_signal_in_window=created_signal,
            )
        )
    return events


def aggregate(
    run_id: str,
    root: Path,
    window_start: dt.datetime,
    window_end: dt.datetime,
    include_ext: List[str],
    exclude_glob: List[str],
    follow_symlinks: bool,
    top_n: int,
    total_scanned: int,
    records: List[FileRecord],
    events: List[Event],
) -> Summary:
    window_start_ts = window_start.timestamp()
    window_end_ts = window_end.timestamp()

    modified_events = [e for e in events if e.modified_in_window]
    created_signal_events = [e for e in events if e.created_signal_in_window]

    most_recent_modified = sorted(modified_events, key=lambda e: e.mtime, reverse=True)[:top_n]
    largest_modified = sorted(modified_events, key=lambda e: e.size, reverse=True)[:top_n]

    # by extension counts for modified events
    counts: Dict[str, int] = {}
    for e in modified_events:
        ex = ext_of(e.rel_path) or "(no_ext)"
        counts[ex] = counts.get(ex, 0) + 1
    by_ext_sorted = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)

    notes = []
    notes.append("This tool is read-only and does not modify or delete user data.")
    notes.append(
        "ctime is platform-dependent (Windows often creation time; Unix often metadata change time). "
        "It is provided only as a signal, not a guaranteed 'created time'."
    )
    notes.append("No automatic judgement is performed; the report is evidence for human review.")

    return Summary(
        run_id=run_id,
        root=str(root),
        window_start=fmt_dt_local(window_start),
        window_end=fmt_dt_local(window_end),
        include_ext=include_ext,
        exclude_glob=exclude_glob,
        follow_symlinks=follow_symlinks,
        top_n=top_n,
        total_scanned=total_scanned,
        total_records=len(records),
        modified_count=len(modified_events),
        created_signal_count=len(created_signal_events),
        most_recent_modified=most_recent_modified,
        largest_modified=largest_modified,
        by_extension_modified=by_ext_sorted,
        notes=notes,
    )


# -----------------------------
# Renderers
# -----------------------------

def render_md(summary: Summary, os_info: str, py_info: str) -> str:
    def ev_line(e: Event, kind: str) -> str:
        return f"- `{e.rel_path}` | size={e.size} | mtime={fmt_ts_local(e.mtime)} | ctime={fmt_ts_local(e.ctime)} | {kind}"

    lines: List[str] = []
    lines.append(f"# Activity Report")
    lines.append("")
    lines.append(f"- **run_id**: `{summary.run_id}`")
    lines.append(f"- **root**: `{summary.root}`")
    lines.append(f"- **window**: `{summary.window_start}` → `{summary.window_end}`")
    lines.append(f"- **filters**: include_ext={summary.include_ext or 'ALL'} | exclude_glob={summary.exclude_glob or 'NONE'}")
    lines.append(f"- **follow_symlinks**: `{summary.follow_symlinks}`")
    lines.append(f"- **environment**: `{os_info}` | `{py_info}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- total_scanned_files: **{summary.total_scanned}**")
    lines.append(f"- total_records_kept (after filters + stat ok): **{summary.total_records}**")
    lines.append(f"- modified_in_window: **{summary.modified_count}**")
    lines.append(f"- created_signal_in_window (platform-dependent): **{summary.created_signal_count}**")
    lines.append("")

    lines.append(f"## Most recently modified (Top {summary.top_n})")
    lines.append("")
    if summary.most_recent_modified:
        for e in summary.most_recent_modified:
            lines.append(ev_line(e, "modified_in_window=true"))
    else:
        lines.append("_No modified files in the window._")
    lines.append("")

    lines.append(f"## Largest modified files (Top {summary.top_n})")
    lines.append("")
    if summary.largest_modified:
        for e in summary.largest_modified:
            lines.append(ev_line(e, "modified_in_window=true"))
    else:
        lines.append("_No modified files in the window._")
    lines.append("")

    lines.append("## Modified files by extension")
    lines.append("")
    if summary.by_extension_modified:
        lines.append("| extension | modified_count |")
        lines.append("|---|---:|")
        for ex, c in summary.by_extension_modified:
            lines.append(f"| `{ex}` | {c} |")
    else:
        lines.append("_No modified files in the window._")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for n in summary.notes:
        lines.append(f"- {n}")
    lines.append("")

    return "\n".join(lines)


def summary_to_json_dict(summary: Summary, os_info: str, py_info: str) -> Dict[str, Any]:
    def ev(e: Event) -> Dict[str, Any]:
        return {
            "rel_path": e.rel_path,
            "size": e.size,
            "mtime": e.mtime,
            "ctime": e.ctime,
            "mtime_local": fmt_ts_local(e.mtime),
            "ctime_local": fmt_ts_local(e.ctime),
            "modified_in_window": e.modified_in_window,
            "created_signal_in_window": e.created_signal_in_window,
        }

    return {
        "run_id": summary.run_id,
        "root": summary.root,
        "window_start_local": summary.window_start,
        "window_end_local": summary.window_end,
        "include_ext": summary.include_ext,
        "exclude_glob": summary.exclude_glob,
        "follow_symlinks": summary.follow_symlinks,
        "top_n": summary.top_n,
        "environment": {"os": os_info, "python": py_info},
        "counts": {
            "total_scanned_files": summary.total_scanned,
            "total_records_kept": summary.total_records,
            "modified_in_window": summary.modified_count,
            "created_signal_in_window": summary.created_signal_count,
        },
        "most_recent_modified": [ev(e) for e in summary.most_recent_modified],
        "largest_modified": [ev(e) for e in summary.largest_modified],
        "by_extension_modified": [{"extension": ex, "modified_count": c} for ex, c in summary.by_extension_modified],
        "notes": summary.notes,
    }


# -----------------------------
# Run ID & output paths
# -----------------------------

def make_run_id(root: Path, window_start: dt.datetime, window_end: dt.datetime) -> str:
    basis = f"{root.resolve()}|{window_start.isoformat()}|{window_end.isoformat()}|{dt.datetime.now().isoformat()}"
    h = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{h}"


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# -----------------------------
# CLI
# -----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="activity_report",
        formatter_class=argparse.RawTextHelpFormatter,
        description=textwrap.dedent(
            """
            Activity Reporter - one-shot audit CLI

            Examples:
              python activity_report.py --path . --since 3d
              python activity_report.py --path C:\\work --since 48h --exclude-glob .git,node_modules,__pycache__
              python activity_report.py --path . --since 2026-03-01T00:00 --include-ext .py,.md --top 30
            """
        ).strip(),
    )
    p.add_argument("--path", required=True, help="Root directory to scan.")
    p.add_argument("--since", required=True, help="Time window start (relative: 3d/48h/30m or absolute: YYYY-MM-DD[THH:MM]).")
    p.add_argument("--include-ext", default="", help="Comma-separated extensions to include (e.g. .py,.md). Empty = all.")
    p.add_argument(
    "--exclude-glob",
    default=".git,node_modules,__pycache__,.venv,venv,reports",
    help="Comma-separated glob patterns to exclude."
)
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks during scan (default: False).")
    p.add_argument("--top", type=int, default=20, help="Top N items in report sections.")
    p.add_argument("--out-dir", default="reports", help="Directory to write reports/logs (default: ./reports).")
    p.add_argument("--format", choices=["md", "json", "both"], default="both", help="Output format.")
    p.add_argument("--full", action="store_true", help="Include a full modified list section in Markdown (may be large).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    now = dt.datetime.now()
    root = Path(args.path).expanduser()

    if not root.exists() or not root.is_dir():
        print(f"[ERROR] --path is not a directory: {root}", file=sys.stderr)
        return 2

    try:
        window_start = parse_since(args.since, now)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    window_end = now
    include_ext = normalize_ext_list(args.include_ext)
    exclude_glob = normalize_glob_list(args.exclude_glob)
    follow_symlinks = bool(args.follow_symlinks)
    top_n = max(1, int(args.top))

    out_dir = Path(args.out_dir).expanduser()
    safe_mkdir(out_dir)

    log_path = out_dir / "activity_reporter.log"

    os_info = f"{platform.system()} {platform.release()} ({platform.version()})"
    py_info = f"Python {sys.version.split()[0]}"

    run_id = make_run_id(root, window_start, window_end)
    md_path = out_dir / f"activity_report_{run_id}.md"
    json_path = out_dir / f"activity_report_{run_id}.json"

    # Log start
    append_log(
        log_path,
        f"START run_id={run_id} root={root.resolve()} since={args.since} window_start={fmt_dt_local(window_start)} "
        f"window_end={fmt_dt_local(window_end)} include_ext={include_ext or 'ALL'} exclude_glob={exclude_glob or 'NONE'} "
        f"follow_symlinks={follow_symlinks} top={top_n} format={args.format}",
    )

    # Scan
    records, total_scanned = scan_records(
        root=root,
        include_ext=include_ext,
        exclude_glob=exclude_glob,
        follow_symlinks=follow_symlinks,
    )

    events = build_events(records, window_start.timestamp(), window_end.timestamp())
    summary = aggregate(
        run_id=run_id,
        root=root,
        window_start=window_start,
        window_end=window_end,
        include_ext=include_ext,
        exclude_glob=exclude_glob,
        follow_symlinks=follow_symlinks,
        top_n=top_n,
        total_scanned=total_scanned,
        records=records,
        events=events,
    )

    # Render + write
    wrote = []

    if args.format in ("md", "both"):
        md = render_md(summary, os_info=os_info, py_info=py_info)

        if args.full:
            # Add full list section (append)
            modified_all = sorted([e for e in events if e.modified_in_window], key=lambda e: e.mtime, reverse=True)
            md += "\n## All modified files (full list)\n\n"
            if modified_all:
                for e in modified_all:
                    md += f"- `{e.rel_path}` | size={e.size} | mtime={fmt_ts_local(e.mtime)} | ctime={fmt_ts_local(e.ctime)}\n"
            else:
                md += "_No modified files in the window._\n"

        md_path.write_text(md, encoding="utf-8")
        wrote.append(str(md_path))

    if args.format in ("json", "both"):
        payload = summary_to_json_dict(summary, os_info=os_info, py_info=py_info)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        wrote.append(str(json_path))

    # Log end
    append_log(
        log_path,
        f"END   run_id={run_id} scanned={summary.total_scanned} kept={summary.total_records} "
        f"modified={summary.modified_count} created_signal={summary.created_signal_count} outputs={wrote}",
    )

    # Stdout summary (human-friendly)
    print(f"[OK] run_id: {run_id}")
    print(f"[OK] root: {root.resolve()}")
    print(f"[OK] window: {summary.window_start} -> {summary.window_end}")
    print(f"[OK] scanned: {summary.total_scanned} | kept: {summary.total_records}")
    print(f"[OK] modified_in_window: {summary.modified_count}")
    print(f"[OK] created_signal_in_window (platform-dependent): {summary.created_signal_count}")
    for p in wrote:
        print(f"[OK] wrote: {p}")
    print(f"[OK] log: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())