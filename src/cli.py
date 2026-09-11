#!/usr/bin/env python3
"""codebuddy-dashboard - CLI for the CodeBuddy usage dashboard.

Commands:
  codebuddy-dashboard run [--host H] [--port P]   start the web dashboard
  codebuddy-dashboard summary                     print a text summary
  codebuddy-dashboard refresh                     re-scan logs and print summary
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collector  # noqa: E402
import server  # noqa: E402


def _fmt(v: int | float) -> str:
    return f"{int(v):,}"


def text_summary() -> int:
    index = collector.build_index(force=True)
    if not index["events"]:
        print("暂无用量数据（未找到会话日志）。")
        print(f"扫描目录: {collector.PROJECTS_ROOT}")
        return 1

    summary = collector.summarize(index["events"])
    totals = summary["totals"]
    print(f"扫描时间    : {index['generated_at']}")
    print(f"会话数      : {_fmt(totals['sessions'])}   请求数: {_fmt(totals['requests'])}")
    print(f"输入        : {_fmt(totals['input'])}  (缓存命中 {_fmt(totals['cached'])}"
          f" / 未命中 {_fmt(totals['cache_miss'])})")
    print(f"缓存命中率  : {totals['cache_hit_rate']}%")
    print(f"输出        : {_fmt(totals['output'])}  (推理 {_fmt(totals['reasoning'])})")
    print(f"合计        : {_fmt(totals['total'])} tokens")
    print(f"计费        : {totals['credit']:.3f} credit")
    print()
    print("按模型")
    for row in summary["by_model"]:
        print(f"  {row['name']:<28} 合计 {_fmt(row['total']):>12}  命中率 {row['cache_hit_rate']:>5}%"
              f"  计费 {row['credit']:.3f}")
    print()
    print("按项目")
    for row in summary["by_project"]:
        print(f"  {row['name']:<28} 合计 {_fmt(row['total']):>12}  命中率 {row['cache_hit_rate']:>5}%"
              f"  计费 {row['credit']:.3f}")
    print()
    print("最近会话 (Top 10)")
    for row in index["sessions"][:10]:
        title = (row.get("title") or "-")[:28]
        print(f"  {row['session_id'][:20]:<20} 轮次 {row['turns']:>3}  请求 {row['requests']:>3}"
              f"  合计 {_fmt(row['total']):>12}  命中率 {collector.hit_rate(row):>5}%  {title}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command in ("run", "serve", "start", "dashboard"):
        return server.main(argv[1:])
    if command in ("summary", "sum"):
        return text_summary()
    if command == "refresh":
        return text_summary()
    if command in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
