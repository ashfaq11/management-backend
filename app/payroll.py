"""Payroll rules, in one place: net = fixed salary + incentive.

Incentive = sum of price of the therapist's COMPLETED appointments in the month
x the therapist's incentive_percent. Prices are snapshotted on the appointment.
"""
import calendar
import io
from decimal import ROUND_HALF_UP, Decimal

Q = Decimal("0.01")


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Q, ROUND_HALF_UP)


def month_bounds(month: str):
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01T00:00", f"{y:04d}-{m:02d}-{last:02d}T23:59:59"


def compute_payslip(db, therapist: dict, month: str) -> dict:
    lo, hi = month_bounds(month)
    rows = db.all(
        """SELECT a.id, a.start_at, a.price, c.name AS customer, s.name AS service
           FROM appointments a JOIN customers c ON c.id=a.customer_id
           JOIN services s ON s.id=a.service_id
           WHERE a.therapist_id=? AND a.status='completed' AND a.start_at BETWEEN ? AND ?
           ORDER BY a.start_at""", (therapist["id"], lo, hi))
    revenue = sum((money(r["price"]) for r in rows), Decimal("0"))
    pct = Decimal(str(therapist["incentive_percent"]))
    incentive = money(revenue * pct / 100)
    fixed = money(therapist["fixed_salary"])
    return {
        "therapist_id": therapist["id"], "therapist": therapist["name"], "month": month,
        "fixed_salary": float(fixed), "services_count": len(rows),
        "service_revenue": float(revenue), "incentive_percent": float(pct),
        "incentive": float(incentive), "net_pay": float(fixed + incentive), "lines": rows,
    }


def payslip_pdf(p: dict, business: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Payslip {p['therapist']} {p['month']}")
    y, m = p["month"].split("-")
    label = f"{calendar.month_name[int(m)]} {y}"
    f = [Paragraph(business, st["Title"]), Paragraph(f"Payslip - {label}", st["Heading2"]),
         Paragraph(f"Therapist: <b>{p['therapist']}</b>", st["Normal"]), Spacer(1, 12)]
    earn = [["Earnings", "Amount (INR)"],
            ["Fixed salary", f"{p['fixed_salary']:,.2f}"],
            [f"Incentive ({p['incentive_percent']:g}% of {p['service_revenue']:,.2f}"
             f" from {p['services_count']} services)", f"{p['incentive']:,.2f}"],
            ["Net pay", f"{p['net_pay']:,.2f}"]]
    t = Table(earn, colWidths=[340, 130])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f5d50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black), ("GRID", (0, 0), (-1, -2), .25, colors.grey)]))
    f += [t, Spacer(1, 18), Paragraph("Completed services this month", st["Heading3"])]
    body = [[r["start_at"].replace("T", " ")[:16], r["customer"], r["service"], f"{r['price']:,.2f}"]
            for r in p["lines"]] or [["-", "-", "-", "-"]]
    rows = [["Date", "Customer", "Service", "Price"]] + body
    t2 = Table(rows, colWidths=[110, 140, 150, 70], repeatRows=1)
    t2.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                            ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("GRID", (0, 0), (-1, -1), .25, colors.grey),
                            ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    f.append(t2)
    doc.build(f)
    return buf.getvalue()
