"""Payroll rules, in one place: net = fixed salary + incentive.

Incentive = sum of price of the therapist's COMPLETED appointments in the month
x the therapist's incentive_percent. Prices are snapshotted on the appointment.
"""
import calendar
import datetime as dt
import io
from decimal import ROUND_HALF_UP, Decimal

Q = Decimal("0.01")


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Q, ROUND_HALF_UP)


def month_bounds(month: str):
    """[first day, first day of next month) as dates: the half-open range used in every monthly query."""
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1), (dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1))


def compute_payslip(db, therapist: dict, month: str) -> dict:
    lo, hi = month_bounds(month)
    rows = db.all(
        """SELECT a.id, a.start_at, a.price, c.name AS customer, s.name AS service
           FROM appointments a JOIN customers c ON c.id=a.customer_id
           JOIN services s ON s.id=a.service_id
           WHERE a.therapist_id=%s AND a.status='completed' AND a.start_at >= %s AND a.start_at < %s
           ORDER BY a.start_at""", (therapist["id"], lo, hi))
    revenue = sum((money(r["price"]) for r in rows), Decimal("0"))
    pct = Decimal(str(therapist["incentive_percent"]))
    incentive = money(revenue * pct / 100)
    fixed = money(therapist["fixed_salary"])
    bonuses = db.all(
        """SELECT id, on_date, amount, note FROM therapist_bonuses
           WHERE therapist_id=%s AND on_date >= %s AND on_date < %s ORDER BY on_date, id""",
        (therapist["id"], lo, hi))
    extra = sum((money(b["amount"]) for b in bonuses), Decimal("0"))
    return {
        "therapist_id": therapist["id"], "therapist": therapist["name"], "month": month,
        "fixed_salary": float(fixed), "services_count": len(rows),
        "service_revenue": float(revenue), "incentive_percent": float(pct),
        "incentive": float(incentive), "extra_incentive": float(extra),
        "net_pay": float(fixed + incentive + extra), "lines": rows,
        "bonuses": [{**b, "amount": float(money(b["amount"]))} for b in bonuses],
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
            *[[f"Extra incentive {str(b['on_date'])}" + (f" - {b['note']}" if b.get("note") else ""),
               f"{b['amount']:,.2f}"] for b in p.get("bonuses", [])],
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


def report_pdf(r: dict, business: str) -> bytes:
    """The monthly revenue report: totals, by service, by therapist (with each one's services), and every
    completed service line by line."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    buf = io.BytesIO()
    if r.get("month"):
        y, m = r["month"].split("-")
        label = f"{calendar.month_name[int(m)]} {y}"
    else:
        label = f"{r['from']} to {r['to']}"
    who = r.get("therapist") or "All therapists"
    more = [f"{k.title()}: <b>{r[k]}</b>" for k in ("customer", "service") if r.get(k)]
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Report {label} {who}")
    head = colors.HexColor("#2f5d50")

    def table(rows, widths, total=False, numeric=2):
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), head), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("GRID", (0, 0), (-1, -1), .25, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                 ("ALIGN", (-numeric, 0), (-1, -1), "RIGHT")]
        if total:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        t.setStyle(TableStyle(style))
        return t

    def amt(v):
        return f"{v:,.2f}"

    f = [Paragraph(business, st["Title"]), Paragraph(f"Revenue report - {label}", st["Heading2"]),
         Paragraph(f"Therapist: <b>{who}</b>" + "".join(" &middot; " + m for m in more), st["Normal"]),
         Paragraph(f"Revenue (completed services): <b>Rs {amt(r['revenue'])}</b> from "
                   f"<b>{r['services_count']}</b> services", st["Normal"])]
    if r.get("therapist_id") is None:
        f.append(Paragraph(f"New customers: {r['new_customers']}", st["Normal"]))

    f += [Spacer(1, 10), Paragraph("By service", st["Heading3"])]
    rows = [["Service", "Done", "Revenue"]] + [[x["service"], x["count"], amt(x["revenue"])] for x in r["by_service"]]
    rows.append(["Total", r["services_count"], amt(r["revenue"])])
    f.append(table(rows, [300, 70, 100], total=True))

    f += [Spacer(1, 10), Paragraph("By therapist", st["Heading3"])]
    rows = [["Therapist / service", "Done", "Amount"]]
    bold = []
    for t in r["by_therapist"]:
        bold.append(len(rows))
        rows.append([t["therapist"], t["count"], amt(t["revenue"])])
        rows += [["    " + x["service"], x["count"], amt(x["revenue"])] for x in t["services"]]
    if len(rows) == 1:
        rows.append(["-", "-", "-"])
    tt = table(rows, [300, 70, 100])
    tt.setStyle(TableStyle([("FONTNAME", (0, i), (-1, i), "Helvetica-Bold") for i in bold]))
    f.append(tt)

    f += [Spacer(1, 10), Paragraph("All services done", st["Heading3"])]
    rows = [["Date", "Customer", "Service", "Therapist", "Price"]]
    rows += [[str(x["start_at"]).replace("T", " ")[:16], x["customer"], x["service"], x["therapist"], amt(x["price"])]
             for x in r["lines"]] or [["-", "-", "-", "-", "-"]]
    rows.append(["Total", "", "", "", amt(r["revenue"])])
    f.append(table(rows, [85, 120, 110, 95, 60], total=True, numeric=1))
    doc.build(f)
    return buf.getvalue()
