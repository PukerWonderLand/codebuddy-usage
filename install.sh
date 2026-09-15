#!/usr/bin/env bash
# codebuddy-usage installer.
#
# Idempotent: safe to re-run. It only adds what is missing and never removes
# unrelated CodeBuddy hooks or settings.
#
#   ./install.sh [options]
#
# Options:
#   --archive-root PATH   where per-turn Markdown archives are written
#                         (default: ~/codebuddy-archive, or an SMB mount)
#   --projects-root PATH  CodeBuddy session logs (default: ~/.codebuddy/projects)
#   --host HOST           dashboard bind address (default: 0.0.0.0)
#   --port PORT           dashboard port (default: 3766)
#   --no-service          do not install/start the systemd user service
#   --no-hooks            do not register the CodeBuddy hooks
#   --copy                copy files instead of symlinking
#   --dry-run             print actions without changing anything
#   -h, --help            show this help

set -euo pipefail

HOST="0.0.0.0"
PORT="3766"
ARCHIVE_ROOT=""
PROJECTS_ROOT=""
NO_SERVICE=0
NO_HOOKS=0
COPY_MODE=0
DRY_RUN=0

# Resolve the repository root portably (no GNU readlink -f required).
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
REPO="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }
log() { printf '%s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --archive-root) ARCHIVE_ROOT="${2:?--archive-root needs a value}"; shift 2 ;;
    --projects-root) PROJECTS_ROOT="${2:?--projects-root needs a value}"; shift 2 ;;
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --no-service) NO_SERVICE=1; shift ;;
    --no-hooks) NO_HOOKS=1; shift ;;
    --copy) COPY_MODE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

OS_NAME="$(uname -s)"

# Find a Python >= 3.10. Try the canonical `python3` first (keeps hook commands
# stable across re-installs); fall back to versioned names, which is what macOS
# needs because its system `python3` is 3.9.
find_python() {
  for cand in ${PYTHON:-} python3 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$cand" ] || continue
    p="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$p" ] || continue
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      printf '%s' "$p"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || die "Python >= 3.10 required. Install one (e.g. 'brew install python@3.12' or 'uv python install 3.12') and re-run, or set PYTHON=/path/to/python3."

CONFIG_DIR="$HOME/.codebuddy-usage"
CONFIG="$CONFIG_DIR/config.json"
BIN_DIR="$HOME/.local/bin"
HOOK_DIR="$HOME/.codebuddy/hooks"
HOOK_PATH="$HOOK_DIR/codebuddy_turn_hook.py"
SETTINGS="$HOME/.codebuddy/settings.json"
HOOK_CMD="$PY $HOOK_PATH"

run() {
  if [ "$DRY_RUN" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

log "codebuddy-usage"
log "  repo       : $REPO"
log "  python     : $PY"
log "  config     : $CONFIG"
log "  dashboard  : http://$HOST:$PORT/"
log ""

# --------------------------------------------------------------------------- #
# 1. directories
# --------------------------------------------------------------------------- #
run mkdir -p "$CONFIG_DIR" "$BIN_DIR" "$HOOK_DIR"

# --------------------------------------------------------------------------- #
# 2. config.json (merge; never clobber unrelated keys)
# --------------------------------------------------------------------------- #
if [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] write $CONFIG (archive_root=${ARCHIVE_ROOT:-<keep>}, projects_root=${PROJECTS_ROOT:-<keep>})"
else
  "$PY" - "$CONFIG" "$ARCHIVE_ROOT" "$PROJECTS_ROOT" <<'PY'
import json, os, sys
path, archive, projects = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
if archive:
    data["archive_root"] = archive
elif "archive_root" not in data:
    data["archive_root"] = os.path.join(os.path.expanduser("~"), "codebuddy-archive")
if projects:
    data["projects_root"] = projects
elif "projects_root" not in data:
    data["projects_root"] = os.path.join(os.path.expanduser("~"), ".codebuddy", "projects")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("config ->", json.dumps(data, ensure_ascii=False))
PY
fi

# --------------------------------------------------------------------------- #
# 3. install launchers (symlink by default, so repo updates apply immediately)
# --------------------------------------------------------------------------- #
link() {
  local src="$1" dst="$2"
  if [ "$COPY_MODE" = 1 ]; then
    run cp -f "$src" "$dst"
    run chmod +x "$dst"
  else
    run rm -f "$dst"
    run ln -s "$src" "$dst"
  fi
}
link "$REPO/bin/codebuddy-usage" "$BIN_DIR/codebuddy-usage"
link "$REPO/bin/codebuddy-dashboard" "$BIN_DIR/codebuddy-dashboard"
link "$REPO/hooks/codebuddy_turn_hook.py" "$HOOK_PATH"
run chmod +x "$REPO/hooks/codebuddy_turn_hook.py" "$REPO/bin/codebuddy-usage" "$REPO/bin/codebuddy-dashboard"

# --------------------------------------------------------------------------- #
# 4. register CodeBuddy hooks (idempotent merge)
# --------------------------------------------------------------------------- #
if [ "$NO_HOOKS" = 1 ]; then
  log "hooks      : skipped (--no-hooks)"
elif [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] merge hooks into $SETTINGS (command: $HOOK_CMD)"
else
  mkdir -p "$(dirname "$SETTINGS")"
  "$PY" - "$SETTINGS" "$HOOK_CMD" <<'PY'
import json, os, sys, time
path, command = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}

hooks = data.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    data["hooks"] = hooks

changed = False
# event, matcher (None = match everything), timeout in ms
SPEC = (
    ("UserPromptSubmit", None, 120),
    ("Stop", None, 120),
    # AskUserQuestion / ExitPlanMode panels always block on a human, and the
    # permission_prompt notification is the only event that fires while they are
    # open; PreToolUse fires right after the human responds and retires the signal.
    ("PreToolUse", "AskUserQuestion|ExitPlanMode", 10),
    ("Notification", "permission_prompt|idle_prompt", 10),
)
for event, matcher, timeout in SPEC:
    groups = hooks.get(event)
    if not isinstance(groups, list):
        groups = []
        hooks[event] = groups
    # Identify our entry by script name, not the full command, so an interpreter
    # path change updates in place instead of adding a duplicate.
    target = None
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if isinstance(hook, dict) and "codebuddy_turn_hook.py" in str(hook.get("command", "")):
                target = (group, hook)
                break
        if target:
            break
    if target:
        group, hook = target
        if hook.get("command") != command:
            hook["command"] = command
            changed = True
        if hook.get("timeout") != timeout:
            hook["timeout"] = timeout
            changed = True
        if matcher is None:
            if "matcher" in group:
                group.pop("matcher")
                changed = True
        elif group.get("matcher") != matcher:
            group["matcher"] = matcher
            changed = True
    else:
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if matcher is not None:
            entry["matcher"] = matcher
        groups.append(entry)
        changed = True

if changed:
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        with open(path, encoding="utf-8") as src, open(backup, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print("backup ->", backup)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("hooks registered ->", path)
else:
    print("hooks already registered (no change)")
PY
fi

# --------------------------------------------------------------------------- #
# 5. background service (systemd --user on Linux, launchd on macOS)
# --------------------------------------------------------------------------- #
LABEL="com.pukerwonderland.codebuddy-dashboard"

# Wait briefly for the dashboard to bind, so the installer's own checks and the
# user's first browser hit do not race the service restart.
wait_for_port() {
  "$PY" - "$HOST" "$PORT" <<'PY' || true
import socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
for _ in range(30):
    try:
        with socket.create_connection((target, port), timeout=0.4):
            print(f"port {port} reachable")
            raise SystemExit(0)
    except OSError:
        time.sleep(0.2)
print(f"port {port} not reachable yet (service may still be starting)")
PY
}

install_linux_service() {
  local unit_src="$REPO/systemd/codebuddy-dashboard.service.in"
  local unit_dir="$HOME/.config/systemd/user"
  local unit="$unit_dir/codebuddy-dashboard.service"
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; start manually: codebuddy-dashboard run --host $HOST --port $PORT"
    return 0
  fi
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "systemd user session unavailable; start manually: codebuddy-dashboard run --host $HOST --port $PORT"
    return 0
  fi
  mkdir -p "$unit_dir"
  sed -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PY|g" \
      -e "s|__HOST__|$HOST|g" -e "s|__PORT__|$PORT|g" \
      "$unit_src" > "$unit"
  systemctl --user daemon-reload
  systemctl --user enable --now codebuddy-dashboard >/dev/null 2>&1 || \
    warn "could not enable/start; run: systemctl --user status codebuddy-dashboard"
  log "service    : codebuddy-dashboard.service (systemd --user)"
  wait_for_port
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" >/dev/null 2>&1 || \
      warn "could not enable linger (boot without login needs it)"
  fi
}

install_macos_service() {
  local plist_src="$REPO/launchd/$LABEL.plist.in"
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  local log_dir="$HOME/Library/Logs"
  mkdir -p "$HOME/Library/LaunchAgents" "$log_dir"
  sed -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PY|g" \
      -e "s|__HOST__|$HOST|g" -e "s|__PORT__|$PORT|g" \
      -e "s|__LOG__|$log_dir|g" \
      "$plist_src" > "$plist"
  launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
  sleep 1
  # bootstrap can transiently fail right after bootout; retry once, then fall
  # back to the legacy loader.
  if launchctl bootstrap "gui/$UID" "$plist" >/dev/null 2>&1 \
     || { sleep 1; launchctl bootstrap "gui/$UID" "$plist" >/dev/null 2>&1; }; then
    launchctl enable "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    launchctl kickstart -k "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    log "service    : $LABEL (launchd, gui/$UID)"
  elif launchctl load -w "$plist" >/dev/null 2>&1; then
    log "service    : $LABEL (launchd, legacy load)"
  else
    warn "could not load LaunchAgent; run: launchctl load -w \"$plist\""
  fi
  wait_for_port
}

if [ "$NO_SERVICE" = 1 ]; then
  log "service    : skipped (--no-service)"
elif [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] install and start background service for $OS_NAME ($HOST:$PORT)"
elif [ "$OS_NAME" = "Darwin" ]; then
  install_macos_service
else
  install_linux_service
fi

# --------------------------------------------------------------------------- #
# 6. summary
# --------------------------------------------------------------------------- #
log ""
if [ "$DRY_RUN" = 0 ]; then
  mkdir -p "$CONFIG_DIR"
  if [ ! -f "$CONFIG_DIR/latest-turn.json" ]; then
    log "tip: complete one CodeBuddy turn to populate the usage ledger."
  fi
fi
log "done."
log "  dashboard : http://$HOST:$PORT/"
LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
[ -n "${LAN_IP:-}" ] || LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${LAN_IP:-}" ] && log "  LAN       : http://$LAN_IP:$PORT/"
log "  summary   : codebuddy-dashboard summary"
log "  usage     : codebuddy-usage [latest|summary|json]"
if [ "$OS_NAME" = "Darwin" ]; then
  log "  restart   : launchctl kickstart -k gui/$UID/$LABEL"
  log "  logs      : tail -f ~/Library/Logs/codebuddy-dashboard.err.log"
else
  log "  restart   : systemctl --user restart codebuddy-dashboard"
fi
