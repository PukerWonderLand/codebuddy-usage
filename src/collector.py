#!/usr/bin/env python3
"""Collect CodeBuddy token usage from local session logs.

Scans ``<projects_root>/<project>/<session>.jsonl`` and extracts one event per
model response, deduped by ``providerData.messageId`` (rawUsage is attached once
per API response but several JSONL entries can share it).

Parsing is cached in memory keyed by ``(path, mtime, size)`` so repeated API
calls only re-read files that actually changed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import config

PROJECTS_ROOT = config.projects_root()
USAGE_ROOT = config.usage_root()

# CodeBuddy injects reminders and slash-command echoes as role=user messages.
NON_PROMPT_PREFIXES = (
    "<system-reminder",
    "<command-name",
    "<command-message",
    "<local-command-stdout",
)

# path -> (mtime, size, parsed session)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _block_text(entry: dict[str, Any], types: set[str]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and block.get("type") in types:
            parts.append(text)
    return "\n".join(parts)


def _is_real_prompt(entry: dict[str, Any]) -> bool:
    text = _block_text(entry, {"input_text", "text"}).lstrip()
    if not text:
        return False
    return not text.startswith(NON_PROMPT_PREFIXES)


def _usage_from_raw(raw: dict[str, Any]) -> dict[str, int]:
    details = raw.get("prompt_tokens_details")
    cached = (
        raw.get("prompt_cache_hit_tokens")
        or (details.get("cached_tokens") if isinstance(details, dict) else 0)
        or raw.get("cached_tokens")
        or 0
    )
    completion_details = raw.get("completion_tokens_details")
    reasoning = (
        completion_details.get("reasoning_tokens")
        if isinstance(completion_details, dict)
        else 0
    )
    return {
        "input": _int(raw.get("prompt_tokens")),
        "cached": _int(cached),
        "cache_miss": _int(raw.get("prompt_cache_miss_tokens")),
        "output": _int(raw.get("completion_tokens")),
        "reasoning": _int(reasoning),
        "total": _int(raw.get("total_tokens")),
    }


def _parse_file(path: Path) -> dict[str, Any]:
    session_id = path.stem
    project = path.parent.name
    cwd = ""
    title = ""
    model = ""
    turn = 0
    seen: set[str] = set()
    events: list[dict[str, Any]] = []

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue

                if item.get("sessionId"):
                    session_id = str(item["sessionId"])
                if item.get("cwd"):
                    cwd = str(item["cwd"])
                if item.get("type") == "ai-title" and item.get("aiTitle"):
                    title = str(item["aiTitle"])
                if (
                    item.get("type") == "message"
                    and item.get("role") == "user"
                    and _is_real_prompt(item)
                ):
                    turn += 1

                provider = item.get("providerData")
                if not isinstance(provider, dict):
                    continue
                if provider.get("model"):
                    model = str(provider["model"])
                raw = provider.get("rawUsage")
                if not isinstance(raw, dict):
                    continue
                key = str(
                    provider.get("messageId")
                    or provider.get("conversationRequestId")
                    or ""
                )
                if key:
                    if key in seen:
                        continue
                    seen.add(key)

                ts = _int(item.get("timestamp"))
                usage = _usage_from_raw(raw)
                events.append(
                    {
                        "ts": ts,
                        "day": _day_of(ts),
                        "session_id": session_id,
                        "project": project,
                        "cwd": cwd,
                        "model": str(provider.get("model") or model),
                        "turn": turn if turn > 0 else 1,
                        "request_id": key,
                        "credit": round(_float(raw.get("credit")), 6),
                        **usage,
                    }
                )
    except OSError:
        pass

    if not model and events:
        model = events[-1]["model"]
    return {
        "session_id": session_id,
        "project": project,
        "cwd": cwd,
        "title": title,
        "model": model,
        "events": events,
    }


def _day_of(ts_ms: int) -> str:
    if ts_ms <= 0:
        return ""
    return dt.datetime.fromtimestamp(ts_ms / 1000.0).astimezone().date().isoformat()


def scan(force: bool = False) -> list[dict[str, Any]]:
    """Return parsed sessions, re-reading only changed files."""
    sessions: list[dict[str, Any]] = []
    if not PROJECTS_ROOT.is_dir():
        return sessions
    for path in sorted(PROJECTS_ROOT.glob("**/*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == 0:
            continue
        key = str(path)
        cached = _CACHE.get(key)
        if cached and not force and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            sessions.append(cached[2])
            continue
        parsed = _parse_file(path)
        _CACHE[key] = (stat.st_mtime, stat.st_size, parsed)
        sessions.append(parsed)
    return sessions


def build_index(force: bool = False) -> dict[str, Any]:
    sessions = scan(force=force)
    events: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        evs = session["events"]
        events.extend(evs)
        row = _session_row(session, evs)
        if row["requests"]:
            session_rows.append(row)
    events.sort(key=lambda e: e["ts"])
    session_rows.sort(key=lambda r: r["last_ts"] or 0, reverse=True)
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "events": events,
        "sessions": session_rows,
    }


def _session_row(session: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    totals = _empty_totals()
    for event in events:
        _accumulate(totals, event)
    first = events[0]["ts"] if events else 0
    last = events[-1]["ts"] if events else 0
    return {
        "session_id": session["session_id"],
        "project": session["project"],
        "cwd": session["cwd"],
        "title": session["title"],
        "model": session["model"],
        "first_ts": first,
        "last_ts": last,
        "turns": max((e["turn"] for e in events), default=0),
        **totals,
    }


def _empty_totals() -> dict[str, Any]:
    return {
        "requests": 0,
        "input": 0,
        "cached": 0,
        "cache_miss": 0,
        "output": 0,
        "reasoning": 0,
        "total": 0,
        "credit": 0.0,
    }


def _accumulate(totals: dict[str, Any], event: dict[str, Any]) -> None:
    totals["requests"] += 1
    for key in ("input", "cached", "cache_miss", "output", "reasoning", "total"):
        totals[key] += int(event.get(key, 0) or 0)
    totals["credit"] += float(event.get("credit", 0) or 0)


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals = _empty_totals()
    by_model: dict[str, dict[str, Any]] = {}
    by_project: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    sessions: set[str] = set()

    for event in events:
        _accumulate(totals, event)
        sessions.add(event["session_id"])
        for bucket, key in (
            (by_model, event.get("model") or "未知"),
            (by_project, event.get("project") or "未知"),
            (by_day, event.get("day") or "未知"),
        ):
            entry = bucket.setdefault(key, _empty_totals())
            _accumulate(entry, event)

    totals["sessions"] = len(sessions)
    totals["cache_hit_rate"] = hit_rate(totals)
    return {
        "totals": totals,
        "by_model": _sorted_rows(by_model, "total"),
        "by_project": _sorted_rows(by_project, "total"),
        "by_day": [{"day": day, **row} for day, row in sorted(by_day.items())],
    }


def _sorted_rows(bucket: dict[str, dict[str, Any]], key: str) -> list[dict[str, Any]]:
    rows = []
    for name, row in bucket.items():
        row = dict(row)
        row["name"] = name
        row["cache_hit_rate"] = hit_rate(row)
        rows.append(row)
    rows.sort(key=lambda r: r.get(key, 0), reverse=True)
    return rows


def hit_rate(row: dict[str, Any]) -> float:
    cached = int(row.get("cached", 0) or 0)
    miss = int(row.get("cache_miss", 0) or 0)
    if cached + miss <= 0:
        return 0.0
    return round(cached * 100.0 / (cached + miss), 1)


def filter_range(events: list[dict[str, Any]], range_key: str) -> list[dict[str, Any]]:
    if range_key in ("all", "", None):
        return events
    now = dt.datetime.now().astimezone()
    if range_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "7d":
        start = (now - dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "30d":
        start = (now - dt.timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        return events
    start_ms = int(start.timestamp() * 1000)
    return [e for e in events if e["ts"] >= start_ms]


def read_ledger(limit: int = 50) -> list[dict[str, Any]]:
    """Recent hook-ledger records (turn accounting + archive status)."""
    path = USAGE_ROOT / "usage.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows[-limit:][::-1]
