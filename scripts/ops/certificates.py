"""Acquire and inspect Certbot certificates; never activate or reload Nginx."""

import configparser
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .nginx_runtime import RuntimePreparationError, ensure_directory

LOGGER = logging.getLogger(__name__)
SERVERS = {
    'staging': 'https://acme-staging-v02.api.letsencrypt.org/directory',
    'production': 'https://acme-v02.api.letsencrypt.org/directory',
}
CERTBOT_TIMEOUT = 300
REUSE_MARGIN = 24 * 60 * 60


class CertificateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Certificate:
    fullchain: Path
    private_key: Path
    cert_name: str
    acme_env: str
    expires_at: datetime


def run(command, label, timeout=15):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_ALL': 'C'},
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise CertificateError(f'{label} timed out after {timeout} seconds.') from error
    except subprocess.CalledProcessError as error:
        raise CertificateError(f'{label} failed (exit {error.returncode}).') from error
    except OSError as error:
        raise CertificateError(
            f'{label} could not run; check installation and permissions.'
        ) from error


def executable(name, minimum, pattern):
    binary = shutil.which(name)
    if binary is None:
        raise CertificateError(
            f'{name} is not installed or not on PATH; prepare the Linux host first.'
        )
    output = run([binary, '--version' if name == 'certbot' else 'version'], f'{name} version check')
    match = re.search(pattern, output)
    if not match or tuple(map(int, match.groups())) < minimum:
        version = '.'.join(map(str, minimum))
        raise CertificateError(f'{name} {version} or newer is required.')
    return binary


def lineage_name(public_host):
    return 'menu-' + hashlib.sha256(public_host.encode()).hexdigest()[:16]


def inspect(config, openssl, minimum_remaining=0):
    directory = config.letsencrypt_dir / config.acme_env
    name = lineage_name(config.public_host)
    live = directory / 'live' / name
    fullchain, key = live / 'fullchain.pem', live / 'privkey.pem'
    if not live.exists() and not live.is_symlink():
        return None
    for path in (fullchain, key):
        try:
            target = path.resolve(strict=True)
            base = directory.resolve()
        except (OSError, RuntimeError) as error:
            raise CertificateError(
                f'Certificate lineage is incomplete or unreadable: {live}'
            ) from error
        if not target.is_file() or not target.is_relative_to(base):
            raise CertificateError(
                'Certificate files must remain inside their environment directory.'
            )
    if key.stat().st_mode & 0o077:
        raise CertificateError(f'Private key permissions must exclude group/other access: {key}')
    metadata = configparser.ConfigParser(interpolation=None)
    try:
        with (directory / 'renewal' / f'{name}.conf').open() as source:
            # Certbot's ConfigObj format starts with unsectioned lineage fields.
            metadata.read_string('[lineage]\n' + source.read())
        if metadata.get('renewalparams', 'server') != SERVERS[config.acme_env]:
            raise CertificateError('Certificate renewal metadata does not match ACME_ENV.')
    except (OSError, UnicodeError, configparser.Error) as error:
        raise CertificateError(
            'Certificate renewal metadata is missing or invalid; inspect the lineage.'
        ) from error
    dates = run(
        [openssl, 'x509', '-in', str(fullchain), '-noout', '-dates'], 'Certificate date inspection'
    )
    try:
        values = dict(line.split('=', 1) for line in dates.strip().splitlines())
        not_before = ssl.cert_time_to_seconds(values['notBefore'])
        expires = ssl.cert_time_to_seconds(values['notAfter'])
    except (KeyError, ValueError) as error:
        raise CertificateError('Cannot parse certificate validity dates.') from error
    if not_before > time.time():
        raise CertificateError('Certificate is not valid yet; check the host clock.')
    san = run(
        [openssl, 'x509', '-in', str(fullchain), '-noout', '-ext', 'subjectAltName'],
        'Certificate SAN inspection',
    )
    try:
        ipaddress.IPv4Address(config.public_host)
        identity = ['-verify_ip', config.public_host]
        has_san = 'IP Address:' in san
    except ipaddress.AddressValueError:
        identity = ['-verify_hostname', config.public_host]
        has_san = 'DNS:' in san
    if not has_san:
        raise CertificateError('Certificate has no required IP/DNS subject alternative name.')
    verify = [openssl, 'verify', '-no_check_time', '-purpose', 'sslserver', *identity]
    if config.acme_env == 'staging':
        # Staging is deliberately not publicly trusted. Check SAN/purpose against
        # the leaf; renewal metadata above binds reuse to the staging endpoint.
        verify += ['-trusted', str(fullchain), '-partial_chain']
    else:
        verify += ['-untrusted', str(fullchain)]
    run([*verify, str(fullchain)], 'Certificate identity/chain verification')
    if config.acme_env == 'production' and expires > time.time():
        # Reusable production chains must also be valid at the current time.
        run(
            [argument for argument in verify if argument != '-no_check_time'] + [str(fullchain)],
            'Current production chain verification',
        )
    certificate_public = run(
        [openssl, 'x509', '-in', str(fullchain), '-pubkey', '-noout'],
        'Certificate public key inspection',
    )
    private_public = run(
        [openssl, 'pkey', '-in', str(key), '-passin', 'pass:', '-pubout'], 'Private key matching'
    )
    if certificate_public.strip() != private_public.strip():
        raise CertificateError('Certificate and private key do not match.')
    if minimum_remaining is not None and expires - time.time() < minimum_remaining:
        return None
    return Certificate(
        fullchain, key, name, config.acme_env, datetime.fromtimestamp(expires, timezone.utc)
    )


def inspect_certificate(config, allow_expired=False):
    """Read certificate state without issuing, renewing or changing any files."""
    openssl = executable('openssl', (1, 1, 1), r'OpenSSL (\d+)\.(\d+)\.(\d+)')
    return inspect(config, openssl, minimum_remaining=None if allow_expired else 0)


def ensure_certificate(config):
    openssl = executable('openssl', (1, 1, 1), r'OpenSSL (\d+)\.(\d+)\.(\d+)')
    existing = inspect(config, openssl, minimum_remaining=REUSE_MARGIN)
    if existing is not None:
        LOGGER.info(
            'Reusing %s certificate for %s; expires %s',
            config.acme_env,
            config.public_host,
            existing.expires_at.isoformat(),
        )
        return existing
    if sys.platform != 'linux':
        raise CertificateError(
            'Certificate issuance is supported on Linux hosts; no request was sent.'
        )
    certbot = executable('certbot', (5, 4, 0), r'certbot\s+(\d+)\.(\d+)\.(\d+)')
    if not config.acme_agree_tos:
        raise CertificateError(
            'Review the ACME subscriber agreement and set ACME_AGREE_TOS=true before issuance.'
        )
    directory = config.letsencrypt_dir / config.acme_env
    work, logs = directory / 'work', directory / 'logs'
    try:
        for path in (directory, work, logs):
            ensure_directory(path, mode=0o700)
    except (OSError, RuntimePreparationError) as error:
        raise CertificateError(
            'Cannot prepare certificate storage; check host ownership and permissions.'
        ) from error
    command = [
        certbot,
        'certonly',
        '--non-interactive',
        '--agree-tos',
        '--email',
        '',
        '--no-eff-email',
        '--pre-hook',
        '',
        '--post-hook',
        '',
        '--deploy-hook',
        '',
        '--no-directory-hooks',
        '--webroot',
        '--webroot-path',
        str(config.certbot_webroot),
        '--preferred-challenges',
        'http',
        '--server',
        SERVERS[config.acme_env],
        '--config-dir',
        str(directory),
        '--work-dir',
        str(work),
        '--logs-dir',
        str(logs),
        '--cert-name',
        lineage_name(config.public_host),
    ]
    try:
        ipaddress.IPv4Address(config.public_host)
        command += ['--ip-address', config.public_host, '--required-profile', 'shortlived']
    except ipaddress.AddressValueError:
        command += ['-d', config.public_host]
    if (directory / 'live' / lineage_name(config.public_host)).exists():
        # Only a structurally valid expired/near-expiry lineage reaches here.
        command.append('--force-renewal')
    LOGGER.info(
        'Requesting %s certificate for %s; Certbot logs: %s',
        config.acme_env,
        config.public_host,
        logs,
    )
    try:
        run(command, 'Certbot issuance', timeout=CERTBOT_TIMEOUT)
    except CertificateError as error:
        raise CertificateError(
            f'{error} Check Certbot configuration and logs at {logs}.'
        ) from error
    certificate = inspect(config, openssl, minimum_remaining=REUSE_MARGIN)
    if certificate is None:
        raise CertificateError(
            'Certbot finished without a usable certificate valid for at least 24 hours.'
        )
    LOGGER.info(
        'Certificate checked: %s; expires %s',
        certificate.fullchain,
        certificate.expires_at.isoformat(),
    )
    return certificate


def fingerprint(certificate):
    end = '-----END CERTIFICATE-----'
    leaf = certificate.fullchain.read_text().split(end, 1)[0] + end
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).hexdigest()


def renew_certificate(config):
    """Let Certbot decide when an existing lineage is due; never force issuance."""
    if sys.platform != 'linux':
        raise CertificateError('Certificate renewal requires a Linux host; no request was sent.')
    openssl = executable('openssl', (1, 1, 1), r'OpenSSL (\d+)\.(\d+)\.(\d+)')
    certbot = executable('certbot', (5, 4, 0), r'certbot\s+(\d+)\.(\d+)\.(\d+)')
    # Validate even an expired lineage before allowing Certbot to replace it.
    previous = inspect(config, openssl, minimum_remaining=None)
    if previous is None:
        raise CertificateError('Managed certificate is missing; complete setup with start first.')
    before = fingerprint(previous)
    directory = config.letsencrypt_dir / config.acme_env
    logs = directory / 'logs'
    command = [
        certbot,
        'renew',
        '--non-interactive',
        '--no-random-sleep-on-renew',
        '--cert-name',
        previous.cert_name,
        '--server',
        SERVERS[config.acme_env],
        '--config-dir',
        str(directory),
        '--work-dir',
        str(directory / 'work'),
        '--logs-dir',
        str(logs),
        '--webroot',
        '--webroot-path',
        str(config.certbot_webroot),
        '--preferred-challenges',
        'http',
        '--email',
        '',
        '--no-eff-email',
        '--pre-hook',
        '',
        '--post-hook',
        '',
        '--deploy-hook',
        '',
        '--no-directory-hooks',
    ]
    LOGGER.info(
        'Checking %s certificate renewal for %s; Certbot logs: %s',
        config.acme_env,
        config.public_host,
        logs,
    )
    try:
        run(command, 'Certbot renewal', timeout=CERTBOT_TIMEOUT)
    except CertificateError as error:
        raise CertificateError(f'{error} Check Certbot logs at {logs}.') from error
    certificate = inspect(config, openssl)
    if certificate is None:
        raise CertificateError('Certbot finished without a currently valid managed certificate.')
    return certificate, before != fingerprint(certificate)


def write_renewal_event(root, event):
    directory = root / 'runtime/logs'
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), **event}) + '\n'
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(directory / 'certificates.log', flags, 0o600)
    with os.fdopen(fd, 'a') as target:
        fcntl.flock(target, fcntl.LOCK_EX)
        os.fchmod(target.fileno(), 0o600)
        target.write(line)
        target.flush()
        os.fsync(target.fileno())


@contextmanager
def renewal_record(root, project, source):
    record = {'run_id': secrets.token_hex(8), 'project': project, 'source': source, 'stage': 'lock'}
    started = time.monotonic()
    write_renewal_event(root, {**record, 'status': 'started'})
    try:
        yield record
    except BaseException as error:
        detail = (
            f'Command failed (exit {error.returncode}).'
            if isinstance(error, subprocess.CalledProcessError)
            else str(error) or type(error).__name__
        )
        try:
            write_renewal_event(
                root,
                {
                    **record,
                    'status': 'failed',
                    'error': detail,
                    'error_type': type(error).__name__,
                    'duration_seconds': round(time.monotonic() - started, 3),
                },
            )
        except OSError as log_error:
            LOGGER.error('Cannot record certificate failure: %s', log_error)
        raise
    else:
        write_renewal_event(
            root,
            {
                **record,
                'status': 'success',
                'duration_seconds': round(time.monotonic() - started, 3),
            },
        )
