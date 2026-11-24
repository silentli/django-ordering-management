#!/usr/bin/env python3
"""Linux host operations for this checkout; requires only Python's standard library."""

import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from ops.certificates import (
    CertificateError,
    ensure_certificate,
    fingerprint,
    inspect_certificate,
    renew_certificate,
    renewal_record,
)
from ops.deploy_config import ConfigurationError, load_config
from ops.nginx_runtime import (
    RuntimePreparationError,
    https_candidate,
    https_configuration,
    prepare,
    probe,
    site_mode,
)

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
        self.deployment = None
        self.compose_environment = None
        self.base = [
            'docker',
            'compose',
            '--project-name',
            project,
            '-f',
            str(ROOT / 'docker/docker-compose.yml'),
        ]

    def configure_deployment(self, path=None):
        target = path if path is not None else ROOT / '.env.deploy'
        try:
            config = (
                load_config(target)
                if path is not None or target.exists() or target.is_symlink()
                else None
            )
        except ConfigurationError as error:
            raise OperationError(str(error)) from error
        environment = os.environ.copy()
        if config is not None:
            environment.update(config.compose_environment())
            LOGGER.info('Using deployment configuration: %s', target)
        self.deployment = config
        self.compose_environment = environment
        try:
            site = ROOT / 'runtime/nginx/conf.d/site.conf'
            https = site.is_file() and site_mode(ROOT) == 'https'
        except (RuntimePreparationError, OSError, UnicodeError) as error:
            # Backup/status/stop remain available even with a broken Nginx configuration.
            LOGGER.warning('Cannot infer HTTPS settings from site.conf: %s', error)
            https = False
        self.set_https_environment(https)

    def set_https_environment(self, enabled):
        self.compose_environment['DJANGO_HTTPS'] = 'true' if enabled else 'false'
        self.compose_environment['DJANGO_CSRF_TRUSTED_ORIGINS'] = (
            f'https://{self.deployment.public_host}'
            if enabled and self.deployment is not None
            else ''
        )

    def compose(self, *args, **kwargs):
        if self.compose_environment is None:
            self.configure_deployment()
        return subprocess.run(
            self.base + list(args), cwd=ROOT, env=self.compose_environment, check=True, **kwargs
        )

    def preflight(self):
        if self.compose_environment is None:
            self.configure_deployment()
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

    def require_env(self, check_hosts=False):
        if self.compose_environment is None:
            self.configure_deployment()
        if not ENV.is_file():
            raise OperationError('Runtime .env is missing. Run start for a new installation.')
        if self.deployment is None or not check_hosts:
            self.compose('config', '--quiet')
            return
        # Compose parses the application's env_file. Do not log its resolved output:
        # it includes credentials as well as ALLOWED_HOSTS.
        result = self.compose('config', '--format', 'json', capture_output=True, text=True)
        try:
            allowed_hosts = json.loads(result.stdout)['services']['web']['environment'][
                'ALLOWED_HOSTS'
            ]
        except (ValueError, KeyError, TypeError) as error:
            raise OperationError(
                'Cannot inspect ALLOWED_HOSTS in the resolved Compose configuration.'
            ) from error
        try:
            if not isinstance(allowed_hosts, str):
                raise ConfigurationError('ALLOWED_HOSTS must be a comma-separated string.')
            self.deployment.check_allowed_hosts(allowed_hosts)
        except ConfigurationError as error:
            raise OperationError(str(error)) from error

    def verify(self, nginx_mode='http'):
        self.compose('exec', '-T', 'web', 'python', 'manage.py', 'check')
        self.compose('exec', '-T', 'web', 'python', 'manage.py', 'migrate', '--check')
        if nginx_mode == 'https':
            self.verify_https()
            self.compose('ps', '-a')
            return
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

    def verify_https(self):
        self.compose('exec', '-T', 'nginx', 'nginx', '-t')
        certificate = None
        if self.deployment is not None and self.deployment.tls_mode == 'letsencrypt':
            try:
                certificate = inspect_certificate(self.deployment)
            except CertificateError as error:
                raise OperationError(str(error)) from error
            if certificate is None:
                raise OperationError('No usable certificate for HTTPS verification.')
        host = self.deployment.public_host if self.deployment else ''
        # Staging certificates are trusted only for this probe, never system-wide.
        trust = (
            certificate.fullchain.read_text()
            if certificate is not None and certificate.acme_env == 'staging'
            else ''
        )
        code = """
import http.client
import os
import socket
import ssl
import sys
import time
import django
django.setup()
from django.conf import settings
host = sys.argv[1] or os.environ['ALLOWED_HOSTS'].split(',')[0].strip()
context = ssl.create_default_context(cadata=sys.argv[2] or None)
if sys.argv[2]:
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
assert settings.SECURE_SSL_REDIRECT
assert settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE
assert settings.SECURE_PROXY_SSL_HEADER == ('HTTP_X_FORWARDED_PROTO', 'https')
class Connection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = context.wrap_socket(
            socket.create_connection(('nginx', 443), timeout=10), server_hostname=host)
for path in ('/health/', '/menu/', '/staff/login/', '/admin/login/', '/static/admin/css/base.css'):
    for attempt in range(20):
        connection = Connection(host, timeout=10, context=context)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 200, (path, response.status)
            if path == '/health/':
                assert response.read().strip() == b'ok'
            if path in ('/staff/login/', '/admin/login/'):
                cookies = response.headers.get_all('Set-Cookie', [])
                assert any('csrftoken=' in c and '; Secure' in c for c in cookies)
            break
        except (OSError, http.client.HTTPException, AssertionError):
            if attempt == 19:
                raise
            time.sleep(0.25)
        finally:
            connection.close()
    print(path + ': HTTPS OK')
connection = http.client.HTTPConnection('nginx', 80, timeout=10)
connection.request('GET', '/menu/?probe=1', headers={'Host': host})
response = connection.getresponse()
assert response.status == 308
assert response.getheader('Location') == 'https://' + host + '/menu/?probe=1'
connection.close()
"""
        self.compose('exec', '-T', 'web', 'python', '-c', code, host, trust)
        if certificate is not None:
            LOGGER.info(
                'Certificate expires %s (%s).',
                certificate.expires_at.isoformat(),
                certificate.acme_env,
            )
        if self.deployment is not None:
            with probe(self.deployment.certbot_webroot) as (url, expected):
                code = (
                    'import http.client, sys; '
                    "c=http.client.HTTPConnection('nginx', 80, timeout=10); "
                    "c.request('GET', sys.argv[1]); r=c.getresponse(); "
                    'assert r.status == 200 and r.read().decode() == sys.argv[2]; c.close()'
                )
                self.compose('exec', '-T', 'web', 'python', '-c', code, url, expected)
        LOGGER.info('HTTPS, redirects, login forms, secure CSRF cookies and ACME path verified.')

    def activate_https(self, certificate, no_seed):
        https_candidate(ROOT, self.deployment, certificate)
        previous_environment = self.compose_environment.copy()
        self.set_https_environment(True)
        try:
            # Prepare the backend while Nginx continues serving its previous configuration.
            self.compose('up', '-d', '--wait', '--wait-timeout', '180', 'web')
            if not no_seed:
                self.compose('exec', '-T', 'web', 'python', 'manage.py', 'load_initial_data')
            with https_configuration(ROOT, self.deployment, certificate):
                self.compose('exec', '-T', 'nginx', 'nginx', '-t')
                self.compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
                self.verify(nginx_mode='https')
        except BaseException:
            self.compose_environment = previous_environment
            # The context restores site.conf before reloading the previous configuration.
            try:
                self.compose('exec', '-T', 'nginx', 'nginx', '-t')
                self.compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
                self.compose('up', '-d', '--wait', '--wait-timeout', '180', 'web')
            except (OperationError, OSError, subprocess.SubprocessError) as error:
                LOGGER.error('Recovery failed; inspect site.conf and service logs: %s', error)
            LOGGER.error(
                'HTTPS activation failed; configuration/service recovery was attempted. '
                'Database migrations and seeded data are not rolled back.'
            )
            raise
        LOGGER.info('HTTPS enabled for %s (%s).', self.deployment.public_host, certificate.acme_env)

    def nginx_endpoint(self, port):
        result = self.compose(
            'port', 'nginx', str(port), capture_output=True, text=True, timeout=15
        )
        try:
            address, published = result.stdout.strip().splitlines()[0].rsplit(':', 1)
            address = address.strip('[]')
            address = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(address, address)
            return address, int(published)
        except (IndexError, ValueError) as error:
            raise OperationError(f'Cannot find the published Nginx port {port}.') from error

    def served_fingerprint(self, endpoint):
        # No application data is sent. Only an exact match to the validated local
        # certificate is accepted; expiry must not prevent replacement of an old leaf.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection(endpoint, timeout=5) as connection:
                with context.wrap_socket(
                    connection, server_hostname=self.deployment.public_host
                ) as tls:
                    return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        except OSError:
            return None

    def renew(self, source='manual'):
        with renewal_record(ROOT, self.project, source) as record:
            with operation_lock():
                record['stage'] = 'preflight'
                self.preflight()
                config = self.deployment
                if config is None or config.tls_mode != 'letsencrypt':
                    raise OperationError('Renewal requires .env.deploy with TLS_MODE=letsencrypt.')
                record.update(host=config.public_host, acme_env=config.acme_env)
                self.require_env()
                if site_mode(ROOT) != 'https':
                    raise OperationError(
                        'Renewal requires active HTTPS; complete setup with start.'
                    )
                previous = inspect_certificate(config, allow_expired=True)
                if previous is None:
                    raise OperationError('Managed certificate is missing; run start first.')
                _, active, expected = https_candidate(ROOT, config, previous)
                if (
                    re.sub(r'#[^\n]*', '', active).strip()
                    != re.sub(r'#[^\n]*', '', expected).strip()
                ):
                    raise OperationError(
                        'Active HTTPS differs from .env.deploy; apply it with start first.'
                    )
                record['stage'] = 'nginx-preflight'
                self.compose('exec', '-T', 'nginx', 'nginx', '-t', timeout=30)
                # Check the running webroot without starting services.
                with probe(config.certbot_webroot) as (url, body):
                    connection = http.client.HTTPConnection(*self.nginx_endpoint(80), timeout=5)
                    try:
                        connection.request('GET', url, headers={'Host': config.public_host})
                        response = connection.getresponse()
                        if response.status != 200 or response.read(len(body) + 1) != body.encode():
                            raise OperationError('Nginx ACME webroot does not match .env.deploy.')
                    finally:
                        connection.close()
                endpoint = self.nginx_endpoint(443)
                record['stage'] = 'renewal'
                certificate, changed = renew_certificate(config)
                expected_fingerprint = fingerprint(certificate)
                record.update(
                    renewed=changed,
                    expires_at=certificate.expires_at.isoformat(),
                    fingerprint=expected_fingerprint,
                    reloaded=False,
                )
                record['stage'] = 'nginx-validation'
                self.compose('exec', '-T', 'nginx', 'nginx', '-t', timeout=30)
                if self.served_fingerprint(endpoint) != expected_fingerprint:
                    record['stage'] = 'reload'
                    self.compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload', timeout=30)
                    record['reloaded'] = True
                    record['stage'] = 'verify-loaded'
                    deadline = time.monotonic() + 10
                    while self.served_fingerprint(endpoint) != expected_fingerprint:
                        if time.monotonic() >= deadline:
                            raise OperationError(
                                'Nginx is not serving the validated certificate; retry renewal.'
                            )
                        time.sleep(0.2)
                record['stage'] = 'completed'
                LOGGER.info(
                    'Certificate check complete: renewed=%s, reloaded=%s, expires=%s',
                    changed,
                    record['reloaded'],
                    certificate.expires_at.isoformat(),
                )

    def prepare_runtime(self, initialize=True):
        try:
            runtime = prepare(
                ROOT,
                self.compose_environment,
                tls_requested=self.deployment is not None
                and self.deployment.tls_mode == 'letsencrypt',
                initialize=initialize,
            )
        except (RuntimePreparationError, OSError) as error:
            raise OperationError(f'Cannot prepare Nginx runtime: {error}') from error
        LOGGER.info('Active Nginx configuration: %s', runtime.mode)
        return runtime

    def start_bootstrap(self, runtime):
        self.compose('up', '-d', '--no-deps', 'nginx')
        self.compose('exec', '-T', 'nginx', 'nginx', '-t')
        self.compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
        result = self.compose('port', 'nginx', '80', capture_output=True, text=True)
        bindings = result.stdout.strip().splitlines()
        if not bindings:
            raise OperationError('Nginx has no published HTTP port for bootstrap validation.')
        try:
            address, port = bindings[0].rsplit(':', 1)
            port = int(port)
        except ValueError as error:
            raise OperationError('Cannot determine the published Nginx HTTP address.') from error
        address = '127.0.0.1' if address == '0.0.0.0' else address.strip('[]')
        try:
            with probe(runtime.webroot) as (url, expected):
                deadline = time.monotonic() + 10
                while True:
                    connection = http.client.HTTPConnection(address, port, timeout=3)
                    try:
                        connection.request('GET', url)
                        response = connection.getresponse()
                        if (
                            response.status != 200
                            or response.read(len(expected) + 1).decode() != expected
                        ):
                            raise OperationError(
                                'Bootstrap probe did not return the expected file content.'
                            )
                        break
                    except (OperationError, OSError, http.client.HTTPException):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
                    finally:
                        connection.close()
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise OperationError(
                f'Bootstrap validation failed; check webroot mounts and permissions: {error}'
            ) from error
        LOGGER.info(
            'HTTP bootstrap verified locally; '
            'external port 80 reachability still requires verification.'
        )
        LOGGER.info('Ready for certificate issuance. HTTPS is not enabled.')

    def diagnostics(self):
        for args in [
            ('ps', '-a'),
            ('logs', '--no-color', '--tail=80', 'web', 'db', 'nginx', 'collect-static'),
        ]:
            try:
                self.compose(*args)
            except (OperationError, OSError, subprocess.SubprocessError):
                LOGGER.warning('Docker diagnostic command unavailable: %s', args[0])

    def diagnose(self, config_path=None):
        try:
            self.configure_deployment(config_path)
        except OperationError as error:
            LOGGER.error('%s', error)
            return 1
        try:
            self.preflight()
        except (OperationError, OSError, subprocess.SubprocessError) as error:
            LOGGER.error('Docker preflight failed: %s', error)
            return 1
        self.diagnostics()
        from ops.schedule import certificate_timer_status

        try:
            LOGGER.info('Certificate timer: %s', certificate_timer_status(self.project))
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning('Certificate timer status unavailable: %s', error)
        failed = False
        try:
            self.require_env(check_hosts=True)
            mode = site_mode(ROOT)
            if mode == 'bootstrap':
                self.compose('exec', '-T', 'nginx', 'nginx', '-t')
                LOGGER.info('Bootstrap configuration is active; application checks are deferred.')
                if self.deployment is None:
                    raise CertificateError(
                        'A deployment configuration is required to inspect certificates.'
                    )
                certificate = inspect_certificate(self.deployment)
                if certificate is None:
                    raise CertificateError(
                        'No usable managed certificate was found for this deployment.'
                    )
                LOGGER.info(
                    'Certificate expires %s (%s).',
                    certificate.expires_at.isoformat(),
                    certificate.acme_env,
                )
            else:
                self.verify(nginx_mode=mode)
        except (
            OperationError,
            RuntimePreparationError,
            CertificateError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            detail = (
                f'Command failed (exit {error.returncode}).'
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
            LOGGER.error('Deployment checks failed: %s', detail)
            failed = True
        return 1 if failed else 0

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
        if self.compose_environment is None:
            self.configure_deployment()
        if not update:
            hosts = args.hosts
            if self.deployment is not None:
                hosts = hosts or ','.join(
                    dict.fromkeys([self.deployment.public_host, 'localhost', '127.0.0.1'])
                )
                if not ENV.exists():
                    try:
                        self.deployment.check_allowed_hosts(hosts)
                    except ConfigurationError as error:
                        raise OperationError(str(error)) from error
            self.prepare_env(hosts or 'localhost,127.0.0.1')
        self.require_env(check_hosts=True)
        runtime = self.prepare_runtime(initialize=not update)
        if runtime.mode == 'bootstrap' or (
            not update and self.deployment is not None and self.deployment.tls_mode == 'letsencrypt'
        ):
            if runtime.mode == 'bootstrap':
                self.start_bootstrap(runtime)
            else:
                self.compose('up', '-d', '--no-deps', 'nginx')
            try:
                certificate = ensure_certificate(self.deployment)
            except CertificateError as error:
                raise OperationError(str(error)) from error
            self.activate_https(certificate, args.no_seed)
            return
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
        self.compose('exec', '-T', 'nginx', 'nginx', '-t')
        self.compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
        self.verify(nginx_mode=runtime.mode)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python3 scripts/manage.py',
        description=(
            'Manage Docker host deployment from the repository root.\n'
            'Django application commands use the separate root manage.py.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Lifecycle: start, update, stop, down, destroy\n'
            'Inspection: status, logs, diagnose\n'
            'Maintenance: backup, renew-certificate, schedule\n'
            'Use COMMAND --help for details. --project-name works before or after a command.\n\n'
            'Examples:\n'
            '  python3 scripts/manage.py start\n'
            '  python3 scripts/manage.py update\n'
            '  python3 scripts/manage.py diagnose\n'
            '  python3 scripts/manage.py schedule status --task certificate'
        ),
    )
    project_help = 'Compose project name (default: docker); keep consistent across operations'
    parser.add_argument('--project-name', default='docker', metavar='NAME', help=project_help)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('start', 'update'):
        command = sub.add_parser(
            action,
            help='Initialize configuration and start HTTP or HTTPS'
            if action == 'start'
            else 'Build current code, back up DB, and recreate services',
            description=(
                'Prepare missing configuration and start the application. TLS mode runs '
                'bootstrap, obtains or reuses a certificate, and enables HTTPS. Existing '
                'images are reused; missing images are built. Use update to rebuild changed code.'
                if action == 'start'
                else 'Build this checkout, back up the database, recreate services and verify '
                'the application. Requires existing configuration; complete TLS setup with start.'
            ),
        )
        if action == 'start':
            command.add_argument(
                '--hosts',
                metavar='HOSTS',
                help='Initial ALLOWED_HOSTS; localhost,127.0.0.1 plus PUBLIC_HOST when configured. '
                'Existing application .env is preserved',
            )
        command.add_argument(
            '--no-seed',
            action='store_true',
            help='Skip importing bundled menu data when the menu table is empty',
        )
    sub.add_parser(
        'stop',
        help='Stop services; retain containers and data',
        description='Pause services while retaining containers and data. Use start to resume.',
    )
    sub.add_parser(
        'down',
        help='Remove containers and network; retain data volumes',
        description='Remove project containers and network while retaining data volumes. '
        'Use start to recreate services with the existing configuration and data.',
    )
    sub.add_parser(
        'status',
        help='Show container state, health and ports',
        description='Quick container-state inspection. Use diagnose to check configuration '
        'and application requests.',
    )
    diagnose = sub.add_parser(
        'diagnose',
        help='Check configuration and the running application',
        description='Check deployment parameters, Docker, Nginx and application requests; '
        'show logs, certificate expiry and timer state when applicable. '
        'Configuration and running services are preserved.',
    )
    diagnose.add_argument(
        '--config',
        type=Path,
        metavar='PATH',
        help='Deployment file for diagnostics only (default: repository .env.deploy if present)',
    )
    backup = sub.add_parser(
        'backup',
        help='Run a PostgreSQL backup now',
        description='Create and inspect a private PostgreSQL archive and record the result '
        'in data/backups/backup.log. Automatic backups are configured with schedule install.',
    )
    backup.add_argument(
        '--source',
        choices=('manual', 'scheduled'),
        default='manual',
        help=argparse.SUPPRESS,  # Internal timer/cron audit metadata.
    )
    renew = sub.add_parser(
        'renew-certificate',
        help='Check certificate renewal and reload Nginx when needed',
        description='Run one renewal check using .env.deploy and active HTTPS. Certbot decides '
        'whether renewal is due. Validate and load any pending certificate with nginx -t, '
        'reload and a live fingerprint check. Automatic checks use schedule --task certificate.',
    )
    renew.add_argument(
        '--source', choices=('manual', 'scheduled'), default='manual', help=argparse.SUPPRESS
    )
    logs = sub.add_parser(
        'logs',
        help='Show container logs',
        description='Show the latest 100 log lines from project containers. '
        'Use --follow to stream new output until Ctrl+C.',
    )
    logs.add_argument('-f', '--follow', action='store_true', help='Stream logs until interrupted')
    schedule = sub.add_parser(
        'schedule',
        help='Install, inspect or disable maintenance timers',
        description='Manage Linux systemd timers for backup (default) or certificate renewal. '
        'The backup and renew-certificate commands execute those tasks manually.',
    )
    schedule_commands = schedule.add_subparsers(dest='schedule_action', required=True)
    install = schedule_commands.add_parser(
        'install',
        help='Check the task, install units and enable its timer',
        description='Validate systemd units and run the selected task once as --user, '
        'then install units and enable the timer. Requires Linux/systemd and sudo. '
        'Backup: daily 03:00 Europe/Berlin by default. Certificate: twice daily UTC with jitter. '
        'Use --dry-run to preview on any host without executing the task or writing units.',
    )
    install.add_argument(
        '--user',
        required=True,
        metavar='USER',
        help='Existing task execution account with Docker and required file permissions',
    )
    install.add_argument('--at', metavar='HH:MM', help='Backup only: daily time (default: 03:00)')
    install.add_argument(
        '--timezone', metavar='ZONE', help='Backup only: IANA timezone (default: Europe/Berlin)'
    )
    install.add_argument(
        '--dry-run', action='store_true', help='Preview units without changing the host'
    )
    schedule_commands.add_parser(
        'status',
        help='Show the selected timer and last service result',
        description='Show timer activations and the last service result for --task. '
        'Requires Linux/systemd; sudo is normally unnecessary.',
    )
    schedule_commands.add_parser(
        'disable',
        help='Disable the selected timer; retain units and task data',
        description='Disable future timer activations for --task. Active attempts and service '
        'retries continue; manual execution remains available. Requires sudo.',
    )
    for command in (schedule, *schedule_commands.choices.values()):
        command.add_argument(
            '--task',
            choices=('backup', 'certificate'),
            default='backup' if command is schedule else argparse.SUPPRESS,
            help='Scheduled task (default: backup)',
        )
    destroy = sub.add_parser(
        'destroy',
        help='Back up DB, then delete containers AND project data volumes',
        description='Create and inspect a database archive, then remove project containers and '
        'data volumes, including database and media. Requires --confirm to match '
        '--project-name. Use down to retain volumes.',
    )
    destroy.add_argument(
        '--confirm',
        required=True,
        metavar='PROJECT_NAME',
        help='Must exactly match --project-name; no interactive default',
    )
    # Suppressed defaults preserve a project supplied before the subcommand.
    for command in [*sub.choices.values(), *schedule_commands.choices.values()]:
        command.add_argument(
            '--project-name',
            default=argparse.SUPPRESS,
            metavar='NAME',
            help=project_help,
        )
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.project_name):
        parser.error('project-name must contain lowercase letters, digits, underscores or hyphens')
    if (
        args.action == 'start'
        and args.hosts is not None
        and not re.fullmatch(r'[A-Za-z0-9.,*:\[\]-]+', args.hosts)
    ):
        parser.error(
            'hosts must be comma-separated hostnames/IP addresses, without spaces or schemes'
        )
    if (
        args.action == 'schedule'
        and args.schedule_action == 'install'
        and args.task == 'certificate'
        and (args.at is not None or args.timezone is not None)
    ):
        install.error('--at and --timezone are backup-only; certificate checks run twice daily UTC')
    if args.action == 'destroy' and args.confirm != args.project_name:
        parser.error('destroy --confirm must exactly match the Compose project name')
    return args


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = parse_args(argv)
    if args.action == 'schedule':
        from ops.schedule import handle

        return handle(ROOT, args.project_name, args)
    manager = Manager(args.project_name)
    try:
        if args.action == 'diagnose':
            return manager.diagnose(args.config)
        if args.action == 'renew-certificate':
            manager.renew(source=args.source)
            return 0
        if args.action == 'backup':
            # Include lock conflicts and preflight errors in the same audit as dump failures.
            manager.backup(source=args.source, prepare=True)
            return 0
        with nullcontext() if args.action in ('status', 'logs') else operation_lock():
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
                elif args.action == 'destroy':
                    manager.compose('up', '-d', '--wait', 'db')
                    manager.backup(source='destroy')  # Failure aborts deletion.
                    manager.compose('down', '--volumes')
            return 0
    except (
        OperationError,
        RuntimePreparationError,
        CertificateError,
        OSError,
        http.client.HTTPException,
        subprocess.SubprocessError,
    ) as error:
        # CalledProcessError includes the command: do not include captured env contents.
        detail = (
            f'Command failed (exit {error.returncode}).'
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        LOGGER.error('%s', detail)
        if args.action == 'renew-certificate':
            LOGGER.error(
                'Check runtime/logs/certificates.log and Certbot logs; no services were restarted.'
            )
            return 1
        if args.action not in ('logs', 'status'):
            manager.diagnostics()
        LOGGER.error(
            'Check Docker access, available ports/disk, and the existing .env. '
            'Database variables do not rotate credentials in an existing volume.'
        )
        return 1
    except KeyboardInterrupt:
        LOGGER.warning('Interrupted. Inspect service state and logs before retrying.')
        return 130


if __name__ == '__main__':

    def terminate(signum, frame):
        raise InterruptedError('Backup or management operation terminated by SIGTERM')

    signal.signal(signal.SIGTERM, terminate)
    sys.exit(main())
