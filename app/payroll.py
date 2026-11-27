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


def generated_at() -> dt.datetime:
    """Now, in India time: what every export prints as its generated date."""
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("Asia/Kolkata"))


def file_name(business: str, doc: str, *parts: str) -> str:
    """Standard export name: Spa-Name_Document_Period[_Filter...]_YYYYMMDD-HHMM (no extension)."""
    import re

    def slug(v: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", str(v)).strip("-") or "x"
    return "_".join([slug(business), slug(doc), *(slug(x) for x in parts if x), generated_at().strftime("%Y%m%d-%H%M")])


def period_label(month: str | None, date_from: str | None = None, date_to: str | None = None) -> str:
    if month:
        y, m = month.split("-")
        return f"{calendar.month_name[int(m)]} {y}"
    fmt = lambda d: dt.date.fromisoformat(str(d)).strftime("%d %b %Y")  # noqa: E731
    return f"{fmt(date_from)} to {fmt(date_to)}"


class _Pdf:
    """The house style for every PDF: a sage header band with the spa name, title, period and generated
    time; KPI tiles; striped tables; and a footer with page numbers."""
    ACC, GOLD, INK, MUT, LINE, SOFT = "#0e6e6a", "#b7791f", "#0f1c2e", "#5b6b7f", "#e3e8ee", "#f4f6f8"

    def __init__(self, business: str, title: str, period: str, subtitle: str = ""):
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

        self.c = colors
        self.business, self.title, self.period, self.subtitle = business, title, period, subtitle
        self.when = generated_at().strftime("%d %b %Y, %I:%M %p IST")
        base = getSampleStyleSheet()["Normal"]
        self.st = {
            "h": ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=11.5, textColor=colors.HexColor(self.ACC),
                                spaceBefore=14, spaceAfter=6),
            "p": ParagraphStyle("p", parent=base, fontSize=9, textColor=colors.HexColor(self.INK), leading=12),
            "m": ParagraphStyle("m", parent=base, fontSize=8, textColor=colors.HexColor(self.MUT), leading=10),
            "kl": ParagraphStyle("kl", parent=base, fontSize=7, textColor=colors.HexColor(self.MUT), leading=9),
            "kv": ParagraphStyle("kv", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16,
                                 textColor=colors.HexColor(self.INK)),
            "r": ParagraphStyle("r", parent=base, alignment=TA_RIGHT, fontSize=8),
        }
        self.flow: list = []

    def _page(self, canvas, doc):
        w, h = doc.pagesize
        c = self.c
        canvas.saveState()
        canvas.setFillColor(c.HexColor(self.ACC))
        canvas.rect(0, h - 78, w, 78, stroke=0, fill=1)
        canvas.setFillColor(c.HexColor(self.GOLD))
        canvas.rect(0, h - 81, w, 3, stroke=0, fill=1)
        canvas.setFillColor(c.white)
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(36, h - 36, self.business)
        canvas.setFont("Helvetica", 10)
        canvas.drawString(36, h - 54, self.title + (f"  \u00b7  {self.subtitle}" if self.subtitle else ""))
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawRightString(w - 36, h - 36, self.period)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(w - 36, h - 54, f"Generated {self.when}")
        canvas.setStrokeColor(c.HexColor(self.LINE))
        canvas.line(36, 40, w - 36, 40)
        canvas.setFillColor(c.HexColor(self.MUT))
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(36, 28, f"{self.business} \u00b7 {self.title} \u00b7 {self.period}")
        canvas.restoreState()

    def heading(self, text):
        from reportlab.platypus import Paragraph
        self.flow.append(Paragraph(text, self.st["h"]))

    def text(self, text, muted=False):
        from reportlab.platypus import Paragraph
        self.flow.append(Paragraph(text, self.st["m" if muted else "p"]))

    def kpis(self, items):
        """items: [(label, value)] shown as tiles, four to a row."""
        from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
        c = self.c
        cells = [[Paragraph(label.upper(), self.st["kl"]), Paragraph(value, self.st["kv"])] for label, value in items]
        for k in range(0, len(cells), 4):
            row = cells[k:k + 4]
            row += [""] * (4 - len(row))
            t = Table([row], colWidths=[128] * 4, rowHeights=[44])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                   *[("BACKGROUND", (i, 0), (i, 0), c.HexColor(self.SOFT)) for i in range(len(cells[k:k + 4]))],
                                   *[("LINEABOVE", (i, 0), (i, 0), 2, c.HexColor(self.ACC)) for i in range(len(cells[k:k + 4]))]]))
            self.flow += [t, Spacer(1, 6)]

    def table(self, rows, widths, numeric=1, total=False, bold_rows=()):
        from reportlab.platypus import Table, TableStyle
        c = self.c
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), c.HexColor(self.ACC)), ("TEXTCOLOR", (0, 0), (-1, 0), c.white),
                 ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                 ("LINEBELOW", (0, 0), (-1, -1), .25, c.HexColor(self.LINE)),
                 ("ALIGN", (-numeric, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), c.HexColor("#f8fafc")))
        for i in bold_rows:
            style += [("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"), ("BACKGROUND", (0, i), (-1, i), c.HexColor("#e3f3f1"))]
        if total:
            style += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("BACKGROUND", (0, -1), (-1, -1), c.HexColor(self.SOFT)),
                      ("LINEABOVE", (0, -1), (-1, -1), 1, c.HexColor(self.ACC))]
        t.setStyle(TableStyle(style))
        self.flow.append(t)

    def build(self, pagesize=None) -> bytes:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=pagesize or A4, leftMargin=36, rightMargin=36, topMargin=100, bottomMargin=54,
                                title=f"{self.business} - {self.title} - {self.period}", author=self.business,
                                subject=self.title, creator="StudioDesk")
        doc.build(self.flow, onFirstPage=self._page, onLaterPages=self._page, canvasmaker=_numbered_canvas(self.MUT))
        return buf.getvalue()

    def spacer(self, h=10):
        from reportlab.platypus import Spacer
        self.flow.append(Spacer(1, h))

    def info(self, pairs, cols=2):
        """Label/value pairs laid out in columns on a soft panel (e.g. 'Billed to', 'Pay to')."""
        from reportlab.platypus import Paragraph, Table, TableStyle
        cells = [[Paragraph(k.upper(), self.st["kl"]), Paragraph(v or "-", self.st["p"])] for k, v in pairs]
        rows = []
        for i in range(0, len(cells), cols):
            r = cells[i:i + cols]
            rows.append(r + [""] * (cols - len(r)))
        width = 523 / cols
        t = Table(rows, colWidths=[width] * cols)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), self.c.HexColor(self.SOFT)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        self.flow.append(t)


def _numbered_canvas(muted: str):
    """A canvas that writes 'Page X of Y' at the bottom right once the page count is known."""
    from reportlab.lib import colors
    from reportlab.pdfgen.canvas import Canvas

    class Numbered(Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                w = self._pagesize[0]
                self.setFont("Helvetica", 7.5)
                self.setFillColor(colors.HexColor(muted))
                self.drawRightString(w - 36, 28, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()
    return Numbered


def amt(v) -> str:
    return f"{float(v):,.2f}"


def payslip_pdf(p: dict, business: str) -> bytes:
    d = _Pdf(business, "Payslip", period_label(p["month"]), p["therapist"])
    d.kpis([("Net pay", f"Rs {amt(p['net_pay'])}"), ("Fixed salary", f"Rs {amt(p['fixed_salary'])}"),
            ("Incentive", f"Rs {amt(p['incentive'] + p.get('extra_incentive', 0))}"),
            ("Services done", str(p["services_count"]))])
    d.heading("Earnings")
    rows = [["Description", "Amount (Rs)"], ["Fixed salary", amt(p["fixed_salary"])],
            [f"Incentive: {p['incentive_percent']:g}% of Rs {amt(p['service_revenue'])} from {p['services_count']} services",
             amt(p["incentive"])],
            *[[f"Extra incentive, {b['on_date']}" + (f" - {b['note']}" if b.get("note") else ""), amt(b["amount"])]
              for b in p.get("bonuses", [])],
            ["Net pay", amt(p["net_pay"])]]
    d.table(rows, [400, 123], total=True)
    d.heading("Completed services")
    body = [[str(r["start_at"]).replace("T", " ")[:16], r["customer"], r["service"], amt(r["price"])] for r in p["lines"]]
    d.table([["Date", "Customer", "Service", "Price (Rs)"], *(body or [["-", "-", "-", "-"]])], [110, 160, 170, 83])
    t = p.get("therapist_profile") or {}
    acct = t.get("bank_account_number") or ""
    pay_to = [("Account holder", t.get("bank_account_name") or p["therapist"]), ("Bank", t.get("bank_name")),
              ("Account", ("XXXX " + acct[-4:]) if len(acct) > 4 else acct), ("IFSC", (t.get("bank_ifsc") or "").upper()),
              ("UPI", t.get("upi_id")), ("Phone", t.get("phone"))]
    if any(v for _, v in pay_to[1:5]):
        d.heading("Pay to")
        d.info(pay_to, cols=3)
    d.spacer(36)
    d.text("This is a computer-generated payslip and does not need a signature.", muted=True)
    return d.build()


def report_pdf(r: dict, business: str) -> bytes:
    """The revenue report: KPIs, money collected and profit, by service, by therapist, every service, plan sales."""
    filters = [r[k] for k in ("therapist", "customer", "service") if r.get(k)]
    d = _Pdf(business, "Revenue report", period_label(r.get("month"), r.get("from"), r.get("to")),
             " / ".join(filters) or "All therapists")
    tiles = [("Service revenue", f"Rs {amt(r['revenue'])}"), ("Services done", str(r["services_count"])),
             ("Service time", f"{r.get('minutes', 0) // 60} h" + (f" {r.get('minutes', 0) % 60} min" if r.get('minutes', 0) % 60 else ""))]
    if r.get("therapist_id") is None:
        tiles.append(("New customers", str(r["new_customers"])))
    if "plan_sales" in r:
        tiles += [("Plan sales", f"Rs {amt(r['plan_sales'])}"), ("Paid from plans", f"Rs {amt(r['paid_by_plan'])}"),
                  ("Money collected", f"Rs {amt(r['collected'])}")]
    if r.get("expenses") is not None and "expense_lines" in r:
        tiles.append(("Expenses", f"Rs {amt(r['expenses'])}"))
    if r.get("profit") is not None:
        tiles += [("Payroll", f"Rs {amt(r['payroll'])}"), ("Net profit", f"Rs {amt(r['profit'])}")]
    d.kpis(tiles)
    if "plan_sales" in r:
        d.text("Money collected = service revenue - services paid from prepaid plans + plan sales. "
               "Net profit = money collected - payroll - expenses.", muted=True)

    if r.get("by_method"):
        d.heading("By payment method")
        rows = [["Method", "Services (Rs)", "Plans (Rs)", "Expenses (Rs)", "Net (Rs)"]]
        rows += [[METHODS.get(m["method"], m["method"]), amt(m["services"]), amt(m["plans"]), amt(m["expenses"]), amt(m["net"])]
                 for m in r["by_method"]]
        d.table(rows, [143, 95, 95, 95, 95], numeric=4)

    d.heading("By service")
    rows = [["Service", "Done", "Minutes", "Revenue (Rs)"]]
    rows += [[x["service"], x["count"], x.get("minutes", ""), amt(x["revenue"])] for x in r["by_service"]]
    rows.append(["Total", r["services_count"], r.get("minutes", ""), amt(r["revenue"])])
    d.table(rows, [283, 70, 80, 90], numeric=3, total=True)

    d.heading("By therapist")
    rows, bold = [["Therapist / service", "Done", "Minutes", "Amount (Rs)"]], []
    for t in r["by_therapist"]:
        bold.append(len(rows))
        rows.append([t["therapist"], t["count"], t.get("minutes", ""), amt(t["revenue"])])
        rows += [["    " + x["service"], x["count"], x.get("minutes", ""), amt(x["revenue"])] for x in t["services"]]
    if len(rows) == 1:
        rows.append(["-", "-", "-", "-"])
    d.table(rows, [283, 70, 80, 90], numeric=3, bold_rows=bold)

    d.heading(f"Services done ({len(r['lines'])})")
    rows = [["Date", "Customer", "Service", "Therapist", "Min", "Price (Rs)"]]
    rows += [[str(x["start_at"]).replace("T", " ")[:16], x["customer"], x["service"], x["therapist"],
              x.get("duration_min", ""), amt(x["price"])] for x in r["lines"]] or [["-"] * 6]
    rows.append(["Total", "", "", "", r.get("minutes", ""), amt(r["revenue"])])
    d.table(rows, [88, 115, 115, 95, 35, 75], numeric=2, total=True)

    if r.get("expense_lines"):
        d.heading("Expenses")
        rows = [["Date", "Category", "Item", "Type", "Paid via", "Amount (Rs)"]]
        rows += [[str(x["on_date"]), x["category"], x.get("name") or "", x["kind"].title(),
                  METHODS.get(x.get("payment_method"), "-"), amt(x["amount"])] for x in r["expense_lines"]]
        rows.append(["Total", "", "", "", "", amt(r["expenses"])])
        d.table(rows, [75, 90, 170, 55, 60, 73], total=True)

    if r.get("plan_lines"):
        d.heading("Prepaid plan sales")
        rows = [["Date", "Customer", "Plan", "Kind", "Amount", "Via", "Paid (Rs)"]]
        rows += [[str(x["at"])[:16], x["customer"], x["plan"], "Top-up" if x["kind"] == "topup" else "New",
                  f"{float(x['amount']):g} {'hrs' if x['plan_type'] == 'hours' else 'Rs'}",
                  METHODS.get(x.get("payment_method"), x.get("payment_method") or "-"), amt(x["price_paid"])]
                 for x in r["plan_lines"]]
        rows.append(["Total", "", "", "", "", "", amt(r["plan_sales"])])
        d.table(rows, [80, 105, 110, 45, 50, 58, 75], numeric=2, total=True)
    return d.build()


METHODS = {"cash": "Cash", "upi": "UPI", "card": "Card", "bank_transfer": "Bank transfer", "other": "Other"}


def receipt_pdf(p: dict, refunds: list, business: str) -> bytes:
    """Minimal editorial-style spa receipt inspired by a clean digital invoice layout."""
    import base64
    import re
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape as e

    INK = colors.HexColor("#171717")
    MUT = colors.HexColor("#777777")
    LINE = colors.HexColor("#dedede")
    BLACK = colors.HexColor("#080808")
    PALE = colors.HexColor("#f1f1f1")
    number = f"SP-{p['id']:06d}"
    received = dt.datetime.fromisoformat(str(p["received_at"]).replace(" ", "T")[:19])
    appt = dt.datetime.fromisoformat(str(p["start_at"]).replace(" ", "T")[:19])
    gross = float(p["amount"])
    refunded = sum(float(r["amount"]) for r in refunds)
    net = gross - refunded
    W = 354

    def style(name, **kw):
        return ParagraphStyle(name, **{"fontName": "Helvetica", "fontSize": 8, "leading": 10,
                                       "textColor": INK, **kw})

    title = style("receipt_title", fontName="Times-Roman", fontSize=19, leading=22, alignment=TA_RIGHT)
    small = style("small", fontSize=6.5, leading=8, textColor=MUT)
    body = style("body", fontSize=7.5, leading=10)
    right = style("right", alignment=TA_RIGHT, fontSize=7.5, leading=10)
    label = style("label", fontName="Helvetica-Bold", fontSize=6.5, leading=8)
    tiny_right = style("tiny_right", fontSize=6.5, leading=8, alignment=TA_RIGHT, textColor=MUT)

    def money(value):
        return f"{value:,.2f}"

    # Keep the logo on the left and spa identity immediately to its right.
    logo_cell = []
    data_url = p.get("logo_data_url")
    if data_url:
        match = re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})", data_url)
        if match:
            try:
                raw = base64.b64decode(match.group(1), validate=True)
                from reportlab.lib.utils import ImageReader
                from io import BytesIO
                reader = ImageReader(BytesIO(raw))
                iw, ih = reader.getSize()
                scale = min(64 / max(iw, 1), 48 / max(ih, 1))
                logo_cell.append(Image(BytesIO(raw), width=iw * scale, height=ih * scale))
            except Exception:
                pass
    spa_details = []
    spa_details.append(Paragraph(e(business), style("brand", fontName="Helvetica-Bold", fontSize=9, leading=11)))
    if p.get("spa_address"):
        spa_details.append(Paragraph(e(p["spa_address"]).replace("\\n", "<br/>"), small))
    if not logo_cell:
        # No uploaded logo: keep the spa identity readable without an empty logo column.
        logo_cell = [Paragraph("", small)]

    head = Table([[logo_cell, spa_details, [Paragraph("RECEIPT", title),
                                            Paragraph(f"{number}<br/>{received:%d %b %Y}", tiny_right)]]],
                 colWidths=[W * .22, W * .38, W * .40])
    head.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING",(0,0),(-1,-1),0),
                              ("RIGHTPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story = [head, Spacer(1, 15)]

    # Customer and issue details, in the simple two-column block from the reference.
    customer = Paragraph(f"<b>{e(p.get('customer') or '')}</b><br/>{e(p.get('phone') or '')}", body)
    meta = Table([[Paragraph("BILL TO", small), Paragraph("ISSUE DATE", tiny_right)],
                  [customer, Paragraph(f"{received:%d %b %Y}", right)],
                  [Paragraph("APPOINTMENT", small), Paragraph("PAYMENT METHOD", tiny_right)],
                  [Paragraph(f"{appt:%d %b %Y, %I:%M %p}<br/>{e(p.get('therapist') or '')}", body),
                   Paragraph(e(METHODS.get(p.get("method"), p.get("method", ""))), right)]],
                 colWidths=[W * .58, W * .42])
    meta.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),
                              ("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),2),
                              ("BOTTOMPADDING",(0,0),(-1,-1),2)]))
    story += [meta, Spacer(1, 16)]

    rows = [[Paragraph("#", style("th", fontName="Helvetica-Bold", fontSize=6.5, textColor=colors.white)),
             Paragraph("ITEM &amp; DESCRIPTION", style("th2", fontName="Helvetica-Bold", fontSize=6.5, textColor=colors.white)),
             Paragraph("QTY", style("th3", fontName="Helvetica-Bold", fontSize=6.5, alignment=TA_RIGHT, textColor=colors.white)),
             Paragraph("RATE", style("th4", fontName="Helvetica-Bold", fontSize=6.5, alignment=TA_RIGHT, textColor=colors.white)),
             Paragraph("TAX", style("th5", fontName="Helvetica-Bold", fontSize=6.5, alignment=TA_RIGHT, textColor=colors.white)),
             Paragraph("AMOUNT", style("th6", fontName="Helvetica-Bold", fontSize=6.5, alignment=TA_RIGHT, textColor=colors.white))],
            ["1", Paragraph(f"<b>{e(p.get('service') or 'Spa service')}</b><br/><font color='#777777' size='6.5'>Payment received · {appt:%d %b %Y}</font>", body),
             "1.00", Paragraph(money(gross), right), "—", Paragraph(money(gross), right)]]
    for idx, refund in enumerate(refunds, 2):
        desc = "Refund" + (f" · {e(refund.get('reason') or '')}" if refund.get("reason") else "")
        rows.append([str(idx), Paragraph(desc, body), "1.00", "—", "—",
                     Paragraph("− " + money(float(refund["amount"])), right)])
    table = Table(rows, colWidths=[20, 157, 35, 47, 35, 60], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),BLACK),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),5),
        ("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),7),
        ("BOTTOMPADDING",(0,0),(-1,-1),7),("LINEBELOW",(0,1),(-1,-1),.45,LINE),
        ("ALIGN",(2,0),(-1,-1),"RIGHT")
    ]))
    story += [table, Spacer(1, 8)]

    totals = [
        ["Subtotal", money(gross)],
    ]
    if refunded:
        totals.append(["Refunds", "− " + money(refunded)])
    totals.append(["TOTAL PAID", money(net)])
    summary = Table([[Paragraph(k, style("sum_"+str(i), fontName="Helvetica-Bold" if i == len(totals)-1 else "Helvetica",
                                        fontSize=8 if i == len(totals)-1 else 7.2,
                                        alignment=TA_RIGHT)),
                      Paragraph(v, style("sumv_"+str(i), fontName="Helvetica-Bold" if i == len(totals)-1 else "Helvetica",
                                        fontSize=8 if i == len(totals)-1 else 7.2,
                                        alignment=TA_RIGHT))]
                     for i,(k,v) in enumerate(totals)],
                    colWidths=[W-145,145])
    summary.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"RIGHT"),("LEFTPADDING",(0,0),(-1,-1),5),
                                 ("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),
                                 ("BOTTOMPADDING",(0,0),(-1,-1),5),
                                 ("BACKGROUND",(0,-1),(-1,-1),PALE),
                                 ("LINEABOVE",(0,-1),(-1,-1),.5,LINE)]))
    story += [summary, Spacer(1, 25)]
    note = Table([[Paragraph("<b>Notes</b><br/>Thank you for choosing us. We hope to see you again.", body)],
                  [Paragraph("<b>Terms &amp; Conditions</b><br/>This is a computer-generated receipt for the payment shown above. Please retain it for your records.", small)]],
                 colWidths=[W])
    note.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                              ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story += [note, Spacer(1, 8), Paragraph(f"{e(business)} · Thank you", style("footer", fontSize=6.5, textColor=MUT))]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=32, rightMargin=32, topMargin=30, bottomMargin=28,
                            title=f"{business} - Receipt {number}", author=business, creator="StudioDesk")
    doc.build(story)
    return buf.getvalue()


def settlement_pdf(r: dict, lines: list, business: str) -> bytes:
    """The day's cash-up: totals by payment method, refunds, net, and every payment."""
    day = dt.date.fromisoformat(str(r["date"])[:10])
    d = _Pdf(business, "Daily settlement", day.strftime("%d %b %Y"))
    d.kpis([("Collected", f"Rs {amt(r['gross'])}"), ("Refunds", f"Rs {amt(r['refunds'])}"), ("Net", f"Rs {amt(r['net'])}"),
            ("Payments", str(sum(m["count"] for m in r["methods"])))])
    d.heading("By payment method")
    rows = [["Method", "Payments", "Collected (Rs)", "Refunds (Rs)", "Net (Rs)"]]
    rows += [[METHODS.get(m["method"], m["method"]), m["count"], amt(m["gross"]), amt(m["refunds"]), amt(m["gross"] - m["refunds"])]
             for m in r["methods"]] or [["-", "-", "-", "-", "-"]]
    rows.append(["Total", sum(m["count"] for m in r["methods"]), amt(r["gross"]), amt(r["refunds"]), amt(r["net"])])
    d.table(rows, [143, 80, 100, 100, 100], numeric=4, total=True)
    d.heading("Payments")
    pr = [["Time", "Receipt", "Customer", "Service", "Method", "Amount (Rs)"]]
    pr += [[str(x["received_at"]).replace("T", " ")[11:16], f"SP-{x['id']:06d}", x["customer"], x["service"],
            METHODS.get(x["method"], x["method"]), amt(x["amount"])] for x in lines] or [["-"] * 6]
    d.table(pr, [45, 70, 130, 130, 68, 80])
    d.spacer(30)
    d.text("Counted by: ____________________        Checked by: ____________________", muted=True)
    return d.build()


def report_xlsx(r: dict, business: str) -> bytes:
    """The revenue report as an Excel workbook: Summary, Services done, By therapist, Plan sales."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="0E6E6A")
    money = "#,##0.00"

    def sheet(ws, title, header, rows, money_cols=(), total=None):
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill, c.alignment = head, fill, Alignment(vertical="center")
        for row in rows:
            ws.append(row)
        if total:
            ws.append(total)
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
        for col in money_cols:
            for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                cell.number_format = money
        for i, _ in enumerate(header, 1):
            width = max(len(str(c.value or "")) for (c,) in ws.iter_rows(min_col=i, max_col=i))
            ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 40)
        ws.freeze_panes = "A2"

    ws = wb.active
    period = period_label(r.get("month"), r.get("from"), r.get("to"))
    rows = [["Spa", business], ["Report", "Revenue report"], ["Period", period],
            ["Generated", generated_at().strftime("%d %b %Y, %I:%M %p IST")]]
    for key, label in (("therapist", "Therapist"), ("customer", "Customer"), ("service", "Service")):
        if r.get(key):
            rows.append([label, r[key]])
    rows += [["Revenue (completed services)", float(r["revenue"])], ["Services done", r["services_count"]],
             ["Service minutes", r.get("minutes", 0)], ["New customers", r["new_customers"]]]
    if "plan_sales" in r:
        rows += [["Plan sales", r["plan_sales"]], ["Services paid from plans", r["paid_by_plan"]],
                 ["Money collected", r["collected"]]]
    if "expense_lines" in r:
        rows.append(["Expenses", r["expenses"]])
    if r.get("profit") is not None:
        rows += [["Payroll", r["payroll"]], ["Net profit (after payroll and expenses)", r["profit"]]]
    sheet(ws, "Summary", ["Item", "Value"], rows)
    for (c,) in ws.iter_rows(min_row=2, min_col=2, max_col=2):
        if isinstance(c.value, float):
            c.number_format = money

    lines = [[str(x["start_at"])[:10], str(x["start_at"]).replace(" ", "T")[11:16], x["customer"], x["service"],
              x["therapist"], x.get("duration_min"), float(x["price"])] for x in r["lines"]]
    sheet(wb.create_sheet(), "Services done", ["Date", "Time", "Customer", "Service", "Therapist", "Minutes", "Price"],
          lines, money_cols=(7,), total=["Total", "", "", "", "", r.get("minutes", 0), float(r["revenue"])])

    th = []
    for t in r["by_therapist"]:
        th.append([t["therapist"], "", t["count"], t.get("minutes", 0), float(t["revenue"])])
        th += [["", x["service"], x["count"], x.get("minutes", 0), float(x["revenue"])] for x in t["services"]]
    sheet(wb.create_sheet(), "By therapist", ["Therapist", "Service", "Done", "Minutes", "Amount"], th, money_cols=(5,))

    if r.get("plan_lines") is not None:
        pl = [[str(x["at"])[:16], x["customer"], x["plan"], "Top-up" if x["kind"] == "topup" else "New",
               float(x["amount"]), "hours" if x["plan_type"] == "hours" else "rupees",
               METHODS.get(x.get("payment_method"), "-"), float(x["price_paid"])] for x in r["plan_lines"]]
        sheet(wb.create_sheet(), "Plan sales", ["Date", "Customer", "Plan", "Kind", "Amount", "Unit", "Paid via", "Paid"], pl,
              money_cols=(8,), total=["Total", "", "", "", "", "", "", float(r["plan_sales"])])

    if r.get("expense_lines"):
        ex = [[str(x["on_date"]), x["category"], x.get("name") or "", x["kind"].title(),
               METHODS.get(x.get("payment_method"), "-"), float(x["amount"])] for x in r["expense_lines"]]
        sheet(wb.create_sheet(), "Expenses", ["Date", "Category", "Item", "Type", "Paid via", "Amount"], ex, money_cols=(6,),
              total=["Total", "", "", "", "", float(r["expenses"])])
    if r.get("by_method"):
        bm = [[METHODS.get(m["method"], m["method"]), m["services"], m["plans"], m["expenses"], m["net"]] for m in r["by_method"]]
        sheet(wb.create_sheet(), "By payment method", ["Method", "Services", "Plans", "Expenses", "Net"], bm,
              money_cols=(2, 3, 4, 5))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
