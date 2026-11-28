"""Report tabs: each kind is one query returning a titled table (columns + rows), scoped to a spa and a period.
The page renders any of them the same way, and the same result exports to Excel."""
import datetime as dt
from decimal import Decimal

from psycopg.rows import tuple_row

C = "a.spa_id=%(spa)s AND a.start_at >= %(lo)s AND a.start_at < %(hi)s"

REPORTS = {
    "therapists": ("Therapist performance", ["Therapist", "Services", "Hours", "Revenue", "Incentive %", "Incentive", "Extra incentive", "No-shows", "Cancelled"],
        f"""SELECT t.name, COUNT(*) FILTER (WHERE a.status='completed'),
                   ROUND(COALESCE(SUM(a.duration_min) FILTER (WHERE a.status='completed'),0)/60.0,1),
                   COALESCE(SUM(a.price) FILTER (WHERE a.status='completed'),0), t.incentive_percent,
                   ROUND(COALESCE(SUM(a.price) FILTER (WHERE a.status='completed'),0)*t.incentive_percent/100.0,2),
                   COALESCE((SELECT SUM(b.amount) FROM therapist_bonuses b WHERE b.therapist_id=t.id AND b.on_date >= %(lo)s::date AND b.on_date < %(hi)s::date),0),
                   COUNT(*) FILTER (WHERE a.status='no_show'), COUNT(*) FILTER (WHERE a.status='cancelled')
            FROM therapists t LEFT JOIN appointments a ON a.therapist_id=t.id AND {C}
            WHERE t.spa_id=%(spa)s GROUP BY t.id ORDER BY 4 DESC"""),
    "plans": ("Plans", ["Customer", "Plan", "Type", "Bought", "Used", "Balance", "Price", "Payment", "Expires", "Phone"],
        """SELECT c.name, p.name, p.plan_type, p.purchased_amount, p.purchased_amount-p.balance, p.balance, COALESCE(p.price_paid,0),
                  CASE WHEN p.paid_at IS NULL THEN 'Pending' ELSE COALESCE(p.payment_method,'paid') END, p.expires_on, c.phone
           FROM customer_plans p JOIN customers c ON c.id=p.customer_id
           WHERE p.spa_id=%(spa)s AND p.created_at >= %(lo)s AND p.created_at < %(hi)s ORDER BY p.balance DESC, c.name"""),
    "services": ("Services", ["Service", "Done", "Revenue", "Avg price", "Hours", "Cancelled", "No-shows"],
        f"""SELECT s.name, COUNT(*) FILTER (WHERE a.status='completed'), COALESCE(SUM(a.price) FILTER (WHERE a.status='completed'),0),
                   ROUND(COALESCE(AVG(a.price) FILTER (WHERE a.status='completed'),0),2),
                   ROUND(COALESCE(SUM(a.duration_min) FILTER (WHERE a.status='completed'),0)/60.0,1),
                   COUNT(*) FILTER (WHERE a.status='cancelled'), COUNT(*) FILTER (WHERE a.status='no_show')
            FROM services s LEFT JOIN appointments a ON a.service_id=s.id AND {C}
            WHERE s.spa_id=%(spa)s GROUP BY s.id ORDER BY 3 DESC"""),
    "customers": ("Customers", ["Customer", "Phone", "Visits", "Spent", "Last visit", "Plan balance (hrs)", "Plan balance (Rs)"],
        f"""SELECT c.name, c.phone, COUNT(a.id), COALESCE(SUM(a.price),0), MAX(a.start_at)::date,
                   COALESCE((SELECT SUM(balance) FROM customer_plans p WHERE p.customer_id=c.id AND p.plan_type='hours' AND (p.expires_on IS NULL OR p.expires_on >= CURRENT_DATE)),0),
                   COALESCE((SELECT SUM(balance) FROM customer_plans p WHERE p.customer_id=c.id AND p.plan_type='rupees' AND (p.expires_on IS NULL OR p.expires_on >= CURRENT_DATE)),0)
            FROM customers c JOIN appointments a ON a.customer_id=c.id AND a.status='completed' AND {C}
            WHERE c.spa_id=%(spa)s GROUP BY c.id ORDER BY 4 DESC LIMIT 500"""),
    "payments": ("Payments by source", ["Source", "Payments", "Received", "Refunded", "Net"],
        """WITH p AS (SELECT method, COUNT(*) n, SUM(amount) v FROM payments
                      WHERE spa_id=%(spa)s AND received_at >= %(lo)s AND received_at < %(hi)s GROUP BY 1),
                r AS (SELECT p.method, SUM(r.amount) v FROM payment_refunds r JOIN payments p ON p.id=r.payment_id
                      WHERE r.spa_id=%(spa)s AND r.refunded_at >= %(lo)s AND r.refunded_at < %(hi)s GROUP BY 1)
           SELECT COALESCE(p.method,r.method), COALESCE(p.n,0), COALESCE(p.v,0), COALESCE(r.v,0), COALESCE(p.v,0)-COALESCE(r.v,0)
           FROM p FULL JOIN r ON r.method=p.method ORDER BY 3 DESC"""),
    "lapsed": ("Customers not seen lately", ["Customer", "Phone", "Last visit", "Days since", "Visits", "Spent"],
        """SELECT c.name, c.phone, MAX(a.start_at)::date, CURRENT_DATE - MAX(a.start_at)::date, COUNT(*), SUM(a.price)
           FROM customers c JOIN appointments a ON a.customer_id=c.id AND a.status='completed'
           WHERE c.spa_id=%(spa)s GROUP BY c.id
           HAVING MAX(a.start_at) < CURRENT_DATE - (%(days)s || ' days')::interval
              AND NOT EXISTS (SELECT 1 FROM appointments b WHERE b.customer_id=c.id AND b.status='booked' AND b.start_at >= now())
           ORDER BY 6 DESC LIMIT 500"""),
    "repeat": ("Repeat customers", ["Customer", "Phone", "Visits", "Spent", "First visit", "Last visit", "Avg gap (days)", "Type"],
        f"""WITH v AS (SELECT a.customer_id, a.start_at::date d, a.price FROM appointments a
                       WHERE a.spa_id=%(spa)s AND a.status='completed'),
                p AS (SELECT DISTINCT customer_id FROM v WHERE d >= %(lo)s AND d < %(hi)s)
           SELECT c.name, c.phone, COUNT(*), SUM(v.price), MIN(v.d), MAX(v.d),
                  CASE WHEN COUNT(*) > 1 THEN ROUND((MAX(v.d) - MIN(v.d))::numeric / (COUNT(*) - 1)) END,
                  CASE WHEN MIN(v.d) >= %(lo)s THEN 'New' ELSE 'Returning' END
           FROM p JOIN customers c ON c.id=p.customer_id JOIN v ON v.customer_id=p.customer_id
           GROUP BY c.id HAVING COUNT(*) > 1 ORDER BY 3 DESC, 4 DESC LIMIT 500"""),
    "busy": ("Busy hours (bookings)", ["Day", "Hour", "Bookings", "Completed"],
        f"""SELECT to_char(a.start_at,'Dy'), to_char(a.start_at,'HH24":00"'), COUNT(*), COUNT(*) FILTER (WHERE a.status='completed')
            FROM appointments a WHERE {C} AND a.status<>'cancelled'
            GROUP BY EXTRACT(ISODOW FROM a.start_at), 1, 2 ORDER BY 3 DESC, EXTRACT(ISODOW FROM a.start_at), 2"""),
    "utilisation": ("Therapist utilisation", ["Therapist", "Shift hours", "Booked hours", "Utilisation %", "Idle hours"],
        f"""WITH days AS (SELECT d::date AS day FROM generate_series(%(lo)s::date, %(hi)s::date - 1, '1 day') d),
                sh AS (SELECT x.therapist_id, SUM(EXTRACT(EPOCH FROM (s.end_time - s.start_time))/3600.0) h
                       FROM days JOIN therapist_shift_assignments x ON x.day_of_week = EXTRACT(ISODOW FROM days.day)::int - 1
                       JOIN therapists jt ON jt.id=x.therapist_id AND jt.spa_id=x.spa_id
                         AND (jt.joined_on IS NULL OR days.day >= jt.joined_on)
                       JOIN shifts s ON s.id=x.shift_id AND s.is_active JOIN therapists tt ON tt.id=x.therapist_id
                       WHERE x.spa_id=%(spa)s AND (tt.joined_on IS NULL OR days.day >= tt.joined_on) GROUP BY 1),
                bk AS (SELECT a.therapist_id, SUM(a.duration_min)/60.0 h FROM appointments a
                       WHERE {C} AND a.status IN ('booked','completed') GROUP BY 1)
           SELECT t.name, ROUND(COALESCE(sh.h,0),1), ROUND(COALESCE(bk.h,0),1),
                  CASE WHEN COALESCE(sh.h,0) > 0 THEN ROUND(100*COALESCE(bk.h,0)/sh.h) END,
                  ROUND(GREATEST(COALESCE(sh.h,0)-COALESCE(bk.h,0),0),1)
           FROM therapists t LEFT JOIN sh ON sh.therapist_id=t.id LEFT JOIN bk ON bk.therapist_id=t.id
           WHERE t.spa_id=%(spa)s AND t.is_active AND (t.joined_on IS NULL OR t.joined_on < %(hi)s) ORDER BY 4 DESC NULLS LAST"""),
    "cancellations": ("Cancellations and no-shows", ["Customer", "Phone", "Cancelled", "No-shows", "Booked total", "Rate %"],
        f"""SELECT c.name, c.phone, COUNT(*) FILTER (WHERE a.status='cancelled'), COUNT(*) FILTER (WHERE a.status='no_show'), COUNT(*),
                   ROUND(100.0*COUNT(*) FILTER (WHERE a.status IN ('cancelled','no_show'))/COUNT(*))
            FROM appointments a JOIN customers c ON c.id=a.customer_id WHERE {C}
            GROUP BY c.id HAVING COUNT(*) FILTER (WHERE a.status IN ('cancelled','no_show')) > 0 ORDER BY 6 DESC, 5 DESC"""),
    "expiring": ("Plans expiring soon", ["Customer", "Phone", "Plan", "Balance", "Type", "Expires", "Days left"],
        """SELECT c.name, c.phone, p.name, p.balance, p.plan_type, p.expires_on, p.expires_on - CURRENT_DATE
           FROM customer_plans p JOIN customers c ON c.id=p.customer_id
           WHERE p.spa_id=%(spa)s AND p.balance > 0 AND p.expires_on IS NOT NULL
             AND p.expires_on BETWEEN CURRENT_DATE AND CURRENT_DATE + (%(days)s || ' days')::interval
           ORDER BY p.expires_on"""),
    "dues": ("Pending dues", ["Type", "Customer", "Phone", "What", "Date", "Due (Rs)"],
        """SELECT 'Appointment', c.name, c.phone, s.name, a.start_at::date,
                  a.price - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.appointment_id=a.id),0)
                          + COALESCE((SELECT SUM(r.amount) FROM payment_refunds r JOIN payments p ON p.id=r.payment_id WHERE p.appointment_id=a.id),0)
           FROM appointments a JOIN customers c ON c.id=a.customer_id JOIN services s ON s.id=a.service_id
           WHERE a.spa_id=%(spa)s AND a.status='completed'
             AND NOT EXISTS (SELECT 1 FROM plan_redemptions r WHERE r.appointment_id=a.id AND r.reversed_at IS NULL)
             AND a.price > COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.appointment_id=a.id),0)
                          - COALESCE((SELECT SUM(r.amount) FROM payment_refunds r JOIN payments p ON p.id=r.payment_id WHERE p.appointment_id=a.id),0)
           UNION ALL
           SELECT 'Plan', c.name, c.phone, p.name, p.created_at::date, COALESCE(p.price_paid,0)
           FROM customer_plans p JOIN customers c ON c.id=p.customer_id WHERE p.spa_id=%(spa)s AND p.paid_at IS NULL
           UNION ALL
           SELECT 'Top-up', c.name, c.phone, p.name, x.created_at::date, COALESCE(x.price_paid,0)
           FROM plan_topups x JOIN customer_plans p ON p.id=x.plan_id JOIN customers c ON c.id=p.customer_id
           WHERE x.spa_id=%(spa)s AND x.paid_at IS NULL
           ORDER BY 5"""),
}


def run(db, kind: str, spa: int, lo: dt.date, hi: dt.date, days: int, expenses_in=None) -> dict:
    if kind == "expenses":
        rows = expenses_in(db, spa, lo, hi)
        cats: dict = {}
        for x in rows:
            c = cats.setdefault(x["category"], [x["category"], 0, 0.0])
            c[1] += 1
            c[2] += float(x["amount"])
        out = sorted(cats.values(), key=lambda r: -r[2])
        return {"title": "Expenses by type", "columns": ["Type", "Entries", "Amount"], "rows": out,
                "total": ["Total", sum(r[1] for r in out), round(sum(r[2] for r in out), 2)]}
    title, cols, sql = REPORTS[kind]
    with db.pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(sql, {"spa": spa, "lo": lo, "hi": hi, "days": str(days)})
        rows = [[float(v) if isinstance(v, Decimal) else (v.isoformat() if isinstance(v, dt.date) else v)
                 for v in r] for r in cur.fetchall()]
    return {"title": title, "columns": cols, "rows": rows}


KINDS = ["therapists", "plans", "services", "customers", "payments", "expenses", "lapsed", "repeat", "busy", "utilisation",
         "cancellations", "expiring", "dues"]
