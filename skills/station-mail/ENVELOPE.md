# Station mail envelope

One message is one JSON file at `messages/<lowercase login>/<agent>/<id>.json` on the mailbox branch, written as UTF-8 with LF line endings, never edited after it lands. `station_mail.py event` builds and validates it; `send --message-file` accepts a hand-built one.

```json
{
  "schema_version": 1,
  "type": "STATUS",
  "id": "jacobcohenisrael-claude-issue42-review-requested-ready-for-cr-1a2b3c4d",
  "created_at_utc": "2026-11-27T09:00:00+00:00",
  "from_agent": "claude",
  "to_agent": "all",
  "actor": {
    "id": "jacobcohenisrael/claude",
    "person": "Jacob",
    "github_login": "jacobcohenisrael",
    "agent": "claude",
    "station": "windows",
    "display_name": "Jacob's Claude",
    "session_id": "windows-issue42"
  },
  "task": {
    "issue": 42,
    "issue_url": "https://github.com/owner/repo/issues/42",
    "event": "review_requested",
    "phase": "ready_for_cr",
    "branch": "claude/42-fix",
    "base": "<40-hex merge-base with the base branch>",
    "head": "<40-hex exact head>",
    "review_head": "<same as head>",
    "pr_url": "https://github.com/owner/repo/pull/43"
  },
  "text": "Ready for CR: ... checks run: ..."
}
```

## Fields

- `actor.id` is `<lowercase login>/<agent>`. It identifies the executor, not the account that pushed the commit.
- `actor.station` names the sending machine. It must be a station in `stations.json`. Messages written before stations existed have no `station`; readers treat them as coming from an unknown station.
- `task.issue_url` must point at the repo in `stations.json`.
- `task.base` and `task.head` are exact 40-character commit IDs. Capture `base` at claim time and `head` when the event happens.
- `to_agent` is a broadcast route (`all`). Name a specific receiver with `task.recipient_actor` and `task.recipient_station`.
- `id` defaults to `<login>-<agent>-issue<N>-<event>[-<phase>]-<head8>`. Resending the same id with the same payload is a no-op, so retries are safe. A different payload under a used id is refused, including ids that were archived.

## Events and phases

Valid events: `started`, `resumed`, `paused`, `handoff`, `review_requested`, `finished`. A phase refines an event:

| Phase | Event | Required extras |
| --- | --- | --- |
| `ready_for_cr` | `review_requested` | `pr_url`; `review_head` = `head` |
| `cr_started` | `started` | `review_head` = `head` (the PR head); actor is the reviewer |
| `cr_result` | `handoff` | `verdict` (`approved`, `changes_required`, `blocked`); `review_head` = `head`; `recipient_station` of the author |
| `repair`, `integration`, `manual_test` | `resumed` | cite the review or merge the resumption answers |
| `human_review`, `blocked`, `usage` | `paused` | the human or action needed, the exact state |
| `ownership` | `handoff` | `recipient_actor`, `recipient_station`; the receiver sends `resumed` once it accepts |

`finished` may carry `intake_returned: true` (`--intake-returned`), meaning the whole task is delivered and the coordinator may take a new one. A reviewer's work ends with a `cr_result` handoff, never `finished`: reviewing doesn't deliver the task.

## Branch layout

```text
stations.json          shared config (see SETUP.md)
README.md              what the branch is
messages/...           one file per message
archive/index.txt      "<path>\t<tag>" for each message moved off the tip
```

Archived messages stay readable through their tag: `git show agent-stations-archive/<date>:<path>`.
