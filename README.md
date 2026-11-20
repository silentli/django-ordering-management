# Menu Management System

A Django application for menu management, inventory tracking, and order processing,
with customer and staff interfaces.

## Stack

| Component | Configured version |
| --- | --- |
| Python | 3.12 |
| Django | 5.1.0 |
| Gunicorn | 21.2.0 |
| Nginx | 1.25 (Alpine) |
| PostgreSQL | 14 |

## Quick start

Prerequisites: Git, Python 3.10+ on the Linux host, Docker, and
[Docker Compose 2.24.0+](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).
Application dependencies run inside the container; the host script uses only
the Python standard library.

```sh
git clone https://github.com/silentli/menu-management-demo.git
cd menu-management-demo
python3 scripts/manage.py start
```

The script generates a private `.env` with random secrets if absent, builds missing
images, starts services, imports the initial menu, and verifies the application.
Existing configuration and menu data are preserved. Read `STAFF_PASSWORD`
from `menu_management/settings/env/.env` to sign in; credentials are not printed.
Use `start --no-seed` for an installation without the sample menu.

Expected result: `db` and `web` become healthy, `nginx` runs, and `collect-static`
exits successfully. Open the [customer menu](http://localhost/menu/) or
[staff login](http://localhost/staff/login/).

See the [management commands](docs/devops/deployment.md#automated-host-management)
for stop, update, backup scheduling, diagnostics, and removal.

This starts the production settings profile with the repository's HTTP configuration.
Public deployment requires the environment checks in the
[deployment guide](docs/devops/deployment.md).

## Documentation

| Topic | Scope |
| --- | --- |
| [Overview](docs/README.md) | Engineering status, system architecture, operational handover |
| [Development](docs/development.md) | Local environment, pytest, Ruff, pre-commit |
| [DevOps](docs/devops/README.md) | Deployment topology, persistent storage, environment status |
| [CI](docs/devops/ci.md) | Workflows, triggers, runners, permissions, failure diagnostics |
| [Deployment](docs/devops/deployment.md) | Configuration, provisioning, release verification, rollback |
| [Backup and restore](docs/devops/backup-and-restore.md) | Manual/scheduled backups, shared log, restore validation |
| [Maintenance](docs/devops/maintenance.md) | Service diagnostics, incident triage, recurring checks |
