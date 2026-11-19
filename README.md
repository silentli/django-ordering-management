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

Prerequisites: Git, Docker, and
[Docker Compose 2.24.0+](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).
Python runs inside the application container.

```sh
git clone https://github.com/silentli/menu-management-demo.git
cd menu-management-demo
cp -n menu_management/settings/env/.env.example menu_management/settings/env/.env
chmod 600 menu_management/settings/env/.env
```

Set unique values for `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, and `STAFF_PASSWORD`
in the new file. Keep `POSTGRES_HOST=db` and `POSTGRES_PORT=5432` for Compose.
Existing configuration must remain intact.

```sh
docker compose -f docker/docker-compose.yml up --build -d --wait
```

Expected result: `db` and `web` become healthy, `nginx` runs, and `collect-static`
exits successfully. Open the [customer menu](http://localhost/menu/) or
[staff login](http://localhost/staff/login/).

The menu starts empty. To import the bundled initial menu and inventory:

```sh
docker compose -f docker/docker-compose.yml exec -T web python manage.py load_initial_data
```

This reads `menu_app/data/initial_menu_items.json` and adds twelve items across
four categories with 20 units of stock each. An optional file path can be supplied.
The command skips import when menu items already exist.

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
| [Backup and restore](docs/devops/backup-and-restore.md) | Database archives, restore validation, recovery coverage |
| [Maintenance](docs/devops/maintenance.md) | Service diagnostics, incident triage, recurring checks |
