import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class Base(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(create_app(":memory:"))
        tok = self.c.post("/api/auth/login", json={"username": "admin", "password": "admin12345"}).json()["token"]
        self.h = {"Authorization": f"Bearer {tok}"}

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

    def test_summary(self):
        t, s, c = self.seed()
        a = self.post("/api/appointments", customer_id=c, therapist_id=t, service_id=s,
                      start_at="2026-11-10T09:00").json()["id"]
        self.c.patch(f"/api/appointments/{a}/status", json={"status": "completed"}, headers=self.h)
        r = self.c.get("/api/reports/summary?month=2026-11", headers=self.h).json()
        self.assertEqual(r["revenue"], 2000.0)


class TestCors(Base):
    def test_ui_origin_allowed(self):
        r = self.c.options("/api/auth/login", headers={"Origin": "http://localhost:4200",
                           "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://localhost:4200")


if __name__ == "__main__":
    unittest.main()
