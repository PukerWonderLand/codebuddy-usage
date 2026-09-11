#!/usr/bin/env python3
"""codebuddy-usage - inspect CodeBuddy token usage recorded by the turn hook.

The hook writes one JSON object per completed turn to
``<usage_root>/usage.jsonl`` and mirrors the newest one to ``latest-turn.json``.

Commands:
  codebuddy-usage            show the most recent turn (same as 'latest')
  codebuddy-usage latest     show the most recent turn
  codebuddy-usage summary    aggregate all recorded turns
  codebuddy-usage json       raw JSONL records
  codebuddy-usage help       this message
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402


LEDGER = config.usage_root()


def fmt(value: int | float) -> str:
    return f"{int(value):,}"


def credit(value: int | float) -> str:
    return f"{float(value) / 1000.0:.3f}"


def load_records() -> list[dict]:
    records: list[dict] = []
    path = LEDGER / "usage.jsonl"
    if not path.is_file():
        return records
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
                records.append(item)
    return records


def show_latest() -> int:
    record = None
    latest = LEDGER / "latest-turn.json"
    if latest.is_file():
        try:
            record = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = None
    if record is None:
        records = load_records()
        record = records[-1] if records else None
    if record is None:
        print("暂无用量记录。完成一轮对话后 hook 会自动写入。")
        return 1

    turn = record.get("turn", {})
    cumulative = record.get("cumulative", {})
    print(f"时间        : {record.get('ts', '')}")
    print(f"会话        : {record.get('session_id', '')}  轮次 {record.get('turn_id', '')}")
    if record.get("model"):
        print(f"模型        : {record['model']}")
    if record.get("cwd"):
        print(f"目录        : {record['cwd']}")
    print()
    print("本轮用量")
    print(f"  输入      : {fmt(turn.get('input', 0))}  "
          f"(缓存命中 {fmt(turn.get('cache_hit', 0))} / 未命中 {fmt(turn.get('cache_miss', 0))})")
    print(f"  缓存命中率: {record.get('turn_cache_hit_rate', 0):.1f}%")
    print(f"  输出      : {fmt(turn.get('output', 0))}  (推理 {fmt(turn.get('reasoning', 0))})")
    print(f"  合计      : {fmt(turn.get('total', 0))} tokens")
    print(f"  计费      : {credit(turn.get('credit', 0))}")
    print()
    print("会话累计")
    print(f"  输入      : {fmt(cumulative.get('input', 0))}  "
          f"(缓存命中率 {record.get('cumulative_cache_hit_rate', 0):.1f}%)")
    print(f"  输出      : {fmt(cumulative.get('output', 0))}  (推理 {fmt(cumulative.get('reasoning', 0))})")
    print(f"  合计      : {fmt(cumulative.get('total', 0))} tokens")
    print(f"  请求次数  : {fmt(cumulative.get('requests', 0))}")
    print(f"  计费      : {credit(cumulative.get('credit', 0))}")
    print()
    print(f"归档        : {record.get('archive', '')}")
    return 0


def show_summary() -> int:
    records = load_records()
    if not records:
        print("暂无用量记录。完成一轮对话后 hook 会自动写入。")
        return 1

    totals = {"input": 0, "cache_hit": 0, "cache_miss": 0, "output": 0,
              "reasoning": 0, "total": 0, "credit": 0}
    today = dt.datetime.now().astimezone().date().isoformat()
    today_totals = dict(totals)
    today_turns = 0
    sessions: dict[str, dict] = {}

    for record in records:
        turn = record.get("turn", {})
        for key in totals:
            totals[key] += int(turn.get(key, 0) or 0)
        sid = str(record.get("session_id", "?"))
        bucket = sessions.setdefault(sid, {"total": 0, "turns": 0, "credit": 0})
        bucket["total"] += int(turn.get("total", 0) or 0)
        bucket["credit"] += int(turn.get("credit", 0) or 0)
        bucket["turns"] += 1
        if str(record.get("ts", "")).startswith(today):
            today_turns += 1
            for key in totals:
                today_totals[key] += int(turn.get(key, 0) or 0)

    hit = totals["cache_hit"]
    denom = hit + totals["cache_miss"]

    print(f"记录轮次    : {len(records)}   会话数: {len(sessions)}")
    print()
    print("全部")
    print(f"  输入      : {fmt(totals['input'])}  (缓存命中 {fmt(hit)} / 未命中 {fmt(totals['cache_miss'])})")
    print(f"  缓存命中率: {(hit * 100 / denom) if denom else 0:.1f}%")
    print(f"  输出      : {fmt(totals['output'])}  (推理 {fmt(totals['reasoning'])})")
    print(f"  合计      : {fmt(totals['total'])} tokens")
    print(f"  计费      : {credit(totals['credit'])}")
    print()
    print(f"今天 ({today})   轮次: {today_turns}")
    print(f"  输入      : {fmt(today_totals['input'])}   输出: {fmt(today_totals['output'])}"
          f"   合计: {fmt(today_totals['total'])} tokens   计费: {credit(today_totals['credit'])}")
    print()
    print("按会话 (Top 10, 按合计排序)")
    ranked = sorted(sessions.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]
    for sid, info in ranked:
        print(f"  {sid[:36]:<36}  轮次 {info['turns']:>3}  合计 {fmt(info['total']):>12}  "
              f"计费 {credit(info['credit'])}")
    return 0


def show_json() -> int:
    path = LEDGER / "usage.jsonl"
    if not path.is_file():
        print("暂无用量记录。")
        return 1
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    command = (sys.argv[1] if len(sys.argv) > 1 else "latest").lower()
    if command in ("latest", "last", "turn", "-d", "--latest"):
        return show_latest()
    if command in ("summary", "sum", "-s", "--summary"):
        return show_summary()
    if command in ("json", "-j", "--json"):
        return show_json()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
