# Client adapters for the coordinator

The watcher is the same on every client: `station_mail.py watch --poll 60` exits 0 and prints `ACTIONABLE STATION MAIL:` lines when this station must act. A client needs three things: a way to run it in the background, a way to be woken when it exits, and a backstop that restarts it.

## Claude Code

- **Watcher**: run it with the Bash tool and `run_in_background: true`. When it exits, the session is woken with its output. After handling the wake, start it again.
- **Backstop**: a session cron every 2 hours that checks the watcher is still running and restarts it if not. Session crons expire with the session or after 7 days, so recreate both in a new coordinator session.
- **Reviewers**: a background subagent starts without the human clicking anything. Give it the worktree the pool picked: brief it to work only in that absolute path and create its review branch there. Use `isolation: "worktree"` only when the pool has no released worktree. If worktree creation fails (for example Windows path-length limits), run the reviewer read-only in the primary checkout. Reviewers there can't send `cr_started`, which is an admission event, so they post the PR comment and send `cr_result` only.
- **Executors**: a new session per task, so the human can open, steer and stop it.

## Codex

- **Watcher**: run `station_mail.py watch --once` from the client's scheduled automation (for example every 10 minutes). Exit 0 means act on the printed lines; exit 3 means nothing new.
- **Reviewers and executors**: a new task per review or issue, with the same brief contents as above.

## Any other client

Use `watch --once` from whatever scheduler the client offers, and treat exit 0 as a wake. With no scheduler, run `watch --once` when the human asks for a mail check.
