# Spa Manager - backend

FastAPI + PostgreSQL REST service: login, customers, onboarding, appointments, incentive reports and PDF payslips.
The web UI lives in the separate `management-ui` (Angular) repo.

## Run locally
```bash
docker compose up --build            # Postgres 16 + API on http://localhost:8080  (docs at /docs)
# or, with your own Postgres:
pip install -r requirements.txt
PG_DSN=postgresql://spa:spa@localhost:5432/spa ADMIN_PASSWORD='choose-one' python -m app
# tests (each test gets a throw-away schema; without TEST_PG_DSN they are skipped)
TEST_PG_DSN=postgresql://spa:spa@localhost:5432/spa python -m unittest discover -s tests -t .
```

| Env | Default | Meaning |
|---|---|---|
| `PG_DSN` | *(required)* | e.g. `postgresql://spa:secret@host:5432/spa` |
| `SPA_PG_SCHEMA` | `spa` | schema that holds every table, so the app can share a database with others |
| `SPA_TZ` | `Asia/Kolkata` | the spa's time zone: timestamps are shown in it and "this month" follows it |
| `PG_POOL_MAX` | `10` | max pooled connections |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / `admin12345` | seeded when there are no users - change it |
| `SPA_NAME` | `Serenity Spa` | business name on payslips |
| `SPA_CORS_ORIGINS` | `http://localhost:4200` | comma-separated UI origins allowed to call the API |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | listen address |

## Database
Forward-only migrations in `migrations/NNN_name.sql`, applied automatically (once each, under an advisory lock) when the
API starts, and recorded in `<schema>.schema_migrations`. To change the schema add the next numbered file; never edit an applied one.

`migrations/001_init.sql`: `users`, `sessions`, `customers`, `therapists`, `services`, `appointments`.
- Money is `NUMERIC(12,2)`, percent `NUMERIC(5,2)`, ids are identity columns, flags are `BOOLEAN`, dates are `DATE`.
- `CHECK` constraints back the API rules (roles, statuses, price >= 0, percent 0-100, duration > 0).
- Usernames and service names are unique **ignoring case**; phone numbers are unique.
- **No double booking is enforced by the database** (`appointments_no_overlap`, an exclusion constraint on the therapist and
  the time range, via `btree_gist`): two simultaneous requests cannot both win, even across several API processes.
  Back-to-back is fine; cancelled and no-show appointments do not block a slot.
- `appointments.start_at` is the spa's local wall-clock time; `end_at` is generated from the duration; the price is the amount
  agreed at booking and is never changed by later price edits.
- Onboarding creates the customer and the first appointment in one transaction.

## Roles
- **admin**: everything, incl. therapists/services, reports, payroll and user management (`GET/POST /api/users`, `PUT /api/users/{id}` role/active, `POST /api/users/{id}/password` reset).
- **staff**: customers, onboarding, appointments.

## Rules
- Opaque bearer token (12 h, stored hashed, revoked on logout). Users can change their own password (`POST /api/auth/password`); other devices are signed out. Deactivating a user or resetting their password signs them out at once. You cannot deactivate/demote yourself or the last active admin.
- `PUT /api/appointments/{id}` reschedules or changes a booking, only while it is `booked` (completed ones feed payroll). Price: explicit value wins; a changed service takes its list price; otherwise the agreed price is kept. Re-opening a cancelled slot that has since been taken returns 409.
- **Payslip = fixed salary + incentive**; incentive = incentive % x price of the therapist's *completed* appointments in the month (half-open range, spa time zone).
- Payslip PDF: `GET /api/payroll/{therapist_id}/{YYYY-MM}/pdf`.

## Deploy on AWS (one EC2 box, Postgres in Docker)
`deploy/aws/bootstrap.sh` is EC2 user-data for Amazon Linux 2023: it installs Docker, starts Postgres 16 (data in the `spa-pgdata`
volume), builds this repo, generates random admin and database passwords into `/opt/spa/env` (`sudo cat /opt/spa/env`), and
fronts the API with Caddy on `https://<public-ip>.sslip.io` (free Let's Encrypt cert). To use an existing/managed Postgres
(RDS, or another server) pass `PG_DSN` to the bootstrap instead and no local database is started. Open ports 80 and 443 only;
SSH via SSM Session Manager.

- Redeploy: `sudo bash /opt/spa/src/deploy/aws/update.sh` (new migrations apply on start)
- Backup: `backup.sh` runs nightly at 02:30 from cron, keeping 14 compressed dumps in `/var/lib/spa/backups`
- Restore: `sudo bash deploy/aws/restore.sh <dump.sql.gz>` into a database without the `spa` schema. It reinstalls the
  `btree_gist` extension (a schema-only dump omits it - without it the no-double-booking constraint would be lost) and
  aborts and rolls back on any error.
