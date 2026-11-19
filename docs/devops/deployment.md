# Deployment

Scope: first installation and updates using `docker/docker-compose.yml`.
Commands run on the target host from the repository root, under an account with
Docker access. Local HTTP checks run on that host unless stated otherwise.
Validation status: source-reviewed on 2026-11-19; deployment and rollback not rehearsed.

## Preconditions and configuration

- Docker and Compose must support the checked-in configuration, including optional
  `env_file` entries, dependency conditions, and `--wait`.
- Host ports `80` and `5432` must be available for the default configuration.
- Public deployment requires an agreed domain, TLS termination, secure cookie
  settings, and restricted database exposure. These are not supplied by the default stack.
- Existing deployments require their recorded checkout path, Compose project name,
  release revision, credentials, and volume identities.

The runtime file is `menu_management/settings/env/.env`. The tracked
[example](../../menu_management/settings/env/.env.example) defines its keys:

| Key | Required value |
| --- | --- |
| `DJANGO_SECRET_KEY` | Unique application secret; retain across normal releases |
| `ALLOWED_HOSTS` | Comma-separated hostnames without schemes or spaces; local default `localhost,127.0.0.1` |
| `STAFF_PASSWORD` | Unique password for the staff login |
| `POSTGRES_DB` | Application database name |
| `POSTGRES_USER` | Database role; default healthcheck assumes `postgres` |
| `POSTGRES_PASSWORD` | Unique database password |
| `POSTGRES_HOST` | `db` for this Compose network |
| `POSTGRES_PORT` | `5432` inside the Compose network |

Keep runtime files outside version control and restrict permissions to `600`.
Do not overwrite an existing `.env`. Changing PostgreSQL initialization values in
that file does not update roles or passwords in an existing database volume.

## First deployment

### 1. Prepare the checkout

Clone the repository using the [quick start](../../README.md#quick-start), select
the approved revision, and prepare the runtime file. Confirm that no existing
deployment volumes will be reused unintentionally.

The optional host `data/` directory is mounted into PostgreSQL's initialization
directory. Inspect its contents before first startup. Initialization runs on
an empty database volume, not on every restart. The application does not seed menu
data automatically.

### 2. Validate and start

```sh
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.yml up --build -d --wait
docker compose -f docker/docker-compose.yml ps -a
```

Expected result: configuration validation succeeds, `db` and `web` become healthy,
`nginx` runs, and `collect-static` exits with code `0`. On failure, inspect the
[service logs](maintenance.md#status-and-logs) before retrying. Do not remove volumes.

The startup sequence checks required variables and waits for the database TCP port,
then applies committed migrations. Static collection writes to a shared volume
before Web starts. Replacing services can interrupt requests; no zero-downtime
rollout is configured.

### 3. Verify the application

Run the checks in [Release verification](#release-verification).
Staff login uses `STAFF_PASSWORD` and does not require an initial Django superuser.
The login creates or updates the application's `staff` account.

### 4. Create a Django administrator if required

The `/admin/` interface uses a Django superuser, separate from staff password login.
After migrations succeed, create the optional credential file without replacing
an existing one:

```sh
cp -n docker/admin.env.example .env.admin
chmod 600 .env.admin
```

Set a unique username, email, and nonempty password in `.env.admin`. Keep the password
single-quoted as shown in the example. Execute:

```sh
docker compose -f docker/docker-compose.yml run --rm --build init-admin
```

Expected result: successful superuser creation and working `/admin/` login.
An existing username causes creation to fail; do not delete the existing account
to retry. After successful verification, remove `.env.admin` and retain the
credentials in approved secret storage.

## Release an existing environment

### 1. Prepare the release

Record the current revision, project identity, service status, and image identities:

```sh
git status --short
git rev-parse HEAD
docker compose -f docker/docker-compose.yml ls
docker compose -f docker/docker-compose.yml images
docker compose -f docker/docker-compose.yml ps -a
```

Stop if local changes are unexplained. Confirm checks for the candidate revision
using [CI run verification](ci.md#run-verification). Review migrations, configuration
changes, downtime, and schema compatibility with the previous release. Secure a
[backup](backup-and-restore.md) and define the recovery path before schema changes.

### 2. Select the approved revision

Fetch source updates, then set `release_ref` to an approved commit SHA:

```sh
git fetch origin
release_ref='REPLACE_WITH_APPROVED_COMMIT_SHA'
git switch --detach "$release_ref"
```

Stop if the revision cannot be selected cleanly. Preserve runtime configuration
and the existing Compose project name. Prepare any required configuration additions
before starting the candidate version.

### 3. Build and replace services

```sh
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d --force-recreate --wait
```

`--force-recreate` reruns static collection for the selected revision. Static
collection uses `--clear`; asset changes can affect requests before Web replacement
finishes. Use a maintenance window when interruption is unacceptable.

Stop on failure and inspect service status and logs. A failed startup does not prove
that migrations were rolled back. Determine the applied migration state before
selecting a recovery action.

## Release verification

For the default local HTTP configuration:

```sh
curl --fail --show-error http://localhost/health/
curl --fail --show-error --output /dev/null http://localhost/menu/
docker compose -f docker/docker-compose.yml exec -T web python manage.py showmigrations --plan
docker compose -f docker/docker-compose.yml logs --tail=100 web nginx db
```

Expected result: `/health/` returns `ok`, `/menu/` succeeds, expected migrations
are applied, and logs contain no new application or database errors. If `localhost`
is not in `ALLOWED_HOSTS`, use the configured hostname and resolve it to the target.

Open the menu in a browser and confirm CSS loads. Verify staff login, inventory,
and order pages. Validate changed write operations with designated test records
under the environment's data policy. A health response alone does not validate
database connectivity or business behavior. For public environments, additionally
verify the actual HTTPS URL, certificate, redirects, and access restrictions from
outside the host.

Record the environment, revision, images, migration result, checks, and outcome in
the private release record. Do not include credentials or customer data.

## Failure handling and rollback

For application-only failures, identify the last working revision and its runtime
configuration. Reuse the release sequence with that revision only after confirming
compatibility with the currently applied schema. Retained images are preferable to
rebuilding: mutable base tags and unpinned transitive dependencies can change a build.

For migration failures or incompatible schemas, keep affected writes paused and
inspect the database state. Returning to older code does not undo a migration.
Database reversal or replacement requires a reviewed recovery plan, verified backup,
and explicit acceptance of any lost writes. The repository currently has no
rehearsed production recovery procedure; the isolated exercise in
[Backup and restore](backup-and-restore.md#isolated-restore-exercise) is not a
production cutover procedure.

Do not use volume deletion, `flush`, or automatic reverse migrations as a generic
rollback. Preserve evidence before further changes.

## Limitations and improvements

- **HTTPS:** The default stack uses HTTP and disables secure cookie flags. Configure
  TLS termination, proxy trust, and secure cookies; verify the public HTTPS path.
- **Database exposure:** PostgreSQL publishes a host port. Restrict access to intended
  clients and verify reachability from an external network.
- **Release reproducibility:** Source builds use mutable base tags. Retain release
  images by digest and associate each image with its source commit.
- **Schema changes:** Web startup applies migrations. Define and rehearse migration
  and recovery procedures for incompatible schema changes.
- **Release readiness:** Releases remain manual and the environment inventory is
  incomplete. Record ownership and environment details; rehearse release and recovery.
