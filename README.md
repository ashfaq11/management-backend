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
- **admin**: everything, incl. therapists/services, reports, payroll, `POST /api/users`.
- **staff**: customers, onboarding, appointments.

## Rules
- Opaque bearer token (12 h, stored hashed, revoked on logout).
- Price is snapshotted on the appointment (override allowed); a therapist cannot be double-booked.
- **Payslip = fixed salary + incentive**; incentive = incentive % x price of the therapist's *completed* appointments in the month.
- `POST /api/onboarding` creates a customer and optionally their first appointment atomically.
- Payslip PDF: `GET /api/payroll/{therapist_id}/{YYYY-MM}/pdf`.
