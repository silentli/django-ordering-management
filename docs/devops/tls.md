# TLS and certificates

## Configuration

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

Set `TLS_MODE=letsencrypt` in `.env.deploy`, then run:

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

Existing HTTPS configuration must be preserved. All operations use the same
Compose project and host paths. Application checks are deferred until HTTPS
activation; they cannot pass while bootstrap returns `503`.
`diagnose` checks bootstrap configuration without requiring the application.
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

## HTTPS template

`nginx/templates/https.conf` requires these values:

| Placeholder | Required value |
| --- | --- |
| `__PUBLIC_HOST__` | Public IPv4 or domain, without scheme, port or path |
| `__CERT_NAME__` | Actual certificate directory under the container's `/etc/letsencrypt/live/` |

The certificate must cover the selected IP or domain. HTTP challenges remain
accessible; other HTTP requests redirect with `308`. HTTPS serves static/media
files and proxies to `web:8000` with `X-Forwarded-Proto: https`.

Activation requires valid certificates, running application services and Django
HTTPS settings. The sequence is: preserve the active configuration → render and
replace `site.conf` → `nginx -t` → reload → application checks. Validation failure
requires restoring the previous file. Reload keeps the container; a restart reads
the configuration on disk.

## Validation

Local Docker checks passed: bootstrap, mounts, temporary-certificate HTTPS,
redirects, static/media files, proxy headers and reload. The backend was a test
server. Public issuance, Django HTTPS login and Linux renewal remain unverified.

## Limitations and improvements

- **Issuance:** Certbot installation and invocation are not implemented.
- **Activation:** HTTP/bootstrap preparation is automated. HTTPS template rendering
  and Django secure cookies still require configuration. Existing HTTPS gets
  container/configuration checks; application HTTPS requests require separate validation.
- **Renewal:** No certificate timer, reload hook or failure alert is configured.
  `ACME_EMAIL` is optional and unused; it does not configure notifications.
