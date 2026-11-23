#!/usr/bin/env python3
"""Linux host operations for this checkout; requires only Python's standard library."""

import argparse
import fcntl
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / 'menu_management/settings/env/.env'
LOGGER = logging.getLogger(__name__)


class OperationError(Exception):
    pass


def private_write(path, content):
    """Create a private file exclusively; never replace existing credentials."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        target.write(content)


def write_backup_event(event):
    """Append one private JSON record, including attempts rejected by the operation lock."""
    directory = ROOT / 'data/backups'
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), **event}) + '\n'
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(directory / 'backup.log', flags, 0o600)
    with os.fdopen(fd, 'a') as target:
        fcntl.flock(target, fcntl.LOCK_EX)
        os.fchmod(target.fileno(), 0o600)
        target.write(line)
        target.flush()
        os.fsync(target.fileno())
    sys.stdout.write(line)
    sys.stdout.flush()


@contextmanager
def operation_lock():
    with (ROOT / '.deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationError(
                'Another management operation is running in this checkout.'
            ) from error
        yield


class Manager:
    def __init__(self, project):
        self.project = project
        self.base = [
            'docker',
            'compose',
            '--project-name',
            project,
            '-f',
            str(ROOT / 'docker/docker-compose.yml'),
        ]

    def compose(self, *args, **kwargs):
        return subprocess.run(self.base + list(args), cwd=ROOT, check=True, **kwargs)

    def preflight(self):
        version = subprocess.run(
            ['docker', 'compose', 'version', '--short'],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        match = re.match(r'v?(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (2, 24, 0):
            raise OperationError('Docker Compose 2.24.0 or newer is required.')
        subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, timeout=30)

    def prepare_env(self, hosts):
        if ENV.is_symlink():
            raise OperationError('Refusing a symlink for the runtime .env file.')
        if ENV.exists():
            ENV.chmod(0o600)
            LOGGER.info('Keeping existing configuration: %s', ENV)
            return
        # Refuse to invent credentials for a database that was initialized earlier.
        volumes = subprocess.run(
            [
                'docker',
                'volume',
                'ls',
                '--format',
                '{{.Name}}',
                '--filter',
                f'label=com.docker.compose.project={self.project}',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if volumes:
            raise OperationError(
                'Configuration is missing but project volumes exist. Restore the original .env; '
                'new passwords cannot authenticate to an existing database.'
            )
        content = (
            f'DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n'
            f'ALLOWED_HOSTS={hosts}\n'
            'POSTGRES_DB=menu_management\nPOSTGRES_USER=postgres\n'
            f'POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n'
            'POSTGRES_HOST=db\nPOSTGRES_PORT=5432\n'
            f'STAFF_PASSWORD={secrets.token_urlsafe(24)}\n'
        )
        ENV.parent.mkdir(parents=True, exist_ok=True)
        private_write(ENV, content)
        LOGGER.info(
            'Created private configuration: %s. '
            'Read STAFF_PASSWORD from that file to sign in; passwords are not logged.',
            ENV,
        )

    def require_env(self):
        if not ENV.is_file():
            raise OperationError('Runtime .env is missing. Run start for a new installation.')
        self.compose('config', '--quiet')

    def verify(self):
        self.compose('exec', '-T', 'web', 'python', 'manage.py', 'check')
        self.compose('exec', '-T', 'web', 'python', 'manage.py', 'migrate', '--check')
        # Exercise Docker DNS, Nginx, Django, the database-backed menu, and a static asset.
        code = """
import os
from urllib.request import Request, urlopen
host = os.environ['ALLOWED_HOSTS'].split(',')[0].strip().lstrip('.')
if host == '*':
    host = 'localhost'
for path in ('/health/', '/menu/', '/staff/login/', '/static/admin/css/base.css'):
    with urlopen(Request('http://nginx' + path, headers={'Host': host}), timeout=15) as response:
        assert response.status == 200, path
        if path == '/health/':
            assert response.read().strip() == b'ok'
    print(path + ': OK')
"""
        self.compose('exec', '-T', 'web', 'python', '-c', code)
        self.compose('ps', '-a')

    def diagnostics(self):
        for args in [
            ('ps', '-a'),
            ('logs', '--no-color', '--tail=80', 'web', 'db', 'nginx', 'collect-static'),
        ]:
            try:
                self.compose(*args)
            except (OSError, subprocess.CalledProcessError):
                LOGGER.warning('Docker diagnostic command unavailable: %s', args[0])

    @contextmanager
    def backup_record(self, source):
        record = {'run_id': secrets.token_hex(8), 'project': self.project, 'source': source}
        started = time.monotonic()
        write_backup_event({**record, 'status': 'started'})
        try:
            yield record
        except BaseException as error:
            if isinstance(error, subprocess.CalledProcessError):
                detail = f'Command failed (exit {error.returncode})'
                if error.stderr:
                    stderr = (
                        error.stderr
                        if isinstance(error.stderr, str)
                        else error.stderr.decode(errors='replace')
                    )
                    detail += ': ' + stderr.strip()[:2000]
            else:
                detail = str(error) or type(error).__name__
            try:
                write_backup_event(
                    {
                        **record,
                        'status': 'failed',
                        'error': detail,
                        'error_type': type(error).__name__,
                        'duration_seconds': round(time.monotonic() - started, 3),
                    }
                )
            except OSError as log_error:
                LOGGER.error('Cannot record backup failure: %s', log_error)
            raise
        else:
            write_backup_event(
                {
                    **record,
                    'status': 'success',
                    'duration_seconds': round(time.monotonic() - started, 3),
                }
            )

    def backup(self, source='manual', prepare=False):
        with self.backup_record(source) as record:
            record['stage'] = 'lock'
            with operation_lock() if prepare else nullcontext():
                if prepare:
                    record['stage'] = 'preflight'
                    self.preflight()
                    record['stage'] = 'configuration'
                    self.require_env()
                path = self._backup_archive(record)
                record.update(archive=str(path), bytes=path.stat().st_size, stage='completed')
        return path

    def _backup_archive(self, record):
        directory = ROOT / 'data/backups'
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path = directory / f'{self.project}-{stamp}-{secrets.token_hex(4)}.dump'
        record['archive'] = str(path)
        temporary = path.with_suffix('.partial')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            record['stage'] = 'dump'
            with os.fdopen(fd, 'wb') as target:
                self.compose(
                    'exec',
                    '-T',
                    'db',
                    'sh',
                    '-ec',
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                    stdout=target,
                    stderr=subprocess.PIPE,
                )
            if not temporary.stat().st_size:
                raise OperationError('Database backup is empty; operation aborted.')
            record['stage'] = 'archive-check'
            with temporary.open('rb') as source:
                self.compose(
                    'exec',
                    '-T',
                    'db',
                    'pg_restore',
                    '--list',
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            record['stage'] = 'publish'
            temporary.rename(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def start(self, args, update=False):
        self.prepare_env(args.hosts)
        self.require_env()
        if update:
            # Build failure leaves the running release untouched.
            self.compose('build')
            self.compose('up', '-d', '--wait', 'db')
            self.backup(source='update')
            flags = ['--force-recreate']
        else:
            # Compose builds missing images on first install; reuse images on later starts.
            # Rebuilding an existing release belongs to update, which takes a backup.
            flags = []
        self.compose('up', '-d', '--wait', '--wait-timeout', '180', *flags)
        if not args.no_seed:
            self.compose('exec', '-T', 'web', 'python', 'manage.py', 'load_initial_data')
        self.verify()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--project-name',
        default='docker',
        help='Compose identity; default docker preserves the existing stack/volumes',
    )
    sub = parser.add_subparsers(dest='action', required=True)
    config = sub.add_parser('config', help='Inspect deployment configuration without host changes')
    config_commands = config.add_subparsers(dest='config_action', required=True)
    check = config_commands.add_parser('check', help='Validate deployment parameter syntax')
    check.add_argument(
        '--file', type=Path, help='Deployment file; default: .env.deploy in the repository root'
    )
    for action in ('start', 'update'):
        command = sub.add_parser(
            action,
            help='Generate missing configuration and deploy'
            if action == 'start'
            else 'Build, back up DB, and deploy this checkout',
        )
        command.add_argument(
            '--hosts',
            default='localhost,127.0.0.1',
            help='ALLOWED_HOSTS for a NEW .env only; existing files are preserved',
        )
        command.add_argument(
            '--no-seed',
            action='store_true',
            help='Do not import the bundled menu into an empty database',
        )
    sub.add_parser(
        'stop', help='Stop services; keep containers and data (pause service availability)'
    )
    sub.add_parser('down', help='Remove containers and network; retain all persistent volumes')
    sub.add_parser('status', help='Show container state')
    sub.add_parser('doctor', help='Show status/logs and verify the application')
    backup = sub.add_parser('backup', help='Create and inspect a private PostgreSQL dump')
    backup.add_argument(
        '--source',
        choices=('manual', 'scheduled'),
        default='manual',
        help='Origin recorded in data/backups/backup.log',
    )
    logs = sub.add_parser('logs', help='Show recent logs')
    logs.add_argument('--follow', action='store_true')
    schedule = sub.add_parser('schedule', help='Manage a Linux systemd backup timer')
    schedule_commands = schedule.add_subparsers(dest='schedule_action', required=True)
    install = schedule_commands.add_parser(
        'install', help='Test backup, install, and enable the timer'
    )
    install.add_argument(
        '--user', required=True, help='Existing deployment account with Docker access'
    )
    install.add_argument('--at', default='03:00', help='Daily HH:MM (default 03:00)')
    install.add_argument(
        '--timezone', default='Europe/Berlin', help='IANA timezone (default Europe/Berlin)'
    )
    install.add_argument(
        '--dry-run', action='store_true', help='Preview units without changing the host'
    )
    schedule_commands.add_parser('status', help='Show the timer and last service result')
    schedule_commands.add_parser(
        'disable', help='Disable future runs; retain units, backups, and logs'
    )
    destroy = sub.add_parser(
        'destroy', help='Back up DB, then delete containers AND project volumes'
    )
    destroy.add_argument(
        '--confirm',
        required=True,
        metavar='PROJECT_NAME',
        help='Must exactly match --project-name; no interactive default',
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.project_name):
        parser.error('project-name must contain lowercase letters, digits, underscores or hyphens')
    if args.action in ('start', 'update') and not re.fullmatch(
        r'[A-Za-z0-9.,*:\[\]-]+', args.hosts
    ):
        parser.error(
            'hosts must be comma-separated hostnames/IP addresses, without spaces or schemes'
        )
    if args.action == 'destroy' and args.confirm != args.project_name:
        parser.error('destroy --confirm must exactly match the Compose project name')
    return args


def check_deploy_config(args):
    from deploy_config import ConfigurationError, load_config

    path = args.file if args.file is not None else ROOT / '.env.deploy'
    try:
        config = load_config(path)
    except ConfigurationError as error:
        LOGGER.error('%s', error)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning('Configuration check interrupted; no changes were made.')
        return 130
    # Command results belong on stdout; diagnostic logs go to stderr.
    json.dump(config.values(), sys.stdout, indent=2)
    sys.stdout.write('\n')
    LOGGER.info('Deployment configuration syntax is valid.')
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = parse_args(argv)
    if args.action == 'config':
        return check_deploy_config(args)
    if args.action == 'schedule':
        from backup_schedule import handle

        return handle(ROOT, args.project_name, args)
    manager = Manager(args.project_name)
    try:
        if args.action == 'backup':
            # Include lock conflicts and preflight errors in the same audit as dump failures.
            manager.backup(source=args.source, prepare=True)
            return 0
        with nullcontext() if args.action in ('status', 'logs', 'doctor') else operation_lock():
            manager.preflight()
            if args.action in ('start', 'update'):
                manager.start(args, update=args.action == 'update')
            else:
                manager.require_env()
                if args.action == 'stop':
                    manager.compose('stop')
                elif args.action == 'down':
                    manager.compose('down')
                elif args.action == 'status':
                    manager.compose('ps', '-a')
                elif args.action == 'logs':
                    manager.compose('logs', '--tail=100', *(['--follow'] if args.follow else []))
                elif args.action == 'doctor':
                    manager.diagnostics()
                    manager.verify()
                elif args.action == 'destroy':
                    manager.compose('up', '-d', '--wait', 'db')
                    manager.backup(source='destroy')  # Failure aborts deletion.
                    manager.compose('down', '--volumes')
            return 0
    except (OperationError, OSError, subprocess.SubprocessError) as error:
        # CalledProcessError includes the command: do not include captured env contents.
        detail = (
            f'Command failed (exit {error.returncode}).'
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        LOGGER.error('%s', detail)
        if args.action not in ('logs', 'status'):
            manager.diagnostics()
        LOGGER.error(
            'Check Docker access, available ports/disk, and the existing .env. '
            'Database variables do not rotate credentials in an existing volume.'
        )
        return 1
    except KeyboardInterrupt:
        LOGGER.warning('Interrupted. No automatic rollback or data deletion was performed.')
        return 130


if __name__ == '__main__':

    def terminate(signum, frame):
        raise InterruptedError('Backup or management operation terminated by SIGTERM')

    signal.signal(signal.SIGTERM, terminate)
    sys.exit(main())
