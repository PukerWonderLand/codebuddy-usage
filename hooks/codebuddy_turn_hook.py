#!/usr/bin/env python3
"""CodeBuddy turn hook: archive the turn as Markdown and account token usage.

One deterministic, model-free script handles two CodeBuddy hook events:

* UserPromptSubmit -> spool the exact user prompt, snapshot cumulative token
  usage as the turn baseline, emit a "turn start" system message.
* Stop             -> combine the spooled prompt with the final assistant
  answer, write a verbatim Markdown document to the archive root (often an SMB
  share), mirror the raw session JSONL into an audit layer, append a token-usage
  ledger record, and emit a "turn end" system message.

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
# Archive writing
# --------------------------------------------------------------------------- #
def markdown_document(
    event: dict[str, Any],
    prompt: str,
    session_record: dict[str, Any],
    answer: str,
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
        "---\n\n"
        "# CodeBuddy 对话归档\n\n"
        "## 用户原文\n\n"
    )
    middle = "\n\n## CodeBuddy 最终回答\n\n"
    # The two payloads are inserted unchanged; only the envelope is generated.
    return (header + prompt + middle + answer + "\n").encode("utf-8")


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


def write_archive(
    event: dict[str, Any],
    session_id: str,
    turn_id: str,
    session_record: dict[str, Any],
    prompt: str,
    answer: str,
) -> str:
    """Write the Markdown + audit mirror. Returns the archive-relative path."""
    date = safe_id(session_record.get("date"), "unknown-date")
    folder_name = str(session_record.get("folder_name", f"未命名对话__{session_id[:8]}"))
    session_dir = ARCHIVE_ROOT / date / folder_name
    turn_file = session_dir / "阅读层" / f"{turn_id}.md"
    audit_file = session_dir / "审计层" / f"{session_id}.jsonl"

    lock_path = STATE_ROOT / "locks" / f"{session_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        exclusive_lock(lock)
        atomic_write_bytes(turn_file, markdown_document(event, prompt, session_record, answer))
        mirror_transcript(event.get("transcript_path"), audit_file)
    return str(turn_file)


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


def handle_stop(event: dict[str, Any], session_id: str, turn_id: str) -> str:
    event["turn_id"] = turn_id
    transcript_path = event.get("transcript_path")
    spooled = load_json(prompt_path(session_id, turn_id)) or {}
    prompt = str(spooled.get("prompt", ""))
    date = str(spooled.get("date", dt.datetime.now().astimezone().date().isoformat()))
    received_at = str(spooled.get("received_at", ""))

    analysis = analyze_transcript(transcript_path)
    if not prompt:
        prompt = analysis["prompt"]
    answer = analysis["answer"] or str(event.get("last_assistant_message") or "")
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

    # ---- archive ----------------------------------------------------------- #
    archive_status = "未归档"
    if answer:
        try:
            relative = write_archive(
                event, session_id, turn_id, session_record, prompt, answer
            )
            archive_status = f"已归档 → {relative}"
        except Exception as exc:  # noqa: BLE001 - archive failures are non-fatal
            archive_status = f"归档失败（本地已保留，下次自动重试）：{exc!r}"
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
            # A fresh prompt supersedes any turn whose Stop was skipped.
            pending = str(session_record.get("current_turn_id") or "")
            if pending:
                try:
                    handle_stop(event, session_id, pending)
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
    except Exception as exc:  # noqa: BLE001 - never block the conversation
        log_error(str(hook), session_id, str(event.get("turn_id", "")), exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
