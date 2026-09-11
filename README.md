# codebuddy-usage

Local-first **token usage dashboard and conversation Markdown archiver for
CodeBuddy**. Pure Python standard library — no third-party dependencies — and a
front-end that needs no external assets, so it works on an air-gapped LAN.

It is the CodeBuddy counterpart of
[`codex-usage`](https://github.com/PukerWonderLand/codex-usage) and
[`codex-durable-archive`](https://github.com/PukerWonderLand/codex-durable-archive).

## Features

- **LAN web dashboard**: total tokens, input, cache hit rate, output, reasoning,
  billing credit, session count; daily trend, per-model / per-project
  breakdown, session and per-turn detail, archive log.
- **Conversation archiving**: at the end of every turn, writes the verbatim
  "user prompt + final answer" Markdown into an archive root (typically an SMB
  share), and mirrors the raw session JSONL as an audit layer, with SHA-256 and
  atomic writes.
- **Turn start/end token accounting**: `UserPromptSubmit` records a baseline,
  `Stop` computes the turn delta and session total and surfaces them in the UI.
- **Full history backfill**: parses `~/.codebuddy/projects/**/*.jsonl` directly,
  so all existing sessions are included from the first start.
- **Zero dependencies**: Python 3.10+ only. No Node, no database, no network.

## Install (one command)

```bash
git clone https://github.com/PukerWonderLand/codebuddy-usage.git
cd codebuddy-usage
./install.sh
```

Point the archive at a mounted share and pick a port:

```bash
./install.sh --archive-root "/mnt/share/CodeBuddyArchive" --port 3766
```

Preview everything first:

```bash
./install.sh --dry-run
```

The installer is **idempotent** — re-running only adds what is missing and never
removes unrelated hooks or settings in `~/.codebuddy/settings.json`.

### What the installer does

1. Checks for `python3 >= 3.10`.
2. Writes/merges `~/.codebuddy-usage/config.json`.
3. Symlinks `codebuddy-usage` and `codebuddy-dashboard` into `~/.local/bin`, and
   the hook into `~/.codebuddy/hooks/codebuddy_turn_hook.py`.
4. Registers the `UserPromptSubmit` / `Stop` hooks in
   `~/.codebuddy/settings.json` (backs up first; never duplicates).
5. Installs and starts the systemd **user** service
   `codebuddy-dashboard.service` on `0.0.0.0:3766`, and enables linger when
   available so it starts without an interactive login.

> Restart CodeBuddy for the hooks to take effect (hooks are snapshotted at
> startup).

## Usage

Open the dashboard (replace the host with the machine running it):

```
http://<host>:3766/
```

Inspect the ledger from the terminal:

```bash
codebuddy-usage            # most recent turn
codebuddy-usage summary    # aggregate all turns
codebuddy-usage json       # raw JSONL

codebuddy-dashboard summary            # text summary from the collector
codebuddy-dashboard run --port 3766    # run the dashboard in the foreground
```

Service management:

```bash
systemctl --user status  codebuddy-dashboard
systemctl --user restart codebuddy-dashboard
journalctl --user -u codebuddy-dashboard -f
```

## Configuration

Precedence: **environment variable > config file > home-relative default**.

The config file lives at `~/.codebuddy-usage/config.json` (relocatable via
`CODEBUDDY_USAGE_CONFIG`):

```json
{
  "archive_root": "~/codebuddy-archive",
  "projects_root": "~/.codebuddy/projects"
}
```

| Key | Environment variable | Default |
| --- | --- | --- |
| `archive_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_ROOT` | `~/codebuddy-archive` |
| `projects_root` | `CODEBUDDY_PROJECTS_ROOT` | `~/.codebuddy/projects` |
| `usage_root` | `CODEBUDDY_USAGE_ROOT` | `~/.codebuddy-usage` |
| `state_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_STATE` | `~/.codebuddy-turn-state` |

## Archive layout

```
<archive_root>/<date>/<title>__<session8>/
├── 阅读层/<turn-id>.md     # prompt + rebuilt Harness section + final answer
└── 审计层/<session-id>.jsonl   # raw session log mirror
```

### About the "Harness assembly (reconstructed)" section

The fully assembled request context (system instructions, AGENTS.md,
environment_context, permissions, skills, memory, full history) is **not
persisted locally**: session logs only store conversation events, `traces/`
holds timing spans only, and `CODEBUDDY_DEBUG_REQUEST` truncates each message to
500 characters.

That section is therefore a **reconstruction, not verbatim**, and states so:
recorded reminder blocks, a manifest of referenced context files (path, size,
SHA-256, current disk state), history scale, and an explicit list of what is
**not recoverable**. The frontmatter marks it `harness_section: reconstructed`.

Byte-exact capture would require intercepting HTTPS (CodeBuddy honors
`HTTPS_PROXY`), which is an optional, heavier add-on.

## Uninstall

```bash
./uninstall.sh            # remove hooks/service/links, keep data
./uninstall.sh --purge    # also delete ~/.codebuddy-usage, ~/.codebuddy-turn-state
```

## Requirements

- Python 3.10+ (macOS system Python is 3.9 — install 3.10+ via `brew` or
  `uv python install`)
- Linux **or** macOS. The service runs as systemd `--user` on Linux and as a
  launchd LaunchAgent (`com.pukerwonderland.codebuddy-dashboard`) on macOS.
  Without either, run `codebuddy-dashboard run` yourself.
- A writable archive directory (SMB/NFS mounts are fine)

## For AI agents

To replicate this environment on another machine, hand the repository to the
agent and point it at [`AGENTS.md`](AGENTS.md), which contains deterministic
install and verification steps.

## License

MIT
