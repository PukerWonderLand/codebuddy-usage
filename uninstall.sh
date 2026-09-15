#!/usr/bin/env bash
# codebuddy-usage uninstaller.
#
# Removes the launchers, the CodeBuddy hook entries this project registered, and
# the systemd user service. Data is kept unless --purge is given.
#
#   ./uninstall.sh [--purge] [--dry-run]

set -euo pipefail

PURGE=0
DRY_RUN=0

SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
REPO="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"

log() { printf '%s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) warn "unknown option: $1"; shift ;;
  esac
done

run() {
  if [ "$DRY_RUN" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

command -v python3 >/dev/null 2>&1 || { warn "python3 not found"; exit 1; }
PY="$(command -v python3)"

# --------------------------------------------------------------------------- #
# 1. background service (systemd --user on Linux, launchd on macOS)
# --------------------------------------------------------------------------- #
if [ "$(uname -s)" = "Darwin" ]; then
  LABEL="com.pukerwonderland.codebuddy-dashboard"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ -f "$PLIST" ]; then
    run launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    run launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
    run rm -f "$PLIST"
    log "service removed"
  fi
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  run systemctl --user disable --now codebuddy-dashboard >/dev/null 2>&1 || true
  run rm -f "$HOME/.config/systemd/user/codebuddy-dashboard.service"
  run systemctl --user daemon-reload || true
  log "service removed"
fi

# --------------------------------------------------------------------------- #
# 2. launchers (only if they resolve into this repository)
# --------------------------------------------------------------------------- #
for dst in "$HOME/.local/bin/codebuddy-usage" "$HOME/.local/bin/codebuddy-dashboard" \
           "$HOME/.codebuddy/hooks/codebuddy_turn_hook.py"; do
  if [ -L "$dst" ]; then
    target="$(readlink "$dst")"
    case "$target" in
      "$REPO"/*) run rm -f "$dst"; log "removed link $dst" ;;
      *) warn "keeping $dst (points outside this repo: $target)" ;;
    esac
  elif [ -f "$dst" ]; then
    warn "keeping regular file $dst (not a symlink created by install.sh)"
  fi
done

# --------------------------------------------------------------------------- #
# 3. hook entries in settings.json
# --------------------------------------------------------------------------- #
SETTINGS="$HOME/.codebuddy/settings.json"
if [ -f "$SETTINGS" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] strip codebuddy_turn_hook entries from $SETTINGS"
  else
    "$PY" - "$SETTINGS" <<'PY'
import json, os, sys, time
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
hooks = data.get("hooks")
if isinstance(hooks, dict):
    # Every event we might have registered, discovered rather than hardcoded so
    # the list cannot go stale as the hook grows more entry points.
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        cleaned = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            kept = [h for h in group.get("hooks", [])
                    if not (isinstance(h, dict) and "codebuddy_turn_hook.py" in str(h.get("command", "")))]
            if kept:
                group["hooks"] = kept
                cleaned.append(group)
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
with open(path, encoding="utf-8") as src, open(backup, "w", encoding="utf-8") as dst:
    dst.write(src.read())
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("settings updated ->", path)
PY
  fi
fi

# --------------------------------------------------------------------------- #
# 4. data
# --------------------------------------------------------------------------- #
if [ "$PURGE" = 1 ]; then
  run rm -rf "$HOME/.codebuddy-usage" "$HOME/.codebuddy-turn-state"
  log "data removed (--purge)"
else
  log "data kept: ~/.codebuddy-usage, ~/.codebuddy-turn-state (use --purge to delete)"
fi

log "done."
