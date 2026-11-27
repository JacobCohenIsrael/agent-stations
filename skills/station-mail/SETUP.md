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

`join` stores the station in the clone's local git config, which every linked worktree of that clone shares. When two stations share one clone (for example Claude and Codex on one PC), give each its own: `join --station codex --worktree` in each of that agent's worktrees, or set `STATION_MAIL_STATION` in its sessions. `$STATION_MAIL_STATION` wins over a worktree's value, which wins over the clone's. `doctor` warns when the station comes from the clone-wide value. `doctor` checks Python, git, the mailbox, `stations.json`, the station name, identity variables, the linked worktree, and gh authentication. FAIL lines block mail; warn lines are worth fixing.

Watcher state (the last-seen commit for each watched branch) lives in `<git common dir>/agent-stations/state-<station>.json`, one file per station, outside the working tree, so stations sharing a clone never consume each other's mail. A station's first run starts from an older single `state.json` if one exists. With no state yet, or if the recorded commit is gone, `watch` reports the last 24 hours of mail. Expect one catch-up wake on a new machine.

## stations.json

```json
{
  "schema_version": 1,
  "repo": "owner/name",
  "base_branch": "main",
  "paused": false,
  "pause_reason": "",
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
  "worktree_pool": {"max": 4},
  "merge_policy": {
    "low_risk": ["test fixes", "CI or tooling fixes", "copy or text tweaks", "small cosmetic tweaks", "docs"],
    "needs_human": ["gameplay tuning", "controls", "game feel", "look", "sound", "UX flow", "narrative",
                    "new dependencies", "save-format changes", "releases"]
  },
  "also_watch": [],
  "changes": []
}
```

- `review_routing`: each rule matches the sender `login/agent@station` (shell-style `*` wildcards) and names the station whose coordinator reviews that sender's PRs. The first match wins. A sender that matches no rule gets no automatic reviewer, so end the list with a catch-all `"*"` if every PR should be routed.
- `watch.always_from`: senders whose every message wakes this coordinator. `notify_events`: events from other stations that wake it, `finished` and `paused` by default.
- `worktree_pool.max`: how many linked worktrees a station keeps for reuse (see the station-coordinator skill).
- `merge_policy`: which kinds of change merge on an approved review alone (`low_risk`), and which also need the human's OK (`needs_human`). Edit the lists to fit the project; when a change fits neither, ask the human.
- `usage_stop`: provider allowance floors that sessions honour before starting work. The tool stores them; agents read them.
- `also_watch`: extra `{"branch", "messages_dir"}` mailboxes that `watch` and `inbox` read, for migrating from an older mail branch. Each branch may appear once and never repeats the mailbox branch itself, because each branch has one read cursor. Old messages have no station, so their sender key ends in `@?`. During the transition, add routing rules such as `{"from": "jacobcohenisrael/claude@?", "reviewer": "windows"}`. Legacy mail about a branch checked out on this machine always counts as this station's own. An entry may also carry `session_stations`, which maps session-id patterns to stations, for example `{"windows-*": "windows", "mac-*": "mac"}`. The first matching pattern names the missing station. Legacy mail about a branch checked out here is still handled as above, first. Otherwise review routing tries the named station's key (for example `jacobcohenisrael/claude@mac`) and, when no rule matches it, falls back to the `@?` rules, so transition routes keep working. Mail named as this station's own never falls back, so it can't route back here.
- `changes`: an append-only record of each config change (`at`, `by`, `quote`), written by `config apply`, `config pause` and `config resume`.

Change config only on the human's own direct instruction, and quote it with `--quote`. `config apply` takes an edited copy of `config show`. If anyone changed `stations.json` in the meantime, it refuses, so run `config show` again and redo the edit. `config pause --reason ... --quote ...` stops admission events on every station, and `config resume --quote ...` lifts the pause. Both act on the latest config, and senders re-check the pause on every append attempt.

## Pre-push reminder (optional)

Add this to a pre-push hook so pushing a branch that's in review reminds the executor to re-request review:

```sh
while read local_ref local_sha remote_ref remote_sha; do
  python station_mail.py review-reminder --branch-name "${remote_ref#refs/heads/}" --head "$local_sha"
done
```

It reads only the last-fetched mailbox ref, so it needs no network access and never blocks the push.
