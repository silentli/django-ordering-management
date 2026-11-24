# Deployment

Run commands from the repository root on the Docker host. The default stack uses
HTTP; public deployment also requires [TLS and certificates](tls.md), secure
cookies, and restricted database exposure.

## Preconditions and configuration

- Linux host with Git, Python 3.10+, Docker, and Docker Compose 2.24.0+.
- Docker access, a writable checkout, and available host ports `80`, `443`, and `5432`.
- For an existing deployment: its Compose project name, runtime configuration,
  source revision, image identities, and persistent volumes.
- For public access: a public IP or domain, TLS termination, secure cookie settings,
  and restricted database exposure. The script does not configure these.

The application runtime file is `menu_management/settings/env/.env`. The management script
creates it on first start if absent; existing files are preserved.

| Key | Purpose / value |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django signing secret; retain across normal releases |
| `ALLOWED_HOSTS` | Comma-separated hostnames/IPs without schemes or spaces; default `localhost,127.0.0.1` |
| `STAFF_PASSWORD` | Shared password for the staff login |
| `POSTGRES_DB` | Application database name |
| `POSTGRES_USER` | Database role; also used by the database healthcheck |
| `POSTGRES_PASSWORD` | Database connection password |
| `POSTGRES_HOST` | `db` inside the Compose network |
| `POSTGRES_PORT` | `5432` inside the Compose network |

Keep credentials outside Git and restrict the runtime file to mode `600`.
Changing PostgreSQL variables does not update roles, passwords, or databases in an
existing data volume. Changing `STAFF_PASSWORD` takes effect after container
recreation; successful staff login then synchronizes the `staff` user's password.
Django superuser credentials are managed separately.

### Deployment parameters

`docker/deploy.env.example` describes deployment inputs separately from application
credentials. Prepare a local deployment file:

```sh
cp -n docker/deploy.env.example .env.deploy
```

Edit `PUBLIC_HOST` before real deployment. The example IP is documentation-only.
Management commands that invoke Compose read `.env.deploy` when present.
Its mount paths override same-named shell variables and are used consistently by
startup, updates, backups and diagnostics. These are host-side Compose inputs,
not additional environment variables injected into application containers.
Without the file, the existing Compose defaults and shell inputs remain in use.

On first start, `PUBLIC_HOST` is included in the generated `ALLOWED_HOSTS`, along
with `localhost,127.0.0.1`. An explicit `start --hosts` must include it too.
For an existing application `.env`, `ALLOWED_HOSTS` must explicitly include
`PUBLIC_HOST`; a mismatch stops deployment without rewriting the file.

`diagnose` checks the same configuration and host consistency. Use
`diagnose --config /absolute/path/to/deploy.env` to inspect another deployment file.
File values override built-in defaults; shell variables do not override them.
The format supports `KEY=value`, quotes and comments, but no `export`, variable
expansion or shell commands. Unknown and duplicate keys are rejected.

`ACME_EMAIL` remains an optional unused key and does not configure notifications.

## Automated host management

Use `python3 scripts/manage.py COMMAND`. Only Python's standard library is needed
on the host; application dependencies run in containers.

| Command | Behavior |
| --- | --- |
| `start` | Prepare application/runtime configuration; start and verify HTTP application or TLS bootstrap |
| `stop` | Stop services; retain containers and data |
| `down` | Remove containers and network; retain persistent volumes |
| `update` | Build current source, back up the database, recreate services, and verify |
| `status` | Show container state, health, and ports; no application requests |
| `logs [-f]` | Show recent logs; `-f` streams until Ctrl+C |
| `diagnose [--config PATH]` | Check deployment parameters, Docker, status/logs and the running application |
| `backup` | Create and inspect a database archive |
| `schedule install --user USER` | Test backup, install and enable a Linux systemd timer; requires sudo |
| `schedule status` | Show the timer and last service result |
| `schedule disable` | Disable future timer activations; requires sudo |
| `destroy --confirm docker` | Back up the database, then remove containers and volumes |

Use `COMMAND --help` for command-specific options. `start --hosts HOSTS` only
initializes a new application `.env`; `update` requires the existing file and does
not accept `--hosts`. Both accept `--no-seed`. `logs -f` and `logs --follow` are
equivalent; ordinary `logs` returns the latest 100 lines.

The default Compose project name is **docker**, matching direct commands using
`-f docker/docker-compose.yml`. For another project, pass `--project-name NAME`
before or after the management command; direct Compose commands require it before
the operation. Keep the project name consistent. A different name selects different
containers and volumes; separate installations also need separate configuration
and nonconflicting ports.

For example, these management commands select the same project:

```sh
python3 scripts/manage.py --project-name staging status
python3 scripts/manage.py status --project-name staging
```

Application lifecycle and standalone backup commands share a checkout lock.
Timer configuration, direct Compose, and other checkouts are outside that lock.

Manual and scheduled backups share `data/backups/backup.log`, including backups
before updates/deletion. See [backup logging and scheduling](backup-and-restore.md#shared-backup-log)
for the systemd installer and cron alternative; schedules are not installed by `start`.

## First deployment

Clone the repository as shown in the [quick start](../../README.md#quick-start),
select the intended revision, and run:

```sh
python3 scripts/manage.py start
```

`start` creates missing runtime directories and initializes `site.conf` from the
HTTP template, or the bootstrap template for `TLS_MODE=letsencrypt`. Bootstrap
starts only Nginx, verifies the HTTP challenge path, then obtains or reuses a
certificate. It remains active until HTTPS activation. See [TLS](tls.md#http-bootstrap)
for requirements and the command reference. Existing HTTPS configuration is preserved;
runtime files are excluded from Git and Docker build contexts.

If the application `.env` is absent and no project volumes exist, the script generates
random Django, database, and staff secrets in a mode-`600` `.env`. It never prints
those secrets or overwrites existing values. Read `STAFF_PASSWORD` from the file
to sign in. If volumes exist but `.env` is missing, restore the original file;
startup refuses to generate replacement credentials.

For a new application `.env`, `--hosts` overrides the initial host list. Otherwise
the list includes `PUBLIC_HOST` when configured, plus `localhost,127.0.0.1`.
For an existing file, edit it explicitly; `--hosts` does not replace it.
Host validation does not configure DNS or HTTPS.

The Web entrypoint validates required variables, waits for the database, applies
migrations, and starts Gunicorn. Static collection finishes before Web starts.
After services are ready, the host script calls `load_initial_data` to import
`menu_app/data/initial_menu_items.json` into an empty menu. Existing menu data is
left intact. The bundled file defines twelve items with 20 units of stock each.
Use `--no-seed` on both `start` and `update` when sample data is unwanted.
The container entrypoint itself does not import menu data.

The root `data/` directory is separately mounted into PostgreSQL's initialization
directory. Inspect it before creating a fresh database; scripts there run only
when the database volume is empty. It is separate from `menu_app/data/`.

Expected result: `db` and `web` are healthy, Nginx runs, static collection exits
with code `0`, and application checks pass. Continue with
[release verification](#release-verification).

### Optional Django administrator

Staff login creates or updates the shared `staff` account. For full access to
the `/admin/` interface, create a separate Django superuser after startup succeeds:

```sh
cp -n docker/admin.env.example .env.admin
chmod 600 .env.admin
```

Set a unique username, email, and nonempty password in `.env.admin`; keep the
password single-quoted as in the example. Then run:

```sh
docker compose -f docker/docker-compose.yml run --rm --build init-admin
```

Creation fails if the username already exists; manage that account rather than
creating it again. Verify `/admin/` login, retain credentials in protected storage,
and remove `.env.admin`. For a custom project, add `--project-name NAME` to direct
Compose commands as well.

## Release an existing environment

1. Record the deployed revision, images, configuration, and project/volume identity.
   Review [CI results](ci.md#run-verification), migrations, downtime, schema
   compatibility, and the recovery plan. Preserve unexplained local changes.
2. Fetch and select the intended source revision without overwriting local work:

   ```sh
   git fetch origin
   release_ref='REPLACE_WITH_APPROVED_COMMIT_SHA'
   git switch --detach "$release_ref"
   ```

3. Preserve the existing application `.env` and project name, prepare configuration
   additions, then deploy:

   ```sh
   python3 scripts/manage.py update
   ```

`update` stops if the application `.env` is missing; it does not initialize
credentials. The script builds first, starts the database if needed, and requires
a successful [database backup](backup-and-restore.md#scripted-database-backup) before recreating
services. Preserve the active `runtime/nginx/conf.d/site.conf`; updates do not
generate or switch Nginx configuration. A failed build leaves running containers unchanged; a failed backup
aborts replacement. Updates also seed an empty menu unless `--no-seed` is supplied.

Recreation can interrupt requests. Static collection clears the shared asset
volume, so asset changes can affect requests before Web replacement completes.
Writes may continue after the backup snapshot; use a maintenance window when
required. Failure after replacement does not trigger automatic rollback.

## Routine start, stop, and removal

Resume a stopped installation with `start`; existing images, credentials, and
volumes are reused. Use `update` after changing source code or dependencies.
`stop` performs graceful shutdown; `down` additionally removes containers and the
network. Neither removes persistent volumes.

`destroy` removes **database, static, and media volumes**. It requires the exact
project name as confirmation and a successful database backup. For example:

```sh
python3 scripts/manage.py --project-name staging destroy --confirm staging
```

It starts the database if necessary and refuses deletion if startup or backup
fails. The dump excludes uploaded media; copy media and configuration separately
before deletion. The checkout, images, `.env`, and host backups remain. Use `down`
when only container removal is intended.

## Release verification

In application mode, `start`, `update`, and `diagnose` check Django configuration and
pending migrations. With the HTTP configuration they also request health, menu,
the staff login page, and an admin CSS asset through Nginx. These requests originate
inside the stack. Verify the published HTTP port:

```sh
curl --fail --show-error http://localhost/health/
curl --fail --show-error --output /dev/null http://localhost/menu/
```

Expected result: health returns `ok` and the menu request succeeds. If `localhost`
is not allowed, use a configured hostname that resolves to the host. Open the menu
in a browser, confirm styles load, and verify staff login, inventory, and orders.
Use designated test records for changed write operations. For public deployment,
check the real HTTPS URL, certificate, redirects, and restrictions externally.
Record the revision, images, migration status, and outcome without credentials or
customer data.

## Failure handling and rollback

Lifecycle failures return nonzero and attempt to display status and recent logs.
Use `python3 scripts/manage.py diagnose` or the [diagnostic commands](maintenance.md#status-and-logs)
to investigate. Correct the first configuration, port, database, or migration error
before retrying; do not delete volumes as a repair step.

For application failures, select the last working revision only after confirming
compatibility with the applied schema. Retained images provide more reproducible
recovery than rebuilding mutable base tags and unpinned transitive dependencies.

For failed or incompatible migrations, preserve data, stop affected writes, and
inspect the applied schema. Reverting code does not revert migrations. Database
reversal or replacement requires a verified backup and an explicit recovery plan
that accounts for lost writes. The [isolated restore exercise](backup-and-restore.md#isolated-restore-exercise)
is not a production cutover procedure.

## Validation

| Check | Local evidence |
| --- | --- |
| Lifecycle, 2026-11-19 | Isolated Docker Desktop setup, resume, recreation, update, confirmed deletion; credentials/data persisted |
| Application and host-script tests, 2026-11-20 | Passed on Linux Python 3.12 |
| Backup audit, 2026-11-20 | Manual/scheduled success and failure share a private log; isolated PostgreSQL 14 archive restored |
| Timer configuration, 2026-11-20 | Generated units passed Linux systemd 249 validation; no host activation |
| Deployment parameters, 2026-11-23 | CLI validation rejected invalid files/formats; configuration reading did not modify files |

## Limitations and improvements

- **Production readiness:** Native Linux provisioning and public HTTPS remain
  unverified. Complete the host inventory and verify TLS and database restrictions.
- **Release reproducibility:** Retain release images; source rebuilds use mutable
  base tags and unpinned transitive dependencies.
- **Recovery:** Production cutover and rollback remain unverified. Rehearse the
  recovery plan before public deployment.
