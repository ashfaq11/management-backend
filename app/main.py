import datetime as dt
import os
import re
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from psycopg import errors as pgerr
from pydantic import BaseModel, Field

from . import payroll, security
from .db import Database, default_dsn

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SESSION_HOURS = 12
SLOT_TAKEN = "Therapist already has an appointment at that time"
Status = Literal["booked", "completed", "cancelled", "no_show"]


# ---- request models -------------------------------------------------------
class Login(BaseModel):
    username: str
    password: str


class CustomerIn(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=5)
    email: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[dt.date] = None
    address: Optional[str] = None
    notes: Optional[str] = None


class TherapistIn(BaseModel):
    name: str = Field(min_length=1)
    phone: Optional[str] = None
    fixed_salary: float = Field(ge=0)
    incentive_percent: float = Field(ge=0, le=100)
    is_active: bool = True
    joined_on: Optional[dt.date] = None


class ServiceIn(BaseModel):
    name: str = Field(min_length=1)
    price: float = Field(ge=0)
    duration_min: int = Field(default=60, gt=0)
    is_active: bool = True


class AppointmentIn(BaseModel):
    customer_id: int
    therapist_id: int
    service_id: int
    start_at: str  # local wall-clock "YYYY-MM-DDTHH:MM"
    notes: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)  # override list price
    duration_min: Optional[int] = Field(default=None, gt=0, le=720)  # override the service's length


class StatusIn(BaseModel):
    status: Status


class OnboardIn(CustomerIn):
    first_appointment: Optional[AppointmentIn] = None  # customer_id ignored


class UserUpdate(BaseModel):
    role: Literal["admin", "staff"]
    is_active: bool


class PasswordReset(BaseModel):
    password: str = Field(min_length=8)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserIn(BaseModel):
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    role: Literal["admin", "staff"] = "staff"


def create_app(dsn: Optional[str] = None, schema: Optional[str] = None) -> FastAPI:
    db = Database(dsn or default_dsn(), schema or os.environ.get("SPA_PG_SCHEMA", "spa"),
                  tz=os.environ.get("SPA_TZ", "Asia/Kolkata"),
                  pool_max=int(os.environ.get("PG_POOL_MAX", "10")))
    business = os.environ.get("SPA_NAME", "Serenity Spa")
    # first start only; atomic, so several processes starting together create exactly one admin
    db.run("""INSERT INTO users(username,password_hash,role)
              SELECT %s,%s,'admin' WHERE NOT EXISTS (SELECT 1 FROM users)""",
           (os.environ.get("ADMIN_USER", "admin"),
            security.hash_password(os.environ.get("ADMIN_PASSWORD", "admin12345"))))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        db.close()

    app = FastAPI(title="Spa Management API", lifespan=lifespan)
    app.state.db = db
    origins = [o.strip() for o in os.environ.get("SPA_CORS_ORIGINS", "http://localhost:4200").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["Authorization", "Content-Type"],
                       max_age=3600)  # browsers reuse the preflight answer, so a cross-origin UI pays it once an hour

    # ---- auth ---------------------------------------------------------
    def current_user(authorization: str = Header(default="")):
        tok = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        u = db.one("""SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id
                      WHERE s.token_hash=%s AND s.expires_at>now() AND u.is_active""",
                   (security.fingerprint(tok),)) if tok else None
        if not u:
            raise HTTPException(401, "Not authenticated")
        return u

    def admin(u=Depends(current_user)):
        if u["role"] != "admin":
            raise HTTPException(403, "Admin only")
        return u

    @app.post("/api/auth/login")
    def login(b: Login):
        u = db.one("SELECT * FROM users WHERE lower(username)=lower(%s) AND is_active", (b.username,))
        if not u or not security.verify_password(b.password, u["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        tok = security.new_token()
        db.run("DELETE FROM sessions WHERE expires_at<now()")  # housekeeping
        db.run("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+make_interval(hours=>%s))",
               (security.fingerprint(tok), u["id"], SESSION_HOURS))
        return {"token": tok, "username": u["username"], "role": u["role"]}

    @app.post("/api/auth/logout", status_code=204)
    def logout(authorization: str = Header(default=""), u=Depends(current_user)):
        db.run("DELETE FROM sessions WHERE token_hash=%s", (security.fingerprint(authorization[7:]),))

    @app.get("/api/auth/me")
    def me(u=Depends(current_user)):
        return u

    @app.post("/api/auth/password", status_code=204)
    def change_password(b: PasswordChange, authorization: str = Header(default=""), u=Depends(current_user)):
        row = db.one("SELECT password_hash FROM users WHERE id=%s", (u["id"],))
        if not security.verify_password(b.current_password, row["password_hash"]):
            raise HTTPException(400, "Current password is wrong")
        with db.tx() as t:
            t.run("UPDATE users SET password_hash=%s WHERE id=%s", (security.hash_password(b.new_password), u["id"]))
            # sign out every other device, keep this one
            t.run("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s",
                  (u["id"], security.fingerprint(authorization[7:])))

    # ---- users (admin) --------------------------------------------------
    @app.get("/api/users")
    def users(_=Depends(admin)):
        return db.all("SELECT id,username,role,is_active FROM users ORDER BY lower(username)")

    @app.post("/api/users", status_code=201)
    def add_user(b: UserIn, _=Depends(admin)):
        try:
            return {"id": db.insert("INSERT INTO users(username,password_hash,role) VALUES(%s,%s,%s)",
                                    (b.username.strip(), security.hash_password(b.password), b.role))}
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Username taken")

    @app.put("/api/users/{uid}")
    def edit_user(uid: int, b: UserUpdate, me=Depends(admin)):
        with db.tx() as t:
            # lock every active admin row so two admins cannot demote each other at the same moment
            admins = {r["id"] for r in t.all("SELECT id FROM users WHERE role='admin' AND is_active ORDER BY id FOR UPDATE")}
            if not t.one("SELECT 1 FROM users WHERE id=%s FOR UPDATE", (uid,)):
                raise HTTPException(404, "User not found")
            if uid == me["id"] and (not b.is_active or b.role != "admin"):
                raise HTTPException(400, "You cannot deactivate or demote your own account")
            if admins <= {uid} and (not b.is_active or b.role != "admin"):
                raise HTTPException(400, "At least one active admin is required")
            t.run("UPDATE users SET role=%s, is_active=%s WHERE id=%s", (b.role, b.is_active, uid))
            if not b.is_active:
                t.run("DELETE FROM sessions WHERE user_id=%s", (uid,))
        return {"id": uid, "role": b.role, "is_active": b.is_active}

    @app.post("/api/users/{uid}/password", status_code=204)
    def reset_password(uid: int, b: PasswordReset, _=Depends(admin)):
        with db.tx() as t:
            if not t.run("UPDATE users SET password_hash=%s WHERE id=%s", (security.hash_password(b.password), uid)):
                raise HTTPException(404, "User not found")
            t.run("DELETE FROM sessions WHERE user_id=%s", (uid,))

    # ---- customers ----------------------------------------------------
    PHONE_TAKEN = "A customer with this phone already exists"

    def insert_customer(t, b: CustomerIn) -> int:
        try:
            return t.insert("""INSERT INTO customers(name,phone,email,gender,dob,address,notes)
                               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                            (b.name.strip(), b.phone.strip(), b.email, b.gender, b.dob, b.address, b.notes))
        except pgerr.UniqueViolation:
            raise HTTPException(409, PHONE_TAKEN)

    @app.get("/api/customers")
    def customers(q: str = "", _=Depends(current_user)):
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return db.all("SELECT * FROM customers WHERE name ILIKE %s OR phone ILIKE %s ORDER BY lower(name) LIMIT 200",
                      (like, like))

    @app.post("/api/customers", status_code=201)
    def add_customer(b: CustomerIn, _=Depends(current_user)):
        with db.tx() as t:
            return {"id": insert_customer(t, b)}

    @app.get("/api/customers/{cid}")
    def customer(cid: int, _=Depends(current_user)):
        c = db.one("SELECT * FROM customers WHERE id=%s", (cid,))
        if not c:
            raise HTTPException(404, "Customer not found")
        c["appointments"] = db.all(
            """SELECT a.*, s.name AS service, t.name AS therapist FROM appointments a
               JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id
               WHERE a.customer_id=%s ORDER BY a.start_at DESC""", (cid,))
        return c

    @app.put("/api/customers/{cid}")
    def edit_customer(cid: int, b: CustomerIn, _=Depends(current_user)):
        try:
            n = db.run("""UPDATE customers SET name=%s,phone=%s,email=%s,gender=%s,dob=%s,address=%s,notes=%s,
                          updated_at=now() WHERE id=%s""",
                       (b.name.strip(), b.phone.strip(), b.email, b.gender, b.dob, b.address, b.notes, cid))
        except pgerr.UniqueViolation:
            raise HTTPException(409, PHONE_TAKEN)
        if not n:
            raise HTTPException(404, "Customer not found")
        return {"id": cid}

    # ---- therapists & services (admin-managed) -------------------------
    @app.get("/api/therapists")
    def therapists(_=Depends(current_user)):
        return db.all("SELECT * FROM therapists ORDER BY lower(name)")

    @app.post("/api/therapists", status_code=201)
    def add_therapist(b: TherapistIn, _=Depends(admin)):
        return {"id": db.insert("""INSERT INTO therapists(name,phone,fixed_salary,incentive_percent,is_active,joined_on)
                                   VALUES(%s,%s,%s,%s,%s,%s)""",
                                (b.name.strip(), b.phone, b.fixed_salary, b.incentive_percent, b.is_active, b.joined_on))}

    @app.put("/api/therapists/{tid}")
    def edit_therapist(tid: int, b: TherapistIn, _=Depends(admin)):
        if not db.run("""UPDATE therapists SET name=%s,phone=%s,fixed_salary=%s,incentive_percent=%s,is_active=%s,
                         joined_on=%s WHERE id=%s""",
                      (b.name.strip(), b.phone, b.fixed_salary, b.incentive_percent, b.is_active, b.joined_on, tid)):
            raise HTTPException(404, "Therapist not found")
        return {"id": tid}

    @app.get("/api/services")
    def services(_=Depends(current_user)):
        return db.all("SELECT * FROM services ORDER BY lower(name)")

    @app.post("/api/services", status_code=201)
    def add_service(b: ServiceIn, _=Depends(admin)):
        try:
            return {"id": db.insert("INSERT INTO services(name,price,duration_min,is_active) VALUES(%s,%s,%s,%s)",
                                    (b.name.strip(), b.price, b.duration_min, b.is_active))}
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Service already exists")

    @app.put("/api/services/{sid}")
    def edit_service(sid: int, b: ServiceIn, _=Depends(admin)):
        try:
            n = db.run("UPDATE services SET name=%s,price=%s,duration_min=%s,is_active=%s WHERE id=%s",
                       (b.name.strip(), b.price, b.duration_min, b.is_active, sid))
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Service already exists")
        if not n:
            raise HTTPException(404, "Service not found")
        return {"id": sid}

    # ---- appointments ---------------------------------------------------
    # Double-booking is refused by the database itself (appointments_no_overlap), so it holds even
    # with several API processes; we only translate that error into a 409.
    def resolve_slot(t, b: AppointmentIn):
        """Validate a booking request; returns (therapist, service, start). Shared by create and edit."""
        try:
            start = dt.datetime.fromisoformat(b.start_at).replace(tzinfo=None, second=0, microsecond=0)
        except ValueError:
            raise HTTPException(422, "start_at must be YYYY-MM-DDTHH:MM")
        if not t.one("SELECT 1 FROM customers WHERE id=%s", (b.customer_id,)):
            raise HTTPException(404, "Customer not found")
        th = t.one("SELECT * FROM therapists WHERE id=%s AND is_active", (b.therapist_id,))
        sv = t.one("SELECT * FROM services WHERE id=%s AND is_active", (b.service_id,))
        if not th or not sv:
            raise HTTPException(404, "Therapist or service not found (or inactive)")
        return th, sv, start

    def insert_appointment(t, b: AppointmentIn) -> int:
        th, sv, start = resolve_slot(t, b)
        try:
            return t.insert("""INSERT INTO appointments(customer_id,therapist_id,service_id,start_at,duration_min,price,notes)
                               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                            (b.customer_id, th["id"], sv["id"], start, b.duration_min or sv["duration_min"],
                             sv["price"] if b.price is None else b.price, b.notes))
        except pgerr.ExclusionViolation:
            raise HTTPException(409, SLOT_TAKEN)

    @app.get("/api/appointments")
    def appointments(date: Optional[dt.date] = None, therapist_id: Optional[int] = None,
                     status: Optional[Status] = None, _=Depends(current_user)):
        sql = """SELECT a.*, c.name AS customer, c.phone, s.name AS service, t.name AS therapist
                 FROM appointments a JOIN customers c ON c.id=a.customer_id
                 JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id WHERE true"""
        args = []
        if date:
            sql += " AND a.start_at >= %s AND a.start_at < %s"
            args += [date, date + dt.timedelta(days=1)]
        if therapist_id:
            sql += " AND a.therapist_id=%s"; args.append(therapist_id)
        if status:
            sql += " AND a.status=%s"; args.append(status)
        return db.all(sql + " ORDER BY a.start_at LIMIT 500", args)

    @app.post("/api/appointments", status_code=201)
    def add_appointment(b: AppointmentIn, _=Depends(current_user)):
        with db.tx() as t:
            return {"id": insert_appointment(t, b)}

    @app.put("/api/appointments/{aid}")
    def edit_appointment(aid: int, b: AppointmentIn, _=Depends(current_user)):
        """Reschedule / change an appointment. Only while still 'booked': completed ones feed payroll."""
        with db.tx() as t:
            a = t.one("SELECT * FROM appointments WHERE id=%s FOR UPDATE", (aid,))
            if not a:
                raise HTTPException(404, "Appointment not found")
            if a["status"] != "booked":
                raise HTTPException(409, "Only booked appointments can be edited; change the status first")
            th, sv, start = resolve_slot(t, b)
            # explicit price wins; a different service takes its list price; otherwise keep what was agreed
            price = b.price if b.price is not None else (sv["price"] if sv["id"] != a["service_id"] else a["price"])
            # the same rule for the length
            minutes = b.duration_min or (sv["duration_min"] if sv["id"] != a["service_id"] else a["duration_min"])
            try:
                t.run("""UPDATE appointments SET customer_id=%s,therapist_id=%s,service_id=%s,start_at=%s,
                         duration_min=%s,price=%s,notes=%s,updated_at=now() WHERE id=%s""",
                      (b.customer_id, th["id"], sv["id"], start, minutes, price, b.notes, aid))
            except pgerr.ExclusionViolation:
                raise HTTPException(409, SLOT_TAKEN)
        return {"id": aid}

    @app.patch("/api/appointments/{aid}/status")
    def set_status(aid: int, b: StatusIn, _=Depends(current_user)):
        try:  # re-opening a cancelled slot someone else has since taken is refused by the constraint
            n = db.run("UPDATE appointments SET status=%s, updated_at=now() WHERE id=%s", (b.status, aid))
        except pgerr.ExclusionViolation:
            raise HTTPException(409, SLOT_TAKEN)
        if not n:
            raise HTTPException(404, "Appointment not found")
        return {"id": aid, "status": b.status}

    # ---- onboarding: customer (+ optional first booking) in ONE transaction ----
    @app.post("/api/onboarding", status_code=201)
    def onboard(b: OnboardIn, _=Depends(current_user)):
        with db.tx() as t:  # a clashing first appointment rolls the new customer back too
            cid = insert_customer(t, b)
            aid = None
            if b.first_appointment:
                aid = insert_appointment(t, b.first_appointment.model_copy(update={"customer_id": cid}))
        return {"customer_id": cid, "appointment_id": aid}

    # ---- reports & payroll (admin) --------------------------------------
    def check_month(month: str):
        if not MONTH.match(month):
            raise HTTPException(422, "month must be YYYY-MM")

    @app.get("/api/reports/summary")
    def summary(month: str = Query(...), therapist_id: int | None = Query(None), _=Depends(admin)):
        """The month's figures. Revenue counts COMPLETED appointments only, at the price agreed at booking.
        `lines` is every completed service (who, what, for whom, how much); `by_therapist` gives each
        therapist's count and amount with a per-service breakdown. `therapist_id` narrows everything but
        new_customers to one therapist."""
        check_month(month)
        lo, hi = payroll.month_bounds(month)
        who, args = ("", (lo, hi)) if therapist_id is None else (" AND a.therapist_id = %s", (lo, hi, therapist_id))
        by_status = db.all(f"""SELECT a.status, COUNT(*) AS count, COALESCE(SUM(a.price),0) AS amount
                               FROM appointments a WHERE a.start_at >= %s AND a.start_at < %s{who}
                               GROUP BY a.status ORDER BY a.status""", args)
        lines = db.all(f"""SELECT a.id, a.start_at, a.price, c.name AS customer, s.name AS service,
                                  t.id AS therapist_id, t.name AS therapist
                           FROM appointments a JOIN customers c ON c.id=a.customer_id
                           JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id
                           WHERE a.status='completed' AND a.start_at >= %s AND a.start_at < %s{who}
                           ORDER BY a.start_at, a.id""", args)
        by_service: dict[str, dict] = {}
        by_therapist: dict[int, dict] = {}
        for r in lines:
            price = payroll.money(r["price"])
            sv = by_service.setdefault(r["service"], {"service": r["service"], "count": 0, "revenue": 0})
            sv["count"] += 1
            sv["revenue"] = payroll.money(sv["revenue"]) + price
            th = by_therapist.setdefault(r["therapist_id"], {"therapist_id": r["therapist_id"],
                                                              "therapist": r["therapist"], "count": 0,
                                                              "revenue": 0, "services": {}})
            th["count"] += 1
            th["revenue"] = payroll.money(th["revenue"]) + price
            ts = th["services"].setdefault(r["service"], {"service": r["service"], "count": 0, "revenue": 0})
            ts["count"] += 1
            ts["revenue"] = payroll.money(ts["revenue"]) + price

        def out(d: dict) -> dict:
            return {**d, "revenue": float(d["revenue"])}

        therapists = []
        for th in sorted(by_therapist.values(), key=lambda x: (-x["revenue"], x["therapist"].lower())):
            services = sorted((out(v) for v in th["services"].values()), key=lambda x: -x["revenue"])
            therapists.append({**out(th), "services": services})
        # created_at is a timestamptz and the session zone is the spa's, so these are local calendar months
        new_c = db.one("SELECT COUNT(*) AS n FROM customers WHERE created_at >= %s AND created_at < %s", (lo, hi))["n"]
        rev = sum((payroll.money(r["price"]) for r in lines), payroll.money(0))
        return {"month": month, "therapist_id": therapist_id, "revenue": float(rev),
                "services_count": len(lines), "new_customers": new_c, "by_status": by_status,
                "by_service": sorted((out(v) for v in by_service.values()), key=lambda x: -x["revenue"]),
                "by_therapist": therapists, "lines": lines}

    @app.get("/api/reports/summary/pdf")
    def summary_pdf(month: str = Query(...), therapist_id: int | None = Query(None), user=Depends(admin)):
        """The same report as /api/reports/summary (same filters), as a PDF to download or print."""
        r = summary(month, therapist_id, user)
        label = "all-therapists"
        if therapist_id is not None:
            t = db.one("SELECT name FROM therapists WHERE id=%s", (therapist_id,))
            if not t:
                raise HTTPException(404, "Therapist not found")
            r["therapist"] = t["name"]
            label = re.sub(r"[^A-Za-z0-9_-]", "_", t["name"])
        return Response(payroll.report_pdf(r, business), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="report_{month}_{label}.pdf"'})

    @app.get("/api/reports/incentives")
    def incentives(month: str = Query(...), _=Depends(admin)):
        check_month(month)
        rows = [payroll.compute_payslip(db, t, month) for t in
                db.all("SELECT * FROM therapists WHERE is_active ORDER BY lower(name)")]
        for r in rows:
            r.pop("lines")
        return {"month": month, "therapists": rows,
                "total_incentive": round(sum(r["incentive"] for r in rows), 2),
                "total_payroll": round(sum(r["net_pay"] for r in rows), 2)}

    def payslip_for(tid: int, month: str):
        check_month(month)
        t = db.one("SELECT * FROM therapists WHERE id=%s", (tid,))
        if not t:
            raise HTTPException(404, "Therapist not found")
        return payroll.compute_payslip(db, t, month)

    @app.get("/api/payroll/{tid}/{month}")
    def payslip(tid: int, month: str, _=Depends(admin)):
        return payslip_for(tid, month)

    @app.get("/api/payroll/{tid}/{month}/pdf")
    def payslip_pdf(tid: int, month: str, _=Depends(admin)):
        p = payslip_for(tid, month)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", p["therapist"])
        return Response(payroll.payslip_pdf(p, business), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="payslip_{name}_{month}.pdf"'})

    @app.get("/api/health")
    def health():
        db.one("SELECT 1")  # fails (500) if the database is unreachable, so the load balancer sees it
        return {"ok": True}

    return app
