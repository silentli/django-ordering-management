# Backup and restore

Scope: backup of the Compose PostgreSQL database and an isolated restore exercise.
Production recovery remains unverified.

## Current coverage and constraints

The [host management script](deployment.md#automated-host-management) backs up the
database on demand and before updates or volume deletion. A sample archive passed
an isolated restore; see the [validation record](deployment.md#validation-and-remaining-work).
No recurring schedule, retention policy, or offsite destination is configured.

A custom-format `pg_dump` archive includes one database's schema and data. It does
not include cluster roles, runtime secrets, uploaded media, host configuration,
or changes committed after the dump's consistent snapshot. A complete recovery plan
must cover those separately and define acceptable data loss and recovery time.

## Scripted database backup

With the database running, execute from the repository root:

```sh
python3 scripts/manage.py backup
```

For another Compose project, put `--project-name NAME` before `backup`.
The script writes a custom-format archive to `data/backups/`, named with the project,
UTC timestamp, and random suffix. Files use mode `600`; only a nonempty dump whose
directory passes `pg_restore --list` is published. Failed attempts remove the
temporary file and return nonzero. `update` and `destroy` abort if this step fails.

Keep the reported archive path with its source revision, database, image identity,
and timestamp. Archive listing checks readability, not complete restoration.
Copy archives to protected offsite storage; local host backups do not cover host loss.

## Manual database backup

Execution location: target host, repository root. Required access: Docker and a
private writable backup directory. The `db` service must be running with its
expected `POSTGRES_USER` and `POSTGRES_DB`. Confirm the Compose project identity
before execution. The database remains online; the dump adds database and I/O load.
The Compose examples use the default project `docker`; add `--project-name NAME`
for a custom project.

Set `backup_dir` to an approved absolute host path outside the checkout. The following
POSIX shell block writes a private temporary file and publishes it only after archive
inspection succeeds:

```sh
(
    set -eu
    umask 077
    backup_dir='/REPLACE_WITH_APPROVED_ABSOLUTE_BACKUP_DIRECTORY'
    test -d "$backup_dir"
    test -w "$backup_dir"
    backup_tmp=$(mktemp "$backup_dir/menu-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
    trap 'rm -f "$backup_tmp"' EXIT
    docker compose -f docker/docker-compose.yml exec -T db sh -ec \
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_tmp"
    test -s "$backup_tmp"
    docker compose -f docker/docker-compose.yml exec -T db \
        pg_restore --list < "$backup_tmp" > /dev/null
    backup_file="$backup_tmp.dump"
    mv "$backup_tmp" "$backup_file"
    trap - EXIT
    printf 'Backup archive: %s\n' "$backup_file"
)
```

Expected result: exit status `0` and a nonempty `.dump` file with private permissions.
On failure, no completed archive is published and the temporary file is removed.
Check database availability, permissions, available storage, and the reported error
before retrying. Existing completed archives remain unchanged.

Archive listing verifies that the archive directory can be read. It does not prove
that all contents can be restored. Record the UTC backup time, source environment,
database, application revision, PostgreSQL image identity, size, and storage location.
Treat the archive as sensitive production data.

## Isolated restore exercise

Execution location: a Docker host approved to hold the source data. Required access:
Docker and a readable backup archive. This exercise uses a new container with no
network or production volume attached. It must not target the running `db` service.

Run these steps in the same POSIX shell session. Replace `backup_file` with a
completed archive. Use a PostgreSQL 14 image approved for the environment; prefer
the source database's recorded image digest when available.

```sh
backup_file='/REPLACE_WITH_COMPLETED_ARCHIVE.dump'
restore_container="menu-restore-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test -s "$backup_file" && \
docker run -d --name "$restore_container" --network none \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_DB=restore_check postgres:14
```

Stop if archive selection or container creation fails. Trust authentication is
limited to this disposable container with no network access; it is not a production
configuration. Wait for readiness:

```sh
docker exec "$restore_container" pg_isready -h 127.0.0.1 -U postgres -d restore_check
```

The TCP check waits for the final server rather than the temporary initialization
server. Repeat it after a short interval if startup is still in progress.
If readiness does not succeed within 60 seconds, inspect the container logs and
stop the exercise. Once ready, restore:

```sh
docker exec -i "$restore_container" pg_restore \
    -U postgres -d restore_check --no-owner --no-privileges --exit-on-error \
    < "$backup_file"
```

Stop on a nonzero restore result. Ownership and grants are deliberately excluded
from this exercise; production recovery must recreate the intended roles and grants.

Inspect restored tables and migration history:

```sh
docker exec "$restore_container" psql -U postgres -d restore_check -c '\dt'
docker exec "$restore_container" psql -U postgres -d restore_check \
    -c 'SELECT app, name FROM django_migrations ORDER BY app, name;'
```

Expected result: restore exits successfully and expected application tables and
migration records are present. Check representative row counts and relationships
against a recorded baseline, without copying sensitive records into shared logs.
Record the archive, PostgreSQL image, elapsed time, checks, and result.

After recording results, remove only the disposable exercise container and its
anonymous database volume:

```sh
docker rm -v -f "$restore_container"
```

A successful database exercise is one recovery checkpoint. Full recovery additionally
requires an isolated application instance at the matching revision, runtime settings,
media restoration, and business-flow verification. No production cutover or database
replacement is specified here.

## Limitations and improvements

- **Recovery validation:** Rehearse with actual release backups, configuration, media,
  and application checks; retain the results. The sample restore does not cover these.
- **Scheduling:** Script backups are on demand and before updates/deletion. Configure
  and verify a scheduled run; no recurring backup job is installed.
- **Offsite storage and retention:** No policy is defined. Configure encrypted
  separate storage and retention; verify archive retrieval.
- **Recovery coverage:** Database archives omit media, configuration, and cluster
  roles. Protect these separately and document full application recovery.
- **Recovery objectives and alerts:** Acceptable data loss, recovery time, and failure
  notification are undefined. Agree on targets and test backup failure alerts.
