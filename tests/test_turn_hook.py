#!/usr/bin/env python3
"""Regression suite for hooks/codebuddy_turn_hook.py.

Pure standard library, no pytest, no network, no writes outside a temporary
directory: every case runs the hook as a subprocess with
CODEBUDDY_CONVERSATION_ARCHIVE_ROOT / _STATE / CODEBUDDY_USAGE_ROOT pointing at a
private sandbox, then asserts on the files it produced. Each scenario gets its
own sandbox so nothing leaks between them.

Covered behaviour:

* a finished turn archives the reading layer exactly once (a real Stop), and
  never before it;
* a turn that finished without ever getting a Stop is repaired from the log at
  the next prompt (`recovered: true`), while one interrupted mid-flight — a call
  in flight, a stream cut off by the user, a turn ending on a tool result — is
  never archived as if it were an answer;
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
PLAN = {"plan": "## 计划\n1. 改 hook\n2. 跑测试"}


# --------------------------------------------------------------------------- #
# sandbox + transcript builders (shapes copied from a real session JSONL)
# --------------------------------------------------------------------------- #
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

    # -- inspection --------------------------------------------------------- #
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

    def read(self, relative: str) -> str:
        folder = self.folder()
        assert folder is not None, "archive folder was never created"
        return (folder / relative).read_text(encoding="utf-8")

    def ledger(self) -> list[dict]:
        path = self.root / "usage" / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def errors(self) -> str:
        path = self.root / "state" / "errors.log"
        return path.read_text(encoding="utf-8") if path.exists() else "无"


def user(text: str, i: int) -> dict:
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}], "id": f"u{i}"}


def assistant(text: str, i: int, usage: dict | None = None, status: str = "completed") -> dict:
    entry = {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}], "id": f"a{i}",
             "status": status}
    if usage:
        entry["providerData"] = {"model": "m", "messageId": f"m{i}", "rawUsage": usage}
    return entry


def call(name: str, call_id: str, arguments: str, i: int) -> dict:
    return {"type": "function_call", "name": name, "callId": call_id,
            "arguments": arguments, "id": f"c{i}", "timestamp": 1000 + i}


def result(name: str, call_id: str, i: int, status: str = "completed") -> dict:
    return {"type": "function_call_result", "name": name, "callId": call_id, "status": status,
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


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def a_pending_question(check: Checker, box: Sandbox) -> None:
    check.section("A. a pending question is signalled, without archiving anything")
    box.write_transcript([
        user("把提问纳入 hook", 1),
        assistant("我先问两个问题。", 2,
                  {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}),
        call("AskUserQuestion", "call_00_abc", Q, 3)])
    box.fire(box.prompt("把提问纳入 hook"))
    rc, _, err = box.fire(box.idle_prompt())
    check.check("exit code", rc, 0)
    check.check("stderr", err, "")
    check.check("signal file", box.signals(), ["_等待回答.md"])
    doc = box.read("_等待回答.md")
    check.check("carries the question", "要接 SessionEnd 吗？" in doc, True)
    check.check("carries the options", ("接上" in doc and "保持简洁" in doc), True)
    check.check("frontmatter kind",
                [l for l in doc.splitlines() if l.startswith("signal:")],
                ["signal: pending_question"])
    check.check("no archive document yet", box.layer("阅读层"), [])


def b_finished_turn(check: Checker, box: Sandbox) -> None:
    check.section("B. a finished turn archives once, and only then")
    box.write_transcript([
        user("把提问纳入 hook", 1), assistant("我先问两个问题。", 2),
        call("AskUserQuestion", "call_00_abc", Q, 3),
        result("AskUserQuestion", "call_00_abc", 4),
        assistant("好，方案如下。", 5,
                  {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220})])
    box.fire(box.prompt("把提问纳入 hook"))
    box.fire(box.permission_prompt("AskUserQuestion"))
    check.check("signal while pending", box.signals(), ["_等待回答.md"])
    rc, out, _ = box.fire(box.stop("好，方案如下。"))
    check.check("exit code", rc, 0)
    check.check("Stop reports the turn", out["systemMessage"].splitlines()[0].startswith("■ 本轮结束"), True)
    check.check("signals cleared", box.signals(), [])
    check.check("one archive document", len(box.layer("阅读层")), 1)
    doc = box.read("阅读层/" + box.layer("阅读层")[0])
    check.check("holds the final answer", "好，方案如下。" in doc, True)
    check.check("marked not recovered", "recovered: false" in doc, True)
    check.check("no recovery callout", "Stop` hook 没有触发" in doc, False)
    box.fire(box.idle_prompt())
    check.check("idle after the turn writes nothing", box.signals(), [])
    check.check("audit layer mirrored", len(box.layer("审计层")), 1)


def c_idle_signal(check: Checker, box: Sandbox) -> None:
    check.section("C. an in-progress turn gone quiet -> generic signal")
    box.write_transcript([
        user("跑个长任务", 1),
        assistant("开始跑。", 2, {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330}),
        call("Bash", "call_00_long", "{}", 3)])
    box.fire(box.prompt("跑个长任务"))
    box.fire(box.idle_prompt())
    check.check("signal file", box.signals(), ["_等待输入.md"])
    check.check("explains the turn is still open", "本轮尚未结束" in box.read("_等待输入.md"), True)
    check.check("no archive document", box.layer("阅读层"), [])


def d_pending_question_superseded(check: Checker, box: Sandbox) -> None:
    check.section("D. a question left unanswered is superseded, not archived")
    box.write_transcript([user("长任务", 1), assistant("先问一句。", 2),
                          call("AskUserQuestion", "call_00_xyz", Q, 3)])
    box.fire(box.prompt("长任务"))
    box.fire(box.permission_prompt("AskUserQuestion"))
    box.fire(box.prompt("算了，换话题"))
    check.check("no archive document", box.layer("阅读层"), [])
    check.check("signals cleared", box.signals(), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "interrupted_pending_question")
    check.check("ledger explains itself", box.ledger()[-1]["archive"],
                "未归档（回合未真正结束：interrupted_pending_question，仅记账）")


def e_cut_off_stream(check: Checker, box: Sandbox) -> None:
    check.section("E. a stream the user cut off is not recovered")
    box.write_transcript([user("普通长任务", 1),
                          assistant("Interrupted by user", 2, status="incomplete")])
    box.fire(box.prompt("普通长任务"))
    box.fire(box.prompt("打断"))
    check.check("no archive document", box.layer("阅读层"), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "superseded_catchup")


def f_turn_scope(check: Checker, box: Sandbox) -> None:
    check.section("F. an abandoned question from an earlier turn cannot leak forward")
    box.write_transcript([
        user("长任务", 1), assistant("先问一句。", 2),
        call("AskUserQuestion", "call_00_old", Q, 3),
        result("AskUserQuestion", "call_00_old", 4),
        user("新的提问轮", 5), call("Bash", "call_00_bash", "{}", 6)])
    box.fire(box.prompt("长任务"))
    box.fire(box.prompt("新的提问轮"))
    box.fire(box.idle_prompt())
    check.check("scope is the current turn only", box.signals(), ["_等待输入.md"])


def g_permission_prompt(check: Checker, box: Sandbox) -> None:
    check.section("G. permission_prompt naming AskUserQuestion is the zero-latency trigger")
    box.write_transcript([user("提问轮", 1), assistant("先问一句。", 2)])
    box.fire(box.prompt("提问轮"))
    rc, _, err = box.fire(box.permission_prompt("AskUserQuestion"))
    check.check("exit code / stderr", (rc, err), (0, ""))
    check.check("signal published immediately", box.signals(), ["_等待回答.md"])
    check.check("baseline version admits the text is missing",
                "问题正文尚未落盘" in box.read("_等待回答.md"), True)
    check.check("no archive document", box.layer("阅读层"), [])

    # the tool call reaches the transcript a fraction of a second later
    threading.Thread(target=lambda: (time.sleep(0.4),
                                     box.append_transcript(call("AskUserQuestion", "call_00_late", Q, 9))),
                     daemon=True).start()
    box.fire(box.permission_prompt("AskUserQuestion"))
    enriched = box.read("_等待回答.md")
    check.check("enriched with the question text", "要接 SessionEnd 吗？" in enriched, True)
    check.check("enriched with the options", ("接上" in enriched and "保持简洁" in enriched), True)
    check.check("placeholder replaced", "问题正文尚未落盘" in enriched, False)
    check.check("raw tool input kept", '"multiSelect": false' in enriched, True)

    box.fire(box.pre_tool_use("AskUserQuestion", json.loads(Q)))
    check.check("signal retired after the answer", box.signals(), [])
    box.fire(box.permission_prompt("Bash"))
    check.check("an auto-approvable tool never signals", box.signals(), [])
    rc, _, err = box.fire({**box.permission_prompt("Bash"), "message": "something else entirely"})
    check.check("unparsable message exit code / stderr", (rc, err), (0, ""))
    check.check("unparsable message signals nothing", box.signals(), [])


def h_retire_on_answer(check: Checker, box: Sandbox) -> None:
    check.section("H. answering retires the signal")
    box.write_transcript([user("提问轮3", 7), assistant("问一句。", 8),
                          call("AskUserQuestion", "call_00_q", Q, 9)])
    box.fire(box.prompt("提问轮3"))
    box.fire(box.permission_prompt("AskUserQuestion"))
    check.check("signal while pending", box.signals(), ["_等待回答.md"])
    rc, _, _ = box.fire(box.pre_tool_use("AskUserQuestion", json.loads(Q)))
    check.check("exit code", rc, 0)
    check.check("signal retired after answering", box.signals(), [])
    rc, _, _ = box.fire(box.pre_tool_use("Bash", {"command": "ls"}))
    check.check("other tools leave signals alone", (rc, box.signals()), (0, []))


def i_plan_approval(check: Checker, box: Sandbox) -> None:
    check.section("I. ExitPlanMode approval panels")
    box.write_transcript([user("做个功能", 11), assistant("先出个计划。", 12)])
    box.fire(box.prompt("做个功能"))
    rc, _, err = box.fire(box.permission_prompt("ExitPlanMode"))
    check.check("exit code / stderr", (rc, err), (0, ""))
    check.check("approval signal published", box.signals(), ["_等待批准.md"])
    check.check("baseline placeholder", "问题正文尚未落盘" in box.read("_等待批准.md"), True)
    check.check("frontmatter kind",
                [l for l in box.read("_等待批准.md").splitlines() if l.startswith("signal:")],
                ["signal: plan_approval"])
    threading.Thread(target=lambda: (time.sleep(0.4),
                                     box.append_transcript(call(
                                         "ExitPlanMode", "call_00_plan",
                                         json.dumps(PLAN, ensure_ascii=False), 13))),
                     daemon=True).start()
    box.fire(box.permission_prompt("ExitPlanMode"))
    enriched = box.read("_等待批准.md")
    check.check("enriched with the plan", "1. 改 hook" in enriched and "2. 跑测试" in enriched, True)
    check.check("placeholder replaced", "问题正文尚未落盘" in enriched, False)
    box.fire(box.pre_tool_use("ExitPlanMode", PLAN))
    check.check("signal retired after approval", box.signals(), [])

    box.fire(box.permission_prompt("AskUserQuestion"))
    check.check("question signal first", box.signals(), ["_等待回答.md"])
    box.fire(box.permission_prompt("ExitPlanMode"))
    check.check("signals are mutually exclusive", box.signals(), ["_等待批准.md"])


def j_recovery(check: Checker, box: Sandbox) -> None:
    check.section("J. a finished turn whose Stop was lost is recovered from the log")
    box.write_transcript([
        user("正常跑完但没收到 Stop", 31),
        assistant("先看一下。", 32),
        call("Bash", "call_00_ok", "{}", 33),
        result("Bash", "call_00_ok", 34),
        assistant("结论：一切正常。", 35,
                  {"prompt_tokens": 400, "completion_tokens": 40, "total_tokens": 440})])
    box.fire(box.prompt("正常跑完但没收到 Stop"))
    box.fire(box.prompt("下一条提示词"))
    check.check("reading layer has one document", len(box.layer("阅读层")), 1)
    doc = box.read("阅读层/" + box.layer("阅读层")[0])
    check.check("marked recovered", "recovered: true" in doc, True)
    check.check("explains the missing Stop", "Stop` hook 没有触发" in doc, True)
    check.check("holds the final answer", "结论：一切正常。" in doc, True)
    check.check("ledger status", box.ledger()[-1]["turn_status"], "recovered")
    check.check("ledger records the archive", box.ledger()[-1]["archive"].startswith("已归档"), True)


def j2_no_false_recovery(check: Checker, box: Sandbox) -> None:
    check.section("J2. unfinished turns are never recovered")
    # a call still in flight (process killed, or the user interrupted a tool)
    box.write_transcript([user("被打断的回合", 41), assistant("先跑一下。", 42),
                          call("Bash", "call_00_killed", "{}", 43)])
    box.fire(box.prompt("被打断的回合"))
    box.fire(box.prompt("打断"))
    check.check("in-flight call is not archived", box.layer("阅读层"), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "superseded_catchup")

    # ends on a tool result, with no answer after it
    box.write_transcript([user("停在工具结果上", 51), assistant("查一下。", 52),
                          call("Bash", "call_00_tail", "{}", 53),
                          result("Bash", "call_00_tail", 54)])
    box.fire(box.prompt("停在工具结果上"))
    box.fire(box.prompt("再来"))
    check.check("turn ending on a result is not archived", box.layer("阅读层"), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "superseded_catchup")

    # ends on thinking, with no answer after it
    box.write_transcript([user("停在思考上", 61), assistant("想一下。", 62),
                          {"type": "reasoning", "text": "…", "id": "rs1"}])
    box.fire(box.prompt("停在思考上"))
    box.fire(box.prompt("继续"))
    check.check("turn ending on reasoning is not archived", box.layer("阅读层"), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "superseded_catchup")


def j3_recovery_tolerates_metadata(check: Checker, box: Sandbox) -> None:
    check.section("J3. metadata entries after the answer do not hide a finished turn")
    box.write_transcript([
        user("带元数据尾巴", 71),
        assistant("完成了。", 72, {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}),
        {"type": "ai-title", "title": "x", "id": "t1"},
        {"type": "file-history-snapshot", "id": "f1"}])
    box.fire(box.prompt("带元数据尾巴"))
    box.fire(box.prompt("继续"))
    check.check("recovered despite the metadata tail", len(box.layer("阅读层")), 1)
    check.check("ledger status", box.ledger()[-1]["turn_status"], "recovered")


def k_hostile(check: Checker, box: Sandbox) -> None:
    check.section("K. hostile input never blocks the conversation")
    check.check("non-idle notification exit code", box.fire(box.notification("permission_prompt"))[0], 0)
    check.check("broken JSON exit code",
                subprocess.run([sys.executable, str(HOOK)], input="{oops",
                               capture_output=True, text=True, env=box.env).returncode, 0)
    check.check("unknown session exit code",
                box.fire({**box.idle_prompt(), "session_id": "nobody"})[0], 0)
    check.check("missing transcript exit code",
                box.fire({**box.idle_prompt(), "transcript_path": "/nonexistent.jsonl"})[0], 0)
    rc, out, _ = box.fire(box.stop())
    check.check("Stop without a pending turn stays silent", (rc, out), (0, None))
    check.check("and invents no archive", box.layer("阅读层"), [])
    check.check("errors.log", box.errors(), "无")


SCENARIOS = (
    ("a_pending_question", a_pending_question),
    ("b_finished_turn", b_finished_turn),
    ("c_idle_signal", c_idle_signal),
    ("d_pending_question_superseded", d_pending_question_superseded),
    ("e_cut_off_stream", e_cut_off_stream),
    ("f_turn_scope", f_turn_scope),
    ("g_permission_prompt", g_permission_prompt),
    ("h_retire_on_answer", h_retire_on_answer),
    ("i_plan_approval", i_plan_approval),
    ("j_recovery", j_recovery),
    ("j2_no_false_recovery", j2_no_false_recovery),
    ("j3_recovery_tolerates_metadata", j3_recovery_tolerates_metadata),
    ("k_hostile", k_hostile),
)


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
        for name, scenario in SCENARIOS:
            scenario(check, Sandbox(root / name))
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
