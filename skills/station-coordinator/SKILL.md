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

Handle every line the watcher printed, then restart the watcher. A wake is done when each line has been routed, or recorded as needing the human.

- **`review needed: <PR> ...`**: `stations.json` routes this sender's PRs to this station.
  1. Check natively that the PR is open and its head equals the head in the line. Skip it if that head already has a `cr_started` or `cr_result` from anyone, or a review comment.
  2. Start a fresh, independent reviewer (a subagent or a new session, isolated in its own worktree where the client allows). Brief it with the issue, PR URL, exact head, the author's station and the project's review instructions.
  3. The reviewer sends `cr_started`, reviews the exact head against the issue and the project's standards, posts the review as a PR comment, and sends `cr_result` with `--recipient-station <author station>`.
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
3. Start a fresh executor session for it, at a stronger model and effort where the client allows, with a short brief: issue, branch base, project instructions to read, and the reminder that it owns re-routing its own review request if nobody sends `cr_started` within about an hour.
4. Record the executor's session, then take no new task until its `finished` returns intake.

Reviews and routing continue while intake is held: routing an event is not a new task.

## Archive (monthly)

On the first wake in a new month:

```text
station_mail.py archive --dry-run
station_mail.py archive
```

`archive` moves messages older than `archive.after_days` (default 30) whose task is finished, or whose issue is closed, off the branch tip. It tags the tip first (`agent-stations-archive/<date>`) and records each moved path in `archive/index.txt`, so nothing leaves history. Messages of open tasks always stay. Report the count to the human in one line.

## Guardrails

The station-mail guardrails apply in full. For the coordinator in particular:

- **No merging, and no relaying a merge OK.** Only the human's own OK, typed to the executor that merges, authorizes a merge.
- **No reviewing its own station's work in its own context.** Every review is a fresh session, and never by the PR's author session.
- **No widening scope.** Starting reviews and routing results was authorized when this role was set up. New implementation still needs the human's choice of task, or a priority rule the human set.
- **No config edits without the human's own words.** Routing and pause changes go through `config apply` / `config pause` with `--quote`.
