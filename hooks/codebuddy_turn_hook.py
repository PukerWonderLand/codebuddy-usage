#!/usr/bin/env python3
"""CodeBuddy turn hook: archive the turn as Markdown, account tokens, and signal
the moments when the CLI is blocked waiting on a human.

One deterministic, model-free script handles three CodeBuddy hook events:

* UserPromptSubmit -> spool the exact user prompt, snapshot cumulative token
  usage as the turn baseline, emit a "turn start" system message.
* Stop             -> combine the spooled prompt with the final assistant
  answer, write a verbatim Markdown document to the archive root (often an SMB
  share), mirror the raw session JSONL into an audit layer, append a token-usage
  ledger record, and emit a "turn end" system message. Only a FINISHED turn may
  write to the archive, and a catch-up call judges that from the log: a turn that
  finished but whose Stop was lost is repaired (`recovered: true`), while a turn
  superseded mid-flight is accounted but never archived, so a half-finished
  answer cannot land on the share.
* Notification     -> publish a "_等待回答.md" / "_等待输入.md" signal in the
  session folder while somebody is needed.

What actually fires when, measured on this machine (see the notes below, they
are not obvious from the docs):

* Stop hooks are SKIPPED while a question is pending — AskUserQuestion is
  delivered as an interruption, and interruptions abort the Stop path. So a
  pending question is invisible to UserPromptSubmit/Stop.
* ``permission_prompt`` fires the instant the AskUserQuestion panel opens, with
  the message "needs your permission to use AskUserQuestion". This is the only
  zero-latency trigger, and for a bypass-permissions session it does not fire for
  auto-approved tools.
* ``idle_prompt`` does NOT fire while a question is pending: the session is not
  considered idle. It only covers an in-progress turn that has gone quiet.
* PreToolUse for AskUserQuestion is useless here: it runs when the tool executes,
  i.e. after the human has already answered.

The question text itself is not in the notification, and the tool call reaches
the transcript a fraction of a second after the panel opens, so the signal is
published immediately and then upgraded once the transcript catches up.

Both signal files are transient: they exist exactly while the CLI waits on a
human, and are removed on the next UserPromptSubmit or Stop.

Every stage is isolated: a failed archive or a missing transcript never blocks
the conversation. Errors are appended to STATE_ROOT/errors.log.

Configuration (first match wins):
  1. environment variables below
  2. ``~/.codebuddy-usage/config.json`` keys archive_root / state_root / usage_root
  3. home-relative defaults (``~/codebuddy-archive`` and ``~/.codebuddy-*``)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any


def _load_config() -> dict:
    override = os.environ.get("CODEBUDDY_USAGE_CONFIG")
    path = Path(override).expanduser() if override else (
        Path.home() / ".codebuddy-usage" / "config.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_CONFIG = _load_config()


def _setting(key: str, env: str, default: Path) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    value = _CONFIG.get(key)
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return default


ARCHIVE_ROOT = _setting(
    "archive_root",
    "CODEBUDDY_CONVERSATION_ARCHIVE_ROOT",
    Path.home() / "codebuddy-archive",
)
STATE_ROOT = _setting(
    "state_root",
    "CODEBUDDY_CONVERSATION_ARCHIVE_STATE",
    Path.home() / ".codebuddy-turn-state",
)
USAGE_ROOT = _setting(
    "usage_root", "CODEBUDDY_USAGE_ROOT", Path.home() / ".codebuddy-usage"
)

# CodeBuddy injects reminders and slash-command echoes as role=user messages.
# They are not real user prompts and must never be archived as one.
NON_PROMPT_PREFIXES = (
    "<system-reminder",
    "<command-name",
    "<command-message",
    "<local-command-stdout",
)

# Transient "a human is needed right now" signals, written into the session
# folder next to 阅读层/审计层 so they surface on the Windows share as well.
PENDING_QUESTION_FILE = "_等待回答.md"
PLAN_APPROVAL_FILE = "_等待批准.md"
WAITING_INPUT_FILE = "_等待输入.md"
SIGNAL_FILES = (PENDING_QUESTION_FILE, PLAN_APPROVAL_FILE, WAITING_INPUT_FILE)

# The notification that announces a panel names the tool in its text:
# "needs your permission to use AskUserQuestion".
PERMISSION_MESSAGE_MARKER = "needs your permission to use "
ASK_USER_QUESTION_TOOL = "AskUserQuestion"

# Tools whose dialog always blocks until a human responds. Everything else can
# be auto-approved, so it must never raise a signal on its own.
SIGNAL_FILE_FOR_TOOL = {
    ASK_USER_QUESTION_TOOL: PENDING_QUESTION_FILE,
    "ExitPlanMode": PLAN_APPROVAL_FILE,
}

# The tool call lands in the transcript a fraction of a second after the panel
# opens, so the trigger publishes a bare signal and then waits this long before
# trying to fill in the question or plan text.
SIGNAL_ENRICH_DELAY = 1.2


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #
def safe_id(value: Any, fallback: str) -> str:
    text = str(value or fallback)
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("._")
    return (text or fallback)[:120]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def title_from_prompt(prompt: str) -> str:
    """Readable, deterministic folder title without a model call."""
    text = re.sub(r"```.*?```", " ", prompt, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s，。；！？]+", " ", text)
    text = re.sub(r"[#>*_~|\[\]{}()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"^(?:麻烦你|请你|请帮我|帮我|我想让你|我需要你|我需要知道|请分析|请说明|请解释)\s*",
        "",
        text,
    )
    if not text:
        return "未命名对话"
    sentence = re.split(r"[。！？!?；;\n]", text, maxsplit=1)[0].strip()
    candidate = sentence if len(sentence) >= 6 else text
    candidate = candidate[:36].strip(" ._-，。；：:！!？?")
    return candidate or "未命名对话"


def folder_slug(title: str) -> str:
    # Windows-invalid filename characters and controls removed: the archive is
    # a CIFS mount of the Windows E: drive.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value or "未命名对话")[:48]


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data)


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def exclusive_lock(handle: Any) -> None:
    """Best-effort cross-platform exclusive lock on an open binary file."""
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except (ImportError, OSError):
        pass


def append_line_locked(path: Path, line: str) -> None:
    """Append one line under a lock so concurrent sessions never interleave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / (path.name + ".lock")).open("a+b") as lock:
        exclusive_lock(lock)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line if line.endswith("\n") else line + "\n")


# --------------------------------------------------------------------------- #
# State paths
# --------------------------------------------------------------------------- #
def prompt_path(session_id: str, turn_id: str) -> Path:
    return STATE_ROOT / "prompts" / session_id / f"{turn_id}.json"


def session_path(session_id: str) -> Path:
    return STATE_ROOT / "sessions" / f"{session_id}.json"


def load_or_create_session(
    session_id: str, prompt: str, date: str, received_at: str
) -> dict[str, Any]:
    existing = load_json(session_path(session_id))
    if existing and existing.get("folder_name"):
        return existing
    title = title_from_prompt(prompt)
    record = {
        "session_id": session_id,
        "date": date,
        "received_at": received_at,
        "title": title,
        "folder_name": f"{folder_slug(title)}__{session_id[:8]}",
        "title_source": "first_archived_user_prompt",
        "reported_turns": [],
    }
    atomic_write_json(session_path(session_id), record)
    return record


# --------------------------------------------------------------------------- #
# Transcript parsing (CodeBuddy session JSONL)
# --------------------------------------------------------------------------- #
def iter_entries(transcript_path: Any) -> list[dict[str, Any]]:
    if not transcript_path:
        return []
    path = Path(str(transcript_path))
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
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
                    entries.append(item)
    except OSError:
        return []
    return entries


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


def user_prompt_text(entry: dict[str, Any]) -> str:
    text = _block_text(entry, {"input_text", "text"})
    stripped = text.lstrip()
    if not stripped:
        return ""
    if stripped.startswith(NON_PROMPT_PREFIXES):
        return ""
    return text


def assistant_text(entry: dict[str, Any]) -> str:
    return _block_text(entry, {"output_text", "text"})


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def usage_from_raw(raw: dict[str, Any]) -> dict[str, int]:
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
        "cache_hit": _int(cached),
        "cache_miss": _int(raw.get("prompt_cache_miss_tokens")),
        "output": _int(raw.get("completion_tokens")),
        "reasoning": _int(reasoning),
        "total": _int(raw.get("total_tokens")),
        "credit": _int(round(float(raw.get("credit") or 0) * 1000)),
        "requests": 1,
    }


def analyze_transcript(transcript_path: Any) -> dict[str, Any]:
    """Single pass over the session JSONL gathering everything the hook needs.

    * prompt   - last genuine user message (reminders/command echoes excluded)
    * answer   - last assistant message carrying output text after that prompt
    * model    - last model identifier seen on a provider response
    * cumulative - token totals, deduped by providerData.messageId because
      rawUsage is attached once per API response but several JSONL entries can
      share it.
    """
    totals = {
        "input": 0,
        "cache_hit": 0,
        "cache_miss": 0,
        "output": 0,
        "reasoning": 0,
        "total": 0,
        "credit": 0,
        "requests": 0,
    }
    seen: set[str] = set()
    model = ""
    last_user_idx = -1
    entries = iter_entries(transcript_path)
    for index, entry in enumerate(entries):
        provider = entry.get("providerData")
        if isinstance(provider, dict):
            if provider.get("model"):
                model = str(provider["model"])
            raw = provider.get("rawUsage")
            if isinstance(raw, dict):
                key = str(
                    provider.get("messageId")
                    or provider.get("conversationRequestId")
                    or ""
                )
                if not key or key not in seen:
                    if key:
                        seen.add(key)
                    for name, value in usage_from_raw(raw).items():
                        totals[name] += value
        if (
            entry.get("type") == "message"
            and entry.get("role") == "user"
            and user_prompt_text(entry)
        ):
            last_user_idx = index

    prompt = user_prompt_text(entries[last_user_idx]) if last_user_idx >= 0 else ""
    answer = ""
    for entry in entries[last_user_idx + 1 :]:
        if entry.get("type") == "message" and entry.get("role") == "assistant":
            text = assistant_text(entry)
            if text:
                answer = text
    return {
        "prompt": prompt,
        "answer": answer,
        "model": model,
        "cumulative": totals,
        "entries": entries,
    }


def cumulative_usage(transcript_path: Any) -> dict[str, int]:
    return analyze_transcript(transcript_path)["cumulative"]


def diff_usage(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    return {key: max(0, current.get(key, 0) - baseline.get(key, 0)) for key in current}


def hit_rate(usage: dict[str, int]) -> float:
    hit = usage.get("cache_hit", 0)
    miss = usage.get("cache_miss", 0)
    if hit + miss <= 0:
        return 0.0
    return hit * 100.0 / (hit + miss)


# --------------------------------------------------------------------------- #
# Harness reconstruction (best-effort; explicitly NOT verbatim)
# --------------------------------------------------------------------------- #
CONTEXT_FILE_NAMES = ("CODEBUDDY.md", "AGENTS.md")
BLOCK_PREVIEW = 600
NOT_PERSISTED = (
    "系统指令 / instructions 基线",
    "environment_context（工作目录、git 状态、平台等）",
    "权限模式与工具白名单的注入文本",
    "技能（Skills）与插件清单的注入文本",
    "记忆（memory）注入全文——仅在磁盘文件清单中体现",
    "实际发送给云端的请求体与完整消息序列（本地日志不保存）",
)


def _reminder_kind(text: str) -> str:
    match = re.search(r'data-role="([^"]+)"', text)
    if match:
        return match.group(1)
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return stripped.split(">", 1)[0][1:].strip() or "system-reminder"
    return "system-reminder"


def injected_blocks(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Non-prompt user messages (injected reminders) belonging to this turn."""
    real = [
        index
        for index, entry in enumerate(entries)
        if entry.get("type") == "message"
        and entry.get("role") == "user"
        and user_prompt_text(entry)
    ]
    if not real:
        return []
    current = real[-1]
    previous = real[-2] if len(real) >= 2 else -1
    blocks: list[tuple[str, str]] = []
    for entry in entries[previous + 1 : current + 1]:
        if entry.get("type") != "message" or entry.get("role") != "user":
            continue
        text = _block_text(entry, {"input_text", "text"})
        if not text.strip() or user_prompt_text(entry):
            continue
        blocks.append((_reminder_kind(text), text))
    return blocks


def context_file_manifest(cwd: str, transcript_path: Any) -> list[dict[str, Any]]:
    """Existing context files with size + sha256 (current disk state, not a snapshot)."""
    candidates: list[Path] = []
    if cwd:
        base = Path(cwd)
        candidates += [base / name for name in CONTEXT_FILE_NAMES]
    home_cb = Path.home() / ".codebuddy"
    candidates += [home_cb / name for name in CONTEXT_FILE_NAMES]
    if transcript_path:
        memory_dir = Path(str(transcript_path)).parent / "memory"
        if memory_dir.is_dir():
            try:
                candidates += sorted(memory_dir.glob("*.md"))
            except OSError:
                pass
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        rows.append(
            {"path": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return rows


def harness_section(
    event: dict[str, Any], entries: list[dict[str, Any]], current: dict[str, int]
) -> str:
    """Render the reconstructed 'what the model saw at the edges' section."""
    blocks = injected_blocks(entries)
    files = context_file_manifest(str(event.get("cwd", "")), event.get("transcript_path"))
    real_count = sum(
        1
        for entry in entries
        if entry.get("type") == "message"
        and entry.get("role") == "user"
        and user_prompt_text(entry)
    )
    assistant_count = sum(
        1
        for entry in entries
        if entry.get("type") == "message"
        and entry.get("role") == "assistant"
        and assistant_text(entry)
    )

    out: list[str] = []
    out.append("## Harness 组装（重建，非逐字）")
    out.append("")
    out.append(
        "> harness 在请求时拼入的完整上下文（系统指令 / instructions / environment_context / "
        "权限 / 技能 / 记忆 / 全量历史）不会落盘。"
    )
    out.append(
        "> 本节仅依据本地可得证据重建；逐字部分已标注来源，不可得的部分明确列为“未持久化”。"
    )
    out.append("")
    out.append("### 本轮日志中记录的 reminder 块（逐字）")
    out.append("")
    out.append(
        "> 注：会话日志不区分“注入给模型的上下文块”与“仅显示给用户的提示”（如 hook 的 systemMessage），"
        "以下按原文列出，供对照。"
    )
    out.append("")
    if blocks:
        for index, (kind, text) in enumerate(blocks, 1):
            body = (
                text
                if len(text) <= BLOCK_PREVIEW
                else text[:BLOCK_PREVIEW] + f"\n…（共 {len(text)} 字符，已截断）"
            )
            out.append(f"**{index}. `{kind}`**")
            out.append("")
            out.append("```text")
            out.append(body)
            out.append("```")
            out.append("")
    else:
        out.append("（会话日志中未记录到本轮注入的 reminder 块）")
        out.append("")
    out.append("### 引用的上下文文件（磁盘当前版本，非请求时快照）")
    out.append("")
    if files:
        for row in files:
            out.append(f"- `{row['path']}` — {row['bytes']} B, sha256={row['sha256']}")
    else:
        out.append("（未发现 CODEBUDDY.md / AGENTS.md / memory 文件）")
    out.append("")
    out.append("### 历史规模")
    out.append("")
    out.append(
        f"- 截至本轮：用户消息 {real_count} 条 · assistant 消息 {assistant_count} 条 · "
        f"累计 tokens {int(current.get('total', 0)):,}"
    )
    out.append(
        f"- 其中输入 {int(current.get('input', 0)):,}（缓存命中 {int(current.get('cache_hit', 0)):,}）"
        f" · 输出 {int(current.get('output', 0)):,}"
    )
    out.append("")
    out.append("### 未持久化（不可得）")
    out.append("")
    for item in NOT_PERSISTED:
        out.append(f"- {item}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Archive writing
# --------------------------------------------------------------------------- #
def markdown_document(
    event: dict[str, Any],
    prompt: str,
    session_record: dict[str, Any],
    answer: str,
    harness: str = "",
    recovered: bool = False,
) -> bytes:
    created_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    header = (
        "---\n"
        f"archived_at: {json.dumps(created_at, ensure_ascii=False)}\n"
        f"session_id: {json.dumps(str(event.get('session_id', '')), ensure_ascii=False)}\n"
        f"turn_id: {json.dumps(str(event.get('turn_id', '')), ensure_ascii=False)}\n"
        f"model: {json.dumps(str(event.get('model', '')), ensure_ascii=False)}\n"
        f"cwd: {json.dumps(str(event.get('cwd', '')), ensure_ascii=False)}\n"
        f"archive_title: {json.dumps(str(session_record.get('title', '')), ensure_ascii=False)}\n"
        f"user_prompt_bytes: {len(prompt.encode('utf-8'))}\n"
        f"user_prompt_sha256: {sha256_text(prompt)}\n"
        f"assistant_answer_bytes: {len(answer.encode('utf-8'))}\n"
        f"assistant_answer_sha256: {sha256_text(answer)}\n"
        "archive_mode: verbatim\n"
        f"harness_section: {'reconstructed' if harness else 'absent'}\n"
        f"recovered: {'true' if recovered else 'false'}\n"
        "---\n\n"
        "# CodeBuddy 对话归档\n\n"
    )
    if recovered:
        header += (
            "> ⚠️ 本轮的 `Stop` hook 没有触发（进程被杀、hook 缺失等），"
            "此归档是下一条提示词到来时从会话日志补回的（`recovered: true`）。\n"
            "> 判据来自日志本身：该轮所有工具调用都有结果，结尾是一条 `status: completed` 的"
            "最终回答；被用户打断的消息日志里标的是 `status: incomplete`，不会被误收。\n\n"
        )
    header += "## 用户原文\n\n"
    middle = "\n\n## CodeBuddy 最终回答\n\n"
    # The prompt and answer payloads are inserted unchanged; the harness section
    # (when present) is clearly marked as a reconstruction, not verbatim.
    body = header + prompt
    if harness:
        body += "\n\n" + harness.rstrip() + "\n"
    body += middle + answer + "\n"
    return body.encode("utf-8")


def same_prefix(source: Path, destination: Path, destination_size: int) -> bool:
    if destination_size == 0:
        return True
    sample = min(65536, destination_size)
    try:
        with source.open("rb") as src, destination.open("rb") as dst:
            for offset in {0, max(0, destination_size - sample)}:
                src.seek(offset)
                dst.seek(offset)
                if src.read(sample) != dst.read(sample):
                    return False
    except OSError:
        return False
    return True


def mirror_transcript(transcript_path: Any, destination: Path) -> None:
    if not transcript_path:
        return
    source = Path(str(transcript_path))
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    if destination.exists():
        destination_size = destination.stat().st_size
        if destination_size <= source_size and same_prefix(source, destination, destination_size):
            if destination_size == source_size:
                return
            with source.open("rb") as src, destination.open("ab") as dst:
                src.seek(destination_size)
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    tmp_path = Path(tmp_name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, destination)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def session_dir(session_record: dict[str, Any], session_id: str) -> Path:
    date = safe_id(session_record.get("date"), "unknown-date")
    folder_name = str(
        session_record.get("folder_name") or f"未命名对话__{session_id[:8]}"
    )
    return ARCHIVE_ROOT / date / folder_name


def titled_session_dir(
    session_record: dict[str, Any], session_id: str
) -> Path | None:
    """Session folder for signal files, or None if the session has no title yet.

    Signals must never materialise a bogus ``未命名对话__``folder, so they are
    simply dropped for untitled sessions.
    """
    folder_name = session_record.get("folder_name")
    if not isinstance(folder_name, str) or not folder_name.strip():
        return None
    return session_dir(session_record, session_id)


def write_archive(
    event: dict[str, Any],
    session_id: str,
    turn_id: str,
    session_record: dict[str, Any],
    prompt: str,
    answer: str,
    harness: str = "",
    recovered: bool = False,
) -> str:
    """Write the Markdown + audit mirror. Returns the archive-relative path."""
    folder = session_dir(session_record, session_id)
    turn_file = folder / "阅读层" / f"{turn_id}.md"
    audit_file = folder / "审计层" / f"{session_id}.jsonl"

    lock_path = STATE_ROOT / "locks" / f"{session_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        exclusive_lock(lock)
        atomic_write_bytes(
            turn_file,
            markdown_document(
                event, prompt, session_record, answer, harness, recovered
            ),
        )
        mirror_transcript(event.get("transcript_path"), audit_file)
    return str(turn_file)


# --------------------------------------------------------------------------- #
# "A human is needed" signals
# --------------------------------------------------------------------------- #
def remove_signal(folder: Path, name: str) -> None:
    try:
        (folder / name).unlink()
    except OSError:
        pass


def clear_signals(session_record: dict[str, Any], session_id: str) -> None:
    """Drop every signal: the CLI is no longer waiting on a human."""
    folder = titled_session_dir(session_record, session_id)
    if folder is None:
        return
    for name in SIGNAL_FILES:
        remove_signal(folder, name)


def render_questions(tool_input: Any) -> str:
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    out: list[str] = []
    if isinstance(questions, list):
        for index, item in enumerate(questions, 1):
            if not isinstance(item, dict):
                continue
            header = str(item.get("header") or "").strip() or "未命名问题"
            text = str(item.get("question") or "").strip() or "（无题干）"
            kind = "多选" if item.get("multiSelect") else "单选"
            out.append(f"### {index}. {header}（{kind}）")
            out.append("")
            out.append(text)
            out.append("")
            options = item.get("options")
            if isinstance(options, list):
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    label = str(option.get("label") or "").strip()
                    description = str(option.get("description") or "").strip()
                    out.append(
                        f"- **{label or '（无标签）'}**"
                        + (f" — {description}" if description else "")
                    )
                out.append("")
        return "\n".join(out).strip() or "（questions 数组为空）"
    if isinstance(tool_input, dict) and tool_input.get("_raw"):
        return "（无法解析为 JSON，原文见下方）"
    return "（问题正文尚未落盘，约 2 秒后本文件会自动补全；也可直接到终端查看）"


def current_turn_entries(transcript_path: Any) -> list[dict[str, Any]]:
    """Entries belonging to the newest turn, i.e. after the last real prompt.

    Scoping everything to the current turn is what stops an abandoned question or
    a stale tool call from an earlier turn haunting the session.
    """
    entries = iter_entries(transcript_path)
    start = 0
    for index, entry in enumerate(entries):
        if (
            entry.get("type") == "message"
            and entry.get("role") == "user"
            and user_prompt_text(entry)
        ):
            start = index + 1
    return entries[start:]


def called_tools(entries: list[dict[str, Any]]) -> tuple[list[tuple[str, str, Any]], set[str]]:
    """(call_id, tool_name, arguments) in order, plus the ids that have a result."""
    calls: list[tuple[str, str, Any]] = []
    answered: set[str] = set()
    for entry in entries:
        call_id = str(entry.get("callId") or "")
        if not call_id:
            continue
        if entry.get("type") == "function_call":
            calls.append((call_id, str(entry.get("name") or ""), entry.get("arguments")))
        elif entry.get("type") == "function_call_result":
            answered.add(call_id)
    return calls, answered


def unanswered_tool(entries: list[dict[str, Any]]) -> str:
    """Name of the newest tool call with no result, or "" when all are answered."""
    calls, answered = called_tools(entries)
    for call_id, name, _ in reversed(calls):
        if call_id not in answered:
            return name
    return ""


# Entry types that say something about where a turn stopped. Metadata entries
# (ai-title, file-history-snapshot, ...) are skipped when looking at the tail.
TURN_TAIL_TYPES = ("message", "function_call", "function_call_result", "reasoning")


def turn_tail(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last entry of the turn that says anything about where it stopped."""
    for entry in reversed(entries):
        if entry.get("type") in TURN_TAIL_TYPES:
            return entry
    return None


def turn_interrupted(entries: list[dict[str, Any]]) -> bool:
    """Whether the user cut this turn off (Esc / ctrl-C).

    CodeBuddy marks the message it was streaming as ``status: "incomplete"`` with
    the text "Interrupted by user", and a Stop hook still fires for such a turn —
    so without this check an interruption gets archived as if its 19-byte
    placeholder were the answer.
    """
    tail = turn_tail(entries)
    return bool(
        tail
        and tail.get("type") == "message"
        and tail.get("role") == "assistant"
        and str(tail.get("status") or "") == "incomplete"
    )


def turn_completed(entries: list[dict[str, Any]]) -> bool:
    """Whether the turn in ``entries`` had finished, judged from the log alone.

    Needed because a Stop event can be lost (process killed, hook removed) while
    the turn itself completed. Codex marks its final answers with
    ``phase: final_answer``; CodeBuddy has no such field, but it does mark a
    message that was cut off by the user with ``status: "incomplete"`` (text
    "Interrupted by user"), so the closest equivalent is:

    * every tool call in the turn has a result (nothing is in flight),
    * the last content entry is an assistant message with output text, and
    * that message is ``status: "completed"`` rather than ``"incomplete"``.

    Anything else — an in-flight call, a turn ending on a tool result, a stream
    the user cut off — counts as unfinished and is never archived.
    """
    if unanswered_tool(entries):
        return False
    tail = turn_tail(entries)
    return bool(
        tail
        and tail.get("type") == "message"
        and tail.get("role") == "assistant"
        and str(tail.get("status") or "") == "completed"
        and bool(assistant_text(tail))
    )


def pending_tool_arguments(transcript_path: Any, tool_name: str) -> dict[str, Any] | None:
    """Arguments of the newest ``tool_name`` call in the turn that has no result.

    This is the only reliable "that panel is on screen right now" marker:
    CodeBuddy appends the function_call entry the moment the panel opens and the
    function_call_result entry only once the human has answered, so an
    unanswered call is visible on disk while every hook event is still silent.
    """
    calls, answered = called_tools(current_turn_entries(transcript_path))
    for call_id, name, arguments in reversed(calls):
        if name != tool_name or call_id in answered:
            continue
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
            return {"_raw": arguments}
        return {"_raw": json.dumps(arguments, ensure_ascii=False)}
    return None


def signal_document(
    event: dict[str, Any],
    session_record: dict[str, Any],
    session_id: str,
    kind: str,
    body_parts: list[str],
) -> bytes:
    created_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    header = (
        "---\n"
        f"signalled_at: {json.dumps(created_at, ensure_ascii=False)}\n"
        f"session_id: {json.dumps(session_id, ensure_ascii=False)}\n"
        f"turn_id: {json.dumps(str(session_record.get('current_turn_id') or ''), ensure_ascii=False)}\n"
        f"archive_title: {json.dumps(str(session_record.get('title', '')), ensure_ascii=False)}\n"
        f"cwd: {json.dumps(str(event.get('cwd', '')), ensure_ascii=False)}\n"
        f"notification_message: {json.dumps(str(event.get('message', '')), ensure_ascii=False)}\n"
        f"signal: {kind}\n"
        "---\n\n"
    )
    return (header + "\n".join(body_parts) + "\n").encode("utf-8")


def publish_signal(
    event: dict[str, Any],
    session_record: dict[str, Any],
    session_id: str,
    filename: str,
    kind: str,
    body_parts: list[str],
) -> None:
    """Publish one signal and retire the others: at most one may ever exist."""
    folder = titled_session_dir(session_record, session_id)
    if folder is None:
        return
    try:
        atomic_write_bytes(
            folder / filename,
            signal_document(event, session_record, session_id, kind, body_parts),
        )
        for other in SIGNAL_FILES:
            if other != filename:
                remove_signal(folder, other)
    except OSError as exc:
        log_error(f"signal-{kind}", session_id, "", exc)


def write_answer_needed(
    event: dict[str, Any],
    session_record: dict[str, Any],
    session_id: str,
    tool_input: dict[str, Any],
) -> None:
    """Signal an unanswered AskUserQuestion, with the question text when known."""
    body = [
        "# ⏸ 等待你回答",
        "",
        "> CodeBuddy 弹出了提问面板（`AskUserQuestion`），正在等你选择。",
        "> 该信号在面板弹出时立即写入；你回答后（或回合结束时）自动删除。",
        "> **只要这个文件还在，就说明这个问题还没被回答。**",
        "",
        "## 问题",
        "",
        render_questions(tool_input),
        "",
        "## 工具调用原文",
        "",
        "```json",
        json.dumps(tool_input, ensure_ascii=False, indent=2),
        "```",
    ]
    publish_signal(
        event, session_record, session_id, PENDING_QUESTION_FILE, "pending_question", body
    )


def write_plan_approval_needed(
    event: dict[str, Any],
    session_record: dict[str, Any],
    session_id: str,
    tool_input: dict[str, Any],
) -> None:
    """Signal an ExitPlanMode approval dialog, with the plan text when known."""
    raw_plan = tool_input.get("plan") if isinstance(tool_input, dict) else None
    plan = raw_plan.strip() if isinstance(raw_plan, str) else ""
    body = [
        "# ⏸ 等待你批准计划",
        "",
        "> CodeBuddy 请求退出计划模式（`ExitPlanMode`），正在等你批准后开始动手。",
        "> 该信号在面板弹出时立即写入；你批准后（或回合结束时）自动删除。",
        "",
        "## 计划",
        "",
        plan or render_questions(tool_input),
        "",
        "## 工具调用原文",
        "",
        "```json",
        json.dumps(tool_input, ensure_ascii=False, indent=2),
        "```",
    ]
    publish_signal(
        event, session_record, session_id, PLAN_APPROVAL_FILE, "plan_approval", body
    )


def write_waiting_input(
    event: dict[str, Any], session_record: dict[str, Any], session_id: str
) -> None:
    """Catch-all signal for 'a turn is open and idle 60s'."""
    body = [
        "# ⏸ 等待你输入",
        "",
        "> 会话已空闲 60 秒，CodeBuddy 侧没有待执行的动作，但本轮尚未结束。",
        "> 也可能是一条尚未落盘的提问，稍后会被更精确的信号文件取代。",
        "",
    ]
    publish_signal(
        event, session_record, session_id, WAITING_INPUT_FILE, "idle_prompt", body
    )


def handle_idle(event: dict[str, Any], session_id: str) -> None:
    """Notification(idle_prompt): an in-progress turn has gone quiet.

    Note that this never fires while a question panel is open, so it is only a
    safety net for the other ways a turn can stall.
    """
    session_record = load_json(session_path(session_id)) or {}
    if titled_session_dir(session_record, session_id) is None:
        return
    if not str(session_record.get("current_turn_id") or ""):
        # The turn already ended: the CLI is only waiting for the next prompt,
        # which is the normal state and must not generate files.
        return
    question = pending_tool_arguments(event.get("transcript_path"), ASK_USER_QUESTION_TOOL)
    if question is not None:
        # Should not happen (a pending question suppresses idle_prompt), but if a
        # build ever does emit it, prefer the precise signal.
        write_answer_needed(event, session_record, session_id, question)
        return
    write_waiting_input(event, session_record, session_id)


def permission_tool(message: Any) -> str:
    """Extract the tool name from a permission_prompt notification message."""
    text = str(message or "")
    if PERMISSION_MESSAGE_MARKER not in text:
        return ""
    return text.split(PERMISSION_MESSAGE_MARKER, 1)[1].strip()


def retire_signal(session_id: str, tool_name: str) -> None:
    """Retire the signal of a dialog that has just been answered.

    PreToolUse runs when the tool finally executes, i.e. right after the human
    responded. Retiring here keeps the promise honest — the file exists only
    while the question is genuinely unanswered, even if the turn then carries on
    for a long time.
    """
    filename = SIGNAL_FILE_FOR_TOOL.get(tool_name)
    if filename is None:
        return
    session_record = load_json(session_path(session_id)) or {}
    folder = titled_session_dir(session_record, session_id)
    if folder is not None:
        remove_signal(folder, filename)


def handle_human_input_prompt(event: dict[str, Any], session_id: str, tool_name: str) -> None:
    """Notification(permission_prompt) naming a tool that always blocks on a human.

    This is the only zero-latency trigger: the question/plan panel is already up
    when it arrives. The tool call itself reaches the transcript a moment later,
    so publish a usable signal right away and upgrade it with the text once it
    lands.
    """
    session_record = load_json(session_path(session_id)) or {}
    if titled_session_dir(session_record, session_id) is None:
        return
    write = (
        write_answer_needed
        if tool_name == ASK_USER_QUESTION_TOOL
        else write_plan_approval_needed
    )
    tool_input = pending_tool_arguments(event.get("transcript_path"), tool_name)
    if tool_input is not None:
        write(event, session_record, session_id, tool_input)
        return
    write(event, session_record, session_id, {})
    time.sleep(SIGNAL_ENRICH_DELAY)
    if not str(session_record.get("current_turn_id") or ""):
        return  # The turn ended while we waited; the signal was already cleaned.
    tool_input = pending_tool_arguments(event.get("transcript_path"), tool_name)
    if tool_input is not None:
        write(event, session_record, session_id, tool_input)


# --------------------------------------------------------------------------- #
# Hook entry points
# --------------------------------------------------------------------------- #
def new_turn_id() -> str:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    suffix = os.urandom(3).hex()
    return f"{stamp}-{suffix}"


def handle_user_prompt(
    event: dict[str, Any], session_id: str, turn_id: str
) -> str | None:
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    now = dt.datetime.now().astimezone()
    received_at = now.isoformat(timespec="seconds")
    date = now.date().isoformat()

    session_record = load_or_create_session(session_id, prompt, date, received_at)
    session_record["current_turn_id"] = turn_id
    baseline = cumulative_usage(event.get("transcript_path"))
    session_record["baseline"] = baseline
    atomic_write_json(session_path(session_id), session_record)
    # The human just typed something, so nothing is waiting on them any more.
    clear_signals(session_record, session_id)

    atomic_write_json(
        prompt_path(session_id, turn_id),
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "received_at": received_at,
            "date": date,
            "prompt": prompt,
            "prompt_bytes": len(prompt.encode("utf-8")),
            "prompt_sha256": sha256_text(prompt),
        },
    )
    return (
        "▶ 本轮开始 | 会话累计：输入 {input}（缓存命中 {rate:.0f}%）· 输出 {output}"
        " · 合计 {total} tokens · 计费 {credit:.3f}"
    ).format(
        input=f"{baseline['input']:,}",
        output=f"{baseline['output']:,}",
        total=f"{baseline['total']:,}",
        credit=baseline["credit"] / 1000.0,
        rate=hit_rate(baseline),
    )


def handle_stop(
    event: dict[str, Any], session_id: str, turn_id: str, origin: str = "stop"
) -> str:
    event["turn_id"] = turn_id
    transcript_path = event.get("transcript_path")
    spooled = load_json(prompt_path(session_id, turn_id)) or {}
    prompt = str(spooled.get("prompt", ""))
    date = str(spooled.get("date", dt.datetime.now().astimezone().date().isoformat()))
    received_at = str(spooled.get("received_at", ""))

    analysis = analyze_transcript(transcript_path)
    if not prompt:
        prompt = analysis["prompt"]
    # The Stop event carries the CLI's own final output ("last_assistant_message",
    # built from the in-memory run result), so prefer it: the session log can lag
    # the Stop event by a beat, and a transcript-only read then records the
    # previous mid-turn segment instead of the answer — which is exactly how a
    # 7.5 KB answer once got archived as a 184-byte "先确认两处细节" note. The
    # transcript remains the fallback, and the only source for catch-up/recovery,
    # where no event field exists.
    answer = str(event.get("last_assistant_message") or "") or analysis["answer"]
    model = analysis["model"] or str(event.get("model", ""))
    event["model"] = model
    current = analysis["cumulative"]

    session_record = load_json(session_path(session_id)) or load_or_create_session(
        session_id, prompt, date, received_at
    )
    session_record["date"] = date
    session_record["received_at"] = received_at or session_record.get("received_at", "")

    # ---- token accounting -------------------------------------------------- #
    baseline = session_record.get("baseline")
    if not isinstance(baseline, dict):
        baseline = {key: 0 for key in current}
    turn_usage = diff_usage(current, baseline)
    reported = session_record.get("reported_turns")
    if not isinstance(reported, list):
        reported = []
    already_reported = turn_id in reported

    # ---- how did the turn end? --------------------------------------------- #
    # A real Stop means the run reached its final output. A catch-up call (the
    # next UserPromptSubmit superseding a turn whose Stop was skipped) has to be
    # judged from the log instead: if the turn did finish and only its Stop went
    # missing, the archive is repaired now (recovered); if it was interrupted or
    # still has a call in flight, it must not be archived at all.
    entries = current_turn_entries(transcript_path)
    turn_status = "completed"
    if origin == "stop":
        if turn_interrupted(entries):
            turn_status = "interrupted_by_user"
    elif turn_completed(entries):
        turn_status = "recovered"
    elif unanswered_tool(entries) == ASK_USER_QUESTION_TOOL:
        turn_status = "interrupted_pending_question"
    else:
        turn_status = "superseded_catchup"

    # ---- archive ----------------------------------------------------------- #
    # Only a finished turn may write to the archive: a real Stop, or a completed
    # turn whose Stop was lost. An interrupted turn does not qualify — its only
    # text is the "Interrupted by user" placeholder, and whoever interrupted it
    # was at the terminal anyway. Anything unarchived stays in the audit mirror,
    # and the ledger below still accounts for its tokens.
    archive_status = "未归档"
    if turn_status in ("interrupted_by_user", "interrupted_pending_question"):
        archive_status = f"未归档（{turn_status}，仅记账）"
    elif origin != "stop" and turn_status != "recovered":
        archive_status = f"未归档（回合未真正结束：{turn_status}，仅记账）"
    elif answer:
        try:
            harness = harness_section(event, analysis.get("entries", []), current)
            relative = write_archive(
                event,
                session_id,
                turn_id,
                session_record,
                prompt,
                answer,
                harness,
                recovered=turn_status == "recovered",
            )
            archive_status = f"已归档 → {relative}"
        except Exception as exc:  # noqa: BLE001 - archive failures are non-fatal
            archive_status = f"归档失败（仅记账，需手动补）：{exc!r}"
            log_error("Stop-archive", session_id, turn_id, exc)
    else:
        archive_status = "未归档（会话日志中未找到最终回答）"

    # ---- usage ledger + state --------------------------------------------- #
    if not already_reported:
        record = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_id": session_id,
            "turn_id": turn_id,
            "cwd": str(event.get("cwd", "")),
            "model": str(event.get("model", "")),
            "turn": turn_usage,
            "turn_cache_hit_rate": round(hit_rate(turn_usage), 1),
            "cumulative": current,
            "cumulative_cache_hit_rate": round(hit_rate(current), 1),
            "archive": archive_status,
            "turn_status": turn_status,
        }
        try:
            append_line_locked(
                USAGE_ROOT / "usage.jsonl",
                json.dumps(record, ensure_ascii=False),
            )
            atomic_write_json(USAGE_ROOT / "latest-turn.json", record)
        except OSError as exc:
            log_error("Stop-usage", session_id, turn_id, exc)
        reported = (reported + [turn_id])[-50:]
        session_record["reported_turns"] = reported

    session_record["current_turn_id"] = ""
    atomic_write_json(session_path(session_id), session_record)
    clear_signals(session_record, session_id)

    return (
        "■ 本轮结束 | 本轮：输入 {tin}（缓存命中 {tin_rate:.0f}%）· 输出 {tout}"
        "（推理 {treason}）· 合计 {ttotal} · 计费 {tcredit:.3f}\n"
        "   会话累计：输入 {cin} · 输出 {cout} · 合计 {ctotal} · 计费 {ccredit:.3f}"
        "（缓存命中率 {crate:.0f}%）\n"
        "   {archive}"
    ).format(
        tin=f"{turn_usage['input']:,}",
        tout=f"{turn_usage['output']:,}",
        treason=f"{turn_usage['reasoning']:,}",
        ttotal=f"{turn_usage['total']:,}",
        tcredit=turn_usage["credit"] / 1000.0,
        tin_rate=hit_rate(turn_usage),
        cin=f"{current['input']:,}",
        cout=f"{current['output']:,}",
        ctotal=f"{current['total']:,}",
        ccredit=current["credit"] / 1000.0,
        crate=hit_rate(current),
        archive=archive_status,
    )


def log_error(stage: str, session_id: str, turn_id: str, exc: Exception) -> None:
    try:
        path = STATE_ROOT / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{stamp}\t{stage}\t{session_id}\t{turn_id}\t{exc!r}\n")
    except OSError:
        pass


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0

    session_id = safe_id(event.get("session_id"), "unknown-session")
    hook = event.get("hook_event_name")

    try:
        if hook == "UserPromptSubmit":
            session_record = load_json(session_path(session_id)) or {}
            # A fresh prompt supersedes any turn whose Stop was skipped. When the
            # Stop and the prompt land in the same instant the session record may
            # still name a turn the Stop already recorded — processing it again
            # would rewrite the same document as "recovered" and double the ledger.
            pending = str(session_record.get("current_turn_id") or "")
            reported = session_record.get("reported_turns")
            if pending and not (isinstance(reported, list) and pending in reported):
                try:
                    handle_stop(event, session_id, pending, origin="catchup")
                except Exception as exc:  # noqa: BLE001
                    log_error("catch-up", session_id, pending, exc)
            turn_id = new_turn_id()
            event["turn_id"] = turn_id
            message = handle_user_prompt(event, session_id, turn_id)
            if message:
                emit({"systemMessage": message})
        elif hook == "Stop":
            session_record = load_json(session_path(session_id)) or {}
            # A Stop with no pending turn means this turn was already recorded
            # (or no prompt was ever submitted). Do not invent a turn.
            turn_id = safe_id(
                event.get("turn_id") or session_record.get("current_turn_id"), ""
            )
            if not turn_id:
                return 0
            message = handle_stop(event, session_id, turn_id)
            emit({"systemMessage": message})
        elif hook == "PreToolUse":
            # Runs when the tool executes, i.e. right after the human responded.
            retire_signal(session_id, str(event.get("tool_name") or ""))
        elif hook == "Notification":
            notification_type = str(event.get("notification_type") or "")
            if notification_type == "idle_prompt":
                handle_idle(event, session_id)
            elif notification_type == "permission_prompt":
                tool = permission_tool(event.get("message"))
                if tool in SIGNAL_FILE_FOR_TOOL:
                    handle_human_input_prompt(event, session_id, tool)
    except Exception as exc:  # noqa: BLE001 - never block the conversation
        log_error(str(hook), session_id, str(event.get("turn_id", "")), exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
