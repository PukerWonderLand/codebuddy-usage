# AGENTS.md — replicate this environment on another machine

This file is written for an AI coding agent. Follow it literally; every step is
deterministic and idempotent. The goal is a working CodeBuddy usage dashboard
plus per-turn Markdown archiving, with no manual editing of source files.

## 1. Preconditions

- **Linux or macOS**, with `bash`, `git`, and **Python 3.10+**
  (`python3 --version`). macOS ships 3.9, so install a newer interpreter first,
  e.g. `brew install python@3.12` or `uv python install 3.12`; the installer
  probes `python3.13`/`python3.12`/`python3.11`/`python3.10` automatically, or
  you can pass `PYTHON=/path/to/python3`.
- The CodeBuddy CLI is installed and has produced at least one session under
  `~/.codebuddy/projects/` (optional; the dashboard also works before that).
- Decide the archive root:
  - Local directory: `~/codebuddy-archive` (default), or
  - A mounted share, e.g. `--archive-root /mnt/share/CodeBuddy自动归档`.

## 2. Install

```bash
git clone https://github.com/PukerWonderLand/codebuddy-usage.git
cd codebuddy-usage
./install.sh
```

Non-interactive variant with an explicit archive root and port:

```bash
./install.sh --archive-root "/mnt/share/CodeBuddy自动归档" --port 3766
```

Headless machine without a systemd user session (SSH without DBus):

```bash
./install.sh --no-service
# then run it yourself, e.g. under tmux or your own supervisor:
codebuddy-dashboard run --host 0.0.0.0 --port 3766
```

Useful flags: `--dry-run`, `--copy`, `--no-hooks`, `--no-service`.

## 3. Verify (all must pass)

```bash
# a) Python files compile
python3 -m py_compile hooks/codebuddy_turn_hook.py src/*.py && echo COMPILE_OK

# b) Hook regression suite (pure stdlib, sandboxed in a temp dir — safe to run
#    on a live machine; it never touches the real archive or ledger)
python3 tests/test_turn_hook.py

# c) The service is up and listening
ss -ltn | grep 3766            # Linux
lsof -nP -iTCP:3766 -sTCP:LISTEN   # macOS
# Linux service state:
systemctl --user status codebuddy-dashboard --no-pager | head -5
# macOS service state:
launchctl print "gui/$(id -u)/com.pukerwonderland.codebuddy-dashboard" | head -20

# d) APIs answer
curl -s http://127.0.0.1:3766/api/health
curl -s "http://127.0.0.1:3766/api/summary?range=all" | head -c 400

# e) The page and assets load
for p in / /app.js /style.css; do
  curl -s -o /dev/null -w "$p %{http_code}\n" http://127.0.0.1:3766$p
done

# f) The hook is registered exactly once per event
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.codebuddy/settings.json")
d = json.load(open(p, encoding="utf-8"))
for ev in ("UserPromptSubmit", "Stop", "PreToolUse", "Notification"):
    n = sum("codebuddy_turn_hook.py" in str(h.get("command",""))
            for g in d.get("hooks", {}).get(ev, []) for h in g.get("hooks", []))
    print(ev, "entries:", n)
PY

# g) Collector totals are self-consistent
codebuddy-dashboard summary
```

`/api/health` must return `{"ok": true, ...}`. Each static route must return
`200` (favicon `204`). Hook entries must print `1` for all four events. The
regression suite must end with `all N checks passed`.

## 4. Activate the hooks

CodeBuddy snapshots hooks at startup **and hot-reloads `settings.json` when it
changes**, so a running session picks the new events up on the next change.
**Restart the CodeBuddy CLI** (or open the `/hooks` menu to review/apply) if in
doubt. After the next conversation turn:

- the UI shows a `▶ 本轮开始` and a `■ 本轮结束` message, and
- a new `*.md` appears under `<archive_root>/<date>/<...>/阅读层/`.

Then trigger an `AskUserQuestion` panel and confirm the "waiting on you" signal:
`<archive_root>/<date>/<...>/_等待回答.md` must appear the moment the panel opens
(with the question text and options), and disappear once the answer is submitted.

Note that the readable layer only receives turns that actually finished: a real
`Stop`, or a completed turn whose `Stop` was lost (repaired at the next prompt as
`turn_status: recovered`). Everything else stays in the ledger and `审计层`:
`interrupted_by_user` (Esc), `interrupted_pending_question`, `superseded_catchup`.

Two traps worth knowing when touching the answer extraction:

- The session log **lags the `Stop` event** (measured ~300 ms), so a log-only read
  records the mid-turn narration as the answer. Use the event's
  `last_assistant_message`; keep the log as fallback and for recovery.
- `Stop` **does** fire on a user interruption, with the turn's last text being the
  19-byte `Interrupted by user` placeholder and `status: incomplete` — check that
  status or the interruption gets archived as an answer.

Session naming (verify after a rename):

- The folder name comes from the first prompt. After a `/rename`, the next turn's
  files must land in a **new** folder named after the new name
  (`retitle_session()`), while the old folder keeps the earlier turns. The rename
  is detected from `custom-title` entries and the
  `Session renamed to: X` echo, filtered by `sessionId` so a forked session does
  not inherit its ancestor's name.
- New sessions are filed under their **first day** (`created_date`); records
  without that field are older and keep the per-turn date.
- `src/collector.py` prefers `custom-title` over `ai-title` for the dashboard, so
  it needs a `systemctl --user restart codebuddy-dashboard` after any change
  there.

Verify the ledger grew:

```bash
codebuddy-usage latest
```

## 5. Configuration reference

Precedence: environment variable > `~/.codebuddy-usage/config.json` > default.

| Key | Env var | Default |
| --- | --- | --- |
| `archive_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_ROOT` | `~/codebuddy-archive` |
| `projects_root` | `CODEBUDDY_PROJECTS_ROOT` | `~/.codebuddy/projects` |
| `usage_root` | `CODEBUDDY_USAGE_ROOT` | `~/.codebuddy-usage` |
| `state_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_STATE` | `~/.codebuddy-turn-state` |

To change paths after install, edit the config file (or pass flags to
`./install.sh` again) and restart the service.

## 6. Troubleshooting

- **Dashboard not reachable from another machine**: confirm it binds `0.0.0.0`
  (`ss -ltn | grep 3766` on Linux, `lsof -nP -iTCP:3766` on macOS) and open the
  host firewall for that port. On macOS the first bind triggers a firewall prompt
  ("Do you want the application python3 to accept incoming network connections?")
  — allow it, or LAN access stays blocked.
- **macOS: service not running after install**: `launchctl print gui/$(id -u)/com.pukerwonderland.codebuddy-dashboard`;
  re-apply with `launchctl bootout gui/$(id -u)/com.pukerwonderland.codebuddy-dashboard` then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pukerwonderland.codebuddy-dashboard.plist`.
- **`systemd user session unavailable`**: use `--no-service` and run
  `codebuddy-dashboard run` under your own supervisor.
- **Service stops after logout**: `loginctl enable-linger "$USER"`.
- **CodeBuddy says "Authentication required. Please use /login"**: the CodeBuddy
  CLI is not signed in. Run `codebuddy` interactively and use the `/login`
  command. Neither the hook nor the dashboard produces data until a CodeBuddy
  turn actually completes.
- **No archive files after a turn**: the hook only archives when the session
  JSONL already contains the final assistant message; check
  `~/.codebuddy-turn-state/errors.log` and confirm the archive root is writable
  (`test -w "$(python3 -c 'import sys;sys.path.insert(0,"src");import config;print(config.archive_root())')"`).
- **Totals look stale**: the server refreshes every 5s; force with
  `curl -s "http://127.0.0.1:3766/api/summary?range=all&refresh=1"`.

## 7. What not to do

- Do not rewrite or "clean up" user session logs under `~/.codebuddy/projects`.
- Do not remove unrelated entries from `~/.codebuddy/settings.json`; the
  installer and uninstaller only touch entries whose command contains
  `codebuddy_turn_hook.py`.
- Do not commit `config.json`, `usage.jsonl`, or archive contents to git.
