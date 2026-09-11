#!/usr/bin/env python3
"""Local-first HTTP dashboard for CodeBuddy token usage.

Zero third-party dependencies (Python standard library only). Serves a static
front-end plus JSON APIs over the collected session logs.

    python3 src/server.py --host 0.0.0.0 --port 3766
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
from pathlib import Path
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collector  # noqa: E402

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
RANGES = {"today", "7d", "30d", "all"}

_LOCK = threading.Lock()
_INDEX: dict[str, Any] | None = None
_INDEX_AT = 0.0
_TTL_SECONDS = 5.0


def get_index(force: bool = False) -> dict[str, Any]:
    """Refresh the collected index at most once every TTL_SECONDS."""
    global _INDEX, _INDEX_AT
    now = dt.datetime.now().timestamp()
    with _LOCK:
        if force or _INDEX is None or now - _INDEX_AT > _TTL_SECONDS:
            _INDEX = collector.build_index(force=force)
            _INDEX_AT = now
        return _INDEX


def session_rows(events: list[dict[str, Any]], index: dict[str, Any]) -> list[dict[str, Any]]:
    meta = {row["session_id"]: row for row in index["sessions"]}
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        sid = event["session_id"]
        row = buckets.get(sid)
        if row is None:
            info = meta.get(sid, {})
            row = {
                "session_id": sid,
                "project": event.get("project") or info.get("project", ""),
                "cwd": event.get("cwd") or info.get("cwd", ""),
                "title": info.get("title", ""),
                "model": event.get("model") or info.get("model", ""),
                "first_ts": event["ts"],
                "last_ts": event["ts"],
                "turns": 0,
                **collector._empty_totals(),
            }
            buckets[sid] = row
        collector._accumulate(row, event)
        row["first_ts"] = min(row["first_ts"], event["ts"])
        row["last_ts"] = max(row["last_ts"], event["ts"])
        row["turns"] = max(row["turns"], int(event.get("turn", 0) or 0))
    rows = list(buckets.values())
    for row in rows:
        row["cache_hit_rate"] = collector.hit_rate(row)
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows


def turn_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: dict[int, dict[str, Any]] = {}
    for event in events:
        index = int(event.get("turn", 0) or 0)
        row = turns.get(index)
        if row is None:
            row = {
                "turn": index,
                "first_ts": event["ts"],
                "last_ts": event["ts"],
                "models": [],
                "details": [],
                **collector._empty_totals(),
            }
            turns[index] = row
        collector._accumulate(row, event)
        row["first_ts"] = min(row["first_ts"], event["ts"])
        row["last_ts"] = max(row["last_ts"], event["ts"])
        if event.get("model") and event["model"] not in row["models"]:
            row["models"].append(event["model"])
        row["details"].append(
            {
                "ts": event["ts"],
                "model": event.get("model", ""),
                "input": event.get("input", 0),
                "cached": event.get("cached", 0),
                "cache_miss": event.get("cache_miss", 0),
                "output": event.get("output", 0),
                "reasoning": event.get("reasoning", 0),
                "total": event.get("total", 0),
                "credit": event.get("credit", 0),
            }
        )
    rows = [turns[k] for k in sorted(turns)]
    for row in rows:
        row["cache_hit_rate"] = collector.hit_rate(row)
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeBuddyDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("CODEBUDDY_DASHBOARD_QUIET"):
            return
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), format % args)
        )

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, list[str]]:
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query)

    # -- routing ---------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            if route.startswith("/api/"):
                self._handle_api(route, self._query())
            elif route in STATIC_ROUTES:
                name, content_type = STATIC_ROUTES[route]
                path = PUBLIC_DIR / name
                if not path.is_file():
                    self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                    return
                self._send_bytes(path.read_bytes(), content_type)
            elif route == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon", 204)
            else:
                self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def _handle_api(self, route: str, query: dict[str, list[str]]) -> None:
        if route == "/api/health":
            index = get_index()
            self._send_json(
                {
                    "ok": True,
                    "generated_at": index["generated_at"],
                    "sessions": len(index["sessions"]),
                    "events": len(index["events"]),
                }
            )
            return

        if route == "/api/summary":
            range_key = self._range(query)
            index = get_index(force=self._force(query))
            events = collector.filter_range(index["events"], range_key)
            payload = collector.summarize(events)
            payload["range"] = range_key
            payload["generated_at"] = index["generated_at"]
            self._send_json(payload)
            return

        if route == "/api/sessions":
            range_key = self._range(query)
            index = get_index(force=self._force(query))
            events = collector.filter_range(index["events"], range_key)
            self._send_json(
                {
                    "range": range_key,
                    "generated_at": index["generated_at"],
                    "sessions": session_rows(events, index),
                }
            )
            return

        if route == "/api/session":
            session_id = (query.get("id") or [""])[0]
            if not session_id:
                self._send_json({"error": "missing id"}, status=400)
                return
            index = get_index(force=self._force(query))
            events = [e for e in index["events"] if e["session_id"] == session_id]
            meta = next(
                (s for s in index["sessions"] if s["session_id"] == session_id), {}
            )
            self._send_json(
                {
                    "generated_at": index["generated_at"],
                    "session": meta,
                    "turns": turn_rows(events),
                }
            )
            return

        if route == "/api/archive":
            limit = int((query.get("limit") or ["50"])[0] or 50)
            self._send_json({"records": collector.read_ledger(limit=limit)})
            return

        self._send_json({"error": "unknown endpoint"}, status=404)

    def _range(self, query: dict[str, list[str]]) -> str:
        value = (query.get("range") or ["all"])[0]
        return value if value in RANGES else "all"

    def _force(self, query: dict[str, list[str]]) -> bool:
        return (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodeBuddy usage dashboard")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "3766"))
    )
    args = parser.parse_args(argv)

    mimetypes.add_type("application/javascript", ".js")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url_host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"CodeBuddy dashboard: http://{url_host}:{args.port}/  (bind {args.host}:{args.port})")
    print(f"data: {collector.PROJECTS_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
