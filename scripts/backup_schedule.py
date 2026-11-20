"""Install project-scoped systemd backup timers; preview is available on any host."""

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNIT_DIRECTORY = Path('/etc/systemd/system')


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%').replace('$', '$$'), ensure_ascii=False)


def unit_path(value):
    # WorkingDirectory takes a complete path, not a quoted ExecStart argument.
    value = str(value)
    if value != value.strip() or '\n' in value or '\r' in value:
        raise ValueError('Checkout paths must not contain newlines or surrounding whitespace.')
    return value.replace('%', '%%')


def render_units(root, project, user, at, timezone):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*\$?', user):
        raise ValueError('Use an existing local Linux deployment username.')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', at):
        raise ValueError('--at must be HH:MM in 24-hour time.')
    try:
        if timezone != 'UTC':
            ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('Unknown timezone; install host timezone data or choose UTC.') from error
    name = f'menu-backup-{project}'
    templates = root / 'scripts/systemd'
    marker = f'# Managed by menu backup scheduler: {root}\n'
    service = (templates / 'menu-backup.service.example').read_text()
    service = service.replace('User=menu-deploy', f'User={user}')
    service = service.replace(
        '# Replace the user and both checkout paths before installation.\n', ''
    )
    service = service.replace(
        'WorkingDirectory=/opt/django-ordering-management', f'WorkingDirectory={unit_path(root)}'
    )
    command = ' '.join(
        [
            '/usr/bin/python3',
            unit_quote(root / 'scripts/manage.py'),
            '--project-name',
            unit_quote(project),
            'backup',
            '--source',
            'scheduled',
        ]
    )
    service = re.sub(r'^ExecStart=.*$', lambda _: f'ExecStart={command}', service, flags=re.M)
    timer = (templates / 'menu-backup.timer.example').read_text()
    timer = re.sub(r'^# Example schedule:.*\n', '', timer, flags=re.M)
    timer = re.sub(r'^OnCalendar=.*$', f'OnCalendar=*-*-* {at}:00 {timezone}', timer, flags=re.M)
    timer = timer.replace('Unit=menu-backup.service', f'Unit={name}.service')
    return {f'{name}.service': marker + service, f'{name}.timer': marker + timer}


def require_systemd(root=False):
    if sys.platform != 'linux' or not Path('/run/systemd/system').is_dir():
        raise ValueError(
            'This command requires a Linux host running systemd. Use --dry-run to preview.'
        )
    if root and os.geteuid() != 0:
        raise ValueError(
            'Installation and disabling require sudo; backups run as --user, not root.'
        )


def assert_owned(path, root):
    if path.is_symlink():
        raise ValueError(f'Refusing to replace a symlink: {path}')
    marker = f'# Managed by menu backup scheduler: {root}\n'
    if path.exists() and not path.read_text().startswith(marker):
        raise ValueError(
            f'{path} belongs to another checkout or was installed manually; inspect it first.'
        )


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def install(root, project, args):
    units = render_units(root, project, args.user, args.at, args.timezone)
    if args.dry_run:
        for filename, content in units.items():
            print(f'--- {UNIT_DIRECTORY / filename} ---\n{content}')
        print('Preview only: no files written, no backup run, no timer enabled.')
        return
    require_systemd(root=True)
    pwd.getpwnam(args.user)
    for filename in units:
        assert_owned(UNIT_DIRECTORY / filename, root)
    if not (root / 'menu_management/settings/env/.env').is_file():
        raise ValueError(
            'Start the application and prepare .env before installing its backup timer.'
        )
    runuser = shutil.which('runuser')
    if not runuser:
        raise ValueError('runuser (util-linux) is required to test backup as the deployment user.')
    with tempfile.TemporaryDirectory(prefix='menu-timer-') as directory:
        paths = []
        for filename, content in units.items():
            path = Path(directory) / filename
            path.write_text(content)
            paths.append(str(path))
        subprocess.run(['systemd-analyze', 'verify', *paths], check=True)
    # Test user permissions, Docker access, configuration, and a real dump before installing.
    subprocess.run(
        [
            runuser,
            '-u',
            args.user,
            '--',
            '/usr/bin/python3',
            str(root / 'scripts/manage.py'),
            '--project-name',
            project,
            'backup',
            '--source',
            'scheduled',
        ],
        check=True,
    )
    for filename, content in units.items():
        atomic_write(UNIT_DIRECTORY / filename, content)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    timer = f'menu-backup-{project}.timer'
    subprocess.run(['systemctl', 'enable', timer], check=True)
    subprocess.run(['systemctl', 'restart', timer], check=True)
    print(
        f'Enabled {timer}: daily {args.at} {args.timezone}; log: {root / "data/backups/backup.log"}'
    )


def handle(root, project, args):
    try:
        name = f'menu-backup-{project}'
        if args.schedule_action == 'install':
            install(root, project, args)
        elif args.schedule_action == 'disable':
            require_systemd(root=True)
            for suffix in ('service', 'timer'):
                assert_owned(UNIT_DIRECTORY / f'{name}.{suffix}', root)
            subprocess.run(['systemctl', 'disable', '--now', f'{name}.timer'], check=True)
            print(
                'Timer disabled; active backups and service retries are unchanged. '
                'Manual backup remains available.'
            )
        else:
            require_systemd()
            subprocess.run(['systemctl', 'list-timers', '--all', f'{name}.timer'], check=True)
            subprocess.run(
                [
                    'systemctl',
                    'show',
                    f'{name}.service',
                    '--property=LoadState,ActiveState,Result,ExecMainStatus',
                ],
                check=True,
            )
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f'Schedule error: {error}', file=sys.stderr)
        print(
            'Inspect schedule status and journalctl before retrying; backups are retained.',
            file=sys.stderr,
        )
        return 1
