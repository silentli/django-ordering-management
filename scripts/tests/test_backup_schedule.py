"""Timer installation safeguards; no host service is installed by these tests."""

import importlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
schedule = importlib.import_module('ops.backup_schedule')


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        templates = self.root / 'scripts/systemd'
        templates.mkdir(parents=True)
        for source in (SCRIPTS / 'systemd').iterdir():
            (templates / source.name).write_text(source.read_text())
        env = self.root / 'menu_management/settings/env/.env'
        env.parent.mkdir(parents=True)
        env.write_text('# test\n')
        self.units = self.root / 'units'
        self.units.mkdir()

    def options(self, **overrides):
        return SimpleNamespace(
            **{'user': 'deploy', 'at': '03:00', 'timezone': 'UTC', 'dry_run': False, **overrides}
        )

    @patch.object(schedule.subprocess, 'run')
    @patch.object(schedule, 'atomic_write')
    def test_preview_never_writes_or_runs_commands(self, write, run):
        units = schedule.install(self.root, 'staging', self.options(dry_run=True))
        run.assert_not_called()
        write.assert_not_called()
        self.assertIn('menu-backup-staging.timer', units)
        self.assertIn('--project-name "staging"', units['menu-backup-staging.service'])

    def test_bad_parameters_cannot_become_unit_directives(self):
        for overrides in (
            {'at': '25:00'},
            {'user': 'deploy\nUser=root'},
            {'timezone': 'UTC\nPersistent=false'},
        ):
            args = self.options(**overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                schedule.render_units(self.root, 'docker', args.user, args.at, args.timezone)

    @patch.object(schedule, 'require_systemd')
    @patch.object(schedule.pwd, 'getpwnam')
    @patch.object(schedule.shutil, 'which', return_value='/usr/sbin/runuser')
    def test_install_refuses_foreign_units_without_changing_files_or_services(self, *_):
        for suffix in ('service', 'timer'):
            for content in ('[Unit]\n', '# Managed by menu backup scheduler: /other\n'):
                with self.subTest(suffix=suffix, content=content):
                    path = self.units / f'menu-backup-docker.{suffix}'
                    path.write_text(content)
                    with (
                        patch.object(schedule, 'UNIT_DIRECTORY', self.units),
                        patch.object(schedule, 'atomic_write') as write,
                        patch.object(schedule.subprocess, 'run') as run,
                        self.assertRaisesRegex(ValueError, 'belongs to another checkout'),
                    ):
                        schedule.install(self.root, 'docker', self.options())
                    write.assert_not_called()
                    run.assert_not_called()
                    self.assertEqual(path.read_text(), content)
                    path.unlink()

    @patch.object(schedule, 'require_systemd')
    @patch.object(schedule.pwd, 'getpwnam')
    @patch.object(schedule.shutil, 'which', return_value='/usr/sbin/runuser')
    def test_failed_backup_probe_prevents_installation(self, *_):
        def run(command, **kwargs):
            if command[0] == '/usr/sbin/runuser':
                raise subprocess.CalledProcessError(1, command)

        for reinstall in (False, True):
            with self.subTest(reinstall=reinstall):
                if reinstall:
                    for filename, content in schedule.render_units(
                        self.root, 'docker', 'deploy', '01:00', 'UTC'
                    ).items():
                        (self.units / filename).write_text(content)
                before = {p.name: p.read_bytes() for p in self.units.iterdir()}
                with (
                    patch.object(schedule, 'UNIT_DIRECTORY', self.units),
                    patch.object(schedule, 'atomic_write') as write,
                    patch.object(schedule.subprocess, 'run', side_effect=run) as commands,
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    schedule.install(self.root, 'docker', self.options())
                write.assert_not_called()
                self.assertEqual({p.name: p.read_bytes() for p in self.units.iterdir()}, before)
                self.assertFalse(
                    any(
                        call.args[0][0] == 'systemctl'
                        and set(call.args[0][1:]) & {'enable', 'start', 'restart'}
                        for call in commands.call_args_list
                    )
                )

    @patch.object(schedule, 'require_systemd')
    @patch.object(schedule.pwd, 'getpwnam')
    @patch.object(schedule.shutil, 'which', return_value='/usr/sbin/runuser')
    def test_install_publishes_units_after_probe_and_enables_the_project_timer(self, *_):
        events = []

        def run(command, **kwargs):
            if command[0] == '/usr/sbin/runuser':
                events.append('probe')
            elif command[:2] == ['systemctl', 'enable']:
                self.assertTrue(list(self.units.glob('*.service')))
                self.assertTrue(list(self.units.glob('*.timer')))
                events.append('enable')
                self.assertIn('menu-backup-staging.timer', command)

        with (
            patch.object(schedule, 'UNIT_DIRECTORY', self.units),
            patch.object(schedule.subprocess, 'run', side_effect=run),
            redirect_stdout(StringIO()),
        ):
            schedule.install(self.root, 'staging', self.options())
        self.assertEqual(events, ['probe', 'enable'])
        for unit in self.units.iterdir():
            self.assertEqual(unit.stat().st_mode & 0o777, 0o644)
            schedule.assert_owned(unit, self.root)

    def test_unit_paths_escape_systemd_specifiers_and_environment_expansion(self):
        self.assertEqual(schedule.unit_quote('/srv/20%/$menu'), '"/srv/20%%/$$menu"')
        self.assertEqual(schedule.unit_path('/srv/20%/$menu'), '/srv/20%%/$menu')


if __name__ == '__main__':
    unittest.main()
