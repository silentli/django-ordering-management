# Continuous integration

## Workflows

| Workflow | Trigger | Runner | Validation |
| --- | --- | --- | --- |
| [Test](../../.github/workflows/check-test.yml) | Path-filtered push/PR to `main`; manual dispatch | `ubuntu-latest` | Django application and host-operation tests |
| [Lint](../../.github/workflows/check-lint.yml) | Path-filtered push/PR to `main`; manual dispatch | `ubuntu-latest` | Ruff lint and format checks |
| [Package CI](../../.github/workflows/package-ci.yml) | Path-filtered push/PR to `main`; manual dispatch | `ubuntu-24.04` | Wheel build, clean install, package contracts |
| [Investigate CI failure](../../.github/workflows/ci-investigate.yml) | Completion of Lint or Package CI on `main`, subject to job conditions | `self-hosted`, `macOS`, `X64`, `ci-investigator` | External investigation pipeline |

## Execution environments

| Workflow | Runtime and dependencies | Configuration |
| --- | --- | --- |
| Test | Python 3.12, PostgreSQL 14, `requirements/test.txt` | Django test settings; GitHub environment `test` |
| Lint | Ruff 0.11.9 | `pyproject.toml` |
| Package CI | Python 3.12; hash-verified build and dependency wheels | `ci_acceptance/build-tools.lock`, `ci_acceptance/dependencies.lock` |
| Investigate CI failure | Host-managed Python environment and `ci_autofix.pipeline_cli` | Runner variable `CI_AUTOFIX_HOME`; absolute path required |

The Test workflow starts PostgreSQL as a GitHub Actions service at
`127.0.0.1:5432`. Database settings are supplied by the job; local `.env.test` is not
required. pytest-django creates and migrates a separate test database.
The workflow also runs `python -m unittest discover -s scripts/tests -v` for
host-operation safeguards; these tests simulate Docker commands.

Package CI runs independent matrix jobs for `logic`, `discovery`, and `dependency`.
Contracts execute against installed wheels in clean environments. These packages
validate CI tooling rather than Django behavior.

## Trigger coverage

| Workflow | Path filter scope |
| --- | --- |
| Test | Application/project packages, `scripts/**`, management entry point, requirements, pytest/project configuration, Test workflow |
| Lint | Python files, Ruff/project configuration, pre-commit configuration, quality requirements, Lint workflow |
| Package CI | `ci_acceptance/**` and Package CI workflow |

Exact patterns are maintained in the workflow files. Docker, Nginx, and documentation
changes alone do not trigger these push/PR checks. A passing run for an earlier
revision does not validate a later commit. The repository does not establish branch
protection or required-check settings.

## Permissions and self-hosted execution

Test and Lint omit an explicit permissions block; effective token permissions depend
on repository settings and event context. Package CI declares `contents: read` and
disables checkout credential persistence. Investigate CI failure declares
`actions: read` and `contents: read`.

The investigation job accepts only failed push runs from the configured repository
and workflow IDs on `main`. Pull request runs and Test workflow failures do not
qualify. Repository and workflow ID conditions must be updated when adapting the
workflow to another repository.

The self-hosted job invokes a preinstalled pipeline without a repository checkout or
dependency installation. `CI_AUTOFIX_HOME` identifies the external runtime; runner
availability and runtime installation require host-level verification. Concurrency
group `ci-investigator` disables cancellation of in-progress jobs; the job timeout
is 30 minutes. The workflow contains no publishing or application deployment step.

## Run verification

1. Open the repository's **Actions** tab and select the applicable workflow.
2. Locate the run for the candidate commit SHA. Check the event, branch, and all job
   results; skipped or queued jobs do not establish successful validation.
3. If path filtering skipped a required check, use **Run workflow** and select the
   candidate branch or tag. Confirm the resulting run resolves to the intended SHA.
4. Record successful check results with the release or change record.

## Failure diagnostics

| Symptom | Inspection |
| --- | --- |
| Test setup failure | PostgreSQL service health, job environment, dependency installation |
| Test assertion failure | First failing test and traceback; reproduce using the corresponding local test selection |
| Lint failure | Reported rule or formatting diff; apply and review corrections locally |
| Package contract failure | Failing matrix project, wheel build/install output, contract traceback |
| Investigation job skipped | Source workflow, event, branch, conclusion, repository/workflow ID conditions |
| Investigation job queued | Matching self-hosted runner labels and availability |
| Investigation runtime failure | `CI_AUTOFIX_HOME`, Python executable, external pipeline logs |

Rerun after the failure cause is resolved. Changes to application or workflow code
require validation against the updated commit SHA.
