"""Portable path configuration for codebuddy-usage.

Resolution order (first match wins):

1. An explicit environment variable (``CODEBUDDY_*``).
2. An entry in the JSON config file (default ``~/.codebuddy-usage/config.json``,
   relocatable via ``CODEBUDDY_USAGE_CONFIG``).
3. A home-relative default.

Keeping every path overridable is what lets the project run on any machine
without editing source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".codebuddy-usage" / "config.json"


def config_path() -> Path:
    override = os.environ.get("CODEBUDDY_USAGE_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_config() -> dict:
    path = config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve(key: str, env: str, default: Path) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    value = load_config().get(key)
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return default


# --------------------------------------------------------------------------- #
# Resolved locations
# --------------------------------------------------------------------------- #
def projects_root() -> Path:
    """CodeBuddy session logs: ``<projects_root>/<project>/<session>.jsonl``."""
    return resolve(
        "projects_root",
        "CODEBUDDY_PROJECTS_ROOT",
        Path.home() / ".codebuddy" / "projects",
    )


def usage_root() -> Path:
    """Hook ledger + this tool's runtime data (usage.jsonl, latest-turn.json)."""
    return resolve(
        "usage_root", "CODEBUDDY_USAGE_ROOT", Path.home() / ".codebuddy-usage"
    )


def state_root() -> Path:
    """Hook spool/lock state for conversation archiving."""
    return resolve(
        "state_root",
        "CODEBUDDY_CONVERSATION_ARCHIVE_STATE",
        Path.home() / ".codebuddy-turn-state",
    )


def archive_root() -> Path:
    """Where per-turn Markdown archives are written (often an SMB mount)."""
    return resolve(
        "archive_root",
        "CODEBUDDY_CONVERSATION_ARCHIVE_ROOT",
        Path.home() / "codebuddy-archive",
    )
