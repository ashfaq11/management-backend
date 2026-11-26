import csv
import io
import datetime as dt
from decimal import Decimal
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
    preferred_payment_method: Optional[Literal["cash", "upi", "card", "bank_transfer", "other"]] = None


class PaymentIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    method: Literal["cash", "upi", "card", "bank_transfer", "other"]
    reference: Optional[str] = Field(default=None, max_length=120)
    note: Optional[str] = Field(default=None, max_length=500)


class RefundIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: Optional[str] = Field(default=None, max_length=500)


class TherapistIn(BaseModel):
    name: str = Field(min_length=1)
    phone: Optional[str] = None
    fixed_salary: float = Field(ge=0)
    incentive_percent: float = Field(ge=0, le=100)
    is_active: bool = True
    joined_on: Optional[dt.date] = None
    email: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[dt.date] = None
    address: Optional[str] = None
    specialities: Optional[str] = None
    experience_years: Optional[float] = None
    notes: Optional[str] = None
    emergency_name: Optional[str] = None
    emergency_phone: Optional[str] = None
    id_proof_type: Optional[str] = None
    id_proof_number: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    upi_id: Optional[str] = None


THERAPIST_DETAILS = ("email", "gender", "dob", "address", "specialities", "experience_years", "notes", "emergency_name", "emergency_phone", "id_proof_type", "id_proof_number", "bank_name", "bank_account_name", "bank_account_number", "bank_ifsc", "upi_id")


def details(b) -> tuple:
    return tuple((v.strip() or None) if isinstance(v, str) else v for v in (getattr(b, n) for n in THERAPIST_DETAILS))


class ShiftIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    start_time: dt.time
    end_time: dt.time
    is_active: bool = True


class ShiftAssignmentIn(BaseModel):
    therapist_id: int
    shift_id: int
    day_of_week: int = Field(ge=0, le=6)  # Monday=0 through Sunday=6


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
    plan_type: Optional[Literal["hours", "rupees"]] = None


class CustomerPlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    plan_type: Literal["hours", "rupees"]
    amount: float = Field(gt=0, le=1_000_000)
    expires_on: Optional[dt.date] = None
    notes: Optional[str] = Field(default=None, max_length=500)
    price_paid: Optional[float] = Field(default=None, ge=0, le=10_000_000)  # INR; rupee plans default to amount


class CustomerPlanUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_on: Optional[dt.date] = None
    notes: Optional[str] = Field(default=None, max_length=500)


def paid_for(plan_type: str, amount: float, price_paid: Optional[float]) -> float:
    """Money received for a plan or top-up: given, or the amount itself for a rupee plan."""
    if price_paid is not None:
        return price_paid
    if plan_type == "rupees":
        return amount
    return 0.0  # an hours plan given free or with an unknown price


class PlanTopUp(BaseModel):
    amount: float = Field(gt=0, le=1_000_000)
    price_paid: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    note: Optional[str] = Field(default=None, max_length=500)


class ExpenseIn(BaseModel):
    on_date: dt.date
    category: str = Field(default="Other", min_length=1, max_length=60)
    amount: float = Field(gt=0, le=100_000_000)
    note: Optional[str] = Field(default=None, max_length=500)


class RecurringExpenseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="Other", min_length=1, max_length=60)
    amount: float = Field(gt=0, le=100_000_000)
    day_of_month: int = Field(default=1, ge=1, le=28)
    starts_on: dt.date
    ends_on: Optional[dt.date] = None
    notes: Optional[str] = Field(default=None, max_length=500)


def expenses_in(db, spa: int, lo: dt.date, hi: dt.date) -> list[dict]:
    """Every cost falling in [lo, hi): one-off expenses on their date, and each regular expense once per month on
    its due day while it is active."""
    rows = [{**r, "kind": "one-off"} for r in db.all(
        """SELECT id, on_date, category, note AS name, amount FROM expenses
           WHERE spa_id=%s AND on_date >= %s AND on_date < %s""", (spa, lo, hi))]
    for r in db.all("SELECT * FROM recurring_expenses WHERE spa_id=%s AND starts_on < %s AND (ends_on IS NULL OR ends_on >= %s)",
                    (spa, hi, lo)):
        start = dt.date.fromisoformat(str(r["starts_on"])[:10])
        end = dt.date.fromisoformat(str(r["ends_on"])[:10]) if r["ends_on"] else None
        y, m = lo.year, lo.month
        while dt.date(y, m, 1) < hi:
            due = dt.date(y, m, r["day_of_month"])
            if lo <= due < hi and start <= due and (end is None or due <= end):
                rows.append({"id": r["id"], "on_date": due.isoformat(), "category": r["category"], "name": r["name"],
                             "amount": r["amount"], "kind": "regular"})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(rows, key=lambda x: (str(x["on_date"]), x["kind"], x["id"]))


class OnboardIn(CustomerIn):
    first_appointment: Optional[AppointmentIn] = None  # customer_id ignored
    plan: Optional[CustomerPlanIn] = None


class BonusIn(BaseModel):
    therapist_id: int
    on_date: dt.date
    amount: float = Field(gt=0, le=1_000_000)
    note: Optional[str] = Field(default=None, max_length=200)
    appointment_id: Optional[int] = None


class UserUpdate(BaseModel):
    role: Literal["admin", "staff"]
    is_active: bool


class SpaIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SpaCreate(SpaIn):
    """A new spa and its first admin, created together."""
    admin_username: str = Field(min_length=3)
    admin_password: str = Field(min_length=8)


class SpaUpdate(SpaIn):
    is_active: bool = True


class PasswordReset(BaseModel):
    password: str = Field(min_length=8)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserIn(BaseModel):
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    role: Literal["superadmin", "admin", "staff"] = "staff"
    spa_id: Optional[int] = None  # superadmin only: which spa an admin/staff login belongs to


def create_app(dsn: Optional[str] = None, schema: Optional[str] = None) -> FastAPI:
    db = Database(dsn or default_dsn(), schema or os.environ.get("SPA_PG_SCHEMA", "spa"),
                  tz=os.environ.get("SPA_TZ", "Asia/Kolkata"),
                  pool_max=int(os.environ.get("PG_POOL_MAX", "10")))
    # The first spa (created by migration 004) takes SPA_NAME until a superadmin renames it.
    db.run("UPDATE spas SET name=%s WHERE id=(SELECT min(id) FROM spas) AND name='My Spa'",
           (os.environ.get("SPA_NAME", "Serenity Spa"),))
    # first start only; atomic, so several processes starting together create exactly one admin
    db.run("""INSERT INTO users(username,password_hash,role,spa_id)
              SELECT %s,%s,'admin',(SELECT min(id) FROM spas) WHERE NOT EXISTS (SELECT 1 FROM users)""",
           (os.environ.get("ADMIN_USER", "admin"),
            security.hash_password(os.environ.get("ADMIN_PASSWORD", "admin12345"))))
    # and one superadmin, who creates spas and their admins
    db.run("""INSERT INTO users(username,password_hash,role,spa_id)
              SELECT %s,%s,'superadmin',NULL WHERE NOT EXISTS (SELECT 1 FROM users WHERE role='superadmin')
              AND NOT EXISTS (SELECT 1 FROM users WHERE lower(username)=lower(%s))""",
           (os.environ.get("SUPERADMIN_USER", "superadmin"),
            security.hash_password(os.environ.get("SUPERADMIN_PASSWORD", "superadmin123")),
            os.environ.get("SUPERADMIN_USER", "superadmin")))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        db.close()

    app = FastAPI(title="Spa Management API", lifespan=lifespan)
    app.state.db = db
    origins = [o.strip() for o in os.environ.get("SPA_CORS_ORIGINS", "http://localhost:4200").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["Authorization", "Content-Type", "X-Spa-Id"],
                       expose_headers=["Content-Disposition"],
                       max_age=3600)  # browsers reuse the preflight answer, so a cross-origin UI pays it once an hour

    # ---- auth ---------------------------------------------------------
    def current_user(authorization: str = Header(default=""), x_spa_id: Optional[int] = Header(default=None)):
        """The caller, with `spa` = the spa they are working in: their own for admin/staff; for a superadmin the
        one chosen with the X-Spa-Id header (None when none is chosen). A deactivated spa signs its users out."""
        tok = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        u = db.one("""SELECT u.id,u.username,u.role,u.spa_id,p.name AS spa_name FROM sessions s
                      JOIN users u ON u.id=s.user_id LEFT JOIN spas p ON p.id=u.spa_id
                      WHERE s.token_hash=%s AND s.expires_at>now() AND u.is_active
                        AND (u.spa_id IS NULL OR p.is_active)""",
                   (security.fingerprint(tok),)) if tok else None
        if not u:
            raise HTTPException(401, "Not authenticated")
        u["spa"] = u["spa_id"]
        if u["role"] == "superadmin" and x_spa_id is not None:
            p = db.one("SELECT id,name FROM spas WHERE id=%s", (x_spa_id,))
            if not p:
                raise HTTPException(404, "Spa not found")
            u["spa"], u["spa_name"] = p["id"], p["name"]
        return u

    def member(u=Depends(current_user)):
        """Anyone working inside one spa (a superadmin must pick one first)."""
        if u["spa"] is None:
            raise HTTPException(400, "Choose a spa first")
        return u

    def admin(u=Depends(member)):
        if u["role"] not in ("admin", "superadmin"):
            raise HTTPException(403, "Admin only")
        return u

    def superadmin(u=Depends(current_user)):
        if u["role"] != "superadmin":
            raise HTTPException(403, "Superadmin only")
        return u

    @app.post("/api/auth/login")
    def login(b: Login):
        u = db.one("""SELECT u.* FROM users u LEFT JOIN spas p ON p.id=u.spa_id
                      WHERE lower(u.username)=lower(%s) AND u.is_active AND (u.spa_id IS NULL OR p.is_active)""",
                   (b.username,))
        if not u or not security.verify_password(b.password, u["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        tok = security.new_token()
        db.run("DELETE FROM sessions WHERE expires_at<now()")  # housekeeping
        db.run("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+make_interval(hours=>%s))",
               (security.fingerprint(tok), u["id"], SESSION_HOURS))
        spa = db.one("SELECT name FROM spas WHERE id=%s", (u["spa_id"],)) if u["spa_id"] else None
        return {"token": tok, "username": u["username"], "role": u["role"], "spa_id": u["spa_id"],
                "spa_name": spa["name"] if spa else None}

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

    # ---- spas (superadmin) ------------------------------------------------
    @app.get("/api/spas")
    def spas(u=Depends(current_user)):
        """Superadmin: every spa with its people and this month's revenue. Others: just their own spa."""
        sql = """SELECT p.id, p.name, p.is_active, p.created_at,
                        (SELECT COUNT(*) FROM users x WHERE x.spa_id=p.id AND x.role='admin' AND x.is_active) AS admins,
                        (SELECT COUNT(*) FROM users x WHERE x.spa_id=p.id AND x.role='staff' AND x.is_active) AS staff,
                        (SELECT COUNT(*) FROM customers c WHERE c.spa_id=p.id) AS customers,
                        (SELECT COALESCE(SUM(a.price),0) FROM appointments a WHERE a.spa_id=p.id AND a.status='completed'
                           AND a.start_at >= date_trunc('month', localtimestamp)) AS revenue_this_month
                 FROM spas p"""
        if u["role"] != "superadmin":
            return db.all(sql + " WHERE p.id=%s", (u["spa_id"],))
        return db.all(sql + " ORDER BY lower(p.name)")

    @app.post("/api/spas", status_code=201)
    def add_spa(b: SpaCreate, _=Depends(superadmin)):
        with db.tx() as t:
            try:
                sid = t.insert("INSERT INTO spas(name) VALUES(%s)", (b.name.strip(),))
            except pgerr.UniqueViolation:
                raise HTTPException(409, "A spa with this name already exists")
            try:
                uid = t.insert("INSERT INTO users(username,password_hash,role,spa_id) VALUES(%s,%s,'admin',%s)",
                               (b.admin_username.strip(), security.hash_password(b.admin_password), sid))
            except pgerr.UniqueViolation:
                raise HTTPException(409, "Username taken")
        return {"id": sid, "admin_id": uid}

    @app.put("/api/spas/{sid}")
    def edit_spa(sid: int, b: SpaUpdate, _=Depends(superadmin)):
        with db.tx() as t:
            try:
                if not t.run("UPDATE spas SET name=%s, is_active=%s WHERE id=%s", (b.name.strip(), b.is_active, sid)):
                    raise HTTPException(404, "Spa not found")
            except pgerr.UniqueViolation:
                raise HTTPException(409, "A spa with this name already exists")
            if not b.is_active:
                t.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE spa_id=%s)", (sid,))
        return {"id": sid}

    # ---- users: superadmin manages everyone; an admin manages the staff of their own spa ----
    def user_in_scope(t, me, uid, lock=False):
        row = t.one("SELECT id,role,spa_id,is_active FROM users WHERE id=%s" + (" FOR UPDATE" if lock else ""), (uid,))
        # an admin sees only their own spa's logins, and changes only staff (and themselves for passwords)
        if not row or (me["role"] != "superadmin" and row["spa_id"] != me["spa_id"]):
            raise HTTPException(404, "User not found")
        return row

    @app.get("/api/users")
    def users(me=Depends(current_user)):
        if me["role"] == "staff":
            raise HTTPException(403, "Admin only")
        sql = """SELECT u.id,u.username,u.role,u.is_active,u.spa_id,p.name AS spa FROM users u
                 LEFT JOIN spas p ON p.id=u.spa_id"""
        if me["role"] == "superadmin":
            return db.all(sql + " ORDER BY p.name NULLS FIRST, lower(u.username)")
        return db.all(sql + " WHERE u.spa_id=%s ORDER BY lower(u.username)", (me["spa_id"],))

    @app.post("/api/users", status_code=201)
    def add_user(b: UserIn, me=Depends(current_user)):
        if me["role"] == "staff":
            raise HTTPException(403, "Admin only")
        if me["role"] == "admin":
            if b.role != "staff":
                raise HTTPException(403, "An admin can create staff logins only; ask the superadmin for an admin")
            spa = me["spa_id"]
        else:
            spa = None if b.role == "superadmin" else (b.spa_id or me["spa"])
            if b.role != "superadmin" and (not spa or not db.one("SELECT 1 FROM spas WHERE id=%s", (spa,))):
                raise HTTPException(422, "Choose the spa for this login")
        try:
            return {"id": db.insert("INSERT INTO users(username,password_hash,role,spa_id) VALUES(%s,%s,%s,%s)",
                                    (b.username.strip(), security.hash_password(b.password), b.role, spa))}
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Username taken")

    @app.put("/api/users/{uid}")
    def edit_user(uid: int, b: UserUpdate, me=Depends(current_user)):
        if me["role"] == "staff":
            raise HTTPException(403, "Admin only")
        with db.tx() as t:
            row = user_in_scope(t, me, uid, lock=True)
            if uid == me["id"]:
                raise HTTPException(400, "You cannot deactivate or demote your own account")
            if row["role"] == "superadmin":
                raise HTTPException(400, "A superadmin cannot be changed here")
            if me["role"] == "admin" and (row["role"] != "staff" or b.role != "staff"):
                raise HTTPException(403, "An admin can change staff logins only")
            t.run("UPDATE users SET role=%s, is_active=%s WHERE id=%s", (b.role, b.is_active, uid))
            if not b.is_active:
                t.run("DELETE FROM sessions WHERE user_id=%s", (uid,))
        return {"id": uid, "role": b.role, "is_active": b.is_active}

    @app.post("/api/users/{uid}/password", status_code=204)
    def reset_password(uid: int, b: PasswordReset, me=Depends(current_user)):
        if me["role"] == "staff":
            raise HTTPException(403, "Admin only")
        with db.tx() as t:
            row = user_in_scope(t, me, uid)
            if me["role"] == "admin" and row["role"] != "staff" and uid != me["id"]:
                raise HTTPException(403, "An admin can reset staff passwords only")
            t.run("UPDATE users SET password_hash=%s WHERE id=%s", (security.hash_password(b.password), uid))
            t.run("DELETE FROM sessions WHERE user_id=%s", (uid,))

    # ---- customers ----------------------------------------------------
    PHONE_TAKEN = "A customer with this phone already exists"

    def insert_customer(t, b: CustomerIn, spa: int) -> int:
        try:
            return t.insert("""INSERT INTO customers(name,phone,email,gender,dob,address,notes,preferred_payment_method,spa_id)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (b.name.strip(), b.phone.strip(), b.email, b.gender, b.dob, b.address, b.notes,
                             b.preferred_payment_method, spa))
        except pgerr.UniqueViolation:
            raise HTTPException(409, PHONE_TAKEN)

    @app.get("/api/customers")
    def customers(q: str = "", u=Depends(member)):
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return db.all("""SELECT * FROM customers WHERE spa_id=%s AND (name ILIKE %s OR phone ILIKE %s)
                         ORDER BY lower(name) LIMIT 200""", (u["spa"], like, like))

    @app.post("/api/customers", status_code=201)
    def add_customer(b: CustomerIn, u=Depends(member)):
        with db.tx() as t:
            return {"id": insert_customer(t, b, u["spa"])}

    @app.get("/api/customers/page")
    def customer_page(q: str = "", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                      u=Depends(member)):
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        total = db.one("""SELECT COUNT(*) AS n FROM customers
                          WHERE spa_id=%s AND (name ILIKE %s OR phone ILIKE %s)""",
                       (u["spa"], like, like))["n"]
        items = db.all("""SELECT * FROM customers WHERE spa_id=%s AND (name ILIKE %s OR phone ILIKE %s)
                          ORDER BY lower(name), id LIMIT %s OFFSET %s""",
                       (u["spa"], like, like, page_size, (page - 1) * page_size))
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/customers/{cid}")
    def customer(cid: int, u=Depends(member)):
        c = db.one("SELECT * FROM customers WHERE id=%s AND spa_id=%s", (cid, u["spa"]))
        if not c:
            raise HTTPException(404, "Customer not found")
        c["appointments"] = db.all(
            """SELECT a.*, s.name AS service, t.name AS therapist,
                      p.name AS plan_name, r.amount AS plan_deducted, r.unit AS plan_unit,
                      r.created_at AS plan_deducted_at, r.reversed_at AS plan_reversed_at,
                      COALESCE(pay.paid_amount,0) AS paid_amount,
                      CASE WHEN r.reversed_at IS NULL AND r.id IS NOT NULL THEN 0
                           ELSE GREATEST(a.price-COALESCE(pay.paid_amount,0),0) END AS balance_due,
                      CASE WHEN r.reversed_at IS NULL AND r.id IS NOT NULL THEN 'prepaid'
                           WHEN a.price=0 OR COALESCE(pay.paid_amount,0) >= a.price THEN 'paid'
                           WHEN COALESCE(pay.paid_amount,0) > 0 THEN 'partial'
                           ELSE 'pending' END AS payment_status
               FROM appointments a
               JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id
               LEFT JOIN LATERAL (
                   SELECT * FROM plan_redemptions x
                   WHERE x.appointment_id=a.id AND x.spa_id=a.spa_id
                   ORDER BY x.created_at DESC, x.id DESC LIMIT 1
               ) r ON TRUE
               LEFT JOIN customer_plans p ON p.id=r.plan_id
               LEFT JOIN LATERAL (
                   SELECT SUM(pm.amount-COALESCE((SELECT SUM(rf.amount) FROM payment_refunds rf
                                                  WHERE rf.payment_id=pm.id AND rf.spa_id=pm.spa_id),0)) AS paid_amount
                   FROM payments pm WHERE pm.appointment_id=a.id AND pm.spa_id=a.spa_id
               ) pay ON TRUE
               WHERE a.customer_id=%s AND a.spa_id=%s ORDER BY a.start_at DESC""", (cid, u["spa"]))
        c["plans"] = db.all(
            """SELECT p.*, COALESCE((SELECT SUM(r.amount) FROM plan_redemptions r
                                      WHERE r.plan_id=p.id AND r.reversed_at IS NULL),0) AS used_amount
               FROM customer_plans p WHERE p.customer_id=%s AND p.spa_id=%s
               ORDER BY p.created_at DESC, p.id DESC""", (cid, u["spa"]))
        c["payments"] = db.all(
            """SELECT p.id, p.appointment_id, p.amount, p.method, p.reference, p.note, p.received_at,
                      COALESCE(r.amount,0) AS refunded_amount, p.amount-COALESCE(r.amount,0) AS net_amount
               FROM payments p LEFT JOIN LATERAL (
                   SELECT SUM(amount) AS amount FROM payment_refunds WHERE payment_id=p.id AND spa_id=p.spa_id
               ) r ON TRUE
               WHERE p.customer_id=%s AND p.spa_id=%s ORDER BY p.received_at DESC, p.id DESC""", (cid, u["spa"]))
        return c

    @app.put("/api/customers/{cid}")
    def edit_customer(cid: int, b: CustomerIn, u=Depends(member)):
        try:
            n = db.run("""UPDATE customers SET name=%s,phone=%s,email=%s,gender=%s,dob=%s,address=%s,notes=%s,
                          preferred_payment_method=%s,updated_at=now() WHERE id=%s AND spa_id=%s""",
                       (b.name.strip(), b.phone.strip(), b.email, b.gender, b.dob, b.address, b.notes,
                        b.preferred_payment_method, cid, u["spa"]))
        except pgerr.UniqueViolation:
            raise HTTPException(409, PHONE_TAKEN)
        if not n:
            raise HTTPException(404, "Customer not found")
        return {"id": cid}

    # ---- therapists & services (admin-managed) -------------------------
    @app.get("/api/therapists")
    def therapists(u=Depends(member)):
        return db.all("SELECT * FROM therapists WHERE spa_id=%s ORDER BY lower(name)", (u["spa"],))

    @app.post("/api/therapists", status_code=201)
    def add_therapist(b: TherapistIn, u=Depends(admin)):
        cols = ",".join(THERAPIST_DETAILS)
        marks = ",".join(["%s"] * len(THERAPIST_DETAILS))
        return {"id": db.insert(f"""INSERT INTO therapists(name,phone,fixed_salary,incentive_percent,is_active,joined_on,spa_id,
                                                         {cols}) VALUES(%s,%s,%s,%s,%s,%s,%s,{marks})""",
                                (b.name.strip(), b.phone, b.fixed_salary, b.incentive_percent, b.is_active, b.joined_on,
                                 u["spa"], *details(b)))}

    @app.put("/api/therapists/{tid}")
    def edit_therapist(tid: int, b: TherapistIn, u=Depends(admin)):
        sets = ",".join(f"{n}=%s" for n in THERAPIST_DETAILS)
        if not db.run(f"""UPDATE therapists SET name=%s,phone=%s,fixed_salary=%s,incentive_percent=%s,is_active=%s,
                          joined_on=%s,{sets} WHERE id=%s AND spa_id=%s""",
                      (b.name.strip(), b.phone, b.fixed_salary, b.incentive_percent, b.is_active, b.joined_on,
                       *details(b), tid, u["spa"])):
            raise HTTPException(404, "Therapist not found")
        return {"id": tid}

    # ---- weekly therapist shifts (admin-managed) -------------------------
    @app.get("/api/shifts")
    def shifts(u=Depends(member)):
        return db.all("SELECT * FROM shifts WHERE spa_id=%s ORDER BY start_time, lower(name)", (u["spa"],))

    @app.post("/api/shifts", status_code=201)
    def add_shift(b: ShiftIn, u=Depends(admin)):
        if not b.name.strip():
            raise HTTPException(422, "Shift name cannot be blank")
        if b.start_time.tzinfo is not None or b.end_time.tzinfo is not None:
            raise HTTPException(422, "Shift times must be local times without a timezone")
        if b.end_time <= b.start_time:
            raise HTTPException(422, "Shift end time must be later than start time")
        try:
            return {"id": db.insert(
                "INSERT INTO shifts(spa_id,name,start_time,end_time,is_active) VALUES(%s,%s,%s,%s,%s)",
                (u["spa"], b.name.strip(), b.start_time, b.end_time, b.is_active))}
        except pgerr.UniqueViolation:
            raise HTTPException(409, "A shift with this name already exists")

    @app.put("/api/shifts/{sid}")
    def edit_shift(sid: int, b: ShiftIn, u=Depends(admin)):
        if not b.name.strip():
            raise HTTPException(422, "Shift name cannot be blank")
        if b.start_time.tzinfo is not None or b.end_time.tzinfo is not None:
            raise HTTPException(422, "Shift times must be local times without a timezone")
        if b.end_time <= b.start_time:
            raise HTTPException(422, "Shift end time must be later than start time")
        try:
            n = db.run(
                "UPDATE shifts SET name=%s,start_time=%s,end_time=%s,is_active=%s WHERE id=%s AND spa_id=%s",
                (b.name.strip(), b.start_time, b.end_time, b.is_active, sid, u["spa"]))
        except pgerr.UniqueViolation:
            raise HTTPException(409, "A shift with this name already exists")
        if not n:
            raise HTTPException(404, "Shift not found")
        return {"id": sid}

    @app.get("/api/shift-assignments")
    def shift_assignments(therapist_id: int | None = Query(None), u=Depends(member)):
        sql = """SELECT a.id, a.therapist_id, t.name AS therapist, a.shift_id, s.name AS shift_name,
                        s.start_time, s.end_time, s.is_active AS shift_is_active, a.day_of_week
                 FROM therapist_shift_assignments a
                 JOIN therapists t ON t.id=a.therapist_id AND t.spa_id=a.spa_id
                 JOIN shifts s ON s.id=a.shift_id AND s.spa_id=a.spa_id
                 WHERE a.spa_id=%s"""
        args: list = [u["spa"]]
        if therapist_id is not None:
            sql += " AND a.therapist_id=%s"; args.append(therapist_id)
        return db.all(sql + " ORDER BY t.name, a.day_of_week", args)

    @app.post("/api/shift-assignments", status_code=201)
    def assign_shift(b: ShiftAssignmentIn, u=Depends(admin)):
        with db.tx() as t:
            if not t.one("SELECT 1 FROM therapists WHERE id=%s AND spa_id=%s",
                         (b.therapist_id, u["spa"])):
                raise HTTPException(404, "Therapist not found")
            if not t.one("SELECT 1 FROM shifts WHERE id=%s AND spa_id=%s AND is_active",
                         (b.shift_id, u["spa"])):
                raise HTTPException(404, "Active shift not found")
            aid = t.insert(
                """INSERT INTO therapist_shift_assignments(spa_id,therapist_id,shift_id,day_of_week)
                   VALUES(%s,%s,%s,%s)
                   ON CONFLICT (spa_id,therapist_id,day_of_week)
                   DO UPDATE SET shift_id=EXCLUDED.shift_id""",
                (u["spa"], b.therapist_id, b.shift_id, b.day_of_week))
        return {"id": aid}

    @app.delete("/api/shift-assignments/{aid}", status_code=204)
    def remove_shift_assignment(aid: int, u=Depends(admin)):
        if not db.run("DELETE FROM therapist_shift_assignments WHERE id=%s AND spa_id=%s",
                      (aid, u["spa"])):
            raise HTTPException(404, "Shift assignment not found")
        return Response(status_code=204)

    @app.get("/api/services")
    def services(u=Depends(member)):
        return db.all("SELECT * FROM services WHERE spa_id=%s ORDER BY lower(name)", (u["spa"],))

    @app.post("/api/services", status_code=201)
    def add_service(b: ServiceIn, u=Depends(admin)):
        try:
            return {"id": db.insert("INSERT INTO services(name,price,duration_min,is_active,spa_id) VALUES(%s,%s,%s,%s,%s)",
                                    (b.name.strip(), b.price, b.duration_min, b.is_active, u["spa"]))}
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Service already exists")

    @app.put("/api/services/{sid}")
    def edit_service(sid: int, b: ServiceIn, u=Depends(admin)):
        try:
            n = db.run("UPDATE services SET name=%s,price=%s,duration_min=%s,is_active=%s WHERE id=%s AND spa_id=%s",
                       (b.name.strip(), b.price, b.duration_min, b.is_active, sid, u["spa"]))
        except pgerr.UniqueViolation:
            raise HTTPException(409, "Service already exists")
        if not n:
            raise HTTPException(404, "Service not found")
        return {"id": sid}

    # ---- appointments ---------------------------------------------------
    # Double-booking is refused by the database itself (appointments_no_overlap), so it holds even
    # with several API processes; we only translate that error into a 409.
    def resolve_slot(t, b: AppointmentIn, spa: int):
        """Validate a booking request; returns (therapist, service, start). Shared by create and edit."""
        try:
            start = dt.datetime.fromisoformat(b.start_at).replace(tzinfo=None, second=0, microsecond=0)
        except ValueError:
            raise HTTPException(422, "start_at must be YYYY-MM-DDTHH:MM")
        if not t.one("SELECT 1 FROM customers WHERE id=%s AND spa_id=%s", (b.customer_id, spa)):
            raise HTTPException(404, "Customer not found")
        th = t.one("SELECT * FROM therapists WHERE id=%s AND spa_id=%s AND is_active", (b.therapist_id, spa))
        sv = t.one("SELECT * FROM services WHERE id=%s AND spa_id=%s AND is_active", (b.service_id, spa))
        if not th or not sv:
            raise HTTPException(404, "Therapist or service not found (or inactive)")

        # Once a weekly roster exists, the date must have an active shift and the
        # full appointment (including its duration override) must fit within it.
        has_roster = t.one(
            "SELECT 1 FROM therapist_shift_assignments WHERE spa_id=%s AND therapist_id=%s LIMIT 1",
            (spa, th["id"]))
        if has_roster:
            assignment = t.one(
                """SELECT s.start_time, s.end_time, s.name FROM therapist_shift_assignments a
                   JOIN shifts s ON s.id=a.shift_id AND s.spa_id=a.spa_id
                   WHERE a.spa_id=%s AND a.therapist_id=%s AND a.day_of_week=%s AND s.is_active""",
                (spa, th["id"], start.weekday()))
            if not assignment:
                raise HTTPException(409, "Therapist is not assigned to a shift on this weekday")
            duration = b.duration_min or sv["duration_min"]
            finish = start + dt.timedelta(minutes=duration)
            if (start.date() != finish.date() or start.time() < assignment["start_time"]
                    or finish.time() > assignment["end_time"]):
                raise HTTPException(
                    409, f"Appointment must fit within {assignment['name']} "
                         f"({assignment['start_time']:%H:%M}-{assignment['end_time']:%H:%M})")
        return th, sv, start

    def insert_appointment(t, b: AppointmentIn, spa: int) -> int:
        th, sv, start = resolve_slot(t, b, spa)
        try:
            return t.insert("""INSERT INTO appointments(customer_id,therapist_id,service_id,start_at,duration_min,price,notes,
                                                        spa_id)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (b.customer_id, th["id"], sv["id"], start, b.duration_min or sv["duration_min"],
                             sv["price"] if b.price is None else b.price, b.notes, spa))
        except pgerr.ExclusionViolation:
            raise HTTPException(409, SLOT_TAKEN)

    @app.get("/api/appointments")
    def appointments(date: Optional[dt.date] = None, therapist_id: Optional[int] = None,
                     status: Optional[Status] = None, u=Depends(member)):
        sql = """SELECT a.*, c.name AS customer, c.phone, s.name AS service, t.name AS therapist
                 FROM appointments a JOIN customers c ON c.id=a.customer_id
                 JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id WHERE a.spa_id=%s"""
        args = [u["spa"]]
        if date:
            sql += " AND a.start_at >= %s AND a.start_at < %s"
            args += [date, date + dt.timedelta(days=1)]
        if therapist_id:
            sql += " AND a.therapist_id=%s"; args.append(therapist_id)
        if status:
            sql += " AND a.status=%s"; args.append(status)
        return db.all(sql + " ORDER BY a.start_at LIMIT 500", args)

    @app.post("/api/appointments", status_code=201)
    def add_appointment(b: AppointmentIn, u=Depends(member)):
        with db.tx() as t:
            return {"id": insert_appointment(t, b, u["spa"])}

    @app.put("/api/appointments/{aid}")
    def edit_appointment(aid: int, b: AppointmentIn, u=Depends(member)):
        """Reschedule / change an appointment. Only while still 'booked': completed ones feed payroll."""
        with db.tx() as t:
            a = t.one("SELECT * FROM appointments WHERE id=%s AND spa_id=%s FOR UPDATE", (aid, u["spa"]))
            if not a:
                raise HTTPException(404, "Appointment not found")
            if a["status"] != "booked":
                raise HTTPException(409, "Only booked appointments can be edited; change the status first")
            th, sv, start = resolve_slot(t, b, u["spa"])
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
    def set_status(aid: int, b: StatusIn, u=Depends(member)):
        try:
            with db.tx() as t:
                a = t.one("SELECT * FROM appointments WHERE id=%s AND spa_id=%s FOR UPDATE", (aid, u["spa"]))
                if not a:
                    raise HTTPException(404, "Appointment not found")
                if a["status"] == b.status:
                    return {"id": aid, "status": b.status, "plan_deduction": None}

                deduction = plan = has_active = None
                if b.status == "completed" and b.plan_type:
                    # Staff explicitly chooses hours or rupees; never auto-select a balance type.
                    amount = (round(a["duration_min"] / 60.0, 2)
                              if b.plan_type == "hours" else round(float(a["price"]), 2))
                    plan = t.one(
                        """SELECT * FROM customer_plans
                           WHERE customer_id=%s AND spa_id=%s AND plan_type=%s AND balance >= %s
                             AND (expires_on IS NULL OR expires_on >= CURRENT_DATE)
                           ORDER BY created_at, id LIMIT 1 FOR UPDATE""",
                        (a["customer_id"], u["spa"], b.plan_type, amount))
                    has_active = plan or t.one(
                        """SELECT 1 FROM customer_plans WHERE customer_id=%s AND spa_id=%s AND plan_type=%s
                             AND (expires_on IS NULL OR expires_on >= CURRENT_DATE)""",
                        (a["customer_id"], u["spa"], b.plan_type))
                if b.status == "completed" and b.plan_type and has_active:
                    if not plan:
                        unit_label = "hours" if b.plan_type == "hours" else "rupees"
                        raise HTTPException(409, f"No active {unit_label} plan has enough balance for this appointment")
                    t.run("UPDATE customer_plans SET balance=balance-%s WHERE id=%s", (amount, plan["id"]))
                    t.insert(
                        """INSERT INTO plan_redemptions(plan_id,appointment_id,spa_id,amount,unit)
                           VALUES(%s,%s,%s,%s,%s)""",
                        (plan["id"], aid, u["spa"], amount, b.plan_type))
                    remaining = round(float(plan["balance"]) - amount, 2)
                    deduction = {"plan_id": plan["id"], "plan_name": plan["name"],
                                 "amount": amount, "unit": b.plan_type, "remaining_balance": remaining}
                elif a["status"] == "completed":
                    # Re-open/cancel a completed booking: refund its active package deduction.
                    redemption = t.one(
                        """SELECT r.*, p.name FROM plan_redemptions r
                           JOIN customer_plans p ON p.id=r.plan_id
                           WHERE r.appointment_id=%s AND r.spa_id=%s AND r.reversed_at IS NULL
                           FOR UPDATE OF r, p""", (aid, u["spa"]))
                    if redemption:
                        t.run("UPDATE customer_plans SET balance=balance+%s WHERE id=%s",
                              (redemption["amount"], redemption["plan_id"]))
                        t.run("UPDATE plan_redemptions SET reversed_at=now() WHERE id=%s", (redemption["id"],))
                t.run("UPDATE appointments SET status=%s, updated_at=now() WHERE id=%s",
                      (b.status, aid))
        except pgerr.ExclusionViolation:
            raise HTTPException(409, SLOT_TAKEN)
        return {"id": aid, "status": b.status, "plan_deduction": deduction}

    @app.post("/api/customers/{cid}/plans", status_code=201)
    def add_customer_plan(cid: int, b: CustomerPlanIn, u=Depends(admin)):
        with db.tx() as t:
            if not t.one("SELECT 1 FROM customers WHERE id=%s AND spa_id=%s FOR UPDATE", (cid, u["spa"])):
                raise HTTPException(404, "Customer not found")
            pid = t.insert(
                """INSERT INTO customer_plans(customer_id,spa_id,name,plan_type,purchased_amount,balance,expires_on,notes,
                                               price_paid)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (cid, u["spa"], b.name.strip(), b.plan_type, b.amount, b.amount, b.expires_on, b.notes,
                 paid_for(b.plan_type, b.amount, b.price_paid)))
        return {"id": pid, "balance": b.amount, "plan_type": b.plan_type}

    @app.put("/api/customers/{cid}/plans/{pid}")
    def edit_customer_plan(cid: int, pid: int, b: CustomerPlanUpdate, u=Depends(admin)):
        """Rename a plan, change or clear its expiry, edit notes. Amounts change only through a top-up."""
        if not db.run("""UPDATE customer_plans SET name=%s, expires_on=%s, notes=%s
                         WHERE id=%s AND customer_id=%s AND spa_id=%s""",
                      (b.name.strip(), b.expires_on, (b.notes or "").strip() or None, pid, cid, u["spa"])):
            raise HTTPException(404, "Plan not found")
        return {"id": pid}

    @app.post("/api/customers/{cid}/plans/{pid}/topup", status_code=201)
    def top_up_plan(cid: int, pid: int, b: PlanTopUp, u=Depends(admin)):
        """Add hours or rupees to a plan: both the purchased amount and the balance grow."""
        with db.tx() as t:
            p = t.one("SELECT * FROM customer_plans WHERE id=%s AND customer_id=%s AND spa_id=%s FOR UPDATE",
                      (pid, cid, u["spa"]))
            if not p:
                raise HTTPException(404, "Plan not found")
            t.run("UPDATE customer_plans SET purchased_amount=purchased_amount+%s, balance=balance+%s WHERE id=%s",
                  (b.amount, b.amount, pid))
            t.insert("""INSERT INTO plan_topups(plan_id,spa_id,amount,note,created_by,price_paid)
                        VALUES(%s,%s,%s,%s,%s,%s)""",
                     (pid, u["spa"], b.amount, (b.note or "").strip() or None, u["id"],
                      paid_for(p["plan_type"], b.amount, b.price_paid)))
        return {"id": pid, "balance": round(float(p["balance"]) + b.amount, 2),
                "purchased_amount": round(float(p["purchased_amount"]) + b.amount, 2)}

    # ---- onboarding: customer (+ optional first booking) in ONE transaction ----
    @app.post("/api/onboarding", status_code=201)
    def onboard(b: OnboardIn, u=Depends(member)):
        with db.tx() as t:  # a clashing first appointment rolls the new customer back too
            cid = insert_customer(t, b, u["spa"])
            aid = None
            if b.first_appointment:
                aid = insert_appointment(t, b.first_appointment.model_copy(update={"customer_id": cid}), u["spa"])
        return {"customer_id": cid, "appointment_id": aid}

    # ---- reports & payroll (admin) --------------------------------------
    def check_month(month: str):
        if not MONTH.match(month):
            raise HTTPException(422, "month must be YYYY-MM")

    def period(month: str | None, date_from: dt.date | None, date_to: dt.date | None):
        """[lo, hi) for a report: a from/to date range (both days included) wins over the month."""
        if date_from or date_to:
            if not (date_from and date_to):
                raise HTTPException(422, "give both from and to")
            if date_to < date_from:
                raise HTTPException(422, "to must not be before from")
            if (date_to - date_from).days > 366:
                raise HTTPException(422, "at most one year at a time")
            return date_from, date_to + dt.timedelta(days=1)
        if not month:
            raise HTTPException(422, "give month (YYYY-MM) or from and to")
        check_month(month)
        return payroll.month_bounds(month)

    @app.get("/api/reports/shifts")
    def shift_report(month: str | None = Query(None), date_from: dt.date | None = Query(None, alias="from"),
                     date_to: dt.date | None = Query(None, alias="to"),
                     therapist_id: int | None = Query(None), u=Depends(admin)):
        """Weekly roster plus appointment and completed-work statistics for the chosen period."""
        lo, hi = period(month, date_from, date_to)
        therapist_filter = " AND t.id=%s" if therapist_id is not None else ""
        args: list = [lo, hi, u["spa"]]
        if therapist_id is not None:
            args.append(therapist_id)
        rows = db.all(
            f"""SELECT sh.id AS shift_id, sh.name AS shift_name, sh.start_time, sh.end_time,
                       sh.is_active AS shift_is_active, a.day_of_week,
                       CASE a.day_of_week WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday'
                            WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday'
                            WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday' ELSE 'Sunday' END AS day_name,
                       t.id AS therapist_id, t.name AS therapist,
                       COUNT(ap.id) FILTER (WHERE ap.status IN ('booked','completed')) AS appointment_count,
                       COUNT(ap.id) FILTER (WHERE ap.status='completed') AS completed_count,
                       COALESCE(SUM(ap.duration_min) FILTER (WHERE ap.status='completed'),0) AS completed_minutes,
                       COALESCE(SUM(ap.price) FILTER (WHERE ap.status='completed'),0) AS completed_revenue
                FROM therapist_shift_assignments a
                JOIN shifts sh ON sh.id=a.shift_id AND sh.spa_id=a.spa_id
                JOIN therapists t ON t.id=a.therapist_id AND t.spa_id=a.spa_id
                LEFT JOIN appointments ap ON ap.therapist_id=t.id AND ap.spa_id=a.spa_id
                     AND ap.start_at >= %s AND ap.start_at < %s
                     AND ((EXTRACT(DOW FROM ap.start_at)::int + 6) %% 7)=a.day_of_week
                     AND ap.start_at::time >= sh.start_time AND (ap.start_at + make_interval(mins => ap.duration_min))::time <= sh.end_time
                WHERE a.spa_id=%s{therapist_filter}
                GROUP BY sh.id, sh.name, sh.start_time, sh.end_time, sh.is_active,
                         a.day_of_week, t.id, t.name
                ORDER BY sh.start_time, sh.name, a.day_of_week, t.name""",
            tuple(args))
        templates = db.all(
            "SELECT id, name, start_time, end_time, is_active FROM shifts WHERE spa_id=%s ORDER BY start_time,name",
            (u["spa"],))
        return {
            "month": month, "from": lo.isoformat(), "to": (hi - dt.timedelta(days=1)).isoformat(),
            "shifts": templates, "assignments": rows,
            "summary": {
                "shift_templates": len(templates),
                "assigned_therapists": len({x["therapist_id"] for x in rows}),
                "shift_assignments": len(rows),
                "appointments": sum(int(x["appointment_count"] or 0) for x in rows),
                "completed": sum(int(x["completed_count"] or 0) for x in rows),
                "completed_minutes": sum(int(x["completed_minutes"] or 0) for x in rows),
                "completed_revenue": round(sum(float(x["completed_revenue"] or 0) for x in rows), 2),
            },
        }

    @app.get("/api/reports/summary")
    def summary(month: str | None = Query(None), therapist_id: int | None = Query(None),
                date_from: dt.date | None = Query(None, alias="from"), date_to: dt.date | None = Query(None, alias="to"),
                customer_id: int | None = Query(None), service_id: int | None = Query(None),
                u=Depends(admin)):
        """The month's figures. Revenue counts COMPLETED appointments only, at the price agreed at booking.
        `lines` is every completed service (who, what, for whom, how much); `by_therapist` gives each
        therapist's count and amount with a per-service breakdown. `therapist_id` narrows everything but
        new_customers to one therapist."""
        lo, hi = period(month, date_from, date_to)
        who, extra = " AND a.spa_id = %s", [u["spa"]]
        for col, val in (("therapist_id", therapist_id), ("customer_id", customer_id), ("service_id", service_id)):
            if val is not None:
                who += f" AND a.{col} = %s"
                extra.append(val)
        args = (lo, hi, *extra)
        by_status = db.all(f"""SELECT a.status, COUNT(*) AS count, COALESCE(SUM(a.price),0) AS amount
                               FROM appointments a WHERE a.start_at >= %s AND a.start_at < %s{who}
                               GROUP BY a.status ORDER BY a.status""", args)
        lines = db.all(f"""SELECT a.id, a.start_at, a.duration_min, a.price, c.name AS customer, s.name AS service,
                                  t.id AS therapist_id, t.name AS therapist
                           FROM appointments a JOIN customers c ON c.id=a.customer_id
                           JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id
                           WHERE a.status='completed' AND a.start_at >= %s AND a.start_at < %s{who}
                           ORDER BY a.start_at, a.id""", args)
        by_service: dict[str, dict] = {}
        by_therapist: dict[int, dict] = {}
        for r in lines:
            price = payroll.money(r["price"])
            sv = by_service.setdefault(r["service"], {"service": r["service"], "count": 0, "minutes": 0, "revenue": 0})
            sv["count"] += 1
            sv["minutes"] += r["duration_min"]
            sv["revenue"] = payroll.money(sv["revenue"]) + price
            th = by_therapist.setdefault(r["therapist_id"], {"therapist_id": r["therapist_id"],
                                                              "therapist": r["therapist"], "count": 0,
                                                              "minutes": 0, "revenue": 0, "services": {}})
            th["count"] += 1
            th["minutes"] += r["duration_min"]
            th["revenue"] = payroll.money(th["revenue"]) + price
            ts = th["services"].setdefault(r["service"], {"service": r["service"], "count": 0, "minutes": 0, "revenue": 0})
            ts["count"] += 1
            ts["minutes"] += r["duration_min"]
            ts["revenue"] = payroll.money(ts["revenue"]) + price

        def out(d: dict) -> dict:
            return {**d, "revenue": float(d["revenue"])}

        therapists = []
        for th in sorted(by_therapist.values(), key=lambda x: (-x["revenue"], x["therapist"].lower())):
            services = sorted((out(v) for v in th["services"].values()), key=lambda x: -x["revenue"])
            therapists.append({**out(th), "services": services})
        # created_at is a timestamptz and the session zone is the spa's, so these are local calendar months
        new_c = db.one("SELECT COUNT(*) AS n FROM customers WHERE spa_id=%s AND created_at >= %s AND created_at < %s",
                       (u["spa"], lo, hi))["n"]
        rev = sum((payroll.money(r["price"]) for r in lines), payroll.money(0))
        # Money side: plan sales are cash in on the sale date; services later paid from a plan are not cash again.
        cust = " AND p.customer_id=%s" if customer_id is not None else ""
        cargs = (customer_id,) if customer_id is not None else ()
        plan_rows = [] if (therapist_id is not None or service_id is not None) else db.all(
            f"""SELECT 'plan' AS kind, p.created_at AS at, c.name AS customer, p.name AS plan, p.plan_type,
                       p.purchased_amount - COALESCE((SELECT SUM(x.amount) FROM plan_topups x WHERE x.plan_id=p.id),0)
                         AS amount, COALESCE(p.price_paid,0) AS price_paid
                FROM customer_plans p JOIN customers c ON c.id=p.customer_id
                WHERE p.spa_id=%s AND p.created_at >= %s AND p.created_at < %s{cust}
                UNION ALL
                SELECT 'topup', x.created_at, c.name, p.name, p.plan_type, x.amount, COALESCE(x.price_paid,0)
                FROM plan_topups x JOIN customer_plans p ON p.id=x.plan_id JOIN customers c ON c.id=p.customer_id
                WHERE x.spa_id=%s AND x.created_at >= %s AND x.created_at < %s{cust}
                ORDER BY 2""", (u["spa"], lo, hi, *cargs, u["spa"], lo, hi, *cargs))
        plan_sales = sum((payroll.money(x["price_paid"]) for x in plan_rows), payroll.money(0))
        by_plan = db.one(f"""SELECT COALESCE(SUM(a.price),0) AS v FROM appointments a
                              WHERE a.status='completed' AND a.start_at >= %s AND a.start_at < %s{who}
                                AND EXISTS (SELECT 1 FROM plan_redemptions r
                                            WHERE r.appointment_id=a.id AND r.reversed_at IS NULL)""", args)["v"]
        paid_by_plan = payroll.money(by_plan)
        # profit only makes sense for a whole month of the whole spa: payroll is monthly
        exp_rows = [] if (therapist_id is not None or customer_id is not None or service_id is not None) \
            else expenses_in(db, u["spa"], lo, hi)
        exp_total = sum((payroll.money(x["amount"]) for x in exp_rows), payroll.money(0))
        pay = None
        if month and not (date_from or date_to) and therapist_id is None and customer_id is None and service_id is None:
            pay = sum((payroll.money(payroll.compute_payslip(db, t, month)["net_pay"]) for t in
                       db.all("SELECT * FROM therapists WHERE spa_id=%s AND is_active", (u["spa"],))), payroll.money(0))
        return {"month": month, "from": lo.isoformat(), "to": (hi - dt.timedelta(days=1)).isoformat(),
                "therapist_id": therapist_id, "customer_id": customer_id, "service_id": service_id, "revenue": float(rev),
                "plan_sales": float(plan_sales), "paid_by_plan": float(paid_by_plan),
                "collected": float(rev - paid_by_plan + plan_sales), "plan_lines": plan_rows,
                "payroll": None if pay is None else float(pay),
                "expenses": float(exp_total), "expense_lines": exp_rows,
                "profit": None if pay is None else float(rev - paid_by_plan + plan_sales - pay - exp_total),
                "services_count": len(lines), "minutes": sum(r["duration_min"] for r in lines), "new_customers": new_c, "by_status": by_status,
                "by_service": sorted((out(v) for v in by_service.values()), key=lambda x: -x["revenue"]),
                "by_therapist": therapists, "lines": lines}

    def report_for(month, therapist_id, date_from, date_to, customer_id, service_id, user):
        r = summary(month, therapist_id, date_from, date_to, customer_id, service_id, user)
        parts = []
        for key, table, val in (("therapist", "therapists", therapist_id), ("customer", "customers", customer_id),
                                ("service", "services", service_id)):
            if val is not None:
                row = db.one(f"SELECT name FROM {table} WHERE id=%s AND spa_id=%s", (val, user["spa"]))
                if not row:
                    raise HTTPException(404, f"{key.title()} not found")
                r[key] = row["name"]
                parts.append(re.sub(r"[^A-Za-z0-9_-]", "_", row["name"]))
        period = month or f"{r['from']}_to_{r['to']}"
        return r, payroll.file_name(user["spa_name"], "Revenue-Report", period, *parts)

    @app.get("/api/reports/summary/pdf")
    def summary_pdf(month: str | None = Query(None), therapist_id: int | None = Query(None),
                    date_from: dt.date | None = Query(None, alias="from"),
                    date_to: dt.date | None = Query(None, alias="to"),
                    customer_id: int | None = Query(None), service_id: int | None = Query(None), user=Depends(admin)):
        """The same report as /api/reports/summary (same filters), as a PDF to download or print."""
        r, name = report_for(month, therapist_id, date_from, date_to, customer_id, service_id, user)
        return Response(payroll.report_pdf(r, user["spa_name"]), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})

    @app.get("/api/reports/summary/xlsx")
    def summary_xlsx(month: str | None = Query(None), therapist_id: int | None = Query(None),
                     date_from: dt.date | None = Query(None, alias="from"),
                     date_to: dt.date | None = Query(None, alias="to"),
                     customer_id: int | None = Query(None), service_id: int | None = Query(None), user=Depends(admin)):
        """The same report as an Excel workbook (Summary, Services done, By therapist, Plan sales)."""
        r, name = report_for(month, therapist_id, date_from, date_to, customer_id, service_id, user)
        return Response(payroll.report_xlsx(r, user["spa_name"]),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})

    @app.get("/api/reports/summary/csv")
    def summary_csv(month: str | None = Query(None), therapist_id: int | None = Query(None),
                    date_from: dt.date | None = Query(None, alias="from"),
                    date_to: dt.date | None = Query(None, alias="to"),
                    customer_id: int | None = Query(None), service_id: int | None = Query(None), user=Depends(admin)):
        """Every completed service in the report (same filters), one row each, for Excel. UTF-8 with BOM
        so Excel shows names correctly."""
        r, name = report_for(month, therapist_id, date_from, date_to, customer_id, service_id, user)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Time", "Customer", "Service", "Therapist", "Minutes", "Price"])
        for x in r["lines"]:
            when = str(x["start_at"]).replace(" ", "T")
            w.writerow([when[:10], when[11:16], x["customer"], x["service"], x["therapist"], x["duration_min"],
                        f"{float(x['price']):.2f}"])
        w.writerow(["Total", "", "", "", "", r["minutes"], f"{r['revenue']:.2f}"])
        if r["plan_lines"]:
            w.writerow([])
            w.writerow(["Date", "Time", "Customer", "Plan", "Kind", "Amount", "Paid"])
            for x in r["plan_lines"]:
                at = str(x["at"]).replace(" ", "T")
                w.writerow([at[:10], at[11:16], x["customer"], x["plan"], "Top-up" if x["kind"] == "topup" else "New",
                            f"{float(x['amount']):g} {'hrs' if x['plan_type'] == 'hours' else 'Rs'}",
                            f"{float(x['price_paid']):.2f}"])
            w.writerow(["Plan sales", "", "", "", "", "", f"{r['plan_sales']:.2f}"])
        w.writerow(["Money collected", "", "", "", "", "", f"{r['collected']:.2f}"])
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

    @app.get("/api/reports/plans")
    def plan_report(month: str | None = Query(None), date_from: dt.date | None = Query(None, alias="from"),
                    date_to: dt.date | None = Query(None, alias="to"), customer_id: int | None = Query(None),
                    u=Depends(admin)):
        """Prepaid plan sales, current available balances, and deduction/reversal activity for a period."""
        lo, hi = period(month, date_from, date_to)
        customer_filter = " AND p.customer_id=%s" if customer_id is not None else ""
        customer_args = (customer_id,) if customer_id is not None else ()

        plans = db.all(
            f"""SELECT p.id, p.customer_id, c.name AS customer, c.phone, p.name, p.plan_type,
                       p.purchased_amount, p.balance, p.expires_on, p.created_at,
                       COALESCE((SELECT SUM(r.amount) FROM plan_redemptions r
                                 WHERE r.plan_id=p.id AND r.reversed_at IS NULL),0) AS used_amount,
                       p.purchased_amount - COALESCE((SELECT SUM(x.amount) FROM plan_topups x WHERE x.plan_id=p.id),0)
                         AS initial_amount
                FROM customer_plans p JOIN customers c ON c.id=p.customer_id
                WHERE p.spa_id=%s AND p.created_at >= %s AND p.created_at < %s{customer_filter}
                ORDER BY p.created_at DESC, p.id DESC""",
            (u["spa"], lo, hi, *customer_args))

        balance_filter = " AND customer_id=%s" if customer_id is not None else ""
        available = db.all(
            f"""SELECT plan_type, COALESCE(SUM(balance),0) AS balance
                FROM customer_plans
                WHERE spa_id=%s AND (expires_on IS NULL OR expires_on >= CURRENT_DATE){balance_filter}
                GROUP BY plan_type""",
            (u["spa"], *customer_args))

        redemption_customer_filter = " AND p.customer_id=%s" if customer_id is not None else ""
        redemptions = db.all(
            f"""SELECT r.id, r.appointment_id, r.amount, r.unit, r.created_at AS deducted_at,
                       r.reversed_at, p.id AS plan_id, p.name AS plan_name,
                       c.id AS customer_id, c.name AS customer, c.phone,
                       a.start_at, a.duration_min, a.price, s.name AS service, t.name AS therapist
                FROM plan_redemptions r
                JOIN customer_plans p ON p.id=r.plan_id
                JOIN customers c ON c.id=p.customer_id
                JOIN appointments a ON a.id=r.appointment_id
                JOIN services s ON s.id=a.service_id
                JOIN therapists t ON t.id=a.therapist_id
                WHERE r.spa_id=%s
                  AND ((r.created_at >= %s AND r.created_at < %s)
                    OR (r.reversed_at >= %s AND r.reversed_at < %s)){redemption_customer_filter}
                ORDER BY GREATEST(r.created_at, COALESCE(r.reversed_at, r.created_at)) DESC, r.id DESC""",
            (u["spa"], lo, hi, lo, hi, *customer_args))

        activity = db.one(
            f"""SELECT
                  COALESCE(SUM(r.amount) FILTER (WHERE r.unit='hours' AND r.created_at >= %s AND r.created_at < %s),0) AS deducted_hours,
                  COALESCE(SUM(r.amount) FILTER (WHERE r.unit='rupees' AND r.created_at >= %s AND r.created_at < %s),0) AS deducted_rupees,
                  COALESCE(SUM(r.amount) FILTER (WHERE r.unit='hours' AND r.reversed_at >= %s AND r.reversed_at < %s),0) AS reversed_hours,
                  COALESCE(SUM(r.amount) FILTER (WHERE r.unit='rupees' AND r.reversed_at >= %s AND r.reversed_at < %s),0) AS reversed_rupees
                FROM plan_redemptions r JOIN customer_plans p ON p.id=r.plan_id
                WHERE r.spa_id=%s
                  AND ((r.created_at >= %s AND r.created_at < %s)
                    OR (r.reversed_at >= %s AND r.reversed_at < %s)){redemption_customer_filter}""",
            (lo, hi, lo, hi, lo, hi, lo, hi, u["spa"], lo, hi, lo, hi, *customer_args)) or {}

        topups = db.one(
            f"""SELECT COALESCE(SUM(x.amount) FILTER (WHERE p.plan_type='hours'),0) AS hours,
                       COALESCE(SUM(x.amount) FILTER (WHERE p.plan_type='rupees'),0) AS rupees, COUNT(*) AS n
                FROM plan_topups x JOIN customer_plans p ON p.id=x.plan_id
                WHERE x.spa_id=%s AND x.created_at >= %s AND x.created_at < %s{customer_filter}""",
            (u["spa"], lo, hi, *customer_args)) or {}

        def total(items, key):
            return round(sum(float(x.get(key) or 0) for x in items), 2)

        balance_by_type = {x["plan_type"]: float(x["balance"] or 0) for x in available}
        return {
            "month": month, "from": lo.isoformat(), "to": (hi - dt.timedelta(days=1)).isoformat(),
            "customer_id": customer_id,
            "summary": {
                "plans_added": len(plans),
                # new plans at their first amount plus every top-up made in the period
                "sold_hours": round(total([p for p in plans if p["plan_type"] == "hours"], "initial_amount")
                                    + float(topups.get("hours") or 0), 2),
                "sold_rupees": round(total([p for p in plans if p["plan_type"] == "rupees"], "initial_amount")
                                     + float(topups.get("rupees") or 0), 2),
                "topped_up_hours": round(float(topups.get("hours") or 0), 2),
                "topped_up_rupees": round(float(topups.get("rupees") or 0), 2),
                "topups": int(topups.get("n") or 0),
                "available_hours": round(balance_by_type.get("hours", 0), 2),
                "available_rupees": round(balance_by_type.get("rupees", 0), 2),
                "deducted_hours": round(float(activity.get("deducted_hours") or 0), 2),
                "deducted_rupees": round(float(activity.get("deducted_rupees") or 0), 2),
                "reversed_hours": round(float(activity.get("reversed_hours") or 0), 2),
                "reversed_rupees": round(float(activity.get("reversed_rupees") or 0), 2),
                "redemptions": len(redemptions),
            },
            "plans": plans,
            "redemptions": redemptions,
        }

    @app.get("/api/reports/incentives")
    def incentives(month: str = Query(...), u=Depends(admin)):
        check_month(month)
        rows = [payroll.compute_payslip(db, t, month) for t in
                db.all("SELECT * FROM therapists WHERE spa_id=%s AND is_active ORDER BY lower(name)", (u["spa"],))]
        for r in rows:
            r.pop("lines"); r.pop("bonuses")
        return {"month": month, "therapists": rows,
                "total_incentive": round(sum(r["incentive"] + r["extra_incentive"] for r in rows), 2),
                "total_payroll": round(sum(r["net_pay"] for r in rows), 2)}

    def payslip_for(tid: int, month: str, spa: int):
        check_month(month)
        t = db.one("SELECT * FROM therapists WHERE id=%s AND spa_id=%s", (tid, spa))
        if not t:
            raise HTTPException(404, "Therapist not found")
        return payroll.compute_payslip(db, t, month)

    # ---- extra incentives (admin) -----------------------------------------
    @app.get("/api/bonuses")
    def bonuses(month: str | None = Query(None), therapist_id: int | None = Query(None),
                date_from: dt.date | None = Query(None, alias="from"), date_to: dt.date | None = Query(None, alias="to"),
                u=Depends(admin)):
        lo, hi = period(month, date_from, date_to)
        sql = """SELECT b.id, b.therapist_id, t.name AS therapist, b.on_date, b.amount, b.note, b.appointment_id,
                        u.username AS added_by, b.created_at
                 FROM therapist_bonuses b JOIN therapists t ON t.id=b.therapist_id
                 LEFT JOIN users u ON u.id=b.created_by
                 WHERE b.spa_id=%s AND b.on_date >= %s AND b.on_date < %s"""
        args: list = [u["spa"], lo, hi]
        if therapist_id is not None:
            sql += " AND b.therapist_id=%s"; args.append(therapist_id)
        return db.all(sql + " ORDER BY b.on_date DESC, b.id DESC", args)

    @app.post("/api/bonuses", status_code=201)
    def add_bonus(b: BonusIn, u=Depends(admin)):
        if not db.one("SELECT 1 FROM therapists WHERE id=%s AND spa_id=%s", (b.therapist_id, u["spa"])):
            raise HTTPException(404, "Therapist not found")
        if b.appointment_id is not None and not db.one(
                "SELECT 1 FROM appointments WHERE id=%s AND therapist_id=%s", (b.appointment_id, b.therapist_id)):
            raise HTTPException(404, "Appointment not found for this therapist")
        bid = db.insert("""INSERT INTO therapist_bonuses(therapist_id,on_date,amount,note,appointment_id,created_by,spa_id)
                           VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                        (b.therapist_id, b.on_date, b.amount, (b.note or "").strip() or None, b.appointment_id, u["id"],
                         u["spa"]))
        return {"id": bid}

    # ---- expenses (admin) ------------------------------------------------
    @app.get("/api/expenses")
    def list_expenses(month: str | None = Query(None), date_from: dt.date | None = Query(None, alias="from"),
                      date_to: dt.date | None = Query(None, alias="to"), u=Depends(admin)):
        """The period's costs (one-off + regular as they fall due), totals by category, and the regular list."""
        lo, hi = period(month, date_from, date_to)
        rows = expenses_in(db, u["spa"], lo, hi)
        cats: dict[str, Decimal] = {}
        for x in rows:
            cats[x["category"]] = cats.get(x["category"], Decimal(0)) + payroll.money(x["amount"])
        return {"from": lo.isoformat(), "to": (hi - dt.timedelta(days=1)).isoformat(), "lines": rows,
                "total": float(sum(cats.values(), Decimal(0))),
                "by_category": [{"category": k, "amount": float(v)} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
                "regular": db.all("SELECT * FROM recurring_expenses WHERE spa_id=%s ORDER BY ends_on NULLS FIRST, lower(name)",
                                  (u["spa"],))}

    @app.post("/api/expenses", status_code=201)
    def add_expense(b: ExpenseIn, u=Depends(admin)):
        return {"id": db.insert("""INSERT INTO expenses(spa_id,on_date,category,amount,note,created_by)
                                   VALUES(%s,%s,%s,%s,%s,%s)""",
                                (u["spa"], b.on_date, b.category.strip(), b.amount, (b.note or "").strip() or None, u["id"]))}

    @app.put("/api/expenses/{eid}")
    def edit_expense(eid: int, b: ExpenseIn, u=Depends(admin)):
        if not db.run("UPDATE expenses SET on_date=%s,category=%s,amount=%s,note=%s WHERE id=%s AND spa_id=%s",
                      (b.on_date, b.category.strip(), b.amount, (b.note or "").strip() or None, eid, u["spa"])):
            raise HTTPException(404, "Expense not found")
        return {"id": eid}

    @app.delete("/api/expenses/{eid}", status_code=204)
    def delete_expense(eid: int, u=Depends(admin)):
        if not db.run("DELETE FROM expenses WHERE id=%s AND spa_id=%s", (eid, u["spa"])):
            raise HTTPException(404, "Expense not found")

    def check_recurring(b: RecurringExpenseIn):
        if b.ends_on and b.ends_on < b.starts_on:
            raise HTTPException(422, "End date must not be before the start date")

    @app.post("/api/expenses/recurring", status_code=201)
    def add_recurring(b: RecurringExpenseIn, u=Depends(admin)):
        check_recurring(b)
        return {"id": db.insert("""INSERT INTO recurring_expenses(spa_id,name,category,amount,day_of_month,starts_on,ends_on,notes)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (u["spa"], b.name.strip(), b.category.strip(), b.amount, b.day_of_month, b.starts_on,
                                 b.ends_on, (b.notes or "").strip() or None))}

    @app.put("/api/expenses/recurring/{rid}")
    def edit_recurring(rid: int, b: RecurringExpenseIn, u=Depends(admin)):
        """Change a regular cost. To stop it, set ends_on: past months keep counting it."""
        check_recurring(b)
        if not db.run("""UPDATE recurring_expenses SET name=%s,category=%s,amount=%s,day_of_month=%s,starts_on=%s,
                         ends_on=%s,notes=%s WHERE id=%s AND spa_id=%s""",
                      (b.name.strip(), b.category.strip(), b.amount, b.day_of_month, b.starts_on, b.ends_on,
                       (b.notes or "").strip() or None, rid, u["spa"])):
            raise HTTPException(404, "Regular expense not found")
        return {"id": rid}

    @app.delete("/api/expenses/recurring/{rid}", status_code=204)
    def delete_recurring(rid: int, u=Depends(admin)):
        """Remove a regular cost entirely (also from past months). Prefer an end date to stop it."""
        if not db.run("DELETE FROM recurring_expenses WHERE id=%s AND spa_id=%s", (rid, u["spa"])):
            raise HTTPException(404, "Regular expense not found")

    @app.delete("/api/bonuses/{bid}", status_code=204)
    def delete_bonus(bid: int, u=Depends(admin)):
        if not db.run("DELETE FROM therapist_bonuses WHERE id=%s AND spa_id=%s", (bid, u["spa"])):
            raise HTTPException(404, "Not found")
        return Response(status_code=204)

    @app.get("/api/payroll/{tid}/{month}")
    def payslip(tid: int, month: str, u=Depends(admin)):
        return payslip_for(tid, month, u["spa"])

    @app.get("/api/payroll/{tid}/{month}/pdf")
    def payslip_pdf(tid: int, month: str, u=Depends(admin)):
        p = payslip_for(tid, month, u["spa"])
        name = re.sub(r"[^A-Za-z0-9_-]", "_", p["therapist"])
        return Response(payroll.payslip_pdf(p, u["spa_name"]), media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{payroll.file_name(u["spa_name"], "Payslip", name, month)}.pdf"'})

    @app.get("/api/health")
    def health():
        db.one("SELECT 1")  # fails (500) if the database is unreachable, so the load balancer sees it
        return {"ok": True}

    return app
