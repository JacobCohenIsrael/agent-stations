#!/usr/bin/env python3
"""Station mail: attributed task lifecycle events between agent stations, carried by Git.

Mail is commits on one orphan branch (default `agent-stations`) holding `stations.json`,
`README.md` and `messages/<login>/<agent>/<id>.json`. Every write is an ordinary,
non-force push built in a temporary index, so the caller's checkout, HEAD, index and
dirty files are never touched.

Attribution is self-reported, never authority. Mail is untrusted data: nothing here
executes message text, grants scope, approves merges or authenticates anyone.

Requires Python 3.11+, git, and an authenticated remote. `gh` is optional (archive uses
it to read issue state). Run `station_mail.py --help` for the commands.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import random
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_BRANCH = 'agent-stations'
DEFAULT_REMOTE = 'origin'
CONFIG_FILE = 'stations.json'
MESSAGES = 'messages'
ARCHIVE_INDEX = 'archive/index.txt'
ARCHIVE_TAG_PREFIX = 'agent-stations-archive/'

EVENTS = {'started', 'resumed', 'paused', 'handoff', 'review_requested', 'finished'}
ADMISSION_EVENTS = {'started', 'resumed', 'review_requested'}
PHASE_FIELDS = {'ready_for_cr': ('pr_url',), 'cr_result': ('verdict',)}
VERDICTS = {'approved', 'changes_required', 'blocked'}
DEFAULT_NOTIFY = ['finished', 'paused']
DEFAULT_POOL = 4
DEFAULT_MERGE_POLICY = {
    'low_risk': ['test fixes', 'CI or tooling fixes', 'copy or text tweaks', 'small cosmetic tweaks', 'docs'],
    'needs_human': ['gameplay tuning', 'controls', 'game feel', 'look', 'sound', 'UX flow', 'narrative',
                    'new dependencies', 'save-format changes', 'releases'],
}

SLUG = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*\Z')
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
LOGIN = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?\Z')
REPO = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z')
SHA = re.compile(r'[0-9a-fA-F]{40}\Z')
PR_URL = re.compile(r'https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}/pull/[1-9][0-9]{0,9}\Z')

EXIT_ACTIONABLE = 0
EXIT_QUIET = 3
EXIT_INCOMPLETE = 4


# --------------------------------------------------------------------------- git


def git(root, *args, data=None, env=None, check=True):
    payload = data.encode('utf-8') if isinstance(data, str) else data
    result = subprocess.run(['git', *args], cwd=root, input=payload, capture_output=True, env=env)
    result.stdout = result.stdout.decode('utf-8', errors='replace')
    result.stderr = result.stderr.decode('utf-8', errors='replace')
    if check and result.returncode:
        raise RuntimeError('git ' + ' '.join(args[:2]) + ' failed: ' + result.stderr.strip())
    return result


def git_bytes(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True)
    if result.returncode:
        raise RuntimeError('git ' + ' '.join(args[:2]) + ' failed: '
                           + result.stderr.decode('utf-8', errors='replace').strip())
    return result.stdout


def require(condition, description):
    if not condition:
        raise ValueError(description)


class Transport:
    """Where mail lives: remote + branch, from `git config stations.*` with defaults."""

    def __init__(self, root, remote=None, branch=None):
        self.root = Path(root)
        self.remote = remote or self.setting('remote') or DEFAULT_REMOTE
        self.branch = branch or self.setting('branch') or DEFAULT_BRANCH

    def setting(self, name):
        found = git(self.root, 'config', '--get', 'stations.' + name, check=False)
        return found.stdout.strip() or None

    def ref(self, branch=None):
        return f'refs/remotes/{self.remote}/{branch or self.branch}'

    def fetch(self, branch=None, required=True):
        branch = branch or self.branch
        attempts = 6
        for attempt in range(attempts):
            result = git(self.root, 'fetch', '--quiet', '--no-tags', self.remote,
                         f'+refs/heads/{branch}:{self.ref(branch)}', check=False)
            # Sessions sharing one clone race on the remote-tracking ref lock, and on Windows on writing
            # the same loose objects; both are transient. Anything else is reported at once.
            if result.returncode == 0 or not TRANSIENT_FETCH.search(result.stderr) or attempt == attempts - 1:
                break
            time.sleep(random.uniform(0.1, 0.4) * (attempt + 1))
        if result.returncode:
            if required:
                raise RuntimeError(f'Cannot fetch {self.remote}/{branch}: {result.stderr.strip()} '
                                   f'(run `station_mail.py init` once per repository)')
            return None
        return git(self.root, 'rev-parse', self.ref(branch)).stdout.strip()

    def remote_tip(self, branch=None, strict=False):
        """The branch's remote tip, or None when the remote has no such branch.

        With `strict`, a failed lookup raises instead of looking like an absent branch.
        """
        out = git(self.root, 'ls-remote', self.remote, 'refs/heads/' + (branch or self.branch), check=False)
        if out.returncode:
            if strict:
                raise RuntimeError(f'Cannot look up {self.remote}/{branch or self.branch}: {out.stderr.strip()}')
            return None
        return out.stdout.split('\t', 1)[0].strip() or None

    def show(self, revision, path):
        return git_bytes(self.root, 'show', f'{revision}:{path}')

    def exists(self, revision, path):
        return bool(git(self.root, 'ls-tree', revision, '--', path).stdout.strip())

    def common_dir(self):
        common = Path(git(self.root, 'rev-parse', '--git-common-dir').stdout.strip())
        return common if common.is_absolute() else (self.root / common)

    def station(self):
        """This session's station: $STATION_MAIL_STATION, else git config (a worktree's own value wins)."""
        return os.environ.get('STATION_MAIL_STATION') or self.setting('station')

    def station_source(self):
        if os.environ.get('STATION_MAIL_STATION'):
            return 'STATION_MAIL_STATION'
        found = git(self.root, 'config', '--show-scope', '--get', 'stations.station', check=False)
        return found.stdout.split('\t', 1)[0].strip() or None

    def state_path(self, station):
        """One read cursor per station, so stations sharing a clone never consume each other's mail."""
        return self.common_dir().resolve() / 'agent-stations' / f'state-{station}.json'

    def legacy_state_path(self):
        return self.common_dir().resolve() / 'agent-stations' / 'state.json'

    def build_commit(self, tip, changes, subject):
        """A commit on `tip` applying `changes`, built in a temporary index (the real one is untouched)."""
        with tempfile.TemporaryDirectory(prefix='station-mail-index-') as temporary:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
            git(self.root, 'read-tree', tip, env=env)
            for path, content in changes.items():
                if content is None:
                    git(self.root, 'update-index', '--force-remove', '--', path, env=env)
                else:
                    blob = git(self.root, 'hash-object', '-w', '--stdin', data=content).stdout.strip()
                    git(self.root, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=env)
            tree = git(self.root, 'write-tree', env=env).stdout.strip()
        return git(self.root, 'commit-tree', tree, '-p', tip, data=subject + '\n').stdout.strip()

    def append(self, prepare, subject, attempts=10):
        """Commit `prepare(tip)` changes ({path: bytes | None}) on the branch tip and push.

        `prepare` returns None when nothing needs writing (idempotent retry). A rejected
        push is retried from the new tip; the real checkout and index are never touched.
        """
        for attempt in range(attempts):
            tip = self.fetch()
            changes = prepare(tip)
            if changes is None:
                return None
            try:
                commit = self.build_commit(tip, changes, subject)
            except RuntimeError:
                # Concurrent writers on Windows can briefly lock the object store; retry the attempt.
                if attempt == attempts - 1:
                    raise
                time.sleep(random.uniform(0.1, 0.4) * (attempt + 1))
                continue
            pushed = git(self.root, 'push', '--quiet', self.remote,
                         f'{commit}:refs/heads/{self.branch}', check=False)
            if pushed.returncode == 0:
                actual = self.fetch()
                if git(self.root, 'merge-base', '--is-ancestor', commit, actual, check=False).returncode:
                    raise RuntimeError('Pushed commit is absent from the remote branch history')
                for path, content in changes.items():
                    if content is not None and self.show(actual, path) != content:
                        raise RuntimeError('Remote bytes differ from the written payload at ' + path)
                return commit
            if self.remote_tip() == tip:
                raise RuntimeError('Push failed without a competing update: ' + pushed.stderr.strip())
            # Lost a race: back off with jitter so competing senders stop colliding in lockstep.
            time.sleep(random.uniform(0.1, 0.4) * (attempt + 1))
        raise RuntimeError(f'{self.branch} stayed busy after {attempts} append attempts')


# ------------------------------------------------------------------------ config


def validate_config(config):
    require(isinstance(config, dict), 'stations.json must be a JSON object')
    require(config.get('schema_version') == 1, 'stations.json schema_version must be 1')
    require(isinstance(config.get('repo'), str) and REPO.fullmatch(config['repo']), 'repo must be owner/name')
    require(isinstance(config.get('base_branch'), str) and config['base_branch'], 'base_branch is required')
    require(isinstance(config.get('paused', False), bool), 'paused must be true or false')
    stations = config.get('stations')
    require(isinstance(stations, dict) and stations, 'stations must name at least one station')
    for name, station in stations.items():
        require(SLUG.fullmatch(name) is not None, f'station name {name!r} must be a lowercase slug')
        require(isinstance(station, dict), f'station {name} must be an object')
    routing = config.get('review_routing', [])
    require(isinstance(routing, list), 'review_routing must be a list')
    for key in ('also_watch', 'changes'):
        require(isinstance(config.get(key, []), list), f'{key} must be a list')
    for key in ('watch', 'archive', 'worktree_pool', 'merge_policy'):
        require(isinstance(config.get(key, {}), dict), f'{key} must be an object')
    for rule in routing:
        require(isinstance(rule, dict) and isinstance(rule.get('from'), str) and rule['from'],
                'each routing rule needs a non-empty "from"')
        require(isinstance(rule.get('reviewer'), str) and rule['reviewer'] in stations,
                f'routing reviewer {rule.get("reviewer")!r} is not a station')
    watch = config.get('watch', {})
    require(isinstance(watch, dict), 'watch must be an object')
    for key in ('always_from', 'notify_events'):
        require(isinstance(watch.get(key, []), list) and all(isinstance(x, str) and x for x in watch.get(key, [])),
                f'watch.{key} must be a list of non-empty strings')
    for extra in config.get('also_watch', []):
        require(isinstance(extra, dict) and valid_branch(extra.get('branch'))
                and isinstance(extra.get('messages_dir'), str) and extra['messages_dir'].strip('/')
                and '..' not in extra['messages_dir'], 'also_watch entries need a branch and a messages_dir')
        hints = extra.get('session_stations', {})
        require(isinstance(hints, dict) and all(isinstance(k, str) and k and isinstance(v, str) and v in stations for k, v in hints.items()),
                'also_watch session_stations must map session-id patterns to stations')
    watched = [extra['branch'] for extra in config.get('also_watch', [])]
    # Each branch has one cursor, so each may be watched once.
    require(len(watched) == len(set(watched)), 'also_watch lists a branch more than once')
    archive = config.get('archive', {})
    require(isinstance(archive, dict) and type(archive.get('after_days', 30)) is int
            and archive.get('after_days', 30) >= 1, 'archive.after_days must be a positive integer')
    require(isinstance(config.get('changes', []), list), 'changes must be a list')
    pool = config.get('worktree_pool', {})
    require(isinstance(pool, dict) and type(pool.get('max', DEFAULT_POOL)) is int and pool.get('max', DEFAULT_POOL) >= 1,
            'worktree_pool.max must be a positive integer')
    policy = config.get('merge_policy', {})
    require(isinstance(policy, dict), 'merge_policy must be an object')
    for key in ('low_risk', 'needs_human'):
        require(isinstance(policy.get(key, []), list) and all(isinstance(x, str) for x in policy.get(key, [])),
                f'merge_policy.{key} must be a list of strings')
    return config


def load_config(transport, revision=None):
    """stations.json at `revision`. A file that is truly absent is a setup error (ValueError); a failed
    Git read stays a RuntimeError so pollers can treat it as transient and retry."""
    revision = revision or transport.ref()
    if not transport.exists(revision, CONFIG_FILE):
        raise ValueError(f'No {CONFIG_FILE} on {transport.remote}/{transport.branch}; run `init` first')
    config = validate_config(json.loads(transport.show(revision, CONFIG_FILE)))
    require(all(extra['branch'] != transport.branch for extra in config.get('also_watch', [])),
            f'also_watch must not repeat the mailbox branch {transport.branch}')
    return config


def config_bytes(config):
    return (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


# ---------------------------------------------------------------------- envelope


def identity(value, field):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 256
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and '/' not in value and '\\' not in value and '..' not in value,
            'Unsafe or missing ' + field)


def valid_branch(branch):
    return (isinstance(branch, str) and bool(branch) and not branch.startswith(('-', '/', '.'))
            and not branch.endswith(('/', '.', '.lock'))
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '~^:?*[\\' for c in branch)
            and branch != '@' and '..' not in branch and '@{' not in branch and '//' not in branch
            and all(not part.startswith('.') and not part.endswith('.lock') for part in branch.split('/')))


REVIEW_PHASES = {  # phase: (event, fields every newly written message of that phase must carry)
    'ready_for_cr': ('review_requested', ('pr_url', 'review_head')),
    'cr_started': ('started', ('review_head',)),
    'cr_result': ('handoff', ('verdict', 'review_head', 'recipient_station')),
    # A liveness check: the addressed station's watcher answers a ping with a pong by itself.
    'ping': ('handoff', ('recipient_station',)),
    'pong': ('handoff', ('recipient_station',)),
}


def validate(message, repo, messages_dir=MESSAGES, strict=False):
    """Check one STATUS envelope; return its path on the mail branch.

    `strict` is for newly written mail: the sender's station and the full review-phase contract are
    required. Reading stays lenient so older envelopes remain inspectable.
    """
    require(isinstance(message, dict), 'Message must be a JSON object')
    require(type(message.get('schema_version')) is int and message['schema_version'] == 1, 'schema_version must be 1')
    require(message.get('type') == 'STATUS', 'Only STATUS may be sent')
    require(isinstance(message.get('id'), str) and SAFE_ID.fullmatch(message['id']), 'Unsafe message id')
    created = message.get('created_at_utc')
    try:
        instant = datetime.datetime.fromisoformat(created)
    except (ValueError, TypeError):
        raise ValueError('created_at_utc must be ISO UTC') from None
    require('T' in created and instant.tzinfo is not None and instant.utcoffset() == datetime.timedelta(0),
            'created_at_utc must be aware UTC')
    actor = message.get('actor')
    require(isinstance(actor, dict), 'actor must be an object')
    for field in ('person', 'display_name', 'session_id', 'github_login', 'agent'):
        identity(actor.get(field), 'actor.' + field)
    require(LOGIN.fullmatch(actor['github_login'].lower()) is not None, 'github_login must be a valid GitHub login')
    require(SLUG.fullmatch(actor['agent']) is not None, 'agent must be a lowercase slug')
    require(actor.get('id') == actor['github_login'].lower() + '/' + actor['agent'], 'actor.id must be login/agent')
    require('station' not in actor or (isinstance(actor['station'], str) and SLUG.fullmatch(actor['station'])),
            'actor.station must be a lowercase slug')
    require(message.get('from_agent') == actor['agent'], 'from_agent must match actor.agent')
    require(isinstance(message.get('to_agent'), str) and SLUG.fullmatch(message['to_agent']), 'to_agent must be a slug')
    task = message.get('task')
    require(isinstance(task, dict), 'task must be an object')
    require(type(task.get('issue')) is int and task['issue'] > 0, 'task.issue must be a positive integer')
    require(task.get('issue_url') == f'https://github.com/{repo}/issues/{task["issue"]}',
            'issue_url must match the configured repository and issue number')
    require(task.get('event') in EVENTS, 'Unknown task event')
    require(valid_branch(task.get('branch')), 'Unsafe task.branch')
    for field in ('base', 'head'):
        require(isinstance(task.get(field), str) and SHA.fullmatch(task[field]), f'task.{field} must be 40 hex digits')
    # Optional fields reach the coordinator's wake output, so each one is held to its exact shape.
    phase = task.get('phase')
    require(phase is None or (isinstance(phase, str) and SLUG.fullmatch(phase.replace('_', '-'))), 'Unsafe task.phase')
    require(task.get('verdict') is None or task['verdict'] in VERDICTS, 'Unknown task.verdict')
    # The PR may live in a sibling repository (a library or skill repo tracked by this repo's issue);
    # the coordinator verifies it natively before acting.
    require(task.get('pr_url') is None or (isinstance(task['pr_url'], str) and PR_URL.fullmatch(task['pr_url'])),
            'pr_url must be a GitHub pull request URL')
    require(task.get('review_head') is None or task['review_head'] == task['head'], 'review_head must equal head')
    for field in ('recipient_station',):
        require(task.get(field) is None or (isinstance(task[field], str) and SLUG.fullmatch(task[field])),
                f'task.{field} must be a lowercase slug')
    for field in ('worktree', 'reused_from'):
        value = task.get(field)
        require(value is None or (isinstance(value, str) and 0 < len(value) <= 300
                                  and all(c.isprintable() for c in value)), f'task.{field} must be printable text')
    recipient = task.get('recipient_actor')
    require(recipient is None or (isinstance(recipient, str) and re.fullmatch(r'[a-z0-9-]{1,39}/[a-z0-9_-]{1,64}', recipient)),
            'task.recipient_actor must be login/agent')
    require(task.get('intake_returned') in (None, True) and (not task.get('intake_returned') or task['event'] == 'finished'),
            'intake_returned is only valid as true on finished')
    for need, fields in PHASE_FIELDS.items():
        if phase == need:
            for field in fields:
                require(task.get(field), f'phase {need} requires task.{field}')
    require(isinstance(message.get('text'), str), 'text must be a string')
    if strict:
        require(isinstance(actor.get('station'), str), 'actor.station is required on new mail')
        require(task['event'] != 'review_requested' or phase == 'ready_for_cr',
                'review_requested must use phase ready_for_cr')
        if phase in REVIEW_PHASES:
            event, fields = REVIEW_PHASES[phase]
            require(task['event'] == event, f'phase {phase} belongs to event {event}')
            for field in fields:
                require(task.get(field), f'phase {phase} requires task.{field}')
    try:
        json.dumps(message, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('Message must contain finite JSON data') from None
    return f"{messages_dir}/{actor['github_login'].lower()}/{actor['agent']}/{message['id']}.json"


def build_event(*, repo, issue, event, branch, base, head, text, person, login, agent, session, station,
                to_agent='all', message_id=None, phase=None, verdict=None, pr_url=None, review_head=None,
                recipient_actor=None, recipient_station=None, intake_returned=False, worktree=None,
                reused_from=None, now=None):
    """One lifecycle event as a validated envelope; phase rules are enforced here."""
    require(event in EVENTS, 'Unknown task event')
    require(phase is None or SLUG.fullmatch(phase.replace('_', '-')) is not None, 'Unsafe task.phase')
    given = {'pr_url': pr_url, 'verdict': verdict}
    for field in PHASE_FIELDS.get(phase, ()):
        require(bool(given[field]), f'phase {phase} requires --{field.replace("_", "-")}')
    require(verdict is None or verdict in VERDICTS, 'verdict must be one of ' + ', '.join(sorted(VERDICTS)))
    require(station and SLUG.fullmatch(station), 'station is required (git config stations.station, or --station)')
    require(recipient_station is None or SLUG.fullmatch(recipient_station), 'recipient station must be a slug')
    if phase == 'cr_result':
        require(recipient_station, 'phase cr_result requires --recipient-station (the author\'s station)')
    if phase in ('ping', 'pong'):
        require(event == 'handoff' and recipient_station, f'phase {phase} is a handoff with --recipient-station')
    if phase in ('ready_for_cr', 'cr_result', 'cr_started'):
        review_head = review_head or head
        require(review_head == head, 'review_head must equal head (the exact reviewed head)')
    login = login.lower()
    task = {'issue': issue, 'issue_url': f'https://github.com/{repo}/issues/{issue}', 'event': event,
            'branch': branch, 'base': base, 'head': head}
    for field, value in (('phase', phase), ('verdict', verdict), ('pr_url', pr_url), ('review_head', review_head),
                         ('recipient_actor', recipient_actor), ('recipient_station', recipient_station)):
        if value:
            task[field] = value
    for field, value in (('worktree', worktree), ('reused_from', reused_from)):
        if value:
            task[field] = value
    if intake_returned:
        require(event == 'finished', 'intake_returned is only valid on finished')
        task['intake_returned'] = True
    # The digest keeps a retry of the same command idempotent, while a new report at the same head, or a
    # parallel session or branch on the same issue, gets its own id.
    digest = hashlib.sha256('\0'.join((text, branch, session, station)).encode('utf-8')).hexdigest()[:8]
    suffix = '-'.join(p for p in (event.replace('_', '-'), (phase or '').replace('_', '-'), head[:8], digest) if p)
    message = {'schema_version': 1, 'type': 'STATUS',
               'id': message_id or f'{login}-{agent}-issue{issue}-{suffix}',
               'created_at_utc': (now or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0).isoformat(),
               'from_agent': agent, 'to_agent': to_agent,
               'actor': {'id': f'{login}/{agent}', 'person': person, 'github_login': login, 'agent': agent,
                         'station': station, 'display_name': f"{person}'s {agent.title()}", 'session_id': session},
               'task': task, 'text': text}
    validate(message, repo, strict=True)
    return message


def payload_bytes(message):
    return (json.dumps(message, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def same_json(left, right):
    """Same message, ignoring created_at_utc so re-running an `event` command is a safe retry."""
    def canonical(raw):
        data = json.loads(raw)
        if isinstance(data, dict):
            data.pop('created_at_utc', None)
        return json.dumps(data, sort_keys=True, ensure_ascii=False)
    try:
        return canonical(left) == canonical(right)
    except (ValueError, UnicodeError):
        return False


def archived_paths(transport, revision):
    if not transport.exists(revision, ARCHIVE_INDEX):
        return {}
    rows = transport.show(revision, ARCHIVE_INDEX).decode('utf-8').splitlines()
    return dict(row.split('\t', 1) for row in rows if '\t' in row)


def is_linked_worktree(root):
    git_dir = Path(git(root, 'rev-parse', '--absolute-git-dir').stdout.strip()).resolve()
    common = Path(git(root, 'rev-parse', '--git-common-dir').stdout.strip())
    common = (common if common.is_absolute() else Path(root) / common).resolve()
    return git_dir != common


def send(transport, message, cwd=None):
    """Append one message; idempotent for an identical payload under the same id."""
    transport.fetch()
    config = load_config(transport)
    path = validate(message, config['repo'], strict=True)
    event = message['task']['event']
    if event in ADMISSION_EVENTS and not is_linked_worktree(cwd or transport.root):
        raise RuntimeError('started, resumed and review_requested must be sent from a linked Git worktree; '
                           'the primary checkout stays read-only')
    payload = payload_bytes(message)
    receipt = {}

    def prepare(tip):
        # Checked against the exact parent of this attempt, so a pause that wins the race is honoured.
        current = load_config(transport, tip)
        station = message['actor']['station']
        require(station in current['stations'], f'actor.station {station!r} is not in stations.json')
        if event in ADMISSION_EVENTS and current.get('paused'):
            raise RuntimeError('Stations are paused (' + str(current.get('pause_reason') or 'no reason given')
                               + '); only paused, handoff and finished may be sent')
        if transport.exists(tip, path):
            if not same_json(transport.show(tip, path), payload):
                raise ValueError('Message id collision: a different payload exists at ' + path)
            receipt['already_sent'] = True
            receipt['commit'] = git(transport.root, 'log', '-1', '--format=%H', tip, '--', path).stdout.strip()
            return None
        tag = archived_paths(transport, tip).get(path)
        if tag:
            git(transport.root, 'fetch', '--quiet', transport.remote, f'refs/tags/{tag}:refs/tags/{tag}', check=False)
            try:
                archived = transport.show('refs/tags/' + tag, path)
            except RuntimeError:
                archived = b''
            if not same_json(archived, payload):
                raise ValueError(f'Message id collision: {path} was used and archived in tag {tag}')
            receipt['already_sent'] = True
            receipt['archived_in'] = tag
            return None
        return {path: payload}

    actor = message['actor']
    subject = (f"station-mail({actor['id']}@{actor.get('station', '?')}): "
               f"{event} #{message['task']['issue']} {message['id']}")
    commit = transport.append(prepare, subject)
    if commit:
        receipt.update(commit=commit, already_sent=False)
    return dict({'id': message['id'], 'path': path}, **receipt)


# ------------------------------------------------------------------------- inbox


def readable(raw, path, repo, messages_dir=MESSAGES):
    """The message in `raw` if it is well formed and stored at its canonical path, else None.

    Shared by inbox and watch so both readers agree on which records exist. Malformed or misplaced
    mail is skipped; it never stops a reader.
    """
    try:
        message = json.loads(raw)
        if validate(message, repo, messages_dir) != path:
            return None
        return message
    except (ValueError, UnicodeError, TypeError, AttributeError, KeyError, RecursionError):
        return None


def iter_messages(transport, revision, messages_dir=MESSAGES):
    listing = git(transport.root, 'ls-tree', '-rz', '--name-only', revision, '--', messages_dir).stdout
    for path in listing.split('\0'):
        if path.endswith('.json'):
            raw = transport.show(revision, path)
            try:
                yield path, json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                yield path, None


def inbox(transport, actor=None, issue=None):
    """Labeled raw records. --actor filters by sender, never by recipient."""
    if actor is not None:
        login, _, agent = actor.partition('/')
        require(LOGIN.fullmatch(login) and SLUG.fullmatch(agent), 'actor filter must be login/agent')
    tip = transport.fetch()
    config = load_config(transport, tip)
    sources = [(transport.branch, tip, MESSAGES)]
    for extra in config.get('also_watch', []):
        legacy_tip = transport.fetch(extra['branch'], required=False)
        if legacy_tip:
            sources.append((extra['branch'], legacy_tip, extra['messages_dir']))
    records = []
    for branch, revision, messages_dir in sources:
        for path, data in iter_messages(transport, revision, messages_dir):
            status, sender = 'ok', None
            try:
                require(path == validate(data, config['repo'], messages_dir), 'path does not match actor and id')
                sender = data['actor']['id']
            except (ValueError, TypeError, KeyError, AttributeError):
                status = 'invalid'
            if actor is not None and sender != actor:
                continue
            task = data.get('task') if isinstance(data, dict) else None
            if issue is not None and not (isinstance(task, dict) and task.get('issue') == issue):
                continue
            records.append({'branch': branch, 'path': path, 'status': status, 'attribution': 'self_reported',
                            'message': data})
    return {'observed_tip': tip, 'paused': config.get('paused', False), 'messages': records}


# ------------------------------------------------------------------------- watch


# Local ref-lock and loose-object write races only; an authentication failure ("Permission denied (publickey)")
# is permanent and reported at once.
TRANSIENT_FETCH = re.compile(r'\block\b|unpack-objects failed|unable to create temporary file|unable to write', re.I)


def sender_key(actor, station=None):
    return f"{str(actor.get('id', '?')).lower()}@{station or actor.get('station') or '?'}"


def plain(value, limit=120):
    """Untrusted message text made safe for one wake line: no control characters, bounded length."""
    text = ''.join(c if c.isprintable() else ' ' for c in str(value))
    return text if len(text) <= limit else text[:limit - 1] + '…'


def route(config, sender):
    """The reviewing station for a sender key (`login/agent@station`): first matching rule wins."""
    for rule in config.get('review_routing', []):
        if fnmatch.fnmatchcase(sender, rule['from'].lower()):
            return rule['reviewer']
    return None


def classify(message, me, config, local_branches, inferred=None):
    """Why `message` needs this station's coordinator, or None when it is quiet.

    `inferred` is the station `session_stations` hints give stationless mail. The legacy
    local-branch rules run first and are unaffected by it. Review routing then tries the
    inferred key (`login/agent@station`) and falls back to the `@?` key when no rule
    matches, except for mail inferred to come from this station, which never routes back here.
    """
    if not isinstance(message, dict) or not isinstance(message.get('actor'), dict):
        return None
    actor, task = message['actor'], message.get('task') or {}
    if not isinstance(task, dict):
        return None
    station, branch = actor.get('station'), task.get('branch')
    inferred = None if station else inferred
    sender = sender_key(actor, inferred)
    senders = [sender] + ([sender_key(actor)] if inferred and inferred != me else [])
    event, phase = plain(task.get('event'), 20), plain(task.get('phase') or '', 20)
    head = task.get('head') if isinstance(task.get('head'), str) and SHA.fullmatch(task['head']) else '?'
    ref = (f"#{plain(task.get('issue'), 12)} {plain(branch or '?', 100)} @ {head} "
           f"from {plain(sender, 120)} ({plain(actor.get('session_id'), 80)})")
    if event == 'handoff' and task.get('recipient_station') == me:
        # An explicit recipient decides first, older mail included.
        return plain(f"{phase or 'handoff'} {task.get('verdict') or ''} for this station: ".replace('  ', ' ')) + ref
    legacy_local = station is None and branch in local_branches
    if legacy_local:
        # Older mail has no station. A verdict about a branch checked out here is for this station's
        # executor; anything else about such a branch came from this station.
        if event == 'handoff' and task.get('phase') == 'cr_result' and not task.get('recipient_station'):
            return plain(f"cr_result {task.get('verdict') or ''} for this station: ".replace('  ', ' ')) + ref
        return None
    if event == 'review_requested':
        if not task.get('pr_url'):
            return None  # not a usable review request
        # Routing decides, even for the sender's own station: the reviewer is a fresh session either way.
        reviewer = next((r for r in (route(config, key) for key in senders) if r), None)
        return f"review needed: {plain(task['pr_url'], 200)} {ref}" if reviewer == me else None
    if (station or inferred) == me:
        return None
    watch = config.get('watch', {})
    if any(fnmatch.fnmatchcase(key, pattern.lower()) for key in senders for pattern in watch.get('always_from', [])):
        return f'{event}/{phase or "-"} {ref}'
    if event == 'handoff':
        # An explicit recipient decides; the local-branch fallback is for older mail without one.
        if not task.get('recipient_station') and branch in local_branches:
            return plain(f"{phase or 'handoff'} {task.get('verdict') or ''} for this station: ".replace('  ', ' ')) + ref
        return None
    if event in watch.get('notify_events', DEFAULT_NOTIFY):
        return f'{event} {ref}'
    return None


def local_branches(root):
    listing = git(root, 'worktree', 'list', '--porcelain').stdout
    return {line[len('branch refs/heads/'):] for line in listing.splitlines() if line.startswith('branch refs/heads/')}


def infer_station(message, session_stations):
    """The station older (stationless) mail came from, by the first matching session-id pattern, or None."""
    actor = message.get('actor') if isinstance(message, dict) else None
    if not session_stations or not isinstance(actor, dict) or actor.get('station'):
        return None
    session = str(actor.get('session_id', '')).lower()
    return next((station for pattern, station in session_stations.items()
                 if fnmatch.fnmatchcase(session, pattern.lower())), None)


def is_ping_for(message, me):
    """A ping a station (not older stationless mail) addressed to this station."""
    task = message.get('task') or {}
    actor = message.get('actor') or {}
    return (task.get('event') == 'handoff' and task.get('phase') == 'ping' and task.get('recipient_station') == me
            and isinstance(actor.get('station'), str) and SLUG.fullmatch(actor['station']) is not None)


def scan(transport, config, me, start, end, messages_dir=MESSAGES, branches=None, session_stations=None,
         pings=None):
    """Actionable lines for commits in start..end (start None: the last 24 hours).

    With a `pings` list, pings addressed here are collected there instead of becoming wake lines.
    """
    branches = local_branches(transport.root) if branches is None else branches
    if start and git(transport.root, 'merge-base', '--is-ancestor', start, end, check=False).returncode == 0:
        commits = git(transport.root, 'rev-list', '--reverse', f'{start}..{end}').stdout.split()
    else:
        commits = git(transport.root, 'rev-list', '--reverse', '--since=24.hours', end).stdout.split()
    lines = []
    for commit in commits:
        changes = git(transport.root, 'diff-tree', '--root', '--no-commit-id', '--name-status', '-r', commit).stdout
        for row in changes.splitlines():
            status, _, path = row.partition('\t')
            if status == 'A' and path.startswith(messages_dir + '/') and path.endswith('.json'):
                raw = transport.show(commit, path)  # a failed read raises, so the cursor stays put
                message = readable(raw, path, config['repo'], messages_dir)
                reason = (classify(message, me, config, branches, infer_station(message, session_stations))
                          if message is not None else None)
                if reason and pings is not None and messages_dir == MESSAGES and is_ping_for(message, me):
                    pings.append((commit, message))
                elif reason:
                    lines.append(f'{commit[:9]} {reason}')
            elif path == CONFIG_FILE and status == 'M':
                subject = git(transport.root, 'log', '-1', '--format=%s', commit).stdout.strip()
                if f'@{me})' not in subject:
                    lines.append(f'{commit[:9]} stations.json changed: {subject}')
    return lines


def read_state(transport, station):
    # Before per-station cursors, one state.json served the clone's only station: start from it.
    for path in (transport.state_path(station), transport.legacy_state_path()):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return {}
    return {}


def write_state(transport, station, state):
    """Atomic replace, so a second watcher of this station never reads half-written state."""
    path = transport.state_path(station)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='state-', suffix='.tmp')
    with os.fdopen(handle, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(state, indent=2) + '\n')
    os.replace(temporary, path)


def watch(transport, me, poll=60, once=False, out=sys.stdout):
    """Poll cheaply (ls-remote); print actionable mail and return 0.

    With --once: 3 when quiet, 4 when a branch could not be read (its cursor is kept for a retry).
    A missing mailbox or an invalid stations.json is a setup error and raises.
    """
    config, config_tip = None, None
    while True:
        state = read_state(transport, me)
        seen = state.setdefault('last_seen', {})
        found, advanced, unreadable, pings = [], {}, [], []

        def incomplete(branch, error):
            print(f'warning: could not read {branch}; its cursor stays put for a retry: {error}', file=sys.stderr)
            unreadable.append(branch)

        # Routing must be current before any cursor moves: classifying with stale routing and then
        # advancing would silently drop mail that the new routing sends here. Once routing is verified,
        # each branch's messages are read independently.
        try:
            main_tip = transport.remote_tip(strict=True)
            if main_tip is None:
                raise ValueError(f'No mailbox {transport.remote}/{transport.branch}; run `init` first')
            if main_tip != config_tip:
                fetched = transport.fetch()
                config, config_tip = load_config(transport, fetched), fetched
            main_tip = config_tip  # scan exactly the revision whose routing was loaded; no second fetch
        except RuntimeError as error:
            incomplete(transport.branch, error)
            config_tip = None  # force a fresh routing load next poll
        watched = []
        if not unreadable:  # routing verified
            watched = [(transport.branch, MESSAGES, None)] + [(e['branch'], e['messages_dir'], e.get('session_stations'))
                                                               for e in config.get('also_watch', [])]
        for branch, messages_dir, session_stations in watched:
            try:
                # A failed lookup or fetch is an incomplete poll; only a branch the remote truly lacks is quiet.
                tip = main_tip if branch == transport.branch else transport.remote_tip(branch, strict=True)
                if not tip or tip == seen.get(branch):
                    continue
                if branch != transport.branch and transport.fetch(branch, required=False) is None:
                    raise RuntimeError(f'fetch of {transport.remote}/{branch} at {tip} failed')
                found += scan(transport, config, me, seen.get(branch), tip, messages_dir,
                              session_stations=session_stations, pings=pings)
            except RuntimeError as error:
                incomplete(branch, error)
                continue
            advanced[branch] = tip
        for commit, ping in pings:
            problem = answer_ping(transport, config, me, ping)
            if problem:  # could not answer by itself: hand the ping to the coordinator
                found.append(f'{commit[:9]} ping from {plain(sender_key(ping["actor"]), 120)}: {plain(problem, 200)}')
        if found:
            print('ACTIONABLE STATION MAIL:', file=out)
            for line in found:
                print(line, file=out)
            out.flush()
        if advanced:  # only after the wake lines are out, so a failed print re-reports next time
            seen.update(advanced)
            write_state(transport, me, state)
        if found:
            return EXIT_ACTIONABLE
        if once:
            return EXIT_INCOMPLETE if unreadable else EXIT_QUIET
        time.sleep(poll)


def answer_ping(transport, config, me, ping):
    """Send the pong for `ping`; None on success, else why it could not be sent.

    The pong's text is fixed and nothing from the ping is followed, so a ping can only ever
    produce one pong. A retry of the same ping reuses the same id and is idempotent.
    """
    names = ('PERSON', 'LOGIN', 'AGENT', 'SESSION')
    who = {name: os.environ.get('STATION_MAIL_' + name) for name in names}
    if not all(who.values()):
        return 'reply needed (an automatic pong needs STATION_MAIL_PERSON, _LOGIN, _AGENT and _SESSION)'
    task = ping['task']
    digest = hashlib.sha256(ping['id'].encode('utf-8')).hexdigest()[:12]
    try:
        pong = build_event(
            repo=config['repo'], issue=task['issue'], event='handoff', branch=task['branch'], base=task['base'],
            head=task['head'], text=f'pong from {me}: watcher running, paused={bool(config.get("paused"))}',
            person=who['PERSON'], login=who['LOGIN'], agent=who['AGENT'], session=who['SESSION'], station=me,
            phase='pong', recipient_station=ping['actor']['station'], recipient_actor=ping['actor']['id'],
            message_id=f"{who['LOGIN'].lower()}-{who['AGENT']}-issue{task['issue']}-pong-{digest}")
        send(transport, pong)
    except (ValueError, RuntimeError) as error:
        return f'automatic pong failed: {error}'
    return None


# ----------------------------------------------------------------------- archive


def archive_plan(transport, revision, config, now=None, issue_closed=None):
    """Message paths eligible to leave the tip: older than after_days and their task resolved.

    A task is one (issue, branch) thread, so a parallel session on the same issue keeps its mail until
    its own branch finishes. A closed issue resolves every thread on it.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=config.get('archive', {}).get('after_days', 30))
    latest, entries = {}, []
    for path, data in iter_messages(transport, revision):
        task = data.get('task') if isinstance(data, dict) else None
        try:
            created = datetime.datetime.fromisoformat(data['created_at_utc'])
        except (TypeError, KeyError, ValueError):
            created = None
        key = (task.get('issue'), task.get('branch')) if isinstance(task, dict) and task.get('issue') else None
        if key and created is not None and task.get('event') in EVENTS:
            # On a tie, finished wins.
            rank = (created, task['event'] == 'finished')
            if key not in latest or rank >= latest[key][0]:
                latest[key] = (rank, task['event'])
        entries.append((path, key, created))
    closed_cache = {}

    def resolved(key):
        if key is None:
            return True
        if latest.get(key, (None, None))[1] == 'finished':
            return True
        if key[0] not in closed_cache:
            closed_cache[key[0]] = bool(issue_closed and issue_closed(key[0]))
        return closed_cache[key[0]]

    return sorted(path for path, key, created in entries
                  if created is not None and created < cutoff and resolved(key))


def gh_issue_closed(repo):
    if not shutil.which('gh'):
        print('warning: gh not found; only tasks with a finished event are archived', file=sys.stderr)
        return None

    def closed(issue):
        result = subprocess.run(['gh', 'issue', 'view', str(issue), '--repo', repo, '--json', 'state', '-q', '.state'],
                                capture_output=True)
        return result.returncode == 0 and result.stdout.decode().strip() == 'CLOSED'
    return closed


def archive(transport, actor_label, dry_run=False, now=None, issue_closed=None):
    tip = transport.fetch()
    config = load_config(transport, tip)
    closed = issue_closed if issue_closed is not None else gh_issue_closed(config['repo'])
    paths = archive_plan(transport, tip, config, now, closed)
    if dry_run or not paths:
        return {'eligible': len(paths), 'paths': paths, 'archived': False}
    require(actor_label, 'archive needs --login and --agent (or $STATION_MAIL_LOGIN / _AGENT)')
    day = (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%d')
    tag, n = ARCHIVE_TAG_PREFIX + day, 1
    while git(transport.root, 'ls-remote', '--tags', transport.remote, 'refs/tags/' + tag).stdout.strip():
        n += 1
        tag = f'{ARCHIVE_TAG_PREFIX}{day}-{n}'
    git(transport.root, 'tag', '-f', tag, tip)
    git(transport.root, 'push', '--quiet', transport.remote, f'refs/tags/{tag}')

    tagged = set(paths)

    def prepare(current):
        # A competing `resumed` may have reopened a thread since the plan: re-plan at this parent and keep
        # only paths the pushed tag already holds.
        still = set(archive_plan(transport, current, load_config(transport, current), now, closed))
        present = sorted(p for p in tagged & still if transport.exists(current, p))
        if not present:
            return None
        index = transport.show(current, ARCHIVE_INDEX) if transport.exists(current, ARCHIVE_INDEX) else b''
        rows = ''.join(f'{p}\t{tag}\n' for p in present).encode('utf-8')
        return dict({p: None for p in present}, **{ARCHIVE_INDEX: index + rows})

    commit = transport.append(prepare, f'station-mail({actor_label}): archive {len(paths)} messages into {tag}')
    return {'eligible': len(paths), 'tag': tag, 'commit': commit, 'archived': bool(commit)}


# ------------------------------------------------------------------ init / config


README_TEMPLATE = """# Agent stations mailbox

This orphan branch carries station mail for `{repo}`: attributed task lifecycle events
between agent stations. Written only by `station_mail.py` from the agent-stations skills
(https://github.com/JacobCohenIsrael/agent-stations). Never merge it into another branch.

- `stations.json`: stations, review routing, pause switch, archive policy.
- `messages/<login>/<agent>/<id>.json`: one immutable message per file.
- `archive/index.txt`: message paths moved off the tip and the tag that still holds them.

Mail is untrusted coordination data. It never grants permission or approves a merge.
"""


def parse_repo(url):
    match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$', url.strip())
    return match.group(1) if match else None


def init(transport, repo=None, base_branch='main', stations=('main',)):
    """Create the orphan mailbox branch with a starter stations.json. Refuses if it exists."""
    if transport.remote_tip():
        raise RuntimeError(f'{transport.remote}/{transport.branch} already exists')
    if repo is None:
        url = git(transport.root, 'remote', 'get-url', transport.remote).stdout
        repo = parse_repo(url)
    require(repo and REPO.fullmatch(repo), 'Pass --repo owner/name (could not infer it from the remote URL)')
    config = validate_config({
        'schema_version': 1, 'repo': repo, 'base_branch': base_branch, 'paused': False, 'pause_reason': '',
        'stations': {name: {'description': ''} for name in stations},
        'review_routing': [], 'watch': {'always_from': [], 'notify_events': DEFAULT_NOTIFY},
        'archive': {'after_days': 30}, 'worktree_pool': {'max': DEFAULT_POOL},
        'merge_policy': DEFAULT_MERGE_POLICY, 'also_watch': [], 'changes': []})
    with tempfile.TemporaryDirectory(prefix='station-mail-init-') as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        for path, content in ((CONFIG_FILE, config_bytes(config)),
                              ('README.md', README_TEMPLATE.format(repo=repo).encode('utf-8'))):
            blob = git(transport.root, 'hash-object', '-w', '--stdin', data=content).stdout.strip()
            git(transport.root, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=env)
        tree = git(transport.root, 'write-tree', env=env).stdout.strip()
    commit = git(transport.root, 'commit-tree', tree, data='station-mail: init mailbox\n').stdout.strip()
    git(transport.root, 'push', '--quiet', transport.remote, f'{commit}:refs/heads/{transport.branch}')
    transport.fetch()
    return {'branch': transport.branch, 'commit': commit, 'config': config}


def change_config(transport, mutate, actor_label, station, quote, now=None):
    """Write `mutate(latest config)` to stations.json, recording who changed it and the human's own words.

    `mutate` runs on the config at the exact parent of each append attempt, so a concurrent change is
    never overwritten by an older snapshot.
    """
    require(isinstance(quote, str) and quote.strip(), 'Record the human instruction with --quote')
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0).isoformat()

    def prepare(tip):
        current = load_config(transport, tip)
        updated = mutate(json.loads(json.dumps(current)))
        updated['changes'] = current.get('changes', []) + [{'at': stamp, 'by': f'{actor_label}@{station}', 'quote': quote}]
        validate_config(updated)
        require(all(extra['branch'] != transport.branch for extra in updated.get('also_watch', [])),
                f'also_watch must not repeat the mailbox branch {transport.branch}')
        return {CONFIG_FILE: config_bytes(updated)}

    commit = transport.append(prepare, f'stations-config({actor_label}@{station}): {quote[:60]}')
    return {'commit': commit}


def apply_config(transport, new_config, actor_label, station, quote, now=None):
    """Replace stations.json with an edited copy of `config show`; refuses when the config moved since."""
    validate_config(new_config)

    def replace(current):
        require(new_config.get('changes', []) == current.get('changes', []),
                'stations.json changed since this copy was taken; run `config show` again and reapply the edit')
        return dict(new_config)
    return change_config(transport, replace, actor_label, station, quote, now)


def set_paused(transport, paused, reason, actor_label, station, quote):
    def toggle(current):
        current['paused'] = paused
        current['pause_reason'] = reason if paused else ''
        return current
    return change_config(transport, toggle, actor_label, station, quote)


# ------------------------------------------------------------------------ doctor


def doctor(transport):
    checks = []

    def check(name, ok, detail='', required=True):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail, 'required': required})

    check('python >= 3.11', sys.version_info >= (3, 11), sys.version.split()[0])
    version = git(transport.root, '--version', check=False)
    check('git available', version.returncode == 0, version.stdout.strip())
    tip = transport.remote_tip()
    check(f'mailbox {transport.remote}/{transport.branch} exists', tip, tip or 'run init')
    config = None
    if tip:
        try:
            transport.fetch()
            config = load_config(transport)
            check('stations.json valid', True, f"repo {config['repo']}, paused={config.get('paused', False)}")
        except (RuntimeError, ValueError) as error:
            check('stations.json valid', False, str(error))
    me = transport.station()
    source = transport.station_source()
    check('this session has a station', me, f'{me} (from {source})' if me else 'run join --station NAME')
    check('station is not shared clone-wide', source != 'local',
          'set in this clone\'s shared config: every worktree and agent here reads it. If another agent shares this '
          'clone, give each `join --worktree` or $STATION_MAIL_STATION', required=False)
    if config and me:
        check('station is listed in stations.json', me in config['stations'], ', '.join(config['stations']))
    shapes = {'STATION_MAIL_PERSON': lambda v: identity(v, 'person'),
              'STATION_MAIL_LOGIN': lambda v: require(LOGIN.fullmatch(v.lower()), 'not a GitHub login'),
              'STATION_MAIL_AGENT': lambda v: require(SLUG.fullmatch(v), 'agent must be lowercase letters, digits, - or _'),
              'STATION_MAIL_SESSION': lambda v: identity(v, 'session')}
    for variable, shape in shapes.items():
        value = os.environ.get(variable)
        if not value:
            check(f'{variable} set (or pass flags)', False, '', False)
            continue
        try:
            shape(value)
            check(f'{variable} valid', True, value)
        except ValueError as error:
            check(f'{variable} valid', False, f'{value!r}: {error}')
    hooks_dir = Path(git(transport.root, 'rev-parse', '--git-path', 'hooks').stdout.strip())  # honours core.hooksPath
    if not hooks_dir.is_absolute():
        hooks_dir = transport.root / hooks_dir
    pre_push = hooks_dir / 'pre-push'
    has_reminder = pre_push.is_file() and 'review-reminder' in pre_push.read_text(encoding='utf-8', errors='replace')
    check('pre-push hook runs review-reminder', has_reminder, str(pre_push), False)
    check('running in a linked worktree', is_linked_worktree(transport.root),
          'started, resumed and review_requested need one', False)
    gh = subprocess.run(['gh', 'auth', 'status'], capture_output=True) if shutil.which('gh') else None
    check('gh authenticated (archive reads issue state)', gh is not None and gh.returncode == 0, '', False)
    return checks


# ----------------------------------------------------------- worktrees / re-review


def worktrees(root, base_ref, pr_state=None):
    """Linked worktrees with what git can tell about reuse, released ones first, most recently used first.

    Agents still confirm the owning session is finished and no editor or process has the folder open.
    """
    blocks = git(root, 'worktree', 'list', '--porcelain').stdout.strip().split('\n\n')
    rows = []
    for block in blocks[1:]:  # the first entry is the primary checkout, never pooled
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(' ')
            fields[key] = value or True
        path = Path(fields['worktree'])
        branch = fields['branch'][len('refs/heads/'):] if isinstance(fields.get('branch'), str) else None
        row = {'path': str(path), 'branch': branch, 'head': fields.get('HEAD'), 'locked': 'locked' in fields,
               'last_used': 0}
        reasons = []
        if not path.is_dir():
            reasons.append('folder missing (git worktree prune)')
        else:
            status = git(path, 'status', '--porcelain', '--untracked-files=normal', check=False)
            if status.returncode:
                reasons.append('git status failed: ' + plain(status.stderr.strip()))
            elif status.stdout.strip():
                reasons.append('uncommitted or untracked work')
            try:
                gitdir = Path(git(path, 'rev-parse', '--absolute-git-dir').stdout.strip())
                row['last_used'] = max((gitdir / name).stat().st_mtime for name in ('HEAD', 'index')
                                       if (gitdir / name).exists())
            except (RuntimeError, ValueError, OSError):
                pass
        if row['locked']:
            reasons.append('locked')
        merged = isinstance(row['head'], str) and git(root, 'merge-base', '--is-ancestor', row['head'], base_ref,
                                                      check=False).returncode == 0
        row['pr_state'] = pr_state(branch) if (pr_state and branch) else None
        if not (merged or row['pr_state'] in ('MERGED', 'CLOSED')):
            reasons.append('work not merged into the base and no merged or closed PR')
        row['released'] = not reasons
        row['blocked_by'] = reasons
        rows.append(row)
    rows.sort(key=lambda r: (not r['released'], -r['last_used']))
    return rows


def gh_pr_state(repo):
    if not shutil.which('gh'):
        return None

    def state(branch):
        result = subprocess.run(['gh', 'pr', 'list', '--repo', repo, '--head', branch, '--state', 'all',
                                 '--json', 'state', '-q', '.[0].state'], capture_output=True)
        return (result.stdout.decode().strip() or None) if result.returncode == 0 else None
    return state


def patch_id(root, base, head):
    diff = git_bytes(root, 'diff', '--binary', base, head)
    if not diff.strip():
        return ''
    result = subprocess.run(['git', 'patch-id', '--stable'], cwd=root, input=diff, capture_output=True)
    return result.stdout.decode().split(' ', 1)[0]


def rereview_check(root, approved_head, head, base_ref):
    """Whether `head` differs from an approved head only by clean merges of the base branch.

    Holds when every commit in approved..head merges the base, each remerge diff is empty (no conflict
    resolved by hand), and the task's own delta against the base is unchanged.
    """
    reasons = []
    if git(root, 'merge-base', '--is-ancestor', approved_head, head, check=False).returncode:
        reasons.append('the approved head is not an ancestor of the new head')
    commits = git(root, 'rev-list', '--first-parent', f'{approved_head}..{head}').stdout.split()  # the task's own line
    for commit in commits:
        parents = git(root, 'rev-list', '--parents', '-n', '1', commit).stdout.split()[1:]
        if len(parents) < 2:
            reasons.append(f'{commit[:9]} is not a merge commit')
            continue
        if not any(git(root, 'merge-base', '--is-ancestor', parent, base_ref, check=False).returncode == 0
                   for parent in parents[1:]):
            reasons.append(f'{commit[:9]} does not merge the base branch')
        remerge = git(root, 'show', '--remerge-diff', '--format=', commit, check=False)
        if remerge.returncode or remerge.stdout.strip():
            reasons.append(f'{commit[:9]} has a non-empty remerge diff (conflicts resolved by hand)')
    old = patch_id(root, git(root, 'merge-base', approved_head, base_ref).stdout.strip(), approved_head)
    new = patch_id(root, git(root, 'merge-base', head, base_ref).stdout.strip(), head)
    if old != new:
        reasons.append('the task delta against the base changed since the approval')
    return {'approved_head': approved_head, 'head': head, 'merges': len(commits), 'short_rereview_ok': not reasons,
            'reasons': reasons}


# --------------------------------------------------------------------------- cli


def review_reminder(transport, branch, head):
    """Offline: text when pushing `head` leaves the branch's open CR request stale, else None."""
    ref = transport.ref()
    if git(transport.root, 'rev-parse', '--verify', '--quiet', ref, check=False).returncode:
        return None
    matches = git(transport.root, 'grep', '-lzF', f'"branch": {json.dumps(branch, ensure_ascii=False)}',
                  ref, '--', MESSAGES, check=False).stdout
    events = []
    for match in matches.split('\0'):
        path = match.split(':', 1)[1] if ':' in match else ''
        if not path.endswith('.json'):
            continue
        try:
            data = json.loads(transport.show(ref, path))
            task = data['task']
            instant = datetime.datetime.fromisoformat(data['created_at_utc'])
        except (ValueError, KeyError, TypeError, RuntimeError):
            continue
        if task.get('branch') == branch and task.get('event') in ('review_requested', 'finished') and instant.tzinfo:
            events.append((instant, task['event'] == 'finished', task))
    if not events:
        return None
    last = max(events, key=lambda item: item[:2])[2]
    reviewed = last.get('review_head') or last.get('head')
    if last['event'] != 'review_requested' or reviewed == head:
        return None
    return (f"station mail: {branch} is in CR for #{last.get('issue')} at {reviewed}; this push moves it to {head}.\n"
            f"After the push, send `resumed` (phase repair or integration) and `review_requested` "
            f"(phase ready_for_cr) for {head}.")


def identity_args(parser):
    for flag, variable in (('--person', 'STATION_MAIL_PERSON'), ('--login', 'STATION_MAIL_LOGIN'),
                           ('--agent', 'STATION_MAIL_AGENT'), ('--session', 'STATION_MAIL_SESSION')):
        parser.add_argument(flag, default=os.environ.get(variable), help=f'default: ${variable}')


def actor_label(args):
    require(args.login and args.agent, '--login and --agent (or $STATION_MAIL_LOGIN / _AGENT) are required')
    return f'{args.login.lower()}/{args.agent}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--remote', help='default: git config stations.remote, else origin')
    parser.add_argument('--branch', help='mailbox branch; default: git config stations.branch, else agent-stations')
    commands = parser.add_subparsers(dest='command', required=True)

    p = commands.add_parser('init', help='create the orphan mailbox branch (once per repository)')
    p.add_argument('--repo', help='owner/name; default: inferred from the remote URL')
    p.add_argument('--base-branch', default='main')
    p.add_argument('--stations', default='main', help='comma-separated station names')

    p = commands.add_parser('join', help='record this station\'s name in git config (this clone, or one worktree)')
    p.add_argument('--station', required=True)
    p.add_argument('--worktree', action='store_true',
                   help='set it for this worktree only, when several stations share one clone')

    p = commands.add_parser('event', help='build one lifecycle event from flags and send it')
    p.add_argument('--issue', type=int, required=True)
    p.add_argument('--event', choices=sorted(EVENTS), required=True)
    p.add_argument('--text', required=True)
    p.add_argument('--branch-name', help='task branch; default: the current branch')
    p.add_argument('--head', help='default: HEAD')
    p.add_argument('--base', help='default: merge-base of HEAD and <remote>/<base_branch>')
    p.add_argument('--phase', help='ready_for_cr, cr_started, cr_result, repair, integration, manual_test, '
                                   'human_review, blocked, usage, ownership')
    p.add_argument('--verdict', choices=sorted(VERDICTS))
    p.add_argument('--pr-url')
    p.add_argument('--review-head')
    p.add_argument('--recipient-actor')
    p.add_argument('--recipient-station')
    p.add_argument('--intake-returned', action='store_true')
    p.add_argument('--worktree', help='absolute path of the linked worktree this session works in')
    p.add_argument('--reused-from', help='prior owner of a reused worktree: session, branch, issue')
    p.add_argument('--to', default='all')
    p.add_argument('--id')
    p.add_argument('--station', help='default: git config stations.station')
    p.add_argument('--dry-run', action='store_true', help='print the envelope without sending')
    identity_args(p)

    p = commands.add_parser('send', help='send a prepared JSON envelope')
    p.add_argument('--message-file', type=Path, required=True)

    p = commands.add_parser('inbox', help='diagnostic listing (not a polling loop)')
    p.add_argument('--actor', help='sender login/agent')
    p.add_argument('--issue', type=int)

    p = commands.add_parser('watch', help='wait for mail this station must act on; exit 0 with it; with --once, 3 when quiet, 4 when a branch could not be read')
    p.add_argument('--poll', type=int, default=60, help='seconds between ls-remote checks')
    p.add_argument('--once', action='store_true')
    p.add_argument('--range', help='FROM..TO: print the classification only, no state change')

    p = commands.add_parser('archive', help='move resolved messages older than archive.after_days off the tip')
    p.add_argument('--dry-run', action='store_true')
    identity_args(p)

    p = commands.add_parser('config', help='show or replace stations.json')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('show')
    a = sub.add_parser('apply', help='replace stations.json (records the human instruction)')
    a.add_argument('--file', type=Path, required=True)
    a.add_argument('--quote', required=True, help="the human's own instruction, verbatim")
    identity_args(a)
    for name in ('pause', 'resume'):
        a = sub.add_parser(name)
        a.add_argument('--quote', required=True, help="the human's own instruction, verbatim")
        if name == 'pause':
            a.add_argument('--reason', required=True)
        identity_args(a)

    p = commands.add_parser('review-reminder', help='offline pre-push check')
    p.add_argument('--branch-name', required=True)
    p.add_argument('--head', required=True)

    commands.add_parser('doctor', help='check this machine and repository setup')

    commands.add_parser('worktrees', help='list linked worktrees for reuse: released first, most recently used first')

    p = commands.add_parser('rereview-check', help='can a head that only merged the base reuse an approval?')
    p.add_argument('--approved-head', required=True)
    p.add_argument('--head', required=True)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Pipes on Windows default to the ANSI code page; message text may hold any Unicode.
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    cwd = Path.cwd()
    transport = Transport(cwd, args.remote, args.branch)
    try:
        if args.command == 'init':
            stations = [s.strip() for s in args.stations.split(',') if s.strip()]
            result = init(transport, args.repo, args.base_branch, stations)
        elif args.command == 'join':
            require(SLUG.fullmatch(args.station) is not None, 'station must be a lowercase slug')
            if args.worktree:
                git(cwd, 'config', 'extensions.worktreeConfig', 'true')
                git(cwd, 'config', '--worktree', 'stations.station', args.station)
            else:
                git(cwd, 'config', 'stations.station', args.station)
            result = {'station': args.station, 'scope': 'worktree' if args.worktree else 'clone'}
        elif args.command == 'event':
            for flag in ('person', 'login', 'agent', 'session'):
                require(getattr(args, flag), f'--{flag} (or $STATION_MAIL_{flag.upper()}) is required')
            transport.fetch()
            config = load_config(transport)
            head = args.head or git(cwd, 'rev-parse', 'HEAD').stdout.strip()
            base = args.base
            if not base:
                git(cwd, 'fetch', '--quiet', transport.remote, config['base_branch'], check=False)
                base = git(cwd, 'merge-base', head, f"{transport.remote}/{config['base_branch']}").stdout.strip()
            branch = args.branch_name or git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
            require(branch != 'HEAD', 'Detached HEAD: pass --branch-name with the task branch')
            message = build_event(
                repo=config['repo'], issue=args.issue, event=args.event, text=args.text, branch=branch,
                base=base, head=head, person=args.person, login=args.login, agent=args.agent,
                session=args.session, station=args.station or transport.station(), to_agent=args.to,
                message_id=args.id, phase=args.phase, verdict=args.verdict, pr_url=args.pr_url,
                review_head=args.review_head, recipient_actor=args.recipient_actor,
                recipient_station=args.recipient_station, intake_returned=args.intake_returned,
                worktree=args.worktree, reused_from=args.reused_from)
            result = message if args.dry_run else send(transport, message, cwd)
        elif args.command == 'send':
            result = send(transport, json.loads(args.message_file.read_text(encoding='utf-8-sig')), cwd)
        elif args.command == 'inbox':
            result = inbox(transport, args.actor, args.issue)
        elif args.command == 'watch':
            me = transport.station()
            require(me, 'This session has no station; run `join --station NAME` or set $STATION_MAIL_STATION')
            if args.range:
                start, _, end = args.range.partition('..')
                transport.fetch()
                lines = scan(transport, load_config(transport), me, start or None, end or transport.ref())
                print('\n'.join(lines))
                return 0
            return watch(transport, me, args.poll, args.once)
        elif args.command == 'archive':
            label = f'{args.login.lower()}/{args.agent}' if args.login and args.agent else None
            result = archive(transport, label, args.dry_run)
        elif args.command == 'config':
            if args.action == 'show':
                result = load_config(transport, transport.fetch())
            else:
                station = transport.station() or '?'
                if args.action == 'apply':
                    new = json.loads(args.file.read_text(encoding='utf-8-sig'))
                    result = apply_config(transport, new, actor_label(args), station, args.quote)
                else:
                    result = set_paused(transport, args.action == 'pause', getattr(args, 'reason', ''),
                                        actor_label(args), station, args.quote)
        elif args.command in ('worktrees', 'rereview-check'):
            transport.fetch()
            config = load_config(transport)
            git(cwd, 'fetch', '--quiet', transport.remote, config['base_branch'], check=False)
            base_ref = f"{transport.remote}/{config['base_branch']}"
            if args.command == 'worktrees':
                rows = worktrees(cwd, base_ref, gh_pr_state(config['repo']))
                pool = config.get('worktree_pool', {}).get('max', DEFAULT_POOL)
                result = {'pool_max': pool, 'over_pool_by': max(0, len(rows) - pool), 'worktrees': rows}
            else:
                result = rereview_check(cwd, args.approved_head, args.head, base_ref)
        elif args.command == 'review-reminder':
            text = review_reminder(transport, args.branch_name, args.head)
            if text:
                print(text)
            return 0
        else:
            result = doctor(transport)
            for row in result:
                mark = 'ok   ' if row['ok'] else ('FAIL ' if row['required'] else 'warn ')
                print(mark + row['check'] + (f"  ({row['detail']})" if row['detail'] else ''))
            return 0 if all(r['ok'] for r in result if r['required']) else 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
