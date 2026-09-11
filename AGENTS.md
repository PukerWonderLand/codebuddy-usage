# AGENTS.md — replicate this environment on another machine

This file is written for an AI coding agent. Follow it literally; every step is
deterministic and idempotent. The goal is a working CodeBuddy usage dashboard
plus per-turn Markdown archiving, with no manual editing of source files.

## 1. Preconditions

- Linux with `bash`, `git`, and **Python 3.10+** (`python3 --version`).
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

# b) The service is up and listening
systemctl --user status codebuddy-dashboard --no-pager | head -5
ss -ltn | grep 3766

# c) APIs answer
curl -s http://127.0.0.1:3766/api/health
curl -s "http://127.0.0.1:3766/api/summary?range=all" | head -c 400

# d) The page and assets load
for p in / /app.js /style.css; do
  curl -s -o /dev/null -w "$p %{http_code}\n" http://127.0.0.1:3766$p
done

# e) The hook is registered exactly once per event
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.codebuddy/settings.json")
d = json.load(open(p, encoding="utf-8"))
for ev in ("UserPromptSubmit", "Stop"):
    n = sum("codebuddy_turn_hook.py" in str(h.get("command",""))
            for g in d.get("hooks", {}).get(ev, []) for h in g.get("hooks", []))
    print(ev, "entries:", n)
PY

# f) Collector totals are self-consistent
codebuddy-dashboard summary
```

`/api/health` must return `{"ok": true, ...}`. Each static route must return
`200` (favicon `204`). Hook entries must print `1` for both events.

## 4. Activate the hooks

CodeBuddy snapshots hooks at startup. **Restart the CodeBuddy CLI** (or open the
`/hooks` menu to review/apply). After the next conversation turn:

- the UI shows a `▶ 本轮开始` and a `■ 本轮结束` message, and
- a new `*.md` appears under `<archive_root>/<date>/<...>/阅读层/`.

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
  (`ss -ltn | grep 3766`) and open the host firewall for that port.
- **`systemd user session unavailable`**: use `--no-service` and run
  `codebuddy-dashboard run` under your own supervisor.
- **Service stops after logout**: `loginctl enable-linger "$USER"`.
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
