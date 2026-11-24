# Technical overview

## Engineering status

Implementation status reflects the source.

### Development

| Area | Implementation |
| --- | --- |
| Application | Customer ordering; staff menu, inventory, and order management |
| Business logic | Service layer, transaction-protected checkout, stock updates, order state transitions |
| Automated tests | Model, service, view, health, management-command, and host-operation tests |
| Local quality checks | Ruff lint/format checks, pre-commit |

### Deployment and operations

| Area | Status | Outstanding work |
| --- | --- | --- |
| Container deployment | Implemented | Production host verification and recovery rehearsal pending |
| Continuous integration | Implemented | Workflow results and external investigation runner/runtime unverified |
| Release delivery | Partial | Host management script available; source selection and rollback remain manual |
| Public deployment | Partial | TLS, secure cookies, and database exposure require environment-specific configuration |
| Backup automation | Partial | Shared audit log and timer installer; host activation, retention, offsite copies and recovery pending |
| Monitoring | Partial | Container checks implemented; external availability checks and alerting absent from repository |
| Environment inventory | Unknown | Host, domain, deployed revision, resource ownership, and runtime state not established |

## System architecture

Server-rendered Django application with a service layer and PostgreSQL persistence.
Views handle routing, request validation, sessions, and template responses. Services
coordinate menu queries, inventory mutations, and order state transitions through
the ORM; model constraints and migrations define the persistence schema.

Customer cart state is stored in the Django session. Checkout and inventory mutations
use database transactions. Nginx proxies application traffic to Gunicorn and serves
static and media assets directly.

## Repository structure

Selected source, configuration, and documentation paths:

```text
.
├── menu_app/                  # Application domain
│   ├── views/                 # Customer and staff HTTP handlers
│   ├── services/              # Business operations and transaction boundaries
│   ├── models/                # ORM entities and database constraints
│   ├── migrations/            # Versioned schema changes
│   ├── templates/             # Server-rendered UI
│   ├── management/commands/   # Explicit data-loading commands
│   └── tests/                 # Application test suite
├── menu_management/           # Django project configuration
│   ├── settings/              # Base, test, and production profiles
│   ├── urls.py                # Root URL configuration
│   └── wsgi.py                # Gunicorn application entry point
├── docker/                    # Dockerfiles and Compose definitions
├── nginx/                     # Reverse proxy and asset-serving configuration
├── static/                    # Source static assets
├── requirements/              # Runtime, development, test, and quality dependencies
├── scripts/                   # Management CLI, ops modules, and safety tests
├── .github/workflows/         # CI and failure investigation workflows
├── ci_acceptance/             # Isolated CI tooling contracts
├── docs/                      # Technical documentation
│   ├── development.md         # Development and test tooling
│   ├── app/                   # Application behavior and security
│   └── devops/                # CI, deployment, backup/recovery, and maintenance
├── entrypoint.sh              # Runtime validation, database wait, migrations
└── manage.py                  # Django management CLI
```

## Documentation index

| Domain | Reference | Coverage |
| --- | --- | --- |
| Development | [Development workflow](development.md) | Local setup, test database, pytest, Ruff, hooks |
| Application security | [Django security](app/security.md) | Authentication, permissions, CSRF, cookies, HTTPS proxy settings and current limits |
| Infrastructure | [DevOps overview](devops/README.md) | Service topology, storage boundaries, environment configuration |
| Continuous integration | [CI](devops/ci.md) | Workflows, triggers, execution environments, permissions, diagnostics |
| Releases | [Deployment](devops/deployment.md) | Initial setup, configuration, rollout, verification, rollback constraints |
| Transport security | [TLS and certificates](devops/tls.md) | Nginx templates, HTTP bootstrap, mounts, HTTPS prerequisites and limitations |
| Data protection | [Backup and restore](devops/backup-and-restore.md) | Manual/scheduled backups, shared log, isolated restore, recovery limitations |
| Operations | [Maintenance](devops/maintenance.md) | Diagnostics, incident triage, recurring maintenance |
| CI tooling | [Acceptance packages](../ci_acceptance/README.md) | Isolated packaging contracts and execution boundaries |

## Operational handover

Outstanding records:

- Environment inventory: host, domain, checkout, Compose project, deployed revision.
- Ownership: releases, incidents, database recovery, DNS/TLS.
- Recovery evidence: backup destination, retention, last successful restore.

Maintain access details and credentials in restricted operational storage.
