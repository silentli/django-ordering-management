"""Parse and validate deployment configuration; no host setup or service operations."""

import ipaddress
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentConfig:
    public_host: str
    tls_mode: str
    acme_env: str
    certbot_webroot: Path
    letsencrypt_dir: Path
    acme_agree_tos: bool = False

    def compose_environment(self):
        """Host-side interpolation inputs, not service environment variables."""
        return {
            'CERTBOT_WEBROOT': str(self.certbot_webroot),
            'LETSENCRYPT_DIR': str(self.letsencrypt_dir),
        }

    def check_allowed_hosts(self, allowed_hosts):
        entries = {host.strip().lower().rstrip('.') for host in allowed_hosts.split(',')}
        if self.public_host not in entries:
            raise ConfigurationError(
                'ALLOWED_HOSTS must explicitly include PUBLIC_HOST in the application .env. '
                'Existing application settings and credentials have not been changed.'
            )


DEFAULTS = {
    'TLS_MODE': 'letsencrypt',
    'ACME_ENV': 'staging',
    'CERTBOT_WEBROOT': '/var/www/certbot',
    'LETSENCRYPT_DIR': '/etc/letsencrypt',
    'ACME_AGREE_TOS': 'false',
}
KEYS = {'PUBLIC_HOST', 'ACME_EMAIL', *DEFAULTS}


def read_values(path):
    """Accept one KEY=value per line, optional quotes/comments, and no expansion."""
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError as error:
        raise ConfigurationError(
            f'Deployment file not found: {path}. Copy docker/deploy.env.example to .env.deploy '
            'or select a file with diagnose --config PATH.'
        ) from error
    except IsADirectoryError as error:
        raise ConfigurationError(f'Deployment configuration must be a file: {path}') from error
    except PermissionError as error:
        raise ConfigurationError(
            f'Cannot read deployment file; check permissions: {path}'
        ) from error
    except UnicodeError as error:
        raise ConfigurationError(f'Deployment file must contain UTF-8 text: {path}') from error
    except OSError as error:
        raise ConfigurationError(
            f'Cannot read deployment file: {path} ({error.strerror or "I/O error"}).'
        ) from error
    values = {}
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, raw = line.partition('=')
        key = key.strip()
        if not separator or key not in KEYS:
            raise ConfigurationError(f'{path}:{number}: unknown key or invalid KEY=value entry.')
        if key in values:
            raise ConfigurationError(f'{path}:{number}: duplicate {key}.')
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError as error:
            raise ConfigurationError(f'{path}:{number}: invalid quoting for {key}.') from error
        if len(parts) > 1:
            raise ConfigurationError(f'{path}:{number}: quote values containing spaces ({key}).')
        value = parts[0] if parts else ''
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ConfigurationError(
                f'{path}:{number}: control characters are not allowed ({key}).'
            )
        if any(character in value for character in ('$', '`')):
            raise ConfigurationError(f'{path}:{number}: expansion is not supported ({key}).')
        values[key] = value
    return values


def validate_host(value):
    """Validate IPv4/DNS syntax only; no claim of public ownership or reachability."""
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        pass
    labels = value.lower().split('.')
    label_pattern = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if (
        len(value) > 253
        or len(labels) < 2
        or all(label.isdigit() for label in labels)
        or not all(re.fullmatch(label_pattern, label) for label in labels)
    ):
        raise ConfigurationError(
            'PUBLIC_HOST must be an IPv4 address or DNS name, without scheme, port or path. '
            'IPv6 is not supported by the current Nginx template.'
        )
    return value.lower()


def absolute_path(key, value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(c.isspace() for c in value):
        raise ConfigurationError(f'{key} must be an absolute path without whitespace or "..".')
    if path == Path('/'):
        raise ConfigurationError(f'{key} must not be the filesystem root.')
    return path


def load_config(path):
    values = DEFAULTS | read_values(Path(path))
    if not values.get('PUBLIC_HOST'):
        raise ConfigurationError('PUBLIC_HOST is required in the deployment configuration.')
    if values['TLS_MODE'] not in ('http', 'letsencrypt'):
        raise ConfigurationError('TLS_MODE must be http or letsencrypt.')
    if values['ACME_ENV'] not in ('staging', 'production'):
        raise ConfigurationError('ACME_ENV must be staging or production.')
    if values['ACME_AGREE_TOS'].lower() not in ('true', 'false'):
        raise ConfigurationError('ACME_AGREE_TOS must be true or false.')
    webroot = absolute_path('CERTBOT_WEBROOT', values['CERTBOT_WEBROOT'])
    certificates = absolute_path('LETSENCRYPT_DIR', values['LETSENCRYPT_DIR'])
    if (
        webroot == certificates
        or webroot in certificates.parents
        or certificates in webroot.parents
    ):
        raise ConfigurationError(
            'Webroot and certificate directories must be separate, not nested.'
        )
    # ACME_EMAIL is accepted as a reserved key, but not consumed or forwarded.
    return DeploymentConfig(
        public_host=validate_host(values['PUBLIC_HOST']),
        tls_mode=values['TLS_MODE'],
        acme_env=values['ACME_ENV'],
        certbot_webroot=webroot,
        letsencrypt_dir=certificates,
        acme_agree_tos=values['ACME_AGREE_TOS'].lower() == 'true',
    )
