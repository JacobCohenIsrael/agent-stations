"""station_mail.py checks against disposable local bare Git remotes only."""
import concurrent.futures
import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'station-mail' / 'scripts'))
import station_mail as mail  # noqa: E402

REPO = 'example/game'
UTC = datetime.timezone.utc


def run(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout.decode('utf-8').strip()


def author(root):
    run(root, 'config', 'user.name', 'Station Mail Test')
    run(root, 'config', 'user.email', 'station-mail@example.invalid')
    run(root, 'config', 'core.autocrlf', 'false')


def event(station='windows', issue=7, kind='started', head='b' * 40, branch='feature/x', login='jacob',
          agent='claude', now=None, **extra):
    return mail.build_event(repo=REPO, issue=issue, event=kind, branch=branch, base='a' * 40, head=head,
                            text='Started café — 星\nnext line', person=login.title(), login=login, agent=agent,
                            session=f'{station}-session', station=station, now=now, **extra)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='station-mail-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / 'remote.git'
        self.remote.mkdir()
        run(self.remote, 'init', '--bare', '--initial-branch', 'main')
        seed = self.base / 'seed'
        seed.mkdir()
        run(seed, 'init', '--initial-branch', 'main')
        author(seed)
        (seed / 'notes.txt').write_text('original\n', encoding='utf-8')
        run(seed, 'add', '.')
        run(seed, 'commit', '-m', 'Fixture')
        run(seed, 'remote', 'add', 'origin', str(self.remote))
        run(seed, 'push', 'origin', 'HEAD:refs/heads/main')
        self.primary, self.root = self.clone('windows')
        self.transport = mail.Transport(self.root)
        mail.init(self.transport, REPO, 'main', ['windows', 'mac'])
        config = mail.load_config(self.transport)
        config['review_routing'] = [{'from': 'jacob/claude@windows', 'reviewer': 'mac'},
                                    {'from': '*@mac', 'reviewer': 'windows'},
                                    {'from': 'phil/*', 'reviewer': 'windows'}]
        mail.apply_config(self.transport, config, 'jacob/claude', 'windows', 'Jacob: Mac reviews Windows')

    def clone(self, station):
        primary = self.base / station
        run(self.base, 'clone', str(self.remote), str(primary))
        author(primary)
        run(primary, 'config', 'stations.station', station)
        linked = self.base / (station + '-linked')
        run(primary, 'worktree', 'add', '-b', f'{station}-task', str(linked), 'origin/main')
        author(linked)
        return primary, linked


class InitAndSend(Fixture):
    def test_init_creates_orphan_mailbox_and_refuses_twice(self):
        tip = self.transport.fetch()
        files = run(self.root, 'ls-tree', '-r', '--name-only', tip).splitlines()
        self.assertEqual(['README.md', 'stations.json'], files)
        self.assertNotEqual(0, subprocess.run(['git', 'merge-base', tip, 'origin/main'], cwd=self.root,
                                              capture_output=True).returncode, 'mailbox must be an orphan')
        with self.assertRaises(RuntimeError):
            mail.init(self.transport, REPO)
        self.assertEqual('example/game', mail.parse_repo('git@github.com:example/game.git'))
        self.assertEqual('example/game', mail.parse_repo('https://github.com/example/game'))

    def test_bytes_idempotence_collision_and_untouched_checkout(self):
        (self.root / 'notes.txt').write_text('staged\n', encoding='utf-8')
        run(self.root, 'add', 'notes.txt')
        (self.root / 'notes.txt').write_text('unstaged\n', encoding='utf-8')
        (self.root / 'untracked').write_text('keep', encoding='utf-8')

        def snapshot():
            index = self.root / run(self.root, 'rev-parse', '--git-path', 'index')
            return (run(self.root, 'rev-parse', 'HEAD'), run(self.root, 'branch', '--show-current'),
                    index.read_bytes(), run(self.root, 'status', '--porcelain'))
        before = snapshot()
        message = event()
        receipt = mail.send(self.transport, message)
        raw = subprocess.run(['git', 'show', receipt['commit'] + ':' + receipt['path']], cwd=self.root,
                             capture_output=True, check=True).stdout
        self.assertEqual(mail.payload_bytes(message), raw)
        self.assertNotIn(b'\r\n', raw)
        self.assertEqual('messages/jacob/claude/' + message['id'] + '.json', receipt['path'])
        self.assertEqual('A\t' + receipt['path'],
                         run(self.root, 'diff-tree', '--no-commit-id', '--name-status', '-r', receipt['commit']))
        self.assertTrue(mail.send(self.transport, message)['already_sent'])
        with self.assertRaises(ValueError):
            mail.send(self.transport, dict(message, text='changed'))
        self.assertEqual(before, snapshot())

    def test_concurrent_senders_all_land(self):
        _, other = self.clone('mac')
        transports = [self.transport, mail.Transport(other)]
        messages = [event(station=('windows', 'mac')[i % 2], issue=i + 1) for i in range(6)]
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda pair: mail.send(*pair), [(transports[i % 2], m) for i, m in enumerate(messages)]))
        listed = mail.inbox(self.transport)['messages']
        self.assertEqual(sorted(m['id'] for m in messages), sorted(r['message']['id'] for r in listed))

    def test_admission_rules_pause_and_primary_checkout(self):
        with self.assertRaises(RuntimeError):
            mail.send(mail.Transport(self.primary), event(), cwd=self.primary)
        mail.send(mail.Transport(self.primary), event(kind='paused'), cwd=self.primary)
        mail.set_paused(self.transport, True, 'usage reset pending', 'jacob/claude', 'windows', 'Jacob: pause all')
        with self.assertRaises(RuntimeError) as raised:
            mail.send(self.transport, event(kind='resumed'))
        self.assertIn('usage reset pending', str(raised.exception))
        mail.send(self.transport, event(kind='finished', intake_returned=True))
        mail.set_paused(self.transport, False, '', 'jacob/claude', 'windows', 'Jacob: resume')
        mail.send(self.transport, event(kind='resumed'))
        changes = mail.load_config(self.transport, self.transport.fetch())['changes']
        self.assertEqual(['Jacob: Mac reviews Windows', 'Jacob: pause all', 'Jacob: resume'], [c['quote'] for c in changes])
        self.assertEqual('jacob/claude@windows', changes[-1]['by'])


class Envelope(unittest.TestCase):
    def test_phase_rules_and_repository_binding(self):
        with self.assertRaises(ValueError):
            event(kind='review_requested', phase='ready_for_cr')
        with self.assertRaises(ValueError):
            event(kind='handoff', phase='cr_result')
        with self.assertRaises(ValueError):
            event(kind='handoff', phase='cr_result', verdict='approved', review_head='c' * 40)
        with self.assertRaises(ValueError):
            event(station='')
        with self.assertRaises(ValueError):
            event(kind='started', intake_returned=True)
        ready = event(kind='review_requested', phase='ready_for_cr', pr_url='https://github.com/example/game/pull/9')
        self.assertEqual('b' * 40, ready['task']['review_head'])
        self.assertEqual('windows', ready['actor']['station'])
        self.assertTrue(ready['id'].endswith('review-requested-ready-for-cr-bbbbbbbb'))
        with self.assertRaises(ValueError):
            mail.validate(ready, 'someone/else')
        legacy = dict(ready, actor={k: v for k, v in ready['actor'].items() if k != 'station'})
        mail.validate(legacy, REPO)

    def test_config_validation(self):
        good = {'schema_version': 1, 'repo': REPO, 'base_branch': 'main', 'stations': {'mac': {}}}
        mail.validate_config(good)
        for bad in ({**good, 'repo': 'nope'}, {**good, 'stations': {}}, {**good, 'stations': {'Mac': {}}},
                    {**good, 'review_routing': [{'from': '*', 'reviewer': 'linux'}]},
                    {**good, 'paused': 'yes'}, {**good, 'archive': {'after_days': 0}}):
            with self.assertRaises(ValueError):
                mail.validate_config(bad)


class Classification(unittest.TestCase):
    config = {'review_routing': [{'from': 'jacob/claude@windows', 'reviewer': 'mac'},
                                 {'from': '*@mac', 'reviewer': 'windows'}],
              'watch': {'always_from': ['*/codex@*'], 'notify_events': ['finished']}}

    def classify(self, message, me='windows', local=()):
        return mail.classify(message, me, self.config, set(local))

    def test_routing_decides_reviews(self):
        pr = {'phase': 'ready_for_cr', 'pr_url': 'https://github.com/example/game/pull/9'}
        self.assertIsNone(self.classify(event(station='windows', kind='review_requested', **pr)))
        self.assertIn('review needed', self.classify(event(station='windows', kind='review_requested', **pr), me='mac'))
        self.assertIn('review needed', self.classify(event(station='mac', kind='review_requested', **pr)))
        self.assertIsNone(self.classify(event(station='linux', kind='review_requested', **pr)), 'unrouted')

    def test_verdicts_reach_the_branch_owner_only(self):
        verdict = event(station='mac', kind='handoff', phase='cr_result', verdict='approved', branch='win/fix')
        self.assertIn('cr_result approved', self.classify(verdict, local={'win/fix'}))
        self.assertIsNone(self.classify(verdict))
        addressed = event(station='mac', kind='handoff', phase='ownership', recipient_station='windows')
        self.assertIn('ownership', self.classify(addressed))

    def test_quiet_and_notify(self):
        self.assertIsNone(self.classify(event(station='mac', kind='started')))
        self.assertIsNone(self.classify(event(station='windows', kind='finished')))
        self.assertIn('finished', self.classify(event(station='mac', kind='finished')))
        self.assertIsNone(self.classify(event(station='mac', kind='paused')))
        self.assertIn('started', self.classify(event(station='cloud', agent='codex', kind='started')))
        legacy = event(station='mac', kind='finished', branch='win/fix')
        del legacy['actor']['station']
        self.assertIsNone(self.classify(legacy, local={'win/fix'}), 'legacy mail for a local branch is ours')
        self.assertIsNone(self.classify({'id': 'x'}))


class Watch(Fixture):
    def test_watch_wakes_reviewer_once_and_reports_config_changes(self):
        mac_primary, mac = self.clone('mac')
        mac_transport = mail.Transport(mac)
        out = io.StringIO()
        self.assertEqual(mail.EXIT_ACTIONABLE, mail.watch(mac_transport, 'mac', once=True, out=out),
                         'first run reads the last 24 hours, which includes the routing change')
        self.assertIn('stations.json changed', out.getvalue())
        self.assertEqual(mail.EXIT_QUIET, mail.watch(mac_transport, 'mac', once=True, out=io.StringIO()))
        mail.send(self.transport, event(kind='started'))
        self.assertEqual(mail.EXIT_QUIET, mail.watch(mac_transport, 'mac', once=True, out=io.StringIO()))
        mail.send(self.transport, event(kind='review_requested', phase='ready_for_cr',
                                        pr_url='https://github.com/example/game/pull/3'))
        out = io.StringIO()
        self.assertEqual(mail.EXIT_ACTIONABLE, mail.watch(mac_transport, 'mac', once=True, out=out))
        self.assertIn('review needed: https://github.com/example/game/pull/3', out.getvalue())
        self.assertEqual(mail.EXIT_QUIET, mail.watch(mac_transport, 'mac', once=True, out=io.StringIO()))
        state = json.loads(mac_transport.state_path().read_text(encoding='utf-8'))
        self.assertEqual(mac_transport.remote_tip(), state['last_seen']['agent-stations'])
        self.assertTrue(str(mac_transport.state_path()).startswith(str((mac_primary / '.git').resolve())))

    def test_also_watch_reads_a_legacy_branch(self):
        legacy = self.base / 'legacy'
        run(self.base, 'clone', str(self.remote), str(legacy))
        author(legacy)
        run(legacy, 'checkout', '-q', '--orphan', 'old-mail')
        run(legacy, 'rm', '-rqf', '.')
        old = event(station='mac', kind='review_requested', phase='ready_for_cr',
                    pr_url='https://github.com/example/game/pull/4')
        path = legacy / 'docs/messages/jacob/claude' / (old['id'] + '.json')
        path.parent.mkdir(parents=True)
        path.write_bytes(mail.payload_bytes(old))
        run(legacy, 'add', '.')
        run(legacy, 'commit', '-qm', 'legacy mail')
        run(legacy, 'push', '-q', 'origin', 'old-mail')
        config = mail.load_config(self.transport, self.transport.fetch())
        config['also_watch'] = [{'branch': 'old-mail', 'messages_dir': 'docs/messages'}]
        mail.apply_config(self.transport, config, 'jacob/claude', 'windows', 'Jacob: watch the old branch too')
        out = io.StringIO()
        self.assertEqual(mail.EXIT_ACTIONABLE, mail.watch(self.transport, 'windows', once=True, out=out))
        self.assertIn('pull/4', out.getvalue())


class ArchiveAndReminder(Fixture):
    def test_archive_moves_only_old_resolved_mail_and_keeps_it_reachable(self):
        old = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        done = [event(issue=1, now=old), event(issue=1, kind='finished', now=old + datetime.timedelta(hours=1))]
        open_task = event(issue=2, now=old)
        closed_task = event(issue=3, now=old)
        recent = event(issue=4, kind='finished')
        for message in done + [open_task, closed_task, recent]:
            mail.send(self.transport, message)
        now = datetime.datetime(2026, 3, 1, tzinfo=UTC)
        result = mail.archive(self.transport, 'jacob/claude', now=now, issue_closed=lambda issue: issue == 3)
        self.assertTrue(result['archived'])
        self.assertEqual(3, result['eligible'])
        tip = self.transport.fetch()
        remaining = [r['message']['id'] for r in mail.inbox(self.transport)['messages']]
        self.assertEqual(sorted([open_task['id'], recent['id']]), sorted(remaining))
        index = mail.archived_paths(self.transport, tip)
        self.assertEqual({result['tag']}, set(index.values()))
        self.assertTrue(run(self.root, 'ls-remote', '--tags', 'origin', result['tag']))
        self.assertTrue(mail.send(self.transport, done[0])['already_sent'], 'archived ids stay idempotent')
        with self.assertRaises(ValueError):
            mail.send(self.transport, dict(done[0], text='reused id'))
        again = mail.archive(self.transport, 'jacob/claude', now=now, issue_closed=lambda issue: issue == 3)
        self.assertEqual(0, again['eligible'])

    def test_review_reminder_after_push_in_cr(self):
        head = run(self.root, 'rev-parse', 'HEAD')
        mail.send(self.transport, event(kind='review_requested', phase='ready_for_cr', head=head, branch='windows-task',
                                        pr_url='https://github.com/example/game/pull/5'))
        self.assertIsNone(mail.review_reminder(self.transport, 'windows-task', head))
        self.assertIn('in CR for #7', mail.review_reminder(self.transport, 'windows-task', 'c' * 40))
        self.assertIsNone(mail.review_reminder(self.transport, 'other', 'c' * 40))


class Cli(Fixture):
    def test_event_dry_run_join_and_doctor(self):
        import contextlib
        import os
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mail.main(['event', '--issue', '7', '--event', 'started', '--text', 'go', '--dry-run',
                                  '--person', 'Jacob', '--login', 'Jacob', '--agent', 'claude', '--session', 's1'])
            self.assertEqual(0, code, out.getvalue())
            message = json.loads(out.getvalue())
            self.assertEqual('windows', message['actor']['station'])
            self.assertEqual(run(self.root, 'rev-parse', 'HEAD'), message['task']['base'])
            with contextlib.redirect_stdout(io.StringIO()) as report:
                code = mail.main(['doctor'])
            self.assertEqual(0, code, report.getvalue())
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
