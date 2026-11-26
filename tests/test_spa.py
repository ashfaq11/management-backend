import os
import threading
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient

from app.main import create_app

DSN = os.environ.get("TEST_PG_DSN")  # e.g. postgresql://spa:spa@localhost:5432/spa (needs CREATE SCHEMA rights)


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
        self.assertEqual(text[0], "Date,Time,Customer,Service,Therapist,Price")
        self.assertEqual((len(text), text[-1]), (5, "Total,,,,,4800.00"))
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
