"""Host-operation safeguards, tested without a Docker daemon or project data."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('menu_ops', Path(__file__).parents[1] / 'manage.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        stdout = patch('sys.stdout', new=StringIO())
        stdout.start()
        self.addCleanup(stdout.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root / 'env/.env'
        for name, value in [('ROOT', self.root), ('ENV', self.env)]:
            patcher = patch.object(ops, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = ops.Manager('test-menu')

    @patch.object(ops.subprocess, 'run')
    def test_generated_secrets_are_private_unique_and_not_printed(self, run):
        run.return_value.stdout = ''
        output = StringIO()
        with redirect_stdout(output):
            self.manager.prepare_env('localhost,example.com')
        values = dict(line.split('=', 1) for line in self.env.read_text().splitlines())
        keys = ['DJANGO_SECRET_KEY', 'POSTGRES_PASSWORD', 'STAFF_PASSWORD']
        self.assertEqual(len({values[key] for key in keys}), 3)
        self.assertGreaterEqual(len(values['DJANGO_SECRET_KEY']), 50)
        for key in keys:
            self.assertNotIn(values[key], output.getvalue())
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    @patch.object(ops.subprocess, 'run')
    def test_existing_credentials_are_never_regenerated(self, run):
        self.env.parent.mkdir()
        self.env.write_text('existing credentials\n')
        self.manager.prepare_env('different.example')
        self.assertEqual(self.env.read_text(), 'existing credentials\n')
        run.assert_not_called()

    @patch.object(ops.subprocess, 'run')
    def test_missing_configuration_with_existing_volume_fails(self, run):
        run.return_value.stdout = 'test-menu_postgres_data\n'
        with self.assertRaises(ops.OperationError):
            self.manager.prepare_env('localhost')
        self.assertFalse(self.env.exists())

    def test_exclusive_write_cannot_overwrite_credentials(self):
        path = self.root / '.env'
        ops.private_write(path, 'original')
        with self.assertRaises(FileExistsError):
            ops.private_write(path, 'replacement')
        self.assertEqual(path.read_text(), 'original')

    def test_lock_rejects_concurrent_mutations(self):
        with ops.operation_lock(), self.assertRaises(ops.OperationError), ops.operation_lock():
            self.fail('Concurrent operation entered lock')

    def test_update_does_not_replace_services_if_backup_fails(self):
        self.manager.prepare_env = Mock()
        self.manager.require_env = Mock()
        self.manager.compose = Mock()
        self.manager.backup = Mock(side_effect=ops.OperationError('backup failed'))
        self.manager.verify = Mock()
        with self.assertRaises(ops.OperationError):
            self.manager.start(ops.parse_args(['update']), update=True)
        commands = [call.args for call in self.manager.compose.call_args_list]
        # Starting the database for backup is allowed; replacing the application is not.
        self.assertTrue(all(args[0] != 'up' or args[-1] == 'db' for args in commands))
        self.assertFalse(any('load_initial_data' in args for args in commands))
        self.manager.backup.assert_called_once()
        self.manager.verify.assert_not_called()

    def test_regular_start_does_not_force_image_rebuild(self):
        self.manager.prepare_env = Mock()
        self.manager.require_env = Mock()
        self.manager.compose = Mock()
        self.manager.verify = Mock()
        self.manager.start(ops.parse_args(['start']))
        commands = [call.args for call in self.manager.compose.call_args_list]
        self.assertTrue(any(args[0] == 'up' for args in commands))
        self.assertFalse(any(args[0] == 'build' or '--build' in args for args in commands))

    def test_backup_failure_is_logged_without_publishing_an_archive(self):
        for stage in ('dump', 'archive-check'):
            with self.subTest(stage=stage):

                def fail_at_stage(*args, **kwargs):
                    if stage == 'dump' or 'pg_restore' in args:
                        raise subprocess.CalledProcessError(
                            1, ['backup'], stderr=f'{stage} failed'.encode()
                        )
                    kwargs['stdout'].write(b'invalid contents')

                self.manager.compose = Mock(side_effect=fail_at_stage)
                with self.assertRaises(subprocess.CalledProcessError):
                    self.manager.backup(source='scheduled')
                directory = self.root / 'data/backups'
                self.assertEqual([p.name for p in directory.iterdir()], ['backup.log'])
                records = [
                    json.loads(line) for line in (directory / 'backup.log').read_text().splitlines()
                ][-2:]
                self.assertEqual([r['status'] for r in records], ['started', 'failed'])
                self.assertEqual(records[-1]['stage'], stage)
                self.assertIn(f'{stage} failed', records[-1]['error'])

    def test_manual_and_scheduled_backups_share_one_private_log(self):
        def dump(*args, **kwargs):
            if 'pg_restore' not in args:
                kwargs['stdout'].write(b'archive contents')

        self.manager.compose = Mock(side_effect=dump)
        for source in ('manual', 'scheduled'):
            archive = self.manager.backup(source=source)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        log = self.root / 'data/backups/backup.log'
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual([r['status'] for r in records], ['started', 'success'] * 2)
        self.assertEqual([r['source'] for r in records], ['manual'] * 2 + ['scheduled'] * 2)
        self.assertNotEqual(records[0]['run_id'], records[2]['run_id'])
        for started, finished in (records[:2], records[2:]):
            self.assertEqual(started['run_id'], finished['run_id'])
            self.assertEqual(finished['bytes'], len(b'archive contents'))
            self.assertTrue(Path(finished['archive']).exists())

    @patch.object(ops.Manager, 'diagnostics')
    @patch.object(ops.Manager, 'preflight')
    def test_busy_scheduled_backup_is_logged_before_any_docker_call(self, preflight, _):
        with ops.operation_lock(), patch('sys.stderr', new=StringIO()):
            result = ops.main(['backup', '--source', 'scheduled'])
        self.assertEqual(result, 1)
        preflight.assert_not_called()
        log = self.root / 'data/backups/backup.log'
        record = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(
            (record['source'], record['status'], record['stage']), ('scheduled', 'failed', 'lock')
        )

    @patch.object(ops, 'write_backup_event', side_effect=OSError('log unavailable'))
    def test_unwritable_log_prevents_backup(self, _):
        self.manager.compose = Mock()
        with self.assertRaises(OSError):
            self.manager.backup()
        self.manager.compose.assert_not_called()

    def test_preflight_failure_is_recorded_without_starting_dump(self):
        self.manager.preflight = Mock(
            side_effect=subprocess.CalledProcessError(
                1, ['docker', 'compose', 'version'], stderr='Docker unavailable'
            )
        )
        self.manager.compose = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.manager.backup(source='scheduled', prepare=True)
        self.manager.compose.assert_not_called()
        log = self.root / 'data/backups/backup.log'
        record = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual((record['status'], record['stage']), ('failed', 'preflight'))
        self.assertIn('Docker unavailable', record['error'])

    def test_destroy_requires_exact_project_confirmation(self):
        with self.assertRaises(SystemExit), patch('sys.stderr', new=StringIO()):
            ops.parse_args(['--project-name', 'test-menu', 'destroy', '--confirm', 'docker'])

    @patch.object(ops.Manager, 'diagnostics')
    @patch.object(ops.Manager, 'require_env')
    @patch.object(ops.Manager, 'preflight')
    @patch.object(ops.Manager, 'backup', side_effect=ops.OperationError('backup failed'))
    @patch.object(ops.Manager, 'compose')
    def test_destroy_does_not_delete_volumes_when_backup_fails(self, compose, *_):
        with patch('sys.stderr', new=StringIO()):
            result = ops.main(['--project-name', 'test-menu', 'destroy', '--confirm', 'test-menu'])
        self.assertEqual(result, 1)
        self.assertFalse(any(call.args[0] == 'down' for call in compose.call_args_list))


if __name__ == '__main__':
    unittest.main()
