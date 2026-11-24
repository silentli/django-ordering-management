# TLS and certificates

## Configuration

Deployment options in `.env.deploy`:

| Option | Behavior |
| --- | --- |
| `TLS_MODE=http` | Initialize HTTP application access; do not call Certbot |
| `TLS_MODE=letsencrypt` | Bootstrap, obtain or reuse a certificate through Certbot, then enable HTTPS |
| `ACME_ENV=staging` | Use the Let’s Encrypt test API; certificates are not browser-trusted |
| `ACME_ENV=production` | Use the Let’s Encrypt production API for publicly trusted certificates |

The example selects `letsencrypt` and `staging`. Certbot and Let’s Encrypt are
fixed in the implementation. `start` completes HTTPS activation after acquisition.
Changing `TLS_MODE` to `http` does not downgrade existing HTTPS.

Nginx loads only `/etc/nginx/conf.d/site.conf`, mounted from
`runtime/nginx/conf.d/site.conf`. `start` initializes this file when absent;
the image has no fallback site configuration. Templates are not loaded directly.

| Template | Behavior |
| --- | --- |
| `nginx/templates/http.conf` | Plain HTTP application access |
| `nginx/templates/bootstrap.conf` | HTTP ACME files; other requests return `503` |
| `nginx/templates/https.conf` | HTTP ACME files and redirects; HTTPS application access |

| Host directory | Nginx mount, read-only |
| --- | --- |
| `runtime/nginx/conf.d` | `/etc/nginx/conf.d` |
| `runtime/certbot/www` | `/var/www/certbot` |
| `runtime/certbot/letsencrypt` | `/etc/letsencrypt` |

Shell variables `CERTBOT_WEBROOT` and `LETSENCRYPT_DIR` override the last two host
paths for direct Compose commands. Management commands use `.env.deploy` paths
when configured, overriding the shell. Certbot must use those same host paths.
Mount the complete certificate directory because `live/` files can link into
`archive/`. Changing mount sources requires
container recreation; updating mounted configuration or certificates requires
validation and reload.

## HTTP bootstrap

Set `TLS_MODE=letsencrypt` in `.env.deploy` and prepare the
[certificate requirements](#certificate-acquisition), then run:

```sh
python3 scripts/manage.py start
```

`start` initializes bootstrap if no active configuration exists. A transition from
the unmodified project HTTP template saves the old file under
`runtime/nginx/history/` before switching. Existing HTTPS is retained; custom HTTP
configuration is not overwritten. Repeated bootstrap starts preserve `site.conf`.
`update` requires an application configuration and does not initialize bootstrap.

Bootstrap provides HTTP validation before certificates are available. It requires
Docker access, readable application configuration, and a public IP with inbound
TCP `80`. Compose publishes ports `80` and `443`, but bootstrap listens only on `80`.

| Step | Internal operation |
| --- | --- |
| Prepare | Create runtime directories and select `bootstrap.conf` as `site.conf` |
| Start | Validate Compose configuration and start only Nginx, without Django or PostgreSQL |
| Verify | Write a temporary webroot probe, check its HTTP response, then remove it |

The probe URL is `http://PUBLIC_IP/.well-known/acme-challenge/PROBE`.
A valid probe returns `200` with matching content; missing files return `404`;
application paths return `503`. A host-local check does not prove public reachability.

All operations use the same Compose project and host paths. Application checks
run after HTTPS activation; they cannot pass while bootstrap returns `503`.
`diagnose` checks bootstrap configuration and its certificate without requiring
the application. It does not issue or renew certificates.
Host paths need appropriate creation/write permissions; the script does not
escalate privileges. The local probe is removed after verification, including handled failures.

### Command reference

These commands illustrate bootstrap operations for inspection and troubleshooting.
Run from the repository root. Examples use the default Compose project `docker`;
add `--project-name NAME` for another deployment. Prepare the application `.env`
first; Compose reads it even with `--no-deps`.

Initialize a new active configuration only; `cp -n` preserves an existing file.
If `cmp` fails, stop and inspect the active configuration before proceeding:

```sh
mkdir -p runtime/nginx/conf.d runtime/certbot/www/.well-known/acme-challenge runtime/certbot/letsencrypt
cp -n nginx/templates/bootstrap.conf runtime/nginx/conf.d/site.conf
cmp nginx/templates/bootstrap.conf runtime/nginx/conf.d/site.conf
```

Start and validate Nginx:

```sh
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.yml up -d --no-deps nginx
docker compose -f docker/docker-compose.yml exec -T nginx nginx -t
```

Write a probe on the host, request it from outside the server, then remove it.
Replace `PUBLIC_IP` with the server address. Use the configured host webroot if
it differs from the default:

```sh
# On the Docker host:
printf 'bootstrap-ok\n' > runtime/certbot/www/.well-known/acme-challenge/probe
# From an external machine:
curl --fail --show-error http://PUBLIC_IP/.well-known/acme-challenge/probe
# Back on the Docker host:
rm runtime/certbot/www/.well-known/acme-challenge/probe
```

## Certificate acquisition

The same `start` command calls
[`ops/certificates.py`](../../scripts/ops/certificates.py) after bootstrap validation.
It reuses a matching certificate with at least 24 hours remaining, or calls host
Certbot once with a 300-second timeout, then proceeds to HTTPS activation.

Requirements: Linux for issuance, Certbot 5.4+, OpenSSL 1.1.1+, a publicly reachable
HTTP challenge path, and permission to write certificate storage. Install Certbot
using the [official instructions](https://certbot.eff.org/instructions).
After reviewing the [subscriber agreement](https://letsencrypt.org/repository/),
set `ACME_AGREE_TOS=true` in `.env.deploy` to permit new issuance.

| `ACME_ENV` | Host lineage directory | Trust |
| --- | --- | --- |
| `staging` | `LETSENCRYPT_DIR/staging/live/menu-HOST_HASH/` | Test certificate; not browser-trusted |
| `production` | `LETSENCRYPT_DIR/production/live/menu-HOST_HASH/` | Chain checked against host OpenSSL trust roots |

Each environment also has its own accounts, archive, renewal configuration,
`work/` and `logs/`. Names are derived from `PUBLIC_HOST`; switching environments
does not overwrite the other store. Mount the complete `LETSENCRYPT_DIR`.

Certbot uses `certonly --webroot`. IPv4 requests add `--ip-address` and require
the `shortlived` profile; domain requests use `-d`.
[IP certificate requirements](https://letsencrypt.org/2026/03/11/shorter-certs-certbot).
CLI arguments disable Certbot deployment hooks; this step does not reload Nginx.
`ACME_EMAIL` remains unused and is not forwarded.

Checks cover renewal-server metadata, validity dates, IP/DNS SAN, certificate/key
matching and private-key permissions. Production also checks the trust chain at
the current time. Malformed or mismatched lineages stop processing rather than
being silently replaced. Structurally valid expired or near-expiry lineages are renewed.
Failure or timeout returns nonzero, preserves the active Nginx configuration, and
reports the environment's Certbot log directory. `diagnose` reports missing or invalid
certificates without contacting the CA.

## HTTPS activation

`nginx/templates/https.conf` requires these values:

| Placeholder | Required value |
| --- | --- |
| `__PUBLIC_HOST__` | Public IPv4 or domain, without scheme, port or path |
| `__ACME_ENV__` | `staging` or `production` |
| `__CERT_NAME__` | Managed lineage name, `menu-HOST_HASH`, returned by certificate acquisition |

Certificate paths inside Nginx are
`/etc/letsencrypt/ACME_ENV/live/CERT_NAME/{fullchain,privkey}.pem`.

The certificate must cover the selected IP or domain. HTTP challenges remain
accessible; other HTTP requests redirect with `308`. HTTPS serves static/media
files and proxies to `web:8000` with `X-Forwarded-Proto: https`.

`start` starts Web and its dependencies with HTTPS settings, seeds initial data
unless `--no-seed`, then backs up and replaces `site.conf`, runs `nginx -t`, reloads
Nginx and verifies requests. Repeated starts reuse matching configuration. An exact
project HTTPS configuration can switch host or certificate environment; custom
configurations stop activation before service changes and require manual review.

Management commands derive `DJANGO_HTTPS=true` and
`DJANGO_CSRF_TRUSTED_ORIGINS=https://PUBLIC_HOST` before starting Web for HTTPS;
application credentials are unchanged. Django's proxy, CSRF and cookie behavior is
described in [Application security](../app/security.md#https-reverse-proxy).
For direct Compose operations that recreate Web, supply these same inputs explicitly;
Compose alone does not read `.env.deploy` or inspect `site.conf`.

Checks cover Django configuration/migrations, HTTPS health/menu/login forms/static
assets, secure CSRF cookies, HTTP redirects and the HTTP ACME path. Production
probes verify public trust and hostname. Staging probes trust the selected certificate
only within the probe process; browsers still reject staging certificates.

A handled activation failure restores the previous file and attempts to reload it
and restore Web settings. Historical configurations remain under `runtime/nginx/history/`.
Recovery failures are logged; inspect services before retrying. Database migrations
and seeded data are not rolled back. A process kill or host failure can interrupt
recovery; inspect `site.conf` and run `diagnose` before restarting services.

## Certificate renewal

Requires active HTTPS matching `.env.deploy`, Linux, Certbot 5.4+, OpenSSL 1.1.1+,
Docker access, and write access to the certificate store, webroot and `runtime/logs`.

```sh
python3 scripts/manage.py renew-certificate
```

| Step | Behavior |
| --- | --- |
| Preflight | Acquire deployment lock; check managed site, Nginx and HTTP challenge path |
| Renew | `certbot renew` checks the selected environment/lineage; renew only when due |
| Load | Validate certificate → `nginx -t` → compare live SHA-256 fingerprint → reload if different → verify |
| Retry | A later run loads a pending certificate even when no further renewal is due |
| Failure | Nonzero exit; failed renewal/validation prevents reload; renewed files remain for retry |

Manual and scheduled attempts share `runtime/logs/certificates.log` (JSON lines,
mode `600`): source, stage, outcome, expiry, fingerprint and duration.
Certbot details: `LETSENCRYPT_DIR/ACME_ENV/logs/`. Rotate/archive the project log.
`diagnose` reports expiry and timer state.

### systemd timer

Requires a persistent checkout, Linux/systemd, rootful Docker (`docker.service`),
sudo, `runuser`, and an existing deployment user with the permissions above.
Preview works on Mac; installation runs a real renewal check and may renew/reload.

```sh
python3 scripts/manage.py schedule install --task certificate --user "$USER" --dry-run
sudo /usr/bin/python3 scripts/manage.py schedule install --task certificate --user "$USER"
python3 scripts/manage.py schedule status --task certificate
journalctl -u menu-certificate-docker.service -n 50 --no-pager
sudo /usr/bin/python3 scripts/manage.py schedule disable --task certificate
```

Installation: validate units → check renewal as `--user` → write units → enable timer.
Templates: [service](../../scripts/systemd/menu-certificate.service.example),
[timer](../../scripts/systemd/menu-certificate.timer.example).
Installer: [`ops/schedule.py`](../../scripts/ops/schedule.py).

| Setting | Behavior |
| --- | --- |
| Units | `/etc/systemd/system/menu-certificate-PROJECT.{service,timer}` |
| Schedule | `00:00` and `12:00 UTC`, plus up to 30 minutes random delay |
| Catch-up | `Persistent=true` runs a missed check after downtime |
| Failure | Three-start limit per hour; 15-minute retry delay; 10-minute attempt timeout |
| Disable | Stops timer activations; active attempts/retries and manual execution remain |

Foreign/manual units are preserved. Activation failure may leave unit files; inspect
status/journal before retrying. Use one renewal scheduler per store: explicit conflicts
with detected Certbot timers are rejected; review cron and indirect overlaps manually.
External jobs are left unchanged. `--task` defaults to backup; `--at` and `--timezone`
apply only to backups.

## Limitations and improvements

- **Host provisioning:** Certbot/OpenSSL installation and storage permissions are
  managed on the target host; the script does not install packages or run sudo.
- **Public validation:** Verify the certificate, firewall, redirects and authenticated
  sessions from outside the target server. HSTS is not enabled automatically.
- **Alerts:** Renewal failures are recorded in the project log and service journal;
  no external alerting is configured. `ACME_EMAIL` remains optional and unused.
