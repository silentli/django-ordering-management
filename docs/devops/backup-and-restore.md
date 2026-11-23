# Backup and restore

Run commands from the repository root on the Docker host. Examples use Compose
project `docker`. Management commands accept `--project-name NAME` before or after
the subcommand; direct Compose commands require it before the operation.

## Backup contract

| Item | Configuration |
| --- | --- |
| Engine | PostgreSQL `pg_dump -Fc`; logical backup of one database |
| Included | Schema and data, including Django users/password hashes |
| Excluded | Cluster roles, `.env`, media, host configuration, writes after the snapshot |
| Archives | `data/backups/PROJECT-UTC_TIMESTAMP-RANDOM.dump`, mode `600` |
| Shared log | `data/backups/backup.log`, JSON Lines, UTC timestamps, mode `600` |
| Trigger | Manual, systemd/cron, or before `update`/`destroy` |
| Verification | Nonempty archive and successful `pg_restore --list` |
| Retention / offsite / alerts | Not configured |

Archive inspection does not prove restoration. A sample archive passed an isolated
restore; see [validation](deployment.md#validation). Production
recovery remains unverified.

## Scripted database backup

Requires Docker access, a readable runtime `.env`, a running database container,
and write access to the checkout/backups.

```sh
python3 scripts/manage.py backup
```

Execution: `log started → lock → Docker/config checks → pg_dump to .partial →
archive inspection → publish .dump → log success`.

- Dump/check failures return nonzero, log the stage/error, and remove partial dumps.
- `update` and `destroy` prepare the database and require backup success before proceeding.
- Use this entry point for manual work; direct `pg_dump` bypasses the shared log.
- Record source database, commit, and PostgreSQL image alongside retained archives.

## Shared backup log

```sh
tail -n 20 data/backups/backup.log
```

| Fields | Meaning |
| --- | --- |
| `timestamp`, `run_id`, `project` | UTC event time, attempt identifier, Compose project |
| `source` | `manual`, `scheduled`, `update`, or `destroy` |
| `status` | `started`, then `success` or `failed` |
| `archive` | Final/intended archive path, once assigned |
| `bytes`, `duration_seconds` | Size on success; elapsed time on completion |
| `stage`, `error`, `error_type` | Failure location and diagnostic |

The same records reach terminal/scheduler output. Standalone attempts also record
lock conflicts, configuration errors, and Docker failures.

| Condition | Behavior |
| --- | --- |
| Checkout lock busy | Fail; no concurrent dump |
| Log cannot be opened | Fail before starting a dump |
| Completion log write fails | Fail; completed archive may remain; update/deletion is blocked |
| SIGTERM / Ctrl+C | Normally log failure and clean partial dump |
| Power loss / SIGKILL | May leave `started` without completion and a `.partial` file |

Check terminal/journal output if the log is unavailable. Monitor disk usage;
archives and log records have no automatic retention or rotation.

## Daily scheduling on Linux

| Component | Responsibility |
| --- | --- |
| `scripts/manage.py` | Administrator CLI and actual backup entry point |
| `scripts/backup_schedule.py` | Generate, validate, install, inspect, and disable systemd configuration |
| `.timer` / `.service` | Linux triggers `manage.py backup --source scheduled` at the scheduled time |

Use one scheduler per installation. `start` does not enable a schedule.

### systemd timer

Prerequisites:

- Linux with systemd, rootful Docker, Python 3.10+, and util-linux `runuser`.
- Persistent checkout and existing deployment account with Docker access,
  `.env` read access, and checkout/backups write access.
- Running application/database; installed timezone data for named zones.
- Disable older manual `menu-backup.timer` or cron entries first.

Preview, then install as root. Examples use the current shell account (`$USER`);
set `--user` explicitly if the deployment account differs:

```sh
python3 scripts/manage.py schedule install --user "$USER" --dry-run
sudo /usr/bin/python3 scripts/manage.py schedule install \
  --user "$USER" --at 03:00 --timezone Europe/Berlin
```

| Setting | Value / behavior |
| --- | --- |
| Default calendar | Daily `03:00 Europe/Berlin`; override `--at` and `--timezone` |
| Generated units | `/etc/systemd/system/menu-backup-PROJECT.service` and `.timer` |
| Backup execution | Service `User=` is `--user`; installation uses sudo |
| Catch-up | `Persistent=true` for missed timer activations |
| Failure retry | `RestartSec=5min`; `StartLimitBurst=3`, `StartLimitIntervalSec=1h` |
| Attempt timeout | `TimeoutStartSec=30min` |
| Preview | `--dry-run`: no writes, backup probe, or activation |

Installation: `render → validate → real backup as --user → write units → reload → enable`.
Validation/probe failure prevents installation. Reinstall updates the same checkout;
foreign/manual units are refused. Users, Docker permissions, and the application
are not provisioned. Activation failure may leave unit files; inspect status/journal
before retrying.

```sh
python3 scripts/manage.py schedule status
journalctl -u menu-backup-docker.service -n 50 --no-pager
sudo /usr/bin/python3 scripts/manage.py schedule disable
```

- Check a `success` timestamp after the timer's due time against the journal.
  The install probe also logs `source=scheduled`; it does not prove a timer activation.
- `schedule disable` stops future timer activations; units, logs, and manual backup remain.
- Active jobs and service retries continue. Cancel them explicitly with
  `sudo systemctl stop menu-backup-docker.service` when required.
- Templates in `scripts/systemd/` support manual installation; their comments identify
  editable fields. Rootless Docker requires custom context/dependency configuration.

### Cron alternative

Use `crontab -e` as the deployment account. Adjust the absolute checkout path:

```cron
0 3 * * * /usr/bin/python3 /opt/django-ordering-management/scripts/manage.py backup --source scheduled
```

Uses the cron daemon's timezone; no retry/catch-up is configured here. Test the
command as that account, then check the next scheduled result in `backup.log`.
Do not enable both cron and the systemd timer.

## Isolated restore exercise

Requires Docker access and a readable archive on a host approved to hold the data.
Use a disposable container with no network or production volume attached; never
target the running `db` service.

Run in one POSIX shell session; stop on any nonzero result. Set `backup_file` and
use a PostgreSQL 14 image matching the source, preferably its recorded digest.

```sh
backup_file='/REPLACE_WITH_COMPLETED_ARCHIVE.dump'
restore_container="menu-restore-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test -s "$backup_file" && \
docker run -d --name "$restore_container" --network none \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_DB=restore_check postgres:14
```

Trust authentication is limited to this network-isolated test container. Wait for
TCP readiness, retrying for up to 60 seconds; inspect logs and stop if unavailable:

```sh
docker exec "$restore_container" pg_isready -h 127.0.0.1 -U postgres -d restore_check
```

TCP readiness avoids mistaking the temporary initialization server for the final
server. Restore only after it succeeds:

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

After recording results, remove only the test container and its anonymous volume:

```sh
docker rm -v -f "$restore_container"
```

A successful database exercise is one recovery checkpoint. Full recovery additionally
requires an isolated application instance at the matching revision, runtime settings,
media restoration, and business-flow verification. No production cutover or database
replacement is specified here.

## Limitations and improvements

- **Retention and offsite storage:** No policy is configured. Define retention,
  encrypted offsite copies, and retrieval checks.
- **Recovery coverage:** Database dumps omit configuration, roles, and media.
  Protect these separately and rehearse full application recovery.
- **Monitoring:** No backup alerts are configured. Monitor failed attempts,
  missing recent successes, and disk pressure.
- **Recovery objectives:** Acceptable data loss and recovery time are undefined.
  Agree targets and validate them against actual release backups.
