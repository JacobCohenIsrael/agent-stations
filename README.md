# agent-stations

Agent skills for coordinating AI coding agents that work on the same GitHub repository from different machines. Each machine is a **station** (for example Windows Claude, Mac Claude, a cloud Codex).

- **station-mail**: attributed task lifecycle events (started, review requested, review verdict, paused, handoff, finished) carried as commits on one orphan Git branch in your repo. It includes a single-file, stdlib-only CLI (`station_mail.py`) and a watcher that wakes a station only when it must act.
- **station-coordinator**: the intake coordinator role. One session per station takes tasks from GitHub issues, starts a fresh executor for each, routes code reviews between stations by config, relays verdicts and archives old mail monthly.

There's no server, no new credentials and no third-party dependency: Python 3.11+, git and your existing GitHub remote. `gh` is optional. It runs on Windows, macOS and Linux.

## Install

```text
npx skills add JacobCohenIsrael/agent-stations
```

Then, once per repository and once per machine, follow [skills/station-mail/SETUP.md](skills/station-mail/SETUP.md):

```text
python .agents/skills/station-mail/scripts/station_mail.py init --stations windows,mac
python .agents/skills/station-mail/scripts/station_mail.py join --station windows
python .agents/skills/station-mail/scripts/station_mail.py doctor
```

## Guardrails built in

- Merges need the human's own OK, given to the merging session; relayed OKs don't count.
- No self-approval: every review is a separate session's own verdict, pinned to the exact PR head.
- No credentials in mail or config.
- Mail and config can't grant permission; only the human can.
- Task writes stay in linked worktrees, and the primary checkout stays read-only.
- A pause switch in `stations.json` stops new work on every station at once.

## Develop

```text
python -m unittest discover -s tests
```

The tests use throwaway local bare Git remotes only.
