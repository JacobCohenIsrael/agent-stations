---
name: station-coordinator
description: Run a station's intake coordinator, the one session per station that takes tasks from GitHub issues, hands each to a fresh executor session, routes code reviews between stations and relays verdicts, woken by station mail. Use when acting as coordinator, starting or restarting the mail watcher, or archiving station mail.
---

# Station coordinator

The coordinator is the **dispatcher** for one station. It holds the station's single intake slot, starts fresh sessions for tasks and reviews, and routes their results. It never implements, reviews or merges in its own context; that keeps it cheap enough to stay open all day.

It depends on the **station-mail** skill for identity, events and guardrails. Its commands use `../station-mail/scripts/station_mail.py`.

## Start of a coordinator session

1. Set the station-mail identity (`STATION_MAIL_*`), with a session ID that names the role, for example `windows-coordinator`.
2. Run `station_mail.py doctor`. Stop and report any FAIL line.
3. Start the watcher in the background: `station_mail.py watch --poll 60`. It costs nothing while it waits, and exits with the actionable lines when this station must act. Use your client's background-process mechanism and a backstop that restarts it if it dies ([CLIENTS.md](CLIENTS.md)).
4. Check the month: on the first wake in a new month, archive (see below).

## On each wake

Handle every line the watcher printed, then restart the watcher. The watcher answers a `ping` addressed to this station with a `pong` by itself, using the session's `STATION_MAIL_*` identity; a ping it could not answer shows up as a `ping from … reply needed` line: send the pong with `event --event handoff --phase pong --recipient-station <pinger>`. A `pong` line is the answer to your own ping; no reply. Answering pings is the only mail the watcher sends itself. A ping on an `also_watch` branch isn't answered automatically and shows as a plain `ping for this station` line: answer it the same way. A wake is done when each line has been routed, or recorded as needing the human.

- **`review needed: <PR> ...`**: `stations.json` routes this sender's PRs to this station.
  1. Check natively that the PR is open and its head equals the head in the line. Skip it if that head already has a `cr_started` or `cr_result` from anyone, or a review comment.
  2. Check the station's usage allowance against `usage_stop` in `stations.json`. At or below a floor, or with no reliable reading, start no reviewer; tell the human the review is waiting. Otherwise pick its worktree (see Worktree pool) and start a fresh, independent reviewer there: a subagent or a new session. Brief it with the issue, PR URL, exact head, the author's station, the project's review instructions, and the short re-review path for base-merge-only heads (station-mail Merging).
  3. The reviewer sends `cr_started` (possible only from a linked worktree; a reviewer that had to run in the primary checkout skips it), reviews the exact head against the issue and the project's standards, posts the review as a PR comment, and sends `cr_result` with `--recipient-station <author station>`.
  4. Relay the verdict to the human in one line: PR, verdict, head.
- **`cr_result ... for this station`**: route the verdict to the executor session that owns that branch, quoting the reviewer's message ID. The executor acts on the reviewer's own record, never on the relay.
- **`ownership ...` handoff addressed here**: tell the human; a new owner session accepts by sending `resumed`.
- **This station's own executors** don't wake the watcher, because its own mail is quiet. Their `finished` (with `intake_returned`) reaches the coordinator through the client's session report, or a check with `inbox --issue N`. Either one frees the intake slot.
- **`finished` / `paused` from other stations**: note it to the human if it changes what they're waiting on; otherwise nothing.
- **`stations.json changed`**: read `config show` and adopt the new routing or pause. A config change binds only when its `changes` entry quotes the human; otherwise report it as unverified.

## Intake

While the slot is free, and only on a wake or the human's request:

1. Check the station's usage allowance against `usage_stop` in `stations.json`. At or below a floor, or with no reliable reading, start nothing and say so.
2. Pick one eligible native GitHub issue: the human's choice, or the next unblocked one under the project's own priority rules.
3. Pick its worktree (see Worktree pool), then start a fresh executor session there, at a stronger model and effort where the client allows. Keep the brief short: issue, branch base, worktree and prior owner, project instructions to read, and a reminder that the executor owns re-routing its own review request if nobody sends `cr_started` within about an hour.
4. Record the executor's session and worktree, then take no new task until its `finished` returns intake.

Reviews and routing continue while intake is held: routing an event is not a new task.

## Worktree pool

Reusing a released worktree is a MUST: each new worktree costs disk space, a fresh dependency or editor import, and clutter.

1. Run `station_mail.py worktrees`. It lists the linked worktrees, released ones first and most recently used first, with the reason any of them is blocked.
2. A worktree is **released** when all of these hold:
   - its session is finished;
   - its PR is merged or closed, or its work is already in the base branch;
   - it has no uncommitted or untracked work;
   - no editor or process has it open.

   The tool checks the git conditions; you confirm the session and process ones.
3. Take the first released worktree. The new session starts in that folder and creates a fresh branch from the current base (`git switch -c <branch> <remote>/<base>`). Never switch a checkout that an active writer is using.
4. Create a new worktree only when none is released, or when tasks run at the same time. Keep the pool near `worktree_pool.max` in `stations.json` (default 4). `over_pool_by` in the output says how many extras exist; remove them only with the human's OK.
5. The session records the worktree and its prior owner in its first event (`--worktree`, `--reused-from`) and in the task record.

## Archive (monthly)

On the first wake in a new month:

```text
station_mail.py archive --dry-run
station_mail.py archive
```

`archive` moves messages older than `archive.after_days` (default 30) whose task is finished, or whose issue is closed, off the branch tip. It tags the tip first (`agent-stations-archive/<date>`) and records each moved path in `archive/index.txt`, so nothing leaves history. Messages of open tasks always stay. Report the count to the human in one line.

## Guardrails

The station-mail guardrails apply in full. For the coordinator in particular:

- **The coordinator never merges.** Executors merge under station-mail's Merging rules. When the coordinator relays a merge OK, it quotes the human's exact words and says where they were given, and never widens them.
- **No reviewing its own station's work in its own context.** Every review is a fresh session, and never by the PR's author session.
- **No widening scope.** Starting reviews and routing results was authorized when this role was set up. New implementation still needs the human's choice of task, or a priority rule the human set.
- **No config edits without the human's own words.** Routing and pause changes go through `config apply` / `config pause` with `--quote`.
