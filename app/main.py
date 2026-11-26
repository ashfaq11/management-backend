import datetime as dt
import os
import re
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import payroll, security
from .db import Database, default_path

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SESSION_HOURS = 12
Status = Literal["booked", "completed", "cancelled", "no_show"]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---- request models -------------------------------------------------------
class Login(BaseModel):
    username: str
    password: str


class CustomerIn(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=5)
    email: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


class TherapistIn(BaseModel):
    name: str = Field(min_length=1)
    phone: Optional[str] = None
    fixed_salary: float = Field(ge=0)
    incentive_percent: float = Field(ge=0, le=100)
    is_active: bool = True
    joined_on: Optional[str] = None


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


class StatusIn(BaseModel):
    status: Status


class OnboardIn(CustomerIn):
    first_appointment: Optional[AppointmentIn] = None  # customer_id ignored


class UserIn(BaseModel):
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    role: Literal["admin", "staff"] = "staff"


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db = Database(db_path or default_path())
    business = os.environ.get("SPA_NAME", "Serenity Spa")
    if not db.one("SELECT 1 FROM users LIMIT 1"):
        db.run("INSERT INTO users(username,password_hash,role) VALUES(?,?,'admin')",
               (os.environ.get("ADMIN_USER", "admin"),
                security.hash_password(os.environ.get("ADMIN_PASSWORD", "admin12345"))))
    app = FastAPI(title="Spa Management API")
    origins = [o.strip() for o in os.environ.get("SPA_CORS_ORIGINS", "http://localhost:4200").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["Authorization", "Content-Type"])

    # ---- auth ---------------------------------------------------------
    def current_user(authorization: str = Header(default="")):
        tok = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        u = db.one("""SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id
                      WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1""",
                   (security.fingerprint(tok), now())) if tok else None
        if not u:
            raise HTTPException(401, "Not authenticated")
        return u

    def admin(u=Depends(current_user)):
        if u["role"] != "admin":
            raise HTTPException(403, "Admin only")
        return u

    @app.post("/api/auth/login")
    def login(b: Login):
        u = db.one("SELECT * FROM users WHERE username=? AND is_active=1", (b.username,))
        if not u or not security.verify_password(b.password, u["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        tok = security.new_token()
        exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=SESSION_HOURS)
               ).strftime("%Y-%m-%dT%H:%M:%S")
        db.run("INSERT INTO sessions VALUES(?,?,?)", (security.fingerprint(tok), u["id"], exp))
        return {"token": tok, "username": u["username"], "role": u["role"]}

    @app.post("/api/auth/logout", status_code=204)
    def logout(authorization: str = Header(default=""), u=Depends(current_user)):
        db.run("DELETE FROM sessions WHERE token_hash=?", (security.fingerprint(authorization[7:]),))

    @app.get("/api/auth/me")
    def me(u=Depends(current_user)):
        return u

    @app.post("/api/users", status_code=201)
    def add_user(b: UserIn, _=Depends(admin)):
        if db.one("SELECT 1 FROM users WHERE username=?", (b.username,)):
            raise HTTPException(409, "Username taken")
        return {"id": db.run("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                             (b.username, security.hash_password(b.password), b.role))}

    # ---- customers ----------------------------------------------------
    def insert_customer(b: CustomerIn) -> int:
        if db.one("SELECT 1 FROM customers WHERE phone=?", (b.phone,)):
            raise HTTPException(409, "A customer with this phone already exists")
        return db.run("""INSERT INTO customers(name,phone,email,gender,dob,address,notes,created_at)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (b.name, b.phone, b.email, b.gender, b.dob, b.address, b.notes, now()))

    @app.get("/api/customers")
    def customers(q: str = "", _=Depends(current_user)):
        like = f"%{q}%"
        return db.all("SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY name LIMIT 200",
                      (like, like))

    @app.post("/api/customers", status_code=201)
    def add_customer(b: CustomerIn, _=Depends(current_user)):
        return {"id": insert_customer(b)}

    @app.get("/api/customers/{cid}")
    def customer(cid: int, _=Depends(current_user)):
        c = db.one("SELECT * FROM customers WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404, "Customer not found")
        c["appointments"] = db.all(
            """SELECT a.*, s.name AS service, t.name AS therapist FROM appointments a
               JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id
               WHERE a.customer_id=? ORDER BY a.start_at DESC""", (cid,))
        return c

    @app.put("/api/customers/{cid}")
    def edit_customer(cid: int, b: CustomerIn, _=Depends(current_user)):
        if not db.one("SELECT 1 FROM customers WHERE id=?", (cid,)):
            raise HTTPException(404, "Customer not found")
        if db.one("SELECT 1 FROM customers WHERE phone=? AND id<>?", (b.phone, cid)):
            raise HTTPException(409, "A customer with this phone already exists")
        db.run("UPDATE customers SET name=?,phone=?,email=?,gender=?,dob=?,address=?,notes=? WHERE id=?",
               (b.name, b.phone, b.email, b.gender, b.dob, b.address, b.notes, cid))
        return {"id": cid}

    # ---- therapists & services (admin-managed) -------------------------
    @app.get("/api/therapists")
    def therapists(_=Depends(current_user)):
        return db.all("SELECT * FROM therapists ORDER BY name")

    @app.post("/api/therapists", status_code=201)
    def add_therapist(b: TherapistIn, _=Depends(admin)):
        return {"id": db.run("""INSERT INTO therapists(name,phone,fixed_salary,incentive_percent,is_active,joined_on)
                                VALUES(?,?,?,?,?,?)""",
                             (b.name, b.phone, b.fixed_salary, b.incentive_percent, int(b.is_active), b.joined_on))}

    @app.put("/api/therapists/{tid}")
    def edit_therapist(tid: int, b: TherapistIn, _=Depends(admin)):
        if not db.one("SELECT 1 FROM therapists WHERE id=?", (tid,)):
            raise HTTPException(404, "Therapist not found")
        db.run("""UPDATE therapists SET name=?,phone=?,fixed_salary=?,incentive_percent=?,is_active=?,joined_on=?
                  WHERE id=?""", (b.name, b.phone, b.fixed_salary, b.incentive_percent, int(b.is_active),
                                  b.joined_on, tid))
        return {"id": tid}

    @app.get("/api/services")
    def services(_=Depends(current_user)):
        return db.all("SELECT * FROM services ORDER BY name")

    @app.post("/api/services", status_code=201)
    def add_service(b: ServiceIn, _=Depends(admin)):
        if db.one("SELECT 1 FROM services WHERE name=?", (b.name,)):
            raise HTTPException(409, "Service already exists")
        return {"id": db.run("INSERT INTO services(name,price,duration_min,is_active) VALUES(?,?,?,?)",
                             (b.name, b.price, b.duration_min, int(b.is_active)))}

    @app.put("/api/services/{sid}")
    def edit_service(sid: int, b: ServiceIn, _=Depends(admin)):
        if not db.one("SELECT 1 FROM services WHERE id=?", (sid,)):
            raise HTTPException(404, "Service not found")
        db.run("UPDATE services SET name=?,price=?,duration_min=?,is_active=? WHERE id=?",
               (b.name, b.price, b.duration_min, int(b.is_active), sid))
        return {"id": sid}

    # ---- appointments ---------------------------------------------------
    def insert_appointment(b: AppointmentIn) -> int:
        try:
            start = dt.datetime.fromisoformat(b.start_at)
        except ValueError:
            raise HTTPException(422, "start_at must be YYYY-MM-DDTHH:MM")
        start_s = start.strftime("%Y-%m-%dT%H:%M")
        if not db.one("SELECT 1 FROM customers WHERE id=?", (b.customer_id,)):
            raise HTTPException(404, "Customer not found")
        t = db.one("SELECT * FROM therapists WHERE id=? AND is_active=1", (b.therapist_id,))
        s = db.one("SELECT * FROM services WHERE id=? AND is_active=1", (b.service_id,))
        if not t or not s:
            raise HTTPException(404, "Therapist or service not found (or inactive)")
        end = (start + dt.timedelta(minutes=s["duration_min"])).strftime("%Y-%m-%dT%H:%M")
        clash = db.one(
            """SELECT 1 FROM appointments WHERE therapist_id=? AND status IN('booked','completed')
               AND start_at<? AND strftime('%Y-%m-%dT%H:%M', start_at, '+'||duration_min||' minutes')>?""",
            (t["id"], end, start_s))
        if clash:
            raise HTTPException(409, "Therapist already has an appointment at that time")
        price = s["price"] if b.price is None else b.price
        return db.run("""INSERT INTO appointments(customer_id,therapist_id,service_id,start_at,duration_min,
                         price,status,notes,created_at) VALUES(?,?,?,?,?,?,'booked',?,?)""",
                      (b.customer_id, t["id"], s["id"], start_s, s["duration_min"], price, b.notes, now()))

    @app.get("/api/appointments")
    def appointments(date: Optional[str] = None, therapist_id: Optional[int] = None,
                     status: Optional[Status] = None, _=Depends(current_user)):
        sql = """SELECT a.*, c.name AS customer, c.phone, s.name AS service, t.name AS therapist
                 FROM appointments a JOIN customers c ON c.id=a.customer_id
                 JOIN services s ON s.id=a.service_id JOIN therapists t ON t.id=a.therapist_id WHERE 1=1"""
        args = []
        if date:
            sql += " AND substr(a.start_at,1,10)=?"; args.append(date)
        if therapist_id:
            sql += " AND a.therapist_id=?"; args.append(therapist_id)
        if status:
            sql += " AND a.status=?"; args.append(status)
        return db.all(sql + " ORDER BY a.start_at LIMIT 500", args)

    @app.post("/api/appointments", status_code=201)
    def add_appointment(b: AppointmentIn, _=Depends(current_user)):
        return {"id": insert_appointment(b)}

    @app.patch("/api/appointments/{aid}/status")
    def set_status(aid: int, b: StatusIn, _=Depends(current_user)):
        if not db.one("SELECT 1 FROM appointments WHERE id=?", (aid,)):
            raise HTTPException(404, "Appointment not found")
        db.run("UPDATE appointments SET status=? WHERE id=?", (b.status, aid))
        return {"id": aid, "status": b.status}

    # ---- onboarding: customer (+ optional first booking) in one call ----
    @app.post("/api/onboarding", status_code=201)
    def onboard(b: OnboardIn, _=Depends(current_user)):
        if b.first_appointment:  # validate first so nothing is half-created
            fa = b.first_appointment
            if not db.one("SELECT 1 FROM therapists WHERE id=? AND is_active=1", (fa.therapist_id,)) or \
               not db.one("SELECT 1 FROM services WHERE id=? AND is_active=1", (fa.service_id,)):
                raise HTTPException(404, "Therapist or service not found (or inactive)")
        cid = insert_customer(b)
        out = {"customer_id": cid, "appointment_id": None}
        if b.first_appointment:
            fa = b.first_appointment.model_copy(update={"customer_id": cid})
            try:
                out["appointment_id"] = insert_appointment(fa)
            except HTTPException:
                db.run("DELETE FROM customers WHERE id=?", (cid,))
                raise
        return out

    # ---- reports & payroll (admin) --------------------------------------
    def check_month(month: str):
        if not MONTH.match(month):
            raise HTTPException(422, "month must be YYYY-MM")

    @app.get("/api/reports/summary")
    def summary(month: str = Query(...), _=Depends(admin)):
        check_month(month)
        lo, hi = payroll.month_bounds(month)
        by_status = db.all("""SELECT status, COUNT(*) AS count, ROUND(SUM(price),2) AS amount
                              FROM appointments WHERE start_at BETWEEN ? AND ? GROUP BY status""", (lo, hi))
        by_service = db.all("""SELECT s.name AS service, COUNT(*) AS count, ROUND(SUM(a.price),2) AS revenue
                               FROM appointments a JOIN services s ON s.id=a.service_id
                               WHERE a.status='completed' AND a.start_at BETWEEN ? AND ?
                               GROUP BY s.id ORDER BY revenue DESC""", (lo, hi))
        new_c = db.one("SELECT COUNT(*) AS n FROM customers WHERE substr(created_at,1,7)=?", (month,))["n"]
        rev = sum(r["revenue"] for r in by_service)
        return {"month": month, "revenue": round(rev, 2), "new_customers": new_c,
                "by_status": by_status, "by_service": by_service}

    @app.get("/api/reports/incentives")
    def incentives(month: str = Query(...), _=Depends(admin)):
        check_month(month)
        rows = [payroll.compute_payslip(db, t, month) for t in
                db.all("SELECT * FROM therapists WHERE is_active=1 ORDER BY name")]
        for r in rows:
            r.pop("lines")
        return {"month": month, "therapists": rows,
                "total_incentive": round(sum(r["incentive"] for r in rows), 2),
                "total_payroll": round(sum(r["net_pay"] for r in rows), 2)}

    def payslip_for(tid: int, month: str):
        check_month(month)
        t = db.one("SELECT * FROM therapists WHERE id=?", (tid,))
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
        return {"ok": True}

    return app



