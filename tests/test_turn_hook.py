#!/usr/bin/env python3
"""Regression suite for hooks/codebuddy_turn_hook.py.

Pure standard library, no pytest, no network, no writes outside a temporary
directory: every case runs the hook as a subprocess with
CODEBUDDY_CONVERSATION_ARCHIVE_ROOT / _STATE / CODEBUDDY_USAGE_ROOT pointing at a
private sandbox, then asserts on the files it produced.

Covered behaviour:

* a finished turn archives the reading layer exactly once (a real Stop), and
  never before it;
* an interrupted/superseded turn is ledger-only — no half-finished Markdown ever
  reaches the archive root;
* the "waiting on you" signals: ``_等待回答.md`` / ``_等待批准.md`` /
  ``_等待输入.md`` appear on the right trigger, carry the question or plan text,
  are mutually exclusive, and are retired the moment the human responds;
* hostile input (bad JSON, unknown session, missing transcript) always exits 0.

Usage:  python3 tests/test_turn_hook.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = Path(os.environ.get("CODEBUDDY_TURN_HOOK", REPO / "hooks" / "codebuddy_turn_hook.py"))
SID = "sess-01a0a305-7c09"
Q = json.dumps(
    {"questions": [
        {"question": "要接 SessionEnd 吗？", "header": "SessionEnd", "multiSelect": False,
         "options": [{"label": "接上", "description": "补归档"},
                     {"label": "不接", "description": "保持简洁"}]},
        {"question": "idle 噪音呢？", "header": "idle", "multiSelect": False,
         "options": [{"label": "压低"}]}]},
    ensure_ascii=False)


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.transcript = root / "transcript.jsonl"
        self.env = dict(
            os.environ,
            CODEBUDDY_CONVERSATION_ARCHIVE_ROOT=str(root / "archive"),
            CODEBUDDY_CONVERSATION_ARCHIVE_STATE=str(root / "state"),
            CODEBUDDY_USAGE_ROOT=str(root / "usage"),
        )
        for name in ("archive", "state", "usage"):
            (root / name).mkdir(parents=True, exist_ok=True)

    # -- hook invocation ---------------------------------------------------- #
    def fire(self, event: dict) -> tuple[int, dict | None, str]:
        proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                              capture_output=True, text=True, env=self.env)
        out = proc.stdout.strip()
        return proc.returncode, (json.loads(out) if out else None), proc.stderr.strip()

    def event(self, name: str, **kw) -> dict:
        base = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": "/home/codex",
                "permission_mode": "bypassPermissions", "hook_event_name": name}
        base.update(kw)
        return base

    def prompt(self, text: str) -> dict:
        return self.event("UserPromptSubmit", prompt=text)

    def stop(self, answer: str = "好，方案如下。") -> dict:
        return self.event("Stop", stop_hook_active=False, last_assistant_message=answer)

    def notification(self, notification_type: str, message: str = "") -> dict:
        return self.event("Notification", message=message, notification_type=notification_type)

    def permission_prompt(self, tool: str) -> dict:
        return self.notification("permission_prompt", f"needs your permission to use {tool}")

    def idle_prompt(self) -> dict:
        return self.notification("idle_prompt", "CodeBuddy is waiting for your input")

    def pre_tool_use(self, tool: str, tool_input: dict | None = None) -> dict:
        return self.event("PreToolUse", tool_name=tool, tool_input=tool_input or {}, call_id="call_x")

    # -- transcript --------------------------------------------------------- #
    def write_transcript(self, rows: list[dict]) -> None:
        self.transcript.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def append_transcript(self, row: dict) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- assertions helpers ------------------------------------------------- #
    def folder(self) -> Path | None:
        found = sorted(self.root.glob("archive/*/*"))
        return found[0] if found else None

    def signals(self) -> list[str]:
        folder = self.folder()
        return sorted(p.name for p in folder.iterdir() if p.name.startswith("_")) if folder else []

    def layer(self, name: str) -> list[str]:
        folder = self.folder()
        target = folder / name if folder else None
        return sorted(p.name for p in target.iterdir()) if target and target.exists() else []

    def read_signal(self, filename: str) -> str:
        folder = self.folder()
        assert folder is not None, "archive folder was never created"
        return (folder / filename).read_text(encoding="utf-8")

    def ledger(self) -> list[dict]:
        path = self.root / "usage" / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# transcript entry builders (shapes copied from a real session JSONL)
# --------------------------------------------------------------------------- #
def user(text: str, i: int) -> dict:
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}], "id": f"u{i}"}


def assistant(text: str, i: int, usage: dict | None = None) -> dict:
    entry = {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}], "id": f"a{i}"}
    if usage:
        entry["providerData"] = {"model": "m", "messageId": f"m{i}", "rawUsage": usage}
    return entry


def function_call(name: str, call_id: str, arguments: str, i: int) -> dict:
    return {"type": "function_call", "name": name, "callId": call_id,
            "arguments": arguments, "id": f"c{i}", "timestamp": 1000 + i}


def function_call_result(name: str, call_id: str, i: int) -> dict:
    return {"type": "function_call_result", "name": name, "callId": call_id, "status": "completed",
            "output": {"type": "text", "text": "ok"}, "id": f"r{i}", "timestamp": 1000 + i}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print()
        print("=" * 22, title)

    def check(self, label: str, got, want) -> None:
        if got == want:
            self.passed += 1
            print(f"   ok   {label}: {got!r}")
        else:
            self.failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"   FAIL {label}: got {got!r}, want {want!r}")


def run(sandbox: Sandbox, check: Checker) -> None:
    fire, ev = sandbox.fire, sandbox.event

    check.section("A. pending question is signalled immediately, without an archive doc")
    sandbox.write_transcript([
        user("把提问纳入 hook", 1),
        assistant("我先问两个问题。", 2,
                  {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}),
        function_call("AskUserQuestion", "call_00_abc", Q, 3)])
    fire(sandbox.prompt("把提问纳入 hook"))
    rc, _, err = fire(sandbox.idle_prompt())
    check.check("exit code", rc, 0)
    check.check("stderr", err, "")
    check.check("signal file", sandbox.signals(), ["_等待回答.md"])
    doc = sandbox.read_signal("_等待回答.md")
    check.check("carries the question", "要接 SessionEnd 吗？" in doc, True)
    check.check("carries the options", ("接上" in doc and "保持简洁" in doc), True)
    check.check("frontmatter kind",
                [l for l in doc.splitlines() if l.startswith("signal:")],
                ["signal: pending_question"])
    check.check("no archive document yet", sandbox.layer("阅读层"), [])

    check.section("B. a finished turn archives once, and only then")
    sandbox.write_transcript([
        user("把提问纳入 hook", 1), assistant("我先问两个问题。", 2),
        function_call("AskUserQuestion", "call_00_abc", Q, 3),
        function_call_result("AskUserQuestion", "call_00_abc", 4),
        assistant("好，方案如下。", 5,
                  {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220})])
    fire(sandbox.stop("好，方案如下。"))
    check.check("signals cleared", sandbox.signals(), [])
    check.check("one archive document", len(sandbox.layer("阅读层")), 1)
    check.check("archive holds the final answer",
                "好，方案如下。" in sandbox.read_signal("阅读层/" + sandbox.layer("阅读层")[0]), True)
    check.check("archive document has no turn_status",
                [l for l in sandbox.read_signal("阅读层/" + sandbox.layer("阅读层")[0]).splitlines()
                 if l.startswith("turn_status")], [])
    fire(sandbox.idle_prompt())
    check.check("idle after the turn writes nothing", sandbox.signals(), [])
    check.check("audit layer mirrored", len(sandbox.layer("审计层")), 1)

    check.section("C. in-progress turn gone quiet -> generic signal")
    sandbox.write_transcript([
        user("跑个长任务", 1),
        assistant("开始。", 2, {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330})])
    fire(sandbox.prompt("跑个长任务"))
    fire(sandbox.idle_prompt())
    check.check("signal file", sandbox.signals(), ["_等待输入.md"])
    check.check("explains the turn is still open",
                "本轮尚未结束" in sandbox.read_signal("_等待输入.md"), True)
    check.check("still no new archive document", len(sandbox.layer("阅读层")), 1)

    check.section("D. question abandoned, superseded by the next prompt -> ledger only")
    before = len(sandbox.layer("阅读层"))
    sandbox.write_transcript([user("长任务", 1), assistant("先问一句。", 2),
                              function_call("AskUserQuestion", "call_00_xyz", Q, 3)])
    fire(sandbox.prompt("算了，换话题"))
    check.check("no new reading-layer document", len(sandbox.layer("阅读层")), before)
    check.check("signals cleared", sandbox.signals(), [])
    check.check("ledger statuses", [r.get("turn_status") for r in sandbox.ledger()],
                ["completed", "interrupted_pending_question"])
    check.check("superseded turn not archived", sandbox.ledger()[-1]["archive"],
                "未归档（回合未真正结束：interrupted_pending_question，仅记账）")

    check.section("E. superseded without a question -> superseded_catchup, still no document")
    before = len(sandbox.layer("阅读层"))
    sandbox.write_transcript([user("普通长任务", 1), assistant("干活中。", 2)])
    fire(sandbox.prompt("打断"))
    check.check("no new reading-layer document", len(sandbox.layer("阅读层")), before)
    check.check("ledger status", sandbox.ledger()[-1]["turn_status"], "superseded_catchup")

    check.section("F. an abandoned question from an earlier turn cannot leak into the next one")
    sandbox.write_transcript([
        user("长任务", 1), assistant("先问一句。", 2),
        function_call("AskUserQuestion", "call_00_old", Q, 3),
        function_call_result("AskUserQuestion", "call_00_old", 4),
        user("新的提问轮", 5), function_call("Bash", "call_00_bash", "{}", 6)])
    fire(sandbox.prompt("新的提问轮"))
    fire(sandbox.idle_prompt())
    check.check("scope is the current turn only", sandbox.signals(), ["_等待输入.md"])

    check.section("G. permission_prompt naming AskUserQuestion is the zero-latency trigger")
    sandbox.write_transcript([user("提问轮", 1), assistant("先问一句。", 2)])
    fire(sandbox.prompt("提问轮"))
    rc, _, err = fire(sandbox.permission_prompt("AskUserQuestion"))
    check.check("exit code / stderr", (rc, err), (0, ""))
    check.check("signal published immediately", sandbox.signals(), ["_等待回答.md"])
    check.check("baseline version admits the text is missing",
                "问题正文尚未落盘" in sandbox.read_signal("_等待回答.md"), True)
    check.check("no archive document", len(sandbox.layer("阅读层")), 1)

    # the tool call reaches the transcript a fraction of a second later
    sandbox.write_transcript([user("提问轮2", 3), assistant("再问一句。", 4)])
    fire(sandbox.prompt("提问轮2"))
    threading.Thread(target=lambda: (time.sleep(0.4),
                                     sandbox.append_transcript(
                                         function_call("AskUserQuestion", "call_00_late", Q, 9))),
                     daemon=True).start()
    fire(sandbox.permission_prompt("AskUserQuestion"))
    enriched = sandbox.read_signal("_等待回答.md")
    check.check("enriched with the question text", "要接 SessionEnd 吗？" in enriched, True)
    check.check("enriched with the options", ("接上" in enriched and "保持简洁" in enriched), True)
    check.check("placeholder replaced", "问题正文尚未落盘" in enriched, False)
    check.check("raw tool input kept", '"multiSelect": false' in enriched, True)

    sandbox.write_transcript([user("普通轮", 5), assistant("干活。", 6)])
    fire(sandbox.prompt("普通轮"))
    fire(sandbox.permission_prompt("Bash"))
    check.check("an auto-approvable tool never signals", sandbox.signals(), [])
    rc, _, err = fire({**sandbox.permission_prompt("Bash"), "message": "something else entirely"})
    check.check("unparsable message exit code / stderr", (rc, err), (0, ""))
    check.check("unparsable message signals nothing", sandbox.signals(), [])

    check.section("H. answering retires the signal")
    sandbox.write_transcript([user("提问轮3", 7), assistant("问一句。", 8),
                              function_call("AskUserQuestion", "call_00_q", Q, 9)])
    fire(sandbox.prompt("提问轮3"))
    fire(sandbox.permission_prompt("AskUserQuestion"))
    check.check("signal while pending", sandbox.signals(), ["_等待回答.md"])
    rc, _, _ = fire(sandbox.pre_tool_use("AskUserQuestion", json.loads(Q)))
    check.check("exit code", rc, 0)
    check.check("signal retired after answering", sandbox.signals(), [])
    rc, _, _ = fire(sandbox.pre_tool_use("Bash", {"command": "ls"}))
    check.check("other tools leave signals alone", (rc, sandbox.signals()), (0, []))

    check.section("I. ExitPlanMode approval panels")
    plan_input = {"plan": "## 计划\n1. 改 hook\n2. 跑测试"}
    sandbox.write_transcript([user("做个功能", 11), assistant("先出个计划。", 12)])
    fire(sandbox.prompt("做个功能"))
    rc, _, err = fire(sandbox.permission_prompt("ExitPlanMode"))
    check.check("exit code / stderr", (rc, err), (0, ""))
    check.check("approval signal published", sandbox.signals(), ["_等待批准.md"])
    check.check("baseline placeholder", "问题正文尚未落盘" in sandbox.read_signal("_等待批准.md"), True)
    check.check("frontmatter kind",
                [l for l in sandbox.read_signal("_等待批准.md").splitlines()
                 if l.startswith("signal:")], ["signal: plan_approval"])
    threading.Thread(target=lambda: (time.sleep(0.4),
                                     sandbox.append_transcript(
                                         function_call("ExitPlanMode", "call_00_plan",
                                                       json.dumps(plan_input, ensure_ascii=False), 13))),
                     daemon=True).start()
    fire(sandbox.permission_prompt("ExitPlanMode"))
    enriched = sandbox.read_signal("_等待批准.md")
    check.check("enriched with the plan", "1. 改 hook" in enriched and "2. 跑测试" in enriched, True)
    check.check("placeholder replaced", "问题正文尚未落盘" in enriched, False)
    fire(sandbox.pre_tool_use("ExitPlanMode", plan_input))
    check.check("signal retired after approval", sandbox.signals(), [])
    fire(sandbox.permission_prompt("Bash"))
    check.check("Bash approval still signals nothing", sandbox.signals(), [])

    sandbox.write_transcript([user("两段式", 21), assistant("问。", 22),
                              function_call("AskUserQuestion", "call_00_q2", Q, 23)])
    fire(sandbox.prompt("两段式"))
    fire(sandbox.permission_prompt("AskUserQuestion"))
    check.check("question signal first", sandbox.signals(), ["_等待回答.md"])
    fire(sandbox.permission_prompt("ExitPlanMode"))
    check.check("signals are mutually exclusive", sandbox.signals(), ["_等待批准.md"])

    check.section("J. hostile input never blocks the conversation")
    check.check("non-idle notification exit code", fire(sandbox.notification("permission_prompt"))[0], 0)
    check.check("broken JSON exit code",
                subprocess.run([sys.executable, str(HOOK)], input="{oops",
                               capture_output=True, text=True, env=sandbox.env).returncode, 0)
    check.check("unknown session exit code",
                fire({**sandbox.idle_prompt(), "session_id": "nobody"})[0], 0)
    check.check("missing transcript exit code",
                fire({**sandbox.idle_prompt(), "transcript_path": "/nonexistent.jsonl"})[0], 0)
    check.check("Stop without a pending turn invents nothing",
                fire(sandbox.stop())[1]["systemMessage"].splitlines()[0].startswith("■"), True)
    errors = sandbox.root / "state" / "errors.log"
    check.check("errors.log", errors.read_text(encoding="utf-8") if errors.exists() else "无", "无")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    options = parser.parse_args()

    if not HOOK.is_file():
        print(f"hook not found: {HOOK}", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="turn-hook-test-"))
    check = Checker()
    try:
        print(f"hook    : {HOOK}")
        print(f"sandbox : {root}")
        run(Sandbox(root), check)
    finally:
        if options.keep:
            print(f"\nsandbox kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    print()
    if check.failures:
        print(f"❌ {len(check.failures)} of {check.passed + len(check.failures)} checks failed")
        for failure in check.failures:
            print("  -", failure)
        return 1
    print(f"✅ all {check.passed} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
