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
import datetime as dt
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

    def date_dir(self) -> str:
        """Date directory the hook files a new session under (today, local time)."""
        return dt.datetime.now().astimezone().date().isoformat()

    def state(self) -> dict:
        path = self.root / "state" / "sessions" / f"{SID}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write_state(self, record: dict) -> None:
        path = self.root / "state" / "sessions" / f"{SID}.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def set_spool_date(self, date: str) -> None:
        """Backdate the pending turn, as an older build would have recorded it."""
        for path in (self.root / "state" / "prompts" / SID).glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["date"] = date
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def date_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / "archive").iterdir() if p.is_dir())

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
                "未归档（interrupted_pending_question，仅记账）")


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


def o_rename_moves_new_files_to_the_new_folder(check: Checker, box: Sandbox) -> None:
    check.section("O. a /rename names the folder for everything written afterwards")
    first = [user("我们先建立工程，从这个工程复制出去", 1),
             assistant("好，先建立工程。", 2)]
    box.write_transcript(first)
    box.fire(box.prompt("我们先建立工程，从这个工程复制出去"))
    box.fire(box.stop("好，先建立工程。"))
    old_folder = box.folder()
    check.check("first folder is named after the prompt",
                old_folder is not None and "我们先建立工程" in old_folder.name, True)

    # the user renames the session, then keeps working
    stale = old_folder / "_等待回答.md"
    stale.write_text("stale signal", encoding="utf-8")
    box.append_transcript({"type": "custom-title", "customTitle": "FPGA设计推进-核心-2",
                           "sessionId": SID, "id": "ct1", "timestamp": 2000})
    box.append_transcript({"type": "message", "role": "user",
                           "content": [{"type": "input_text",
                                        "text": "<local-command-stdout>Session renamed to: FPGA设计推进-核心-2</local-command-stdout>"}],
                           "sessionId": SID, "id": "rn1", "timestamp": 2001})
    box.append_transcript(assistant("改名后的第一条回答。", 3))
    box.fire(box.prompt("继续"))
    box.fire(box.stop("改名后的第一条回答。"))

    folders = sorted(p.name for p in (box.root / "archive" / box.date_dir()).iterdir()
                     if p.is_dir())
    check.check("a new folder carries the new name",
                [f for f in folders if f.startswith("FPGA设计推进-核心-2")] != [], True)
    check.check("the old folder is left alone",
                [f for f in folders if f.startswith("我们先建立工程")] != [], True)
    new_md = sorted((box.root / "archive" / box.date_dir()
                     / [f for f in folders if f.startswith("FPGA设计推进")][0] / "阅读层").iterdir())
    check.check("the new turn landed in the new folder", len(new_md), 1)
    check.check("its frontmatter records the new name",
                'archive_title: "FPGA设计推进-核心-2"' in new_md[0].read_text(encoding="utf-8"), True)
    state = json.loads((box.root / "state" / "sessions" / f"{SID}.json").read_text(encoding="utf-8"))
    check.check("state keeps the original as initial_title",
                state.get("initial_title"), "我们先建立工程，从这个工程复制出去")
    check.check("title_source", state.get("title_source"), "custom_title")
    check.check("no signal left behind in the old folder", stale.exists(), False)


def p_rename_of_a_forked_ancestor_is_ignored(check: Checker, box: Sandbox) -> None:
    check.section("P. an ancestor's rename cannot title a forked session")
    box.write_transcript([
        user("我们先建立工程", 1), assistant("好。", 2),
        # the ancestor's entries, which a fork carries along in its log
        {"type": "custom-title", "customTitle": "祖先会话的名字",
         "sessionId": "01a0a2ea-9f5b-7f88-b050-387bb18611bd", "id": "ct0", "timestamp": 1000},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text",
                      "text": "<local-command-stdout>Session renamed to: 祖先会话的名字</local-command-stdout>"}],
         "sessionId": "01a0a2ea-9f5b-7f88-b050-387bb18611bd", "id": "rn0", "timestamp": 1001}])
    box.fire(box.prompt("我们先建立工程"))
    box.fire(box.stop("好。"))
    folders = sorted(p.name for p in (box.root / "archive" / box.date_dir()).iterdir())
    check.check("folder keeps the prompt name",
                [f for f in folders if "祖先会话的名字" in f], [])
    check.check("and uses the prompt title", [f for f in folders if "我们先建立工程" in f] != [], True)


def q_session_date_is_pinned(check: Checker, box: Sandbox) -> None:
    check.section("Q. a session is filed under the day it first appeared")
    box.write_transcript([user("跨天会话", 1), assistant("第一天的回答。", 2)])
    box.fire(box.prompt("跨天会话"))
    box.fire(box.stop("第一天的回答。"))
    check.check("record stores created_date", box.state().get("created_date"), box.date_dir())
    check.check("one date directory", box.date_dirs(), [box.date_dir()])

    # The same session is continued on a later day: the turn's own date is later,
    # but the pinned folder keeps the session in one place.
    box.write_transcript([user("跨天会话", 1), assistant("第一天的回答。", 2),
                          assistant("第二天早上的回答。", 3)])
    box.fire(box.prompt("第二天"))
    box.set_spool_date("2099-01-01")
    box.fire(box.stop("第二天早上的回答。"))
    check.check("still one date directory", box.date_dirs(), [box.date_dir()])

    # A record that predates the pin keeps the per-turn behaviour, so an existing
    # session's new files are not silently moved to another day.
    record = box.state()
    record.pop("created_date", None)
    box.write_state(record)
    box.write_transcript([user("跨天会话", 1), assistant("第一天的回答。", 2),
                          assistant("第三天早上的回答。", 4)])
    box.fire(box.prompt("第三天"))
    box.set_spool_date("2099-01-02")
    box.fire(box.stop("第三天早上的回答。"))
    check.check("legacy record follows the turn date",
                box.date_dirs(), sorted([box.date_dir(), "2099-01-02"]))


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


def l_stop_event_wins_over_a_lagging_log(check: Checker, box: Sandbox) -> None:
    check.section("L. a lagging session log cannot truncate the answer")
    # Real case: the log still showed the mid-turn note ("先确认两处细节") when the
    # Stop hook read it, while the actual answer (a 7.5 KB checklist) arrived a beat
    # later — the archive recorded 184 bytes of narration as the final answer.
    # The Stop event carries the CLI's own final output, so it must win.
    full_answer = "最后两块也确认了。现在给你完整清单。\n\n" + "| 表 | 作用 |\n| --- | --- |\n" * 20
    box.write_transcript([
        user("配置转发要配哪些东西？", 1),
        assistant("这个问题正好把前面所有核实串成一份清单。先确认最后两处细节，再给清单。", 2)])
    box.fire(box.prompt("配置转发要配哪些东西？"))
    box.fire(box.event("Stop", stop_hook_active=False, last_assistant_message=full_answer))
    check.check("archive holds the event's full answer",
                full_answer in box.read("阅读层/" + box.layer("阅读层")[0]), True)
    recorded = int([l.split(":")[1] for l in box.read("阅读层/" + box.layer("阅读层")[0]).splitlines()
                    if l.startswith("assistant_answer_bytes")][0])
    check.check("recorded answer length", recorded, len(full_answer.encode()))
    check.check("mid-turn note is not the answer",
                "先确认最后两处细节" in box.read("阅读层/" + box.layer("阅读层")[0]), False)


def m_user_interruption_is_not_an_answer(check: Checker, box: Sandbox) -> None:
    check.section("M. a turn cut off by the user is not archived as an answer")
    # CodeBuddy still fires Stop when the user interrupts, and the turn's last text
    # is the 19-byte placeholder "Interrupted by user": it used to be archived as
    # the final answer of the turn.
    box.write_transcript([
        user("帮我查一下", 1),
        assistant("你的直觉值得认真查。我去 FPGA 代码里找谁发出配置事务。", 2),
        assistant("Interrupted by user", 3, status="incomplete")])
    box.fire(box.prompt("帮我查一下"))
    rc, out, _ = box.fire(box.event("Stop", stop_hook_active=False,
                                    last_assistant_message="Interrupted by user"))
    check.check("exit code", rc, 0)
    check.check("no archive document", box.layer("阅读层"), [])
    check.check("ledger status", box.ledger()[-1]["turn_status"], "interrupted_by_user")
    check.check("ledger explains the skip", box.ledger()[-1]["archive"],
                "未归档（interrupted_by_user，仅记账）")
    check.check("Stop message still reports the turn",
                out["systemMessage"].splitlines()[0].startswith("■ 本轮结束"), True)


def n_no_double_processing(check: Checker, box: Sandbox) -> None:
    check.section("N. a prompt landing right after the Stop does not redo the turn")
    box.write_transcript([
        user("第一轮", 1),
        assistant("结论：一切正常。", 2, {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})])
    box.fire(box.prompt("第一轮"))
    turn = box.ledger()[-1]["turn_id"] if box.ledger() else ""
    box.fire(box.stop("结论：一切正常。"))
    check.check("archived by the real Stop", len(box.layer("阅读层")), 1)
    recorded = box.read("阅读层/" + box.layer("阅读层")[0])
    check.check("marked as a real completion", "recovered: false" in recorded, True)
    # the session record still names the turn, as happens when the next prompt
    # arrives in the same instant as the Stop
    state = json.loads((box.root / "state" / "sessions" / f"{SID}.json").read_text(encoding="utf-8"))
    state["current_turn_id"] = turn
    (box.root / "state" / "sessions" / f"{SID}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    box.fire(box.prompt("第二轮"))
    check.check("no duplicate archive", len(box.layer("阅读层")), 1)
    check.check("still marked as a real completion",
                "recovered: false" in box.read("阅读层/" + box.layer("阅读层")[0]), True)
    check.check("no duplicate ledger line",
                [r["turn_status"] for r in box.ledger()], ["completed"])


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
    ("l_stop_event_wins_over_a_lagging_log", l_stop_event_wins_over_a_lagging_log),
    ("m_user_interruption_is_not_an_answer", m_user_interruption_is_not_an_answer),
    ("n_no_double_processing", n_no_double_processing),
    ("o_rename_moves_new_files_to_the_new_folder", o_rename_moves_new_files_to_the_new_folder),
    ("p_rename_of_a_forked_ancestor_is_ignored", p_rename_of_a_forked_ancestor_is_ignored),
    ("q_session_date_is_pinned", q_session_date_is_pinned),
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
