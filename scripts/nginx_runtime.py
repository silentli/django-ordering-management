"""Prepare Nginx runtime files; service lifecycle remains in the management CLI."""

import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class RuntimePreparationError(ValueError):
    pass


@dataclass(frozen=True)
class NginxRuntime:
    mode: str
    webroot: Path


def template_body(content):
    return re.sub(r'^# menu-nginx-mode: \w+\n', '', content, flags=re.M).strip()


def site_mode(root):
    site = root / 'runtime/nginx/conf.d/site.conf'
    if site.is_symlink():
        raise RuntimePreparationError('Active Nginx site.conf must not be a symlink.')
    if not site.is_file():
        raise RuntimePreparationError(
            'Active Nginx site.conf is missing; run start to initialize it.'
        )
    try:
        content = site.read_text(encoding='utf-8')
    except UnicodeError as error:
        raise RuntimePreparationError('Active Nginx configuration must be UTF-8 text.') from error
    if '__PUBLIC_HOST__' in content or '__CERT_NAME__' in content:
        raise RuntimePreparationError('HTTPS template placeholders have not been rendered.')
    directives = re.sub(r'#[^\n]*', '', content)
    if re.search(r'\blisten\s+(?:[^\s;]+:)?443\b[^;]*\bssl\b', directives):
        return 'https'
    marker = re.search(r'^# menu-nginx-mode: (http|bootstrap)$', content, flags=re.M)
    if marker:
        return marker[1]
    # Recognize project configurations created before mode markers were added.
    for mode in ('http', 'bootstrap'):
        if template_body(content) == template_body(
            (root / f'nginx/templates/{mode}.conf').read_text()
        ):
            return mode
    raise RuntimePreparationError(
        'Unrecognized active Nginx configuration; it was not changed. '
        'Review it against the project templates before deployment.'
    )


def ensure_directory(path, mode=0o755):
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise RuntimePreparationError(f'Expected a directory: {ancestor}')
    for directory in reversed(missing):
        directory.mkdir(mode=mode if directory == path else 0o755)
        directory.chmod(mode if directory == path else 0o755)


def publish(path, content, replace=False):
    """Publish complete configuration; initial creation never overwrites a file."""
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def prepare(root, environment, tls_requested, initialize=True):
    def host_path(key, default):
        value = Path(environment.get(key) or default)
        return value if value.is_absolute() else root / 'docker' / value

    webroot = host_path('CERTBOT_WEBROOT', root / 'runtime/certbot/www')
    certificates = host_path('LETSENCRYPT_DIR', root / 'runtime/certbot/letsencrypt')
    try:
        resolved_webroot, resolved_certificates = webroot.resolve(), certificates.resolve()
    except (OSError, RuntimeError) as error:
        raise RuntimePreparationError(
            'Cannot resolve runtime paths; check directory symlinks.'
        ) from error
    if (
        resolved_webroot == resolved_certificates
        or resolved_webroot in resolved_certificates.parents
        or resolved_certificates in resolved_webroot.parents
    ):
        raise RuntimePreparationError(
            'Webroot and certificate storage must be separate directories.'
        )
    active = root / 'runtime/nginx/conf.d'
    site = active / 'site.conf'
    if site.is_symlink():
        raise RuntimePreparationError('Active Nginx site.conf must not be a symlink.')
    mode = site_mode(root) if site.exists() else None
    if not initialize and mode is None:
        raise RuntimePreparationError('Active Nginx configuration is missing; run start first.')
    if not initialize and mode == 'bootstrap':
        raise RuntimePreparationError(
            'Bootstrap is not an application release; complete setup with start.'
        )
    if mode == 'bootstrap' and not tls_requested:
        raise RuntimePreparationError(
            'Bootstrap is active but TLS_MODE is not letsencrypt. Configuration was preserved.'
        )
    for directory in (active, webroot, webroot / '.well-known/acme-challenge'):
        ensure_directory(directory)
    ensure_directory(certificates, mode=0o700)
    if mode is None:
        mode = 'bootstrap' if tls_requested else 'http'
        publish(site, (root / f'nginx/templates/{mode}.conf').read_text())
    elif mode == 'http' and tls_requested:
        content = site.read_text()
        if template_body(content) != template_body(
            (root / 'nginx/templates/http.conf').read_text()
        ):
            raise RuntimePreparationError(
                'Custom HTTP configuration was preserved; review it before switching to bootstrap.'
            )
        if not initialize:
            raise RuntimePreparationError(
                'TLS setup requires start before update; HTTP was preserved.'
            )
        history = root / 'runtime/nginx/history'
        ensure_directory(history)
        publish(history / f'http-{secrets.token_hex(8)}.conf', content)
        if site.read_text() != content:
            raise RuntimePreparationError('Active Nginx configuration changed during preparation.')
        publish(site, (root / 'nginx/templates/bootstrap.conf').read_text(), replace=True)
        mode = 'bootstrap'
    return NginxRuntime(mode=mode, webroot=webroot)


@contextmanager
def probe(webroot):
    path = webroot / '.well-known/acme-challenge' / secrets.token_hex(16)
    body = secrets.token_hex(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, 'w') as target:
            os.fchmod(target.fileno(), 0o644)
            target.write(body)
        yield '/.well-known/acme-challenge/' + path.name, body
    finally:
        path.unlink(missing_ok=True)
