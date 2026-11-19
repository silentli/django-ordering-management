# Development workflow

Execution model: Python tooling on the host; PostgreSQL in a dedicated test container.
Working directory: repository root. Shell: POSIX-compatible, with `.venv` activated
after setup.

## Toolchain

| Component | Version | Configuration |
| --- | --- | --- |
| Python | 3.12 | Host runtime |
| PostgreSQL | 14 | `docker/docker-compose.test.yml` |
| pytest | 8.0.0 | `requirements/test.txt`, `pytest.ini` |
| Ruff | 0.11.9 | `requirements/quality.txt`, `pyproject.toml` |
| pre-commit | 3.6.2 | `requirements/dev.txt`, `.pre-commit-config.yaml` |

Prerequisites: Git, Python 3.12, Docker, and Docker Compose.

## Local setup

### 1. Install dependencies

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/dev.txt
```

`requirements/dev.txt` includes runtime, test, quality, and development dependencies.

### 2. Install Git hooks

```sh
python -m pre_commit install
```

The hooks apply Ruff lint fixes and formatting to staged files. Reinstall hooks
after moving or recreating `.venv`; the hook references its installation environment.

### 3. Configure the test database

Create `menu_management/settings/env/.env.test` if absent. Preserve existing settings.

```dotenv
POSTGRES_DB=menu_management_test
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5433
```

The database role requires `CREATEDB`. Exported variables override `.env.test`;
exclude production database settings from the shell environment.

Start the test database with an explicit host port:

```sh
POSTGRES_PORT=5433 docker compose -f docker/docker-compose.test.yml up -d --wait db
```

Expected result: `db` becomes healthy at `127.0.0.1:5433`; PostgreSQL listens on
port `5432` inside the container. The test service uses trust authentication and
publishes its port on host interfaces; restrict execution to an isolated local
environment and block external access.

For a different host port, update both the command and `.env.test`. Compose port
interpolation uses the shell environment, not the service's `env_file`.

## Validation

### Tests

```sh
DJANGO_ENV=test python -m pytest
```

Expected result: exit status `0`. pytest-django creates `test_<POSTGRES_DB>`, applies
migrations, and drops the database after execution. Manual migration is not required.

Optional test selections and database reuse:

```sh
# Run model tests
DJANGO_ENV=test python -m pytest menu_app/tests/models/test_models.py

# Retain and reuse the local test database
DJANGO_ENV=test python -m pytest --reuse-db

# Rebuild a reused database after schema changes
DJANGO_ENV=test python -m pytest --reuse-db --create-db
```

Coverage: models, services, views, health responses, and management commands under
`menu_app/tests/`. On database setup failure, check endpoint configuration, service
health, and role privileges before retrying.

Host-operation safety tests run separately, using only the standard library and
temporary files; they do not require Docker or a database:

```sh
python -m unittest discover -s scripts/tests -v
```

When changing container configuration or startup logic, also verify image build,
service health, and HTTP access in an isolated Compose installation. Mocked command
tests do not establish that containers can start.

### Lint and format checks

```sh
ruff check .
ruff format --check .
```

Expected result: exit status `0` for both checks. Rules and formatter settings:
`pyproject.toml`. Apply corrections with `ruff check --fix .` and `ruff format .`;
inspect the resulting diff before committing.

### Git hook validation

```sh
python -m pre_commit run --all-files
```

Expected result: all hooks pass. Hooks can modify files; inspect and stage changes,
then rerun validation.
