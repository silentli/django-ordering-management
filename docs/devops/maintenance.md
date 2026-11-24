# Maintenance

Scope: service inspection, incident triage, and recurring maintenance for the
checked-in Compose stack. Commands run on the target host from the repository root
with Docker access.

## Status and logs

| Command | Purpose |
| --- | --- |
| `status` | Container state, health, and ports |
| `logs [-f]` | Recent logs; `-f` streams until Ctrl+C |
| `diagnose` | Deployment configuration, Docker, status/logs, and application checks |

```sh
python3 scripts/manage.py status
python3 scripts/manage.py logs
python3 scripts/manage.py diagnose
```

`diagnose` checks `.env.deploy` when present; `--config PATH` selects another file.
Application checks require running Web and Nginx services. Failures return nonzero;
diagnosis does not modify configuration, restart services, or apply migrations.
Keep the same `--project-name NAME` for all operations.

In bootstrap mode, `diagnose` validates Nginx configuration and the managed
certificate without requiring Web or database services. It never requests certificates.
With existing HTTPS, it checks Django/migrations and Nginx
configuration; verify HTTPS requests separately.

For focused inspection:

```sh
docker compose -f docker/docker-compose.yml exec -T nginx nginx -t
docker compose -f docker/docker-compose.yml exec -T web python manage.py showmigrations --plan
df -h
docker system df
```

In application mode, `db` and `web` are healthy, `nginx` is running, and `collect-static`
has exited with code `0`. `init-admin` may be absent because it is an optional task.
Migration inspection requires a running Web container; if startup failed, begin
with Web and database logs. Nginx configuration inspection requires running Nginx.

For request checks and expected responses, use
[Release verification](deployment.md#release-verification). Container health does
not establish external reachability, valid TLS, or successful business operations.
Logs can contain sensitive details; redact before sharing.

## Incident triage

Record the onset, affected URLs or operations, current revision, and recent changes.
Preserve logs before replacing services. Follow the relevant symptom below:

| Symptom | Initial checks | Next decision |
| --- | --- | --- |
| No HTTP connection | Host reachability, port 80, `nginx` status | Separate external routing/firewall issues from a stopped stack |
| HTTP 502 | Web status and logs, Nginx logs | Check failed startup, database wait, or migration errors |
| HTTP 400 | Requested hostname and `ALLOWED_HOSTS` | Correct the hostname/configuration; retain an explicit allowlist |
| HTTP 500 | Web logs, database status, recent migrations | Identify the failing dependency or application operation |
| Web repeatedly restarts | Missing variable errors, DB reachability, migration output | Correct the first startup failure before retrying |
| Missing or stale CSS | `collect-static` exit status, static manifest errors, Nginx logs | Check the collection job and shared volume for the release |
| Healthy endpoint but failed page | Database-backed request and Web logs | `/health/` does not query the database |
| Database unhealthy | DB logs, initialized role, disk space | Healthcheck uses the configured role; changing `.env` does not modify existing roles |
| Empty database after a move | Compose project name and attached volumes | Locate the original volumes before any initialization or recovery |
| Backup missing or failed | [Shared backup log](backup-and-restore.md#shared-backup-log), disk space | Check the last success, failure stage, and archive path |
| Scheduled backup absent | `schedule status`, service journal, due time/timezone | Confirm activation and execution account; installer probe success is not a timed run |
| Backup log has only `started` | Service state, journal, `.partial` files | Check interruption/timeout; verify archive integrity before cleanup |

A Web restart executes migrations and is not a neutral diagnostic action. For
data or schema damage, preserve volumes and follow the recovery constraints in
[Deployment](deployment.md#failure-handling-and-rollback).

After recovery, verify the affected business paths, external entry point, and logs.
Record impact, cause, action, outcome, and follow-up work in the incident record.

## Recurring checks

The following cadence is proposed, not evidence of existing scheduled jobs.
Assign responsibility and adjust frequency to the environment's recovery objectives.

| Trigger | Check | Evidence |
| --- | --- | --- |
| After a release or host restart | Services, HTTP entry point, changed business flows, migrations | Release or maintenance record |
| Daily or through monitoring | Backup freshness, free storage, service failures | Monitor result or maintenance log |
| During planned maintenance | Host updates, container rebuild requirements, storage growth | Reviewed changes and post-change checks |
| Monthly, initially | Isolated restore and access review | Recovery exercise and access review record |
| After personnel or host changes | Access, secret references, project/volume identity | Updated restricted environment inventory |

Host patching and TLS renewal procedures depend on the hosting setup and are not
defined in the repository. Container restarts alone do not update the image's
installed packages.

## Data protection during maintenance

`docker compose down -v`, volume pruning, and database `flush` can remove persistent
data. They are not routine cleanup or outage recovery steps. Disk pressure requires
identifying the growing resource and preserving recoverable data before cleanup.
Changing a database password requires coordinating the live role and application
configuration; editing the initialization environment alone is insufficient.

## Limitations and improvements

- **Availability monitoring:** No external check is configured. Monitor the public
  URL and test the alert route.
- **Storage and backup alerts:** No failure alerts are configured. Define storage
  thresholds and backup freshness checks; verify notification delivery.
- **Operational ownership:** Responsibilities are not recorded. Assign release,
  incident, backup, and access ownership.
- **Health coverage:** The container health route omits database checks. Define
  health semantics and add separate dependency and business-operation checks.
