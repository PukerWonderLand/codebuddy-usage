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
- **"It is waiting on you" signals**: when CodeBuddy opens a question or
  plan-approval panel, or sits idle mid-turn, a `_等待回答.md` / `_等待批准.md` /
  `_等待输入.md` file appears in the session archive folder with the question text
  and options. It is written the instant the panel opens and removed as soon as
  you respond, so you can see it without looking at the terminal. Only turns that
  truly finish produce an archive document; interrupted turns are accounted in
  the ledger but never archived mid-conversation.
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
4. Registers the `UserPromptSubmit` / `Stop` hooks, plus matcher-scoped
   `PreToolUse` (retiring a panel signal) and `Notification`
   (`permission_prompt` / `idle_prompt` signals) hooks in
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
├── 审计层/<session-id>.jsonl   # raw session log mirror
└── _等待回答.md            # "waiting on you" signal, see below
```

### Waiting-on-you signals (`_等待回答.md` / `_等待批准.md` / `_等待输入.md`)

When CodeBuddy stops and waits for a human, one transient file appears in the
session folder — never more than one at a time:

| File | Triggered by | Content |
| :--- | :--- | :--- |
| `_等待回答.md` | the instant an `AskUserQuestion` panel opens | question, every option, raw tool call |
| `_等待批准.md` | the instant an `ExitPlanMode` panel opens | the plan text |
| `_等待输入.md` | a turn still open but quiet for 60s (catch-all) | note that the session is waiting |

The file is **removed the moment you respond** (via `PreToolUse`) and also
cleaned up when the turn ends, so "the file exists" means "it is still waiting".
Because a panel's text reaches the log a fraction of a second after the panel
itself, the signal is written immediately and enriched with the text ~1.2s later.

Only turns that truly finish are written into `阅读层`. A turn interrupted with
Esc, superseded before its question was answered, or stranded on a tool call or
tool result is recorded in the ledger and the audit layer only — never as
half-finished Markdown mid-conversation; the ledger marks it
`interrupted_pending_question` or `superseded_catchup`.

Conversely, when a turn *did* finish but its `Stop` event never arrived (process
killed, hook removed), the next prompt repairs it from the log:
`turn_status: recovered`, with `recovered: true` in the front matter. The
evidence is taken from the log itself — every tool call in the turn has a result
and the turn ends on a `status: completed` answer — so a stream the user cut off
(marked `status: incomplete`) is never mistaken for an answer.

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
