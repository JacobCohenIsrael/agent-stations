# Station mail setup

## Once per repository

Run `station_mail.py` from any clone of the repository, with push access:

```text
python station_mail.py init --stations windows,mac --base-branch main
python station_mail.py config show > stations.json      # edit locally, outside the repo
python station_mail.py config apply --file stations.json --quote "<the human's instruction>" --login <login> --agent <agent>
```

`init` creates the orphan branch `agent-stations`, refusing if it already exists, holding a starter `stations.json` and a README. It infers `owner/name` from the remote URL; pass `--repo` when it can't. Use `--branch <name>` on every command (or `git config stations.branch <name>` per clone) to give the mailbox a different name.

## Once per machine (each clone)

```text
python station_mail.py join --station mac
python station_mail.py doctor
```

`join` stores the station in the clone's local git config, which every linked worktree of that clone shares. `doctor` checks Python, git, the mailbox, `stations.json`, the station name, identity variables, the linked worktree, and gh authentication. FAIL lines block mail; warn lines are worth fixing.

Per-machine watcher state (the last-seen commit for each watched branch) lives in `<git common dir>/agent-stations/state.json`, outside the working tree.

## stations.json

```json
{
  "schema_version": 1,
  "repo": "owner/name",
  "base_branch": "main",
  "paused": false,
  "pause_reason": "",
  "require_linked_worktree": true,
  "stations": {
    "windows": {"description": "Jacob's Windows PC: Claude coordinator"},
    "mac": {"description": "Jacob's MacBook: Claude"}
  },
  "review_routing": [
    {"from": "jacobcohenisrael/claude@windows", "reviewer": "mac"},
    {"from": "*@mac", "reviewer": "windows"},
    {"from": "phillips-login/*", "reviewer": "windows"}
  ],
  "watch": {"always_from": ["*/codex@*"], "notify_events": ["finished", "paused"]},
  "usage_stop": {"claude": {"weekly_remaining_percent": 5, "five_hour_remaining_percent": 10}},
  "archive": {"after_days": 30},
  "also_watch": [],
  "changes": []
}
```

- `review_routing`: each rule matches the sender `login/agent@station` (shell-style `*` wildcards) and names the station whose coordinator reviews that sender's PRs. The first match wins. A sender that matches no rule gets no automatic reviewer, so end the list with a catch-all `"*"` if every PR should be routed.
- `watch.always_from`: senders whose every message wakes this coordinator. `notify_events`: events from other stations that wake it, `finished` and `paused` by default.
- `usage_stop`: provider allowance floors that sessions honour before starting work. The tool stores them; agents read them.
- `also_watch`: extra `{"branch", "messages_dir"}` mailboxes that `watch` reads, for migrating from an older mail branch.
- `changes`: an append-only record of each config change (`at`, `by`, `quote`), written by `config apply`, `config pause` and `config resume`.

Change config only on the human's own direct instruction, and quote it with `--quote`. `config pause --reason ... --quote ...` stops admission events on every station; `config resume --quote ...` lifts the pause.

## Pre-push reminder (optional)

Add this to a pre-push hook so pushing a branch that's in review reminds the executor to re-request review:

```sh
while read local_ref local_sha remote_ref remote_sha; do
  python station_mail.py review-reminder --branch-name "${remote_ref#refs/heads/}" --head "$local_sha"
done
```

It reads only the last-fetched mailbox ref, so it needs no network access and never blocks the push.
