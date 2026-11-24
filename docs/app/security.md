# Django application security

Security configuration: [base settings](../../menu_management/settings/base.py)
and [production settings](../../menu_management/settings/production.py).

## Authentication and authorization

| Entry point | Behavior |
| --- | --- |
| Customer menu | Public access; cart stored in the Django session |
| Staff login | Check `STAFF_PASSWORD` with `constant_time_compare`; create or reuse the shared `staff` user |
| Staff operations | `LoginRequiredMixin` and `is_staff` checks protect menu, inventory and order management |
| `/admin/` | Active staff may sign in; model permissions control access; superusers have full access |

Successful [staff login](../../menu_app/views/staff_views.py) synchronizes the user's
hashed password through `set_password()`, leaving separate admin passwords unchanged.
Users and sessions persist in PostgreSQL (`auth_user`, `django_session`) across Web
rebuilds. See [Deployment](../devops/deployment.md#preconditions-and-configuration)
for credential setup and database passwords.

## Request and session protection

| Mechanism | Behavior / source |
| --- | --- |
| CSRF | `CsrfViewMiddleware` checks tokens and request origins; [forms](../../menu_app/templates/menu_app/staff/login.html) include `{% csrf_token %}` |
| Host validation | `ALLOWED_HOSTS` accepts only listed request hostnames/IPs |
| Production errors | `DEBUG=False` disables Django debug error pages |
| Input validation | [Forms](../../menu_app/forms.py) validate menu fields and stock quantities; services enforce business constraints |
| SQL queries | Django ORM queries parameterize values |
| HTML and framing | Template autoescaping protects HTML output; `XFrameOptionsMiddleware` restricts framing |

Inherited [Django defaults](https://docs.djangoproject.com/en/5.1/ref/settings/):
session cookies are HttpOnly; session and CSRF cookies use `SameSite=Lax`.
CSRF cookies remain readable by JavaScript. These complement CSRF token checks.

Keep the signing secret `DJANGO_SECRET_KEY` private and stable across releases.
Credentials live in the application `.env`, generated with mode `600` on first setup.

## HTTPS reverse proxy

```text
Browser -- HTTPS --> Nginx -- HTTP (Docker network) --> Django
```

Nginx handles certificates and TLS; Django recognizes the original HTTPS request
while serving internal HTTP. Nginx overwrites `X-Forwarded-Proto`, and Web has no
published host port; this boundary makes the proxy header trustworthy.

| Setting | HTTPS behavior |
| --- | --- |
| `SECURE_PROXY_SSL_HEADER` | Trust Nginx's overwritten `X-Forwarded-Proto: https` |
| `USE_X_FORWARDED_HOST`, `USE_X_FORWARDED_PORT` | Both `False`; use the Nginx-supplied `Host` |
| `SECURE_SSL_REDIRECT` | Enabled; internal `/health/` is exempt for container checks |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | Both enabled; browsers send these cookies only over HTTPS |
| `CSRF_TRUSTED_ORIGINS` | Includes `https://PUBLIC_HOST`; token validation still applies |

Nginx already redirects external HTTP to HTTPS. Django's `SECURE_SSL_REDIRECT`
adds a second check; it could be disabled if redirects remain Nginx's responsibility.
The current implementation enables both. With Django redirects enabled, correct
proxy recognition prevents redirect loops.

Even without Django redirects, retain `SECURE_PROXY_SSL_HEADER`: `request.is_secure()`
supports CSRF origin/referer checks and HTTPS URL generation. Secure cookie flags
are configured separately; the header does not enable them automatically.

[Management commands](../../scripts/manage.py) pass `DJANGO_HTTPS=true` and
`DJANGO_CSRF_TRUSTED_ORIGINS=https://PUBLIC_HOST` to Web through
[Compose](../../docker/docker-compose.yml). With HTTP configuration, secure cookies
and Django HTTPS redirects are disabled.

See [TLS](../devops/tls.md#https-activation) for certificates, switching, direct
Compose inputs and recovery.

## Current limits

- Shared staff identity prevents per-employee attribution. Login rate limiting and
  MFA are not configured.
- `STAFF_PASSWORD` bypasses configured password validators: the custom login uses
  `set_password()` without `validate_password()`.
- `StaffLogoutView` uses GET, allowing cross-site forced logout; it should use POST.
- HSTS is disabled. Local HTTPS login checks passed; public verification and
  scheduled renewal remain pending in [TLS](../devops/tls.md#validation).
- [Django 5.1.0](../../requirements/base.txt) is pinned and
  [no longer supported](https://docs.djangoproject.com/en/5.1/topics/security/).
  Production maintenance requires a supported version and security updates.
