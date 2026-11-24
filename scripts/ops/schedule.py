"""Install project-scoped systemd maintenance timers; preview is available on any host."""

import json
import logging
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
LOGGER = logging.getLogger(__name__)


def task_name(project, task='backup'):
    return f'menu-{task}-{project}'


def marker(root, task='backup'):
    return f'# Managed by menu {task} scheduler: {root}\n'


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%').replace('$', '$$'), ensure_ascii=False)


def unit_path(value):
    # WorkingDirectory takes a complete path, not a quoted ExecStart argument.
    value = str(value)
    if value != value.strip() or '\n' in value or '\r' in value:
        raise ValueError('Checkout paths must not contain newlines or surrounding whitespace.')
    return value.replace('%', '%%')


def render_units(root, project, user, at, timezone, task='backup'):
    if task not in ('backup', 'certificate'):
        raise ValueError('Unknown scheduled task.')
    if task == 'certificate' and (at is not None or timezone is not None):
        raise ValueError(
            '--at and --timezone apply only to backup; certificates are checked twice daily.'
        )
    at = '03:00' if at is None else at
    timezone = (
        ('UTC' if task == 'certificate' else 'Europe/Berlin') if timezone is None else timezone
    )
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*\$?', user):
        raise ValueError('Use an existing local Linux deployment username.')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', at):
        raise ValueError('--at must be HH:MM in 24-hour time.')
    try:
        if timezone != 'UTC':
            ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('Unknown timezone; install host timezone data or choose UTC.') from error
    name = task_name(project, task)
    templates = root / 'scripts/systemd'
    service = (templates / f'menu-{task}.service.example').read_text()
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
            'backup' if task == 'backup' else 'renew-certificate',
            '--source',
            'scheduled',
        ]
    )
    service = re.sub(r'^ExecStart=.*$', lambda _: f'ExecStart={command}', service, flags=re.M)
    timer = (templates / f'menu-{task}.timer.example').read_text()
    timer = re.sub(r'^# Example schedule:.*\n', '', timer, flags=re.M)
    if task == 'backup':
        timer = re.sub(
            r'^OnCalendar=.*$', f'OnCalendar=*-*-* {at}:00 {timezone}', timer, flags=re.M
        )
    timer = timer.replace(f'Unit=menu-{task}.service', f'Unit={name}.service')
    return {
        f'{name}.service': marker(root, task) + service,
        f'{name}.timer': marker(root, task) + timer,
    }


def require_systemd(root=False):
    if sys.platform != 'linux' or not Path('/run/systemd/system').is_dir():
        raise ValueError(
            'This command requires a Linux host running systemd. Use --dry-run to preview.'
        )
    if root and os.geteuid() != 0:
        raise ValueError('Installation and disabling require sudo; tasks run as --user, not root.')


def assert_owned(path, root, task='backup'):
    if path.is_symlink():
        raise ValueError(f'Refusing to replace a symlink: {path}')
    if path.exists() and not path.read_text().startswith(marker(root, task)):
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
    task = getattr(args, 'task', 'backup')
    units = render_units(root, project, args.user, args.at, args.timezone, task)
    if args.dry_run:
        return units
    require_systemd(root=True)
    pwd.getpwnam(args.user)
    for filename in units:
        assert_owned(UNIT_DIRECTORY / filename, root, task)
    if not (root / 'menu_management/settings/env/.env').is_file():
        raise ValueError('Start the application and prepare .env before installing its timer.')
    if task == 'certificate':
        from .deploy_config import load_config

        config = load_config(root / '.env.deploy')
        if config.tls_mode != 'letsencrypt':
            raise ValueError('Certificate scheduling requires TLS_MODE=letsencrypt.')
        check_certbot_timers(config)
    runuser = shutil.which('runuser')
    if not runuser:
        raise ValueError(
            'runuser (util-linux) is required to test the task as the deployment user.'
        )
    with tempfile.TemporaryDirectory(prefix='menu-timer-') as directory:
        paths = []
        for filename, content in units.items():
            path = Path(directory) / filename
            path.write_text(content)
            paths.append(str(path))
        subprocess.run(['systemd-analyze', 'verify', *paths], check=True)
    # Use the service's PATH for the deployment-user probe as well.
    service_path = '/usr/local/bin:/usr/bin:/bin' + (':/snap/bin' if task == 'certificate' else '')
    subprocess.run(
        [
            runuser,
            '-u',
            args.user,
            '--',
            '/usr/bin/env',
            f'PATH={service_path}',
            '/usr/bin/python3',
            str(root / 'scripts/manage.py'),
            '--project-name',
            project,
            'backup' if task == 'backup' else 'renew-certificate',
            '--source',
            'scheduled',
        ],
        check=True,
    )
    for filename, content in units.items():
        atomic_write(UNIT_DIRECTORY / filename, content)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    timer = f'{task_name(project, task)}.timer'
    subprocess.run(['systemctl', 'enable', timer], check=True)
    subprocess.run(['systemctl', 'restart', timer], check=True)
    LOGGER.info(
        'Enabled %s; task=%s. Inspect schedule status and its service journal.', timer, task
    )


def check_certbot_timers(config):
    """Report packaged timers and reject an explicit overlap; never disable other jobs."""
    result = subprocess.run(
        ['systemctl', 'list-timers', '--all', '--no-legend', '--plain'],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    directory = str((config.letsencrypt_dir / config.acme_env).resolve())
    for line in result.stdout.splitlines():
        if 'certbot' not in line.lower():
            continue
        services = re.findall(r'[^\s]+\.service', line)
        timers = re.findall(r'[^\s]+\.timer', line)
        if not services or not timers:
            continue
        state = subprocess.run(
            ['systemctl', 'show', timers[-1], '--property=ActiveState,UnitFileState'],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        if not re.search(r'^(ActiveState=active|UnitFileState=enabled(?:-runtime)?)$', state, re.M):
            continue
        command = subprocess.run(
            ['systemctl', 'show', services[-1], '--property=ExecStart', '--value'],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        option = re.search(r"--config-dir(?:=|\s+)[\"']?([^\"'\s;}]+)", command)
        if option and str(Path(option[1]).resolve()) == directory:
            raise ValueError(
                f'{services[-1]} already targets this certificate store; keep one scheduler.'
            )
        LOGGER.warning(
            'Existing Certbot task %s: review its config/cron settings for overlap; '
            'it was not changed.',
            services[-1],
        )


def certificate_timer_status(project):
    if sys.platform != 'linux' or not Path('/run/systemd/system').is_dir():
        return 'unavailable (Linux/systemd required)'
    result = subprocess.run(
        [
            'systemctl',
            'show',
            f'{task_name(project, "certificate")}.timer',
            '--property=LoadState,ActiveState,UnitFileState,NextElapseUSecRealtime',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip().replace('\n', ', ')


def handle(root, project, args):
    try:
        task = getattr(args, 'task', 'backup')
        name = task_name(project, task)
        if args.schedule_action == 'install':
            units = install(root, project, args)
            if args.dry_run:
                for filename, content in units.items():
                    sys.stdout.write(f'--- {UNIT_DIRECTORY / filename} ---\n{content}\n')
                LOGGER.info('Preview only: no files written, no task run, no timer enabled.')
        elif args.schedule_action == 'disable':
            require_systemd(root=True)
            for suffix in ('service', 'timer'):
                assert_owned(UNIT_DIRECTORY / f'{name}.{suffix}', root, task)
            subprocess.run(['systemctl', 'disable', '--now', f'{name}.timer'], check=True)
            LOGGER.info(
                'Disabled %s; active tasks and service retries are unchanged. '
                'Manual execution remains available.',
                f'{name}.timer',
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
        LOGGER.error('Schedule error: %s', error)
        LOGGER.error(
            'Inspect schedule status and journalctl before retrying; task data is retained.'
        )
        return 1
    except KeyboardInterrupt:
        LOGGER.warning(
            'Schedule operation interrupted; inspect units and timer status before retrying.'
        )
        return 130
