"""Host-operation safeguards, tested without a Docker daemon or project data."""

import importlib.util
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

    def test_regular_start_reuses_images_and_seeds_then_verifies(self):
        self.manager.prepare_env = Mock()
        self.manager.require_env = Mock()
        events = []
        self.manager.compose = Mock(side_effect=lambda *args: events.append(args))
        self.manager.verify = Mock(side_effect=lambda: events.append(('verify',)))
        self.manager.start(ops.parse_args(['start']))
        self.assertFalse(any(args[0] == 'build' or '--build' in args for args in events))
        started = next(i for i, args in enumerate(events) if args[0] == 'up')
        seeded = next(i for i, args in enumerate(events) if 'load_initial_data' in args)
        self.assertLess(started, seeded)
        self.assertLess(seeded, events.index(('verify',)))
        self.manager.verify.assert_called_once()

    def test_failed_backup_removes_partial_archive(self):
        self.manager.compose = Mock(side_effect=subprocess.CalledProcessError(1, ['pg_dump']))
        with self.assertRaises(subprocess.CalledProcessError):
            self.manager.backup()
        self.assertEqual(list((self.root / 'data/backups').iterdir()), [])

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
