import os
from datetime import datetime
from zoneinfo import ZoneInfo
import threading
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient

from app.main import create_app

DSN = os.environ.get("TEST_PG_DSN")  # e.g. postgresql://spa:spa@localhost:5432/spa (needs CREATE SCHEMA rights)


def spa_today() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


@unittest.skipUnless(DSN, "set TEST_PG_DSN to run the tests against a Postgres database")
class Base(unittest.TestCase):
    def setUp(self):
        self.schema = "t_" + uuid.uuid4().hex[:12]  # every test gets its own throwaway schema
        self.app = create_app(DSN, self.schema)
        self.c = TestClient(self.app)
        tok = self.c.post("/api/auth/login", json={"username": "admin", "password": "admin12345"}).json()["token"]
        self.h = {"Authorization": f"Bearer {tok}"}

    def tearDown(self):
        self.app.state.db.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def post(self, url, **j):
        return self.c.post(url, json=j, headers=self.h)

    def seed(self, pct=10, salary=20000):
        t = self.post("/api/therapists", name="Asha", fixed_salary=salary, incentive_percent=pct).json()["id"]
        s = self.post("/api/services", name="Massage", price=2000, duration_min=60).json()["id"]
        c = self.post("/api/customers", name="Ravi", phone="9990001111").json()["id"]
        return t, s, c


class TestAuth(Base):
    def test_requires_login(self):
        self.assertEqual(self.c.get("/api/customers").status_code, 401)

    def test_bad_password(self):
        r = self.c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_logout_kills_token(self):
        self.assertEqual(self.c.post("/api/auth/logout", headers=self.h).status_code, 204)
        self.assertEqual(self.c.get("/api/auth/me", headers=self.h).status_code, 401)

    def test_staff_cannot_see_payroll(self):
        self.post("/api/users", username="front", password="frontdesk1", role="staff")
        tok = self.c.post("/api/auth/login", json={"username": "front", "password": "frontdesk1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.c.get("/api/reports/incentives?month=2026-11", headers=h).status_code, 403)
        self.assertEqual(self.c.get("/api/customers", headers=h).status_code, 200)


class TestFlow(Base):
    def test_duplicate_phone(self):
        self.seed()
        self.assertEqual(self.post("/api/customers", name="X", phone="9990001111").status_code, 409)

    def test_overlap_rejected_but_back_to_back_ok(self):
        t, s, c = self.seed()
        a = dict(customer_id=c, therapist_id=t, service_id=s)
        self.assertEqual(self.post("/api/appointments", start_at="2026-11-10T10:00", **a).status_code, 201)
        self.assertEqual(self.post("/api/appointments", start_at="2026-11-10T10:30", **a).status_code, 409)
        self.assertEqual(self.post("/api/appointments", start_at="2026-11-10T11:00", **a).status_code, 201)

    def test_cancelled_frees_slot(self):
        t, s, c = self.seed()
        a = dict(customer_id=c, therapist_id=t, service_id=s, start_at="2026-11-10T10:00")
        aid = self.post("/api/appointments", **a).json()["id"]
        self.c.patch(f"/api/appointments/{aid}/status", json={"status": "cancelled"}, headers=self.h)
        self.assertEqual(self.post("/api/appointments", **a).status_code, 201)

    def test_onboarding_with_first_appointment(self):
        t, s, _ = self.seed()
        r = self.post("/api/onboarding", name="Meena", phone="8880002222",
                      first_appointment=dict(customer_id=0, therapist_id=t, service_id=s,
                                             start_at="2026-11-12T15:00"))
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertIsNotNone(body["appointment_id"])
        det = self.c.get(f"/api/customers/{body['customer_id']}", headers=self.h).json()
        self.assertEqual(len(det["appointments"]), 1)

    def test_onboarding_rolls_back_on_clash(self):
        t, s, c = self.seed()
        self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s, start_at="2026-11-12T15:00")
        r = self.post("/api/onboarding", name="Meena", phone="8880002222",
                      first_appointment=dict(customer_id=0, therapist_id=t, service_id=s,
                                             start_at="2026-11-12T15:00"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.c.get("/api/customers?q=8880002222", headers=self.h).json()), 0)


class TestPayroll(Base):
    def test_salary_plus_incentive_only_completed(self):
        t, s, c = self.seed(pct=10, salary=20000)
        ids = []
        for hr in ("09", "11", "13", "15"):
            ids.append(self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                                 start_at=f"2026-11-10T{hr}:00").json()["id"])
        for i, st in zip(ids, ["completed", "completed", "cancelled", "booked"]):
            self.c.patch(f"/api/appointments/{i}/status", json={"status": st}, headers=self.h)
        # a completed one in another month must not count
        o = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-10-31T10:00").json()["id"]
        self.c.patch(f"/api/appointments/{o}/status", json={"status": "completed"}, headers=self.h)
        p = self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()
        self.assertEqual((p["services_count"], p["service_revenue"], p["incentive"], p["net_pay"]),
                         (2, 4000.0, 400.0, 20400.0))
        inc = self.c.get("/api/reports/incentives?month=2026-11", headers=self.h).json()
        self.assertEqual(inc["total_payroll"], 20400.0)

    def test_extra_incentives(self):
        t, s, c = self.seed(pct=10, salary=20000)
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        b1 = self.post("/api/bonuses", therapist_id=t, on_date="2026-11-14", amount=500, note="Festival bonus")
        self.assertEqual(b1.status_code, 201)
        self.post("/api/bonuses", therapist_id=t, on_date="2026-11-10", amount=150, appointment_id=a)
        self.post("/api/bonuses", therapist_id=t, on_date="2026-12-01", amount=999)  # next month
        p = self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()
        self.assertEqual((p["incentive"], p["extra_incentive"], p["net_pay"], len(p["bonuses"])), (200.0, 650.0, 20850.0, 2))
        self.assertTrue(self.c.get(f"/api/payroll/{t}/2026-11/pdf", headers=self.h).content.startswith(b"%PDF"))
        inc = self.c.get("/api/reports/incentives?month=2026-11", headers=self.h).json()
        self.assertEqual((inc["total_incentive"], inc["total_payroll"]), (850.0, 20850.0))
        listed = self.c.get(f"/api/bonuses?month=2026-11&therapist_id={t}", headers=self.h).json()
        self.assertEqual([x["amount"] for x in listed], [500.0, 150.0])
        self.assertEqual(self.c.delete(f"/api/bonuses/{b1.json()['id']}", headers=self.h).status_code, 204)
        self.assertEqual(self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()["extra_incentive"], 150.0)
        self.assertEqual(self.post("/api/bonuses", therapist_id=t, on_date="2026-11-14", amount=0).status_code, 422)

    def test_price_override_and_snapshot(self):
        t, s, c = self.seed(pct=10)
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00", price=1500).json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        self.c.put(f"/api/services/{s}", json=dict(name="Massage", price=9999), headers=self.h)
        p = self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()
        self.assertEqual(p["service_revenue"], 1500.0)

    def test_pdf(self):
        t, s, c = self.seed()
        r = self.c.get(f"/api/payroll/{t}/2026-11/pdf", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_bad_month(self):
        t, *_ = self.seed()
        self.assertEqual(self.c.get(f"/api/payroll/{t}/2026-13", headers=self.h).status_code, 422)

    def test_duration_override(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00", duration_min=90).json()["id"]
        # 90 minutes: 10:00 now clashes, 10:30 does not
        self.assertEqual(self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                                   start_at="2026-11-10T10:00").status_code, 409)
        self.assertEqual(self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                                   start_at="2026-11-10T10:30").status_code, 201)
        body = dict(customer_id=c, therapist_id=t, service_id=s, start_at="2026-11-10T08:00")
        self.assertEqual(self.c.put(f"/api/appointments/{a}", json=body, headers=self.h).status_code, 200)
        row = [x for x in self.c.get("/api/appointments?date=2026-11-10", headers=self.h).json() if x["id"] == a][0]
        self.assertEqual(row["duration_min"], 90)  # kept when not given
        self.c.put(f"/api/appointments/{a}", json={**body, "duration_min": 45}, headers=self.h)
        row = [x for x in self.c.get("/api/appointments?date=2026-11-10", headers=self.h).json() if x["id"] == a][0]
        self.assertEqual(row["duration_min"], 45)

    def test_summary(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        r = self.c.get("/api/reports/summary?month=2026-11", headers=self.h).json()
        self.assertEqual(r["revenue"], 2000.0)

    def test_summary_details_by_therapist(self):
        t, s, c = self.seed()
        t2 = self.post("/api/therapists", name="Meena", fixed_salary=0, incentive_percent=5).json()["id"]
        s2 = self.post("/api/services", name="Facial", duration_min=30, price=800).json()["id"]
        done = [(t, s, "09"), (t, s2, "11"), (t2, s, "09"), (t, s, "13")]
        for th, sv, hr in done:
            a = self.post("/api/appointments", customer_id=c, therapist_id=th, service_id=sv,
                          start_at=f"2026-11-12T{hr}:00").json()["id"]
            self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        # booked (not done) does not count as revenue
        self.post("/api/appointments", customer_id=c, therapist_id=t2, service_id=s2, start_at="2026-11-13T09:00")
        r = self.c.get("/api/reports/summary?month=2026-11", headers=self.h).json()
        self.assertEqual((r["revenue"], r["services_count"], len(r["lines"])), (6800.0, 4, 4))
        first = r["by_therapist"][0]
        self.assertEqual((first["therapist_id"], first["count"], first["revenue"]), (t, 3, 4800.0))
        self.assertEqual([(x["service"], x["count"], x["revenue"]) for x in first["services"]],
                         [("Massage", 2, 4000.0), ("Facial", 1, 800.0)])
        self.assertEqual({x["therapist"] for x in r["lines"]}, {"Asha", "Meena"})
        one = self.c.get(f"/api/reports/summary?month=2026-11&therapist_id={t2}", headers=self.h).json()
        self.assertEqual((one["revenue"], one["services_count"], len(one["by_therapist"])), (2000.0, 1, 1))
        self.assertEqual(sum(x["count"] for x in one["by_status"]), 2)  # the booked one shows in by_status
        for q in ("", f"&therapist_id={t2}"):
            pdf = self.c.get(f"/api/reports/summary/pdf?month=2026-11{q}", headers=self.h)
            self.assertEqual(pdf.status_code, 200)
            self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("Meena", self.c.get(f"/api/reports/summary/pdf?month=2026-11&therapist_id={t2}",
                                          headers=self.h).headers["content-disposition"])
        rng = self.c.get("/api/reports/summary?from=2026-11-12&to=2026-11-12", headers=self.h).json()
        self.assertEqual((rng["revenue"], rng["from"], rng["to"]), (6800.0, "2026-11-12", "2026-11-12"))
        self.assertEqual(self.c.get("/api/reports/summary?from=2026-11-13&to=2026-11-30", headers=self.h).json()["revenue"], 0.0)
        self.assertEqual(self.c.get("/api/reports/summary?from=2026-11-13", headers=self.h).status_code, 422)
        self.assertEqual(self.c.get("/api/reports/summary?from=2026-11-13&to=2026-11-01", headers=self.h).status_code, 422)
        self.assertTrue(self.c.get("/api/reports/summary/pdf?from=2026-11-01&to=2026-11-15", headers=self.h).content.startswith(b"%PDF"))
        by_svc = self.c.get(f"/api/reports/summary?month=2026-11&service_id={s2}", headers=self.h).json()
        self.assertEqual((by_svc["revenue"], by_svc["services_count"]), (800.0, 1))
        by_cust = self.c.get(f"/api/reports/summary?month=2026-11&customer_id={c}&therapist_id={t}", headers=self.h).json()
        self.assertEqual(by_cust["services_count"], 3)
        csv_ = self.c.get(f"/api/reports/summary/csv?month=2026-11&therapist_id={t}", headers=self.h)
        self.assertEqual(csv_.status_code, 200)
        text = csv_.content.decode("utf-8-sig").splitlines()
        self.assertEqual(text[0], "Date,Time,Customer,Service,Therapist,Minutes,Price")
        self.assertEqual(len(text), 6)
        self.assertTrue(text[-2].startswith("Total,,,,,") and text[-2].endswith(",4800.00"))
        rep = self.c.get(f"/api/reports/summary?month=2026-11&therapist_id={t}", headers=self.h).json()
        self.assertEqual(rep["minutes"], sum(x["duration_min"] for x in rep["lines"]))
        self.assertEqual(rep["by_therapist"][0]["minutes"], rep["minutes"])
        self.assertIn("Asha", csv_.headers["content-disposition"])
        self.assertEqual(self.c.get("/api/reports/summary/pdf?month=2026-11&therapist_id=99999",
                                    headers=self.h).status_code, 404)


class TestCors(Base):
    def test_ui_origin_allowed(self):
        r = self.c.options("/api/auth/login", headers={"Origin": "http://localhost:4200",
                           "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://localhost:4200")


class TestEdits(Base):
    def put(self, url, **j):
        return self.c.put(url, json=j, headers=self.h)

    def test_edit_customer_and_phone_conflict(self):
        _, _, c = self.seed()
        d = self.post("/api/customers", name="Other", phone="7770001111").json()["id"]
        self.assertEqual(self.put(f"/api/customers/{c}", name="Ravi K", phone="9990001111").status_code, 200)
        self.assertEqual(self.c.get(f"/api/customers/{c}", headers=self.h).json()["name"], "Ravi K")
        self.assertEqual(self.put(f"/api/customers/{d}", name="Other", phone="9990001111").status_code, 409)

    def test_edit_therapist_changes_future_pay_rule(self):
        t, *_ = self.seed()
        self.put(f"/api/therapists/{t}", name="Asha", fixed_salary=25000, incentive_percent=12, is_active=True)
        p = self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()
        self.assertEqual((p["fixed_salary"], p["incentive_percent"]), (25000.0, 12.0))

    def test_edit_appointment_reschedule_keeps_price_and_ignores_self_clash(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T10:00", price=1500).json()["id"]
        body = dict(customer_id=c, therapist_id=t, service_id=s, notes="moved")
        # overlaps its own old slot: must be allowed
        self.assertEqual(self.put(f"/api/appointments/{a}", start_at="2026-11-10T10:30", **body).status_code, 200)
        row = self.c.get("/api/appointments?date=2026-11-10", headers=self.h).json()[0]
        self.assertEqual((row["start_at"], row["price"], row["notes"]), ("2026-11-10T10:30", 1500.0, "moved"))

    def test_edit_appointment_clash_and_service_price(self):
        t, s, c = self.seed()
        s2 = self.post("/api/services", name="Facial", price=800, duration_min=30).json()["id"]
        self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s, start_at="2026-11-10T10:00")
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T12:00").json()["id"]
        body = dict(customer_id=c, therapist_id=t, service_id=s)
        self.assertEqual(self.put(f"/api/appointments/{a}", start_at="2026-11-10T10:30", **body).status_code, 409)
        body["service_id"] = s2  # new service -> its list price
        self.assertEqual(self.put(f"/api/appointments/{a}", start_at="2026-11-10T12:00", **body).status_code, 200)
        rows = self.c.get("/api/appointments?date=2026-11-10", headers=self.h).json()
        self.assertEqual([r["price"] for r in rows if r["id"] == a], [800.0])

    def test_completed_appointment_cannot_be_edited(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T10:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        r = self.put(f"/api/appointments/{a}", customer_id=c, therapist_id=t, service_id=s, start_at="2026-11-10T11:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.put("/api/appointments/999", customer_id=c, therapist_id=t, service_id=s,
                                  start_at="2026-11-10T11:00").status_code, 404)


class TestUsers(Base):
    def login(self, u, p):
        r = self.c.post("/api/auth/login", json={"username": u, "password": p})
        return r, {"Authorization": f"Bearer {r.json().get('token', '')}"}

    def make_staff(self, name="front", pw="frontdesk1"):
        return self.post("/api/users", username=name, password=pw, role="staff").json()["id"]

    def test_list_users_admin_only_and_hides_hashes(self):
        self.make_staff()
        rows = self.c.get("/api/users", headers=self.h).json()
        self.assertEqual({r["username"] for r in rows}, {"admin", "front"})
        self.assertTrue(all("password_hash" not in r for r in rows))
        _, h = self.login("front", "frontdesk1")
        self.assertEqual(self.c.get("/api/users", headers=h).status_code, 403)

    def test_deactivate_user_blocks_login_and_kills_session(self):
        uid = self.make_staff()
        _, h = self.login("front", "frontdesk1")
        self.assertEqual(self.c.get("/api/customers", headers=h).status_code, 200)
        r = self.c.put(f"/api/users/{uid}", json={"role": "staff", "is_active": False}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/customers", headers=h).status_code, 401)
        self.assertEqual(self.login("front", "frontdesk1")[0].status_code, 401)

    def test_admin_manages_staff_only(self):
        uid = self.make_staff()
        r = self.c.put(f"/api/users/{uid}", json={"role": "admin", "is_active": True}, headers=self.h)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.post("/api/users", username="boss2", password="boss12345", role="admin").status_code, 403)

    def test_cannot_lock_yourself_out(self):
        me = self.c.get("/api/auth/me", headers=self.h).json()["id"]
        for body in ({"role": "staff", "is_active": True}, {"role": "admin", "is_active": False}):
            self.assertIn(self.c.put(f"/api/users/{me}", json=body, headers=self.h).status_code, (400, 403))

    def test_admin_reset_password_revokes_sessions(self):
        uid = self.make_staff()
        _, h = self.login("front", "frontdesk1")
        r = self.c.post(f"/api/users/{uid}/password", json={"password": "brandnew99"}, headers=self.h)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.c.get("/api/customers", headers=h).status_code, 401)
        self.assertEqual(self.login("front", "frontdesk1")[0].status_code, 401)
        self.assertEqual(self.login("front", "brandnew99")[0].status_code, 200)
        self.assertEqual(self.c.post("/api/users/999/password", json={"password": "brandnew99"}, headers=self.h).status_code, 404)

    def test_change_own_password(self):
        self.make_staff()
        _, h1 = self.login("front", "frontdesk1")
        _, h2 = self.login("front", "frontdesk1")  # another device
        bad = self.c.post("/api/auth/password", json={"current_password": "x", "new_password": "newpass123"}, headers=h1)
        self.assertEqual(bad.status_code, 400)
        short = self.c.post("/api/auth/password", json={"current_password": "frontdesk1", "new_password": "short"}, headers=h1)
        self.assertEqual(short.status_code, 422)
        ok = self.c.post("/api/auth/password", json={"current_password": "frontdesk1", "new_password": "newpass123"}, headers=h1)
        self.assertEqual(ok.status_code, 204)
        self.assertEqual(self.c.get("/api/auth/me", headers=h1).status_code, 200)   # this device stays in
        self.assertEqual(self.c.get("/api/auth/me", headers=h2).status_code, 401)   # others signed out
        self.assertEqual(self.login("front", "newpass123")[0].status_code, 200)


class TestPostgres(Base):
    def booking(self, t, s, c, start="2026-11-10T10:00"):
        return self.c.post("/api/appointments", headers=self.h,
                           json=dict(customer_id=c, therapist_id=t, service_id=s, start_at=start))

    def test_concurrent_bookings_only_one_wins(self):
        """The database, not the app, refuses a double booking - so it holds across processes too."""
        t, s, c = self.seed()
        codes, gate = [], threading.Barrier(8)

        def go():
            gate.wait()
            codes.append(self.booking(t, s, c).status_code)
        th = [threading.Thread(target=go) for _ in range(8)]
        [x.start() for x in th]; [x.join() for x in th]
        self.assertEqual(sorted(codes), [201] + [409] * 7)

    def test_reopening_a_cancelled_slot_someone_took_is_refused(self):
        t, s, c = self.seed()
        a = self.booking(t, s, c).json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "cancelled"}, headers=self.h)
        self.assertEqual(self.booking(t, s, c).status_code, 201)  # someone else takes the freed slot
        r = self.c.patch(f"/api/appointments/{a}/status", json={"status": "booked"}, headers=self.h)
        self.assertEqual(r.status_code, 409)

    def test_username_is_case_insensitive(self):
        self.assertEqual(self.post("/api/users", username="ADMIN", password="whatever123").status_code, 409)
        r = self.c.post("/api/auth/login", json={"username": "Admin", "password": "admin12345"})
        self.assertEqual(r.status_code, 200)

    def test_service_name_unique_ignoring_case(self):
        self.post("/api/services", name="Massage", price=1)
        self.assertEqual(self.post("/api/services", name="massage", price=2).status_code, 409)

    def test_bad_dates_are_422_not_500(self):
        r = self.post("/api/customers", name="X", phone="5551234567", dob="31-12-1990")
        self.assertEqual(r.status_code, 422)
        ok = self.post("/api/customers", name="X", phone="5551234567", dob="1990-12-31")
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(self.c.get(f"/api/customers/{ok.json()['id']}", headers=self.h).json()["dob"], "1990-12-31")

    def test_search_is_case_insensitive_and_escapes_wildcards(self):
        self.seed()
        self.assertEqual(len(self.c.get("/api/customers?q=rav", headers=self.h).json()), 1)
        self.assertEqual(len(self.c.get("/api/customers?q=%25", headers=self.h).json()), 0)

    def test_money_roundtrips_exactly(self):
        t, s, c = self.seed(pct=7.5)
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00", price=333.33).json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        p = self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()
        self.assertEqual((p["service_revenue"], p["incentive"]), (333.33, 25.0))  # 24.99975 -> 25.00

    def test_month_boundaries_are_half_open(self):
        t, s, c = self.seed()
        for start in ("2026-10-31T23:00", "2026-11-01T00:00", "2026-11-30T23:00", "2026-12-01T00:00"):
            a = self.booking(t, s, c, start).json()["id"]
            self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        self.assertEqual(self.c.get(f"/api/payroll/{t}/2026-11", headers=self.h).json()["services_count"], 2)

    def test_december_rolls_into_next_year(self):
        t, s, c = self.seed()
        a = self.booking(t, s, c, "2026-12-31T20:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        self.assertEqual(self.c.get(f"/api/payroll/{t}/2026-12", headers=self.h).json()["services_count"], 1)

    def test_timestamps_render_as_iso_strings(self):
        _, _, c = self.seed()
        row = self.c.get(f"/api/customers/{c}", headers=self.h).json()
        self.assertRegex(row["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_schemas_are_isolated_and_migrations_idempotent(self):
        from app.migrate import migrate
        self.assertEqual(migrate(DSN, self.schema), [])  # already applied
        other = "t_" + uuid.uuid4().hex[:12]
        app2 = create_app(DSN, other)
        try:
            self.seed()
            self.assertEqual(app2.state.db.one("SELECT COUNT(*) AS n FROM customers")["n"], 0)
        finally:
            app2.state.db.close()
            with psycopg.connect(DSN, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {other} CASCADE")

    def test_health_checks_the_database(self):
        self.assertEqual(self.c.get("/api/health").json(), {"ok": True})

    def test_rejects_unsafe_schema_name(self):
        with self.assertRaises(ValueError):
            create_app(DSN, "x; drop table users")


if __name__ == "__main__":
    unittest.main()


class TestTenancy(Base):
    def sa(self):
        tok = self.c.post("/api/auth/login", json={"username": "superadmin", "password": "superadmin123"}).json()["token"]
        return {"Authorization": f"Bearer {tok}"}

    def test_spas_are_isolated(self):
        sa = self.sa()
        self.seed()
        r = self.c.post("/api/spas", json={"name": "Lotus", "admin_username": "lotus", "admin_password": "lotus12345"},
                        headers=sa)
        self.assertEqual(r.status_code, 201, r.text)
        spa2 = r.json()["id"]
        tok = self.c.post("/api/auth/login", json={"username": "lotus", "password": "lotus12345"}).json()["token"]
        h2 = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.c.get("/api/customers", headers=h2).json(), [])
        self.assertEqual(self.c.get("/api/therapists", headers=h2).json(), [])
        # same phone is fine in another spa
        self.assertEqual(self.c.post("/api/customers", json={"name": "R", "phone": "9990001111"}, headers=h2).status_code, 201)
        cid1 = self.c.get("/api/customers", headers=self.h).json()[0]["id"]
        self.assertEqual(self.c.get(f"/api/customers/{cid1}", headers=h2).status_code, 404)
        # the lotus admin sees only its own logins
        self.assertEqual({u["username"] for u in self.c.get("/api/users", headers=h2).json()}, {"lotus"})
        # superadmin picks a spa with the header
        self.assertEqual(self.c.get("/api/customers", headers=sa).status_code, 400)
        self.assertEqual(len(self.c.get("/api/customers", headers={**sa, "X-Spa-Id": str(spa2)}).json()), 1)
        self.assertEqual(len(self.c.get("/api/spas", headers=sa).json()), 2)
        self.assertEqual([p["id"] for p in self.c.get("/api/spas", headers=h2).json()], [spa2])

    def test_deactivated_spa_blocks_its_users(self):
        sa = self.sa()
        spa2 = self.c.post("/api/spas", json={"name": "Lotus", "admin_username": "lotus", "admin_password": "lotus12345"},
                           headers=sa).json()["id"]
        self.assertEqual(self.c.put(f"/api/spas/{spa2}", json={"name": "Lotus", "is_active": False}, headers=sa).status_code, 200)
        r = self.c.post("/api/auth/login", json={"username": "lotus", "password": "lotus12345"})
        self.assertEqual(r.status_code, 401)


class TestTherapistDetails(Base):
    def test_details_saved_and_edited(self):
        tid = self.post("/api/therapists", name="Asha", fixed_salary=1, incentive_percent=1,
                        specialities="Thai", bank_ifsc="HDFC0001", experience_years=3.5).json()["id"]
        row = next(t for t in self.c.get("/api/therapists", headers=self.h).json() if t["id"] == tid)
        self.assertEqual((row["specialities"], row["bank_ifsc"], float(row["experience_years"])), ("Thai", "HDFC0001", 3.5))
        self.c.put(f"/api/therapists/{tid}", json={"name": "Asha", "fixed_salary": 1, "incentive_percent": 1,
                                                    "upi_id": "asha@upi", "specialities": " "}, headers=self.h)
        row = next(t for t in self.c.get("/api/therapists", headers=self.h).json() if t["id"] == tid)
        self.assertEqual((row["upi_id"], row["specialities"]), ("asha@upi", None))


class TestTherapistShifts(Base):
    def create_shift(self, name="Day shift", start_time="09:00", end_time="17:00"):
        return self.post("/api/shifts", name=name, start_time=start_time, end_time=end_time).json()["id"]

    def assign(self, therapist, shift, weekday):
        return self.post("/api/shift-assignments", therapist_id=therapist, shift_id=shift,
                         day_of_week=weekday)

    def test_assigned_shift_restricts_booking_window_and_weekday(self):
        t, s, c = self.seed()
        shift = self.create_shift()
        assigned = self.assign(t, shift, 4)  # Friday
        self.assertEqual(assigned.status_code, 201, assigned.text)

        inside = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                           start_at="2026-11-20T10:00")
        self.assertEqual(inside.status_code, 201, inside.text)

        before = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                           start_at="2026-11-20T08:30")
        self.assertEqual(before.status_code, 409)

        wrong_day = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                              start_at="2026-11-21T10:00")
        self.assertEqual(wrong_day.status_code, 409)

    def test_appointment_must_finish_before_shift_end(self):
        t, s, c = self.seed()
        shift = self.create_shift("Short", "09:00", "11:00")
        self.assign(t, shift, 4)
        too_long = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                             start_at="2026-11-20T10:30", duration_min=60)
        self.assertEqual(too_long.status_code, 409)

    def test_weekday_assignment_is_upserted_not_duplicated(self):
        t, _, _ = self.seed()
        morning = self.create_shift("Morning", "09:00", "13:00")
        evening = self.create_shift("Evening", "14:00", "20:00")
        first = self.assign(t, morning, 0)
        second = self.assign(t, evening, 0)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        rows = self.c.get(f"/api/shift-assignments?therapist_id={t}", headers=self.h).json()
        monday = [x for x in rows if x["day_of_week"] == 0]
        self.assertEqual(len(monday), 1)
        self.assertEqual(monday[0]["shift_id"], evening)

    def test_shift_report_shows_weekly_roster_and_completed_work(self):
        t, s, c = self.seed()
        shift = self.create_shift("Friday", "09:00", "17:00")
        self.assign(t, shift, 4)
        aid = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                        start_at="2026-11-20T10:00", duration_min=90).json()["id"]
        completed = self.c.patch(f"/api/appointments/{aid}/status",
                                 json={"status": "completed"}, headers=self.h)
        self.assertEqual(completed.status_code, 200, completed.text)

        report = self.c.get("/api/reports/shifts?month=2026-11", headers=self.h)
        self.assertEqual(report.status_code, 200, report.text)
        body = report.json()
        self.assertEqual(body["summary"]["shift_templates"], 1)
        self.assertEqual(body["summary"]["assigned_therapists"], 1)
        self.assertEqual(body["summary"]["completed"], 1)
        self.assertEqual(body["summary"]["completed_minutes"], 90)
        self.assertEqual(body["assignments"][0]["day_name"], "Friday")

    def test_staff_cannot_create_or_assign_shifts_or_view_shift_reports(self):
        t, _, _ = self.seed()
        self.post("/api/users", username="shiftstaff", password="shiftstaff1", role="staff")
        token = self.c.post("/api/auth/login", json={"username": "shiftstaff", "password": "shiftstaff1"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        create = self.c.post("/api/shifts",
                             json={"name": "Day", "start_time": "09:00", "end_time": "17:00"},
                             headers=headers)
        self.assertEqual(create.status_code, 403)
        report = self.c.get("/api/reports/shifts?month=2026-11", headers=headers)
        self.assertEqual(report.status_code, 403)
        self.assertEqual(self.c.get("/api/shift-assignments", headers=headers).status_code, 200)


class TestCustomerPlans(Base):
    def make_appointment(self, therapist, service, customer, **extra):
        body = dict(customer_id=customer, therapist_id=therapist, service_id=service,
                    start_at="2026-11-20T10:00", **extra)
        return self.post("/api/appointments", **body).json()["id"]

    def add_plan(self, customer, **body):
        return self.c.post(f"/api/customers/{customer}/plans", json=body, headers=self.h)

    def complete(self, appointment, plan_type=None):
        return self.c.patch(f"/api/appointments/{appointment}/status",
                            json={"status": "completed", "plan_type": plan_type}, headers=self.h)

    def customer(self, customer):
        return self.c.get(f"/api/customers/{customer}", headers=self.h).json()

    def test_hours_plan_deducted_only_when_completed(self):
        t, s, c = self.seed()
        plan = self.add_plan(c, name="10 hour package", plan_type="hours", amount=10)
        self.assertEqual(plan.status_code, 201, plan.text)
        a = self.make_appointment(t, s, c, duration_min=90)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 10.0)
        done = self.complete(a, "hours")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["plan_deduction"]["amount"], 1.5)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 8.5)
        self.assertEqual(self.customer(c)["appointments"][0]["plan_unit"], "hours")

    def test_rupee_plan_deducts_agreed_appointment_price(self):
        t, s, c = self.seed()
        self.add_plan(c, name="Rs 5000 wallet", plan_type="rupees", amount=5000)
        a = self.make_appointment(t, s, c, price=1250.50)
        done = self.complete(a, "rupees")
        self.assertEqual(done.json()["plan_deduction"]["amount"], 1250.5)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 3749.5)

    def test_no_partial_deduction_when_plan_balance_is_insufficient(self):
        t, s, c = self.seed()
        self.add_plan(c, name="Small wallet", plan_type="rupees", amount=1000)
        a = self.make_appointment(t, s, c, price=1250)
        done = self.complete(a, "rupees")
        self.assertEqual(done.status_code, 409, done.text)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 1000.0)

    def test_completion_is_idempotent_and_reopening_refunds_plan(self):
        t, s, c = self.seed()
        self.add_plan(c, name="Hours", plan_type="hours", amount=4)
        a = self.make_appointment(t, s, c, duration_min=60)
        self.complete(a, "hours")
        self.complete(a, "hours")
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 3.0)
        reopened = self.c.patch(f"/api/appointments/{a}/status", json={"status": "cancelled"}, headers=self.h)
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 4.0)
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed", "plan_type": "hours"}, headers=self.h)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 3.0)

    def test_prepaid_plan_report_splits_sales_balances_and_redemptions(self):
        t, s, c = self.seed()
        self.assertEqual(self.add_plan(c, name="Hours package", plan_type="hours", amount=3).status_code, 201)
        self.assertEqual(self.add_plan(c, name="Rupee wallet", plan_type="rupees", amount=5000).status_code, 201)
        a = self.make_appointment(t, s, c, duration_min=60, price=1250)
        done = self.complete(a, "hours")
        self.assertEqual(done.status_code, 200, done.text)

        r = self.c.get("/api/reports/plans?month=2026-11", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        summary = data["summary"]
        self.assertEqual(summary["plans_added"], 2)
        self.assertEqual(summary["sold_hours"], 3.0)
        self.assertEqual(summary["sold_rupees"], 5000.0)
        self.assertEqual(summary["available_hours"], 2.0)
        self.assertEqual(summary["available_rupees"], 5000.0)
        self.assertEqual(summary["deducted_hours"], 1.0)
        self.assertEqual(summary["deducted_rupees"], 0.0)
        self.assertEqual(len(data["redemptions"]), 1)
        self.assertEqual(data["redemptions"][0]["unit"], "hours")

    def test_staff_cannot_create_plans_or_view_plan_reports(self):
        t, s, c = self.seed()
        self.post("/api/users", username="frontdesk", password="frontdesk1", role="staff")
        tok = self.c.post("/api/auth/login", json={"username": "frontdesk", "password": "frontdesk1"}).json()["token"]
        headers = {"Authorization": f"Bearer {tok}"}
        created = self.c.post(f"/api/customers/{c}/plans",
                              json={"name": "Hours", "plan_type": "hours", "amount": 3}, headers=headers)
        self.assertEqual(created.status_code, 403)
        report = self.c.get("/api/reports/plans?month=2026-11", headers=headers)
        self.assertEqual(report.status_code, 403)

    def test_expired_plan_cannot_be_redeemed(self):
        t, s, c = self.seed()
        self.add_plan(c, name="Expired", plan_type="hours", amount=5, expires_on="2020-01-01")
        a = self.make_appointment(t, s, c)
        done = self.complete(a, "hours")
        self.assertEqual(done.status_code, 409, done.text)
        self.assertEqual(self.customer(c)["plans"][0]["balance"], 5.0)
        self.assertEqual(self.customer(c)["appointments"][0]["status"], "booked")

    def test_plan_is_tenant_scoped(self):
        sa = self.c.post("/api/auth/login", json={"username": "superadmin", "password": "superadmin123"}).json()["token"]
        sa_headers = {"Authorization": f"Bearer {sa}"}
        _, _, c = self.seed()
        r = self.c.post("/api/spas", json={"name": "Lotus", "admin_username": "lotus", "admin_password": "lotus12345"},
                        headers=sa_headers)
        spa2 = r.json()["id"]
        tok = self.c.post("/api/auth/login", json={"username": "lotus", "password": "lotus12345"}).json()["token"]
        h2 = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.c.post(f"/api/customers/{c}/plans",
                         json={"name": "Wrong spa", "plan_type": "hours", "amount": 2}, headers=h2).status_code, 404)


class TestPlanUpdates(Base):
    def test_edit_and_top_up_plan(self):
        _, _, c = self.seed()
        pid = self.post(f"/api/customers/{c}/plans", name="5 hrs", plan_type="hours", amount=5).json()["id"]
        r = self.c.put(f"/api/customers/{c}/plans/{pid}", json={"name": "Gold 5 hrs", "expires_on": "2030-01-01",
                                                                 "notes": "paid cash"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.post(f"/api/customers/{c}/plans/{pid}/topup", amount=3, note="UPI")
        self.assertEqual((r.status_code, r.json()["balance"], r.json()["purchased_amount"]), (201, 8.0, 8.0))
        plan = self.c.get(f"/api/customers/{c}", headers=self.h).json()["plans"][0]
        self.assertEqual((plan["name"], float(plan["balance"]), str(plan["expires_on"])), ("Gold 5 hrs", 8.0, "2030-01-01"))
        month = __import__("datetime").date.today().strftime("%Y-%m")
        rep = self.c.get(f"/api/reports/plans?month={month}", headers=self.h).json()["summary"]
        self.assertEqual((rep["sold_hours"], rep["topped_up_hours"], rep["topups"]), (8.0, 3.0, 1))
        self.assertEqual(self.post(f"/api/customers/{c}/plans/999/topup", amount=1).status_code, 404)
        self.assertEqual(self.post(f"/api/customers/{c}/plans/{pid}/topup", amount=0).status_code, 422)


class TestPlanRevenue(Base):
    def test_plan_sale_is_revenue_and_plan_paid_service_is_not_counted_twice(self):
        import datetime as d
        t, s, c = self.seed()
        self.post(f"/api/customers/{c}/plans", name="Wallet", plan_type="rupees", amount=5000)
        pid = self.post(f"/api/customers/{c}/plans", name="5 hrs", plan_type="hours", amount=5, price_paid=6000).json()["id"]
        self.post(f"/api/customers/{c}/plans/{pid}/topup", amount=2, price_paid=2000)
        day = d.date.today()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at=f"{day}T10:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed", "plan_type": "rupees"}, headers=self.h)
        r = self.c.get(f"/api/reports/summary?month={day:%Y-%m}", headers=self.h).json()
        self.assertEqual((r["revenue"], r["plan_sales"], r["paid_by_plan"], r["collected"]), (2000.0, 13000.0, 2000.0, 13000.0))
        self.assertEqual(len(r["plan_lines"]), 3)
        self.assertEqual(r["profit"], 13000.0 - r["payroll"] - r["expenses"])
        self.assertTrue(self.c.get(f"/api/reports/summary/pdf?month={day:%Y-%m}", headers=self.h).content.startswith(b"%PDF"))
        x = self.c.get(f"/api/reports/summary/xlsx?month={day:%Y-%m}", headers=self.h)
        self.assertEqual(x.status_code, 200)
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(x.content))
        self.assertEqual(wb.sheetnames, ["Summary", "Services done", "By therapist", "Plan sales"])
        self.assertEqual(wb["Plan sales"].max_row, 5)


class TestExpenses(Base):
    def test_regular_and_one_off_expenses(self):
        rent = self.post("/api/expenses/recurring", name="Shop rent", category="Rent", amount=15000, day_of_month=5,
                         starts_on="2026-10-01").json()["id"]
        self.post("/api/expenses/recurring", name="Wifi", category="Utilities", amount=800, starts_on="2026-11-10")
        self.post("/api/expenses", on_date="2026-11-12", category="Supplies", amount=2500, note="Oils")
        nov = self.c.get("/api/expenses?month=2026-11", headers=self.h).json()
        self.assertEqual(nov["total"], 17500.0)  # wifi starts after its due day (1st) in November
        self.assertEqual([c["category"] for c in nov["by_category"]], ["Rent", "Supplies"])
        dec = self.c.get("/api/expenses?month=2026-12", headers=self.h).json()
        self.assertEqual(dec["total"], 15800.0)
        q = self.c.get("/api/expenses?from=2026-10-01&to=2026-12-31", headers=self.h).json()
        self.assertEqual(q["total"], 15000 * 3 + 2500 + 800)
        # stop the rent after November: December drops it, November keeps it
        self.c.put(f"/api/expenses/recurring/{rent}", json={"name": "Shop rent", "category": "Rent", "amount": 15000,
                   "day_of_month": 5, "starts_on": "2026-10-01", "ends_on": "2026-11-30"}, headers=self.h)
        self.assertEqual(self.c.get("/api/expenses?month=2026-12", headers=self.h).json()["total"], 800.0)
        rep = self.c.get("/api/reports/summary?month=2026-11", headers=self.h).json()
        self.assertEqual(rep["expenses"], 17500.0)
        self.assertEqual(rep["profit"], rep["collected"] - rep["payroll"] - 17500.0)
        _, h = self.login_staff()
        self.assertEqual(self.c.get("/api/expenses?month=2026-11", headers=h).status_code, 403)

    def login_staff(self):
        self.post("/api/users", username="front", password="frontdesk1", role="staff")
        tok = self.c.post("/api/auth/login", json={"username": "front", "password": "frontdesk1"}).json()["token"]
        return None, {"Authorization": f"Bearer {tok}"}
class TestPriorityOneOperations(Base):
    def booking(self, therapist_id, service_id, customer_id, start):
        return self.post("/api/appointments", therapist_id=therapist_id, service_id=service_id,
                         customer_id=customer_id, start_at=start)

    def test_customer_page_is_paginated_and_searchable(self):
        self.post("/api/customers", name="Aarav", phone="9991000001")
        self.post("/api/customers", name="Bhavna", phone="9991000002")
        self.post("/api/customers", name="Charan", phone="9991000003")
        page = self.c.get("/api/customers/page?page=2&page_size=2", headers=self.h)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual((page.json()["total"], page.json()["page"], len(page.json()["items"])), (3, 2, 1))
        search = self.c.get("/api/customers/page?q=bhav", headers=self.h).json()
        self.assertEqual([x["name"] for x in search["items"]], ["Bhavna"])

    def test_customer_optional_preferred_payment_method(self):
        created = self.post("/api/customers", name="Meera", phone="9991000004",
                            preferred_payment_method="upi")
        self.assertEqual(created.status_code, 201, created.text)
        cid = created.json()["id"]
        detail = self.c.get(f"/api/customers/{cid}", headers=self.h).json()
        self.assertEqual(detail["preferred_payment_method"], "upi")
        invalid = self.post("/api/customers", name="Wrong", phone="9991000005",
                            preferred_payment_method="crypto")
        self.assertEqual(invalid.status_code, 422)

    def test_appointment_calendar_date_range(self):
        t, s, c = self.seed()
        self.booking(t, s, c, "2026-11-10T10:00")
        self.booking(t, s, c, "2026-11-12T10:00")
        self.booking(t, s, c, "2026-11-14T10:00")
        r = self.c.get("/api/appointments?from=2026-11-11&to=2026-11-13", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["start_at"][:10] for x in r.json()], ["2026-11-12"])

    def test_dashboard_returns_daily_operational_metrics(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-20T10:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        r = self.c.get("/api/dashboard", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("today", body)
        self.assertIn("collections", body)
        self.assertIn("therapists_on_shift", body)
        self.assertIn("upcoming", body)
        self.assertEqual(len(body["monthly"]), 6)
        self.assertTrue(all({"month","completed","revenue","new_customers"} <= set(x) for x in body["monthly"]))

    def test_staff_cannot_add_prepaid_plan_during_onboarding(self):
        self.post("/api/users", username="planstaff", password="planstaff1", role="staff")
        token = self.c.post("/api/auth/login", json={"username":"planstaff","password":"planstaff1"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        response = self.c.post("/api/onboarding", headers=h, json={
            "name":"Staff attempt","phone":"9991000010",
            "plan":{"name":"Hidden wallet","plan_type":"hours","amount":5}
        })
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.c.get("/api/customers?q=9991000010",headers=self.h).json(), [])

    def test_partial_payments_and_refund_respect_outstanding_balance(self):
        t, s, c = self.seed()
        a = self.booking(t, s, c, "2026-11-20T10:00").json()["id"]
        first = self.c.post(f"/api/appointments/{a}/payments",
                            json={"amount": 600, "method": "upi", "reference": "UPI-123"},
                            headers=self.h)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["balance_due"], 1400.0)
        partial_row = self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual(partial_row["payment_status"],"partial")
        second = self.c.post(f"/api/appointments/{a}/payments",
                             json={"amount": 1400, "method": "cash"}, headers=self.h)
        self.assertEqual(second.status_code, 201, second.text)
        paid_row = self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual(paid_row["payment_status"],"paid")
        over = self.c.post(f"/api/appointments/{a}/payments",
                           json={"amount": 1, "method": "cash"}, headers=self.h)
        self.assertEqual(over.status_code, 409)
        fractional_cent = self.c.post(f"/api/appointments/{a}/payments",
                                      json={"amount": 0.001, "method": "cash"}, headers=self.h)
        self.assertEqual(fractional_cent.status_code, 422)

        refund = self.c.post(f"/api/payments/{first.json()['id']}/refunds",
                             json={"amount": 100, "reason": "Customer overpayment"}, headers=self.h)
        self.assertEqual(refund.status_code, 201, refund.text)
        over_refund = self.c.post(f"/api/payments/{first.json()['id']}/refunds",
                                  json={"amount": 501}, headers=self.h)
        self.assertEqual(over_refund.status_code, 409)

        rows = self.c.get("/api/appointments?date=2026-11-20", headers=self.h).json()
        self.assertEqual(rows[0]["paid_amount"], 1900.0)
        self.assertEqual(rows[0]["balance_due"], 100.0)
        self.assertEqual(rows[0]["payment_status"],"partial")
        settlement = self.c.get("/api/payments/settlement?received_on="+spa_today(),headers=self.h).json()
        self.assertEqual((settlement["gross"],settlement["refunds"],settlement["net"]),(2000.0,100.0,1900.0))
        activity=self.c.get("/api/payments?received_on="+spa_today(),headers=self.h).json()
        self.assertEqual(sum(1 for x in activity if x["event_type"]=="payment"),2)
        self.assertEqual(sum(1 for x in activity if x["event_type"]=="refund"),1)
        self.assertTrue(all(x["payment_id"] for x in activity))
        history = self.c.get(f"/api/customers/{c}", headers=self.h).json()["payments"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["method"], "cash")

    def test_prepaid_redemption_prevents_double_cash_collection(self):
        t, s, c = self.seed()
        self.c.post(f"/api/customers/{c}/plans",
                    json={"name": "Rupee wallet", "plan_type": "rupees", "amount": 3000}, headers=self.h)
        a = self.booking(t, s, c, "2026-11-20T10:00").json()["id"]
        done = self.c.patch(f"/api/appointments/{a}/status",
                            json={"status": "completed", "plan_type": "rupees"}, headers=self.h)
        self.assertEqual(done.status_code, 200, done.text)
        status_row=self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual((status_row["payment_status"],status_row["plan_unit"]),("prepaid","rupees"))
        reopened=self.c.patch(f"/api/appointments/{a}/status",json={"status":"booked"},headers=self.h)
        self.assertEqual(reopened.status_code,200,reopened.text)
        status_row=self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual((status_row["payment_status"],status_row["balance_due"]),("pending",2000.0))
        redone=self.c.patch(f"/api/appointments/{a}/status",json={"status":"completed","plan_type":"rupees"},headers=self.h)
        self.assertEqual(redone.status_code,200,redone.text)
        status_row=self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual((status_row["payment_status"],status_row["plan_unit"]),("prepaid","rupees"))
        payment = self.c.post(f"/api/appointments/{a}/payments",
                              json={"amount": 1, "method": "cash"}, headers=self.h)
        self.assertEqual(payment.status_code, 409)

    def test_staff_can_collect_but_cannot_refund_or_view_settlement(self):
        t, s, c = self.seed()
        a = self.booking(t, s, c, "2026-11-20T10:00").json()["id"]
        self.post("/api/users", username="cashier", password="cashier123", role="staff")
        token = self.c.post("/api/auth/login", json={"username": "cashier", "password": "cashier123"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        payment = self.c.post(f"/api/appointments/{a}/payments",
                              json={"amount": 100, "method": "cash"}, headers=h)
        self.assertEqual(payment.status_code, 201, payment.text)
        self.assertEqual(self.c.post(f"/api/payments/{payment.json()['id']}/refunds",
                                     json={"amount": 10}, headers=h).status_code, 403)
        self.assertEqual(self.c.get("/api/payments/settlement", headers=h).status_code, 200)

    def test_payment_receipt_is_available_to_spa_member(self):
        t, s, c = self.seed()
        a = self.booking(t, s, c, "2026-11-20T10:00").json()["id"]
        payment = self.c.post(f"/api/appointments/{a}/payments",
                              json={"amount": 500, "method": "card"}, headers=self.h).json()
        receipt = self.c.get(f"/api/payments/{payment['id']}/receipt", headers=self.h)
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.headers["content-type"], "application/pdf")


    def test_paid_amount_cannot_exceed_new_appointment_price(self):
        t, s, c = self.seed()
        a = self.booking(t,s,c,"2026-11-20T10:00").json()["id"]
        payment = self.c.post(f"/api/appointments/{a}/payments",
                              json={"amount":500,"method":"cash"},headers=self.h)
        self.assertEqual(payment.status_code,201,payment.text)
        update = self.c.put(f"/api/appointments/{a}",headers=self.h,json={
            "customer_id":c,"therapist_id":t,"service_id":s,
            "start_at":"2026-11-20T10:00","price":400
        })
        self.assertEqual(update.status_code,409,update.text)

    def test_customer_payment_preference_defaults_to_none(self):
        response=self.post("/api/customers",name="No preference",phone="9991000011")
        self.assertEqual(response.status_code,201,response.text)
        detail=self.c.get(f"/api/customers/{response.json()['id']}",headers=self.h).json()
        self.assertIsNone(detail["preferred_payment_method"])


    def test_no_payment_is_pending_and_cancelled_cannot_be_paid(self):
        t,s,c=self.seed()
        a=self.booking(t,s,c,"2026-11-20T10:00").json()["id"]
        row=self.c.get("/api/appointments?date=2026-11-20",headers=self.h).json()[0]
        self.assertEqual((row["payment_status"],row["balance_due"]),("pending",2000.0))
        self.c.patch(f"/api/appointments/{a}/status",json={"status":"cancelled"},headers=self.h)
        response=self.c.post(f"/api/appointments/{a}/payments",json={"amount":100,"method":"cash"},headers=self.h)
        self.assertEqual(response.status_code,409)

    def test_member_can_access_daily_settlement_summary(self):
        t,s,c=self.seed()
        a=self.booking(t,s,c,"2026-11-20T10:00").json()["id"]
        self.c.post(f"/api/appointments/{a}/payments",json={"amount":300,"method":"upi"},headers=self.h)
        self.post("/api/users",username="summaryuser",password="summary123",role="staff")
        token=self.c.post("/api/auth/login",json={"username":"summaryuser","password":"summary123"}).json()["token"]
        response=self.c.get("/api/payments/settlement?received_on="+spa_today(),headers={"Authorization":f"Bearer {token}"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["gross"],300.0)


    def test_onboarding_saves_payment_preference_and_plan_atomically(self):
        t,s,_=self.seed()
        response=self.post("/api/onboarding", name="Priya", phone="9991000022",
            preferred_payment_method="upi",
            plan={"name":"Five-hour package","plan_type":"hours","amount":5,"price_paid":750},
            first_appointment={"customer_id":0,"therapist_id":t,"service_id":s,"start_at":"2026-11-24T15:00"})
        self.assertEqual(response.status_code,201,response.text)
        body=response.json()
        self.assertIsNotNone(body["appointment_id"])
        self.assertIsNotNone(body["plan_id"])
        detail=self.c.get(f"/api/customers/{body['customer_id']}",headers=self.h).json()
        self.assertEqual(detail["preferred_payment_method"],"upi")
        self.assertEqual(detail["plans"][0]["balance"],5.0)
        self.assertEqual(detail["plans"][0]["price_paid"],750.0)

    def test_onboarding_plan_rolls_back_if_first_appointment_conflicts(self):
        t,s,c=self.seed()
        self.booking(t,s,c,"2026-11-24T15:00")
        response=self.post("/api/onboarding",name="Rollback",phone="9991000023",
            preferred_payment_method="cash",
            plan={"name":"Rollback hours","plan_type":"hours","amount":5},
            first_appointment={"customer_id":0,"therapist_id":t,"service_id":s,"start_at":"2026-11-24T15:00"})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.c.get("/api/customers?q=9991000023",headers=self.h).json(),[])
