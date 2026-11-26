# Spa Manager - backend

FastAPI + SQLite REST service: login, customers, onboarding, appointments, incentive reports and PDF payslips.
The web UI lives in the separate `spa-ui` (Angular) repo.

```bash
pip install -r requirements.txt
ADMIN_PASSWORD='choose-one' SPA_NAME='My Spa' SPA_CORS_ORIGINS='http://localhost:4200' python -m app   # :8080
python -m unittest discover -s tests -t .
```

| Env | Default | Meaning |
|---|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / `admin12345` | seeded on first start only - change it |
| `SPA_DB` | `spa.db` | SQLite file |
| `SPA_NAME` | `Serenity Spa` | business name on payslips |
| `SPA_CORS_ORIGINS` | `http://localhost:4200` | comma-separated UI origins allowed to call the API |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | listen address |

Interactive docs at `/docs`.

## Roles
- **admin**: everything, incl. therapists/services, reports, payroll and user management (`GET/POST /api/users`, `PUT /api/users/{id}` role/active, `POST /api/users/{id}/password` reset).
- **staff**: customers, onboarding, appointments.

## Rules
- Users can change their own password (`POST /api/auth/password`); other devices are signed out. Deactivating a user or resetting their password signs them out at once. You cannot deactivate/demote yourself or the last active admin.
- `PUT /api/appointments/{id}` reschedules or changes a booking, only while it is `booked` (completed ones feed payroll). Price: explicit value wins; a changed service takes its list price; otherwise the agreed price is kept.
- Opaque bearer token (12 h, stored hashed, revoked on logout).
- Price is snapshotted on the appointment (override allowed); a therapist cannot be double-booked.
- **Payslip = fixed salary + incentive**; incentive = incentive % x price of the therapist's *completed* appointments in the month.
- `POST /api/onboarding` creates a customer and optionally their first appointment atomically.
- Payslip PDF: `GET /api/payroll/{therapist_id}/{YYYY-MM}/pdf`.

## Deploy on AWS (single EC2 box)
SQLite lives on the instance disk, so run exactly one instance. `Dockerfile` builds the API; `deploy/aws/bootstrap.sh`
is EC2 user-data for Amazon Linux 2023: it installs Docker, builds this repo, generates a random admin password into
`/opt/spa/env` (read it with `sudo cat /opt/spa/env`), mounts `/var/lib/spa` as the data volume, and fronts the API with
Caddy on `https://<public-ip>.sslip.io` (free Let's Encrypt cert). Open ports 80 and 443 only; SSH via SSM Session Manager.

- Redeploy: `sudo bash /opt/spa/src/deploy/aws/update.sh`
- Backup (cron, nightly): `deploy/aws/backup.sh` keeps 14 consistent copies in `/var/lib/spa/backups`
