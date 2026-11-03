# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erweiterte Demodaten (nur Demo-Modus): ~2 Jahre Buchhaltung, relativ zu heute.

Szenario in ``seed_data/test/demo_scenario.json``. Alles läuft über die
App-eigenen Codepfade, nicht per Roh-INSERT – damit EÜR, Offene Posten und
DATEV-Export dieselbe Logik zeigen wie bei echten Daten:

* Rechnungen: Nummernkreis (``get_next_number``), Status *versendet* →
  Forderungsbuchung (Debitoren an Wartekonto), PDF/XRechnung beim Versenden.
* Zahlungen: Bankbewegung + ``link_booking_to_invoice_capped`` → Buchungssätze
  *Zahlung* und *Umbuchung* Wartekonto→Erlöskonto, Status *bezahlt*/*Teilzahlung*.
  Keine Zahlung vor dem Rechnungsdatum; Zahlungen in der Zukunft entfallen
  (Rechnung bleibt offen bzw. überfällig).
* Ausgaben/Splits: Bankbewegung mit Buchungssätzen als Kinder
  (``ParentBooking_ID``), Splits wie im Split-Editor.

Die fest datierten Testrechnungen aus ``test_invoices.json`` werden im Demo-Modus
bewusst nicht geladen (``load_test_seed_data(include_invoices=False)``): sie sind
als bezahlt markiert, haben aber keine Zahlungen.
"""
import calendar
import datetime
import json
import os

SCENARIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "seed_data", "test", "demo_scenario.json")


def _tax_amount(amount, rate):
    """Enthaltene Steuer (Brutto-Betrag, wie test_bookings/seed)."""
    if not rate or not amount:
        return None
    t = round(abs(amount) - abs(amount) / (1 + rate), 2)
    return -t if amount < 0 else t


def _month_start(today, months_back):
    y, m = today.year, today.month - months_back
    while m < 1:
        m += 12
        y -= 1
    return datetime.date(y, m, 1)


def _clip_day(first, day):
    last = calendar.monthrange(first.year, first.month)[1]
    return first.replace(day=min(day, last))


def rename_demo_accounts(db, renames):
    """Konten der Testdaten für die Demo schöner benennen (App-Pfad
    ``update_account``; IBAN/BIC/SKR bleiben). ``renames``: {alter Name:
    {"name": …, "bank_name": …}}. Idempotent – fehlende alte Namen werden
    übersprungen."""
    for old, new in renames.items():
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID, Owner, Number, BIC, BankName, SKRAccount FROM Accounts "
                    "WHERE Name = ?", (old,))
        row = cur.fetchone()
        conn.close()
        if not row:
            continue
        acc_id, owner, number, bic, bank_name, skr = row
        db.update_account(acc_id, new["name"], owner, number, bic,
                          new.get("bank_name", bank_name), skr)


class DemoScenario:
    def __init__(self, db, today=None, scenario=None):
        self.db = db
        self.today = today or datetime.date.today()
        if scenario is None:
            with open(SCENARIO_FILE, encoding="utf-8") as f:
                scenario = json.load(f)
        self.sc = scenario
        # Vor allem anderen: Rechnungs-Snapshots übernehmen den Banknamen
        rename_demo_accounts(db, self.sc.get("account_renames", {}))
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID, SKRAccount FROM Accounts WHERE Name = ?",
                    (self.sc["bank_account"],))
        row = cur.fetchone()
        conn.close()
        self.account_id, skr = row
        self.bank_coa = db.get_coa_id_by_account_number(skr)
        self.coa = {n: db.get_coa_id_by_account_number(n) for n in self._coa_numbers()}

    def _coa_numbers(self):
        nums = set()
        for key in ("monthly", "quarterly", "one_off"):
            nums |= {e["coa"] for e in self.sc.get(key, [])}
        for s in self.sc.get("splits", []):
            nums |= {r["coa"] for r in s["rows"]}
        return nums

    def _date(self, days_ago):
        return self.today - datetime.timedelta(days=days_ago)

    # ── Ausgaben / Splits ────────────────────────────────────────────────
    def _bank(self, date, amount, recipient, text, doc=None):
        return self.db.insert_booking(
            date_booking=date.isoformat(), amount=amount, account_id=self.account_id,
            recipient_client=recipient, text=text, document_number=doc,
            booking_type='bank', log_description="Demo-Szenario")

    def _entry(self, parent_id, date, amount, coa, tax_rate, recipient, text, doc=None):
        self.db.insert_booking(
            date_booking=date.isoformat(), amount=amount, coa_id=self.coa[coa],
            counter_coa_id=self.bank_coa, tax_rate=tax_rate,
            tax_amount=_tax_amount(amount, tax_rate), recipient_client=recipient,
            text=text, document_number=doc, booking_type='entry',
            parent_booking_id=parent_id, log_description="Demo-Szenario")

    def _expense(self, date, e, seq):
        text = e["text"].format(mm=f"{date.month:02d}", yyyy=date.year)
        doc = f"E{date:%y%m}-{seq:02d}"
        bid = self._bank(date, e["amount"], e["recipient"], text, doc)
        self._entry(bid, date, e["amount"], e["coa"], e.get("tax_rate"),
                    e["recipient"], text, doc)

    def load_expenses(self):
        seq = 0
        dated = []
        for e in self.sc.get("one_off", []):
            dated.append((self._date(e["days_ago"]), e))
        for key, step in (("monthly", 1), ("quarterly", 3)):
            for e in self.sc.get(key, []):
                for k in range(e["months_back"], -1, -1):
                    if k % step:
                        continue
                    d = _clip_day(_month_start(self.today, k), e["day"])
                    if d <= self.today:
                        dated.append((d, e))
        for d, e in sorted(dated, key=lambda x: x[0]):
            seq += 1
            self._expense(d, e, seq % 100)
        for s in self.sc.get("splits", []):
            d = self._date(s["days_ago"])
            doc = f"S{d:%y%m%d}"
            bid = self._bank(d, s["amount"], s["recipient"], s["text"], doc)
            assert abs(sum(r["amount"] for r in s["rows"]) - s["amount"]) < 0.005
            for r in s["rows"]:
                self._entry(bid, d, r["amount"], r["coa"], r.get("tax_rate"),
                            s["recipient"], r["text"], doc)

    # ── Rechnungen + Zahlungen ───────────────────────────────────────────
    def load_invoices(self):
        from server import handlers
        db = self.db
        customers = {c[2]: c for c in db.fetch_contacts(contact_type='customer')}
        for spec in sorted(self.sc.get("invoices", []), key=lambda s: -s["days_ago"]):
            inv_date = self._date(spec["days_ago"])
            number = db.get_next_number('invoice', inv_date.year, 'R')
            due = inv_date + datetime.timedelta(days=spec["due_days"])
            inv = {
                "invoice_number": number, "invoice_date": inv_date.isoformat(),
                "delivery_date": inv_date.isoformat(),
                "payment_due_date": due.isoformat(),
                "payment_terms": f"Zahlbar innerhalb von {spec['due_days']} Tagen ohne Abzug",
                "customer_number": spec["customer_number"], "status": "finalized",
                "items": spec["items"],
            }
            invoice_id = db.insert_seed_invoices([inv]).get(number)
            if not invoice_id:
                raise RuntimeError(f"Demo-Rechnung {number} nicht angelegt")
            # Versenden wie im UI: Status → Forderungsbuchung → PDF/XRechnung
            db.update_invoice_status(invoice_id, 'sent')
            db.recalc_invoice_payment_state(invoice_id, adjust_status=False)
            handlers._sync_receivable_booking(db, invoice_id, 'sent')
            try:
                handlers._ensure_sent_documents(db, invoice_id, 'finalized', 'sent')
            except Exception as exc:                       # PDF ist Beiwerk
                print(f"Demo: PDF zu {number} nicht erzeugt: {exc}")
            self._pay(invoice_id, number, customers[spec["customer_number"]], inv_date, spec)

    def _pay(self, invoice_id, number, customer, inv_date, spec):
        from server import handlers
        after = spec.get("pay_after_days")
        if after is None:
            return
        pay_date = inv_date + datetime.timedelta(days=after)
        if pay_date > self.today:
            return
        invoice = self.db.get_invoice_by_id(invoice_id)
        gross = float(invoice[38])
        share = spec.get("partial_share")
        amount = round(gross * share, 2) if share else gross
        bid = self.db.insert_booking(
            date_booking=pay_date.isoformat(), amount=amount, account_id=self.account_id,
            recipient_client=customer[3], contact_id=customer[0],
            text=f"{number} {customer[3]}", booking_type='bank',
            log_description="Demo-Szenario Zahlungseingang")
        ok, err = handlers.link_booking_to_invoice_capped(self.db, invoice_id, bid)
        if not ok:
            raise RuntimeError(f"Zahlung zu {number}: {err}")

    # ── Fahrten ──────────────────────────────────────────────────────────
    def load_trips(self):
        # Fahrer = dieselbe Person wie bei den Arbeitszeiten (Seed: K022)
        persons = {c[2]: c[0] for c in self.db.fetch_contacts(contact_type='customer')}
        driver = persons.get(self.sc.get("driver_number"))
        if not driver:
            return
        km = self.sc.get("start_km", 0)
        for t in sorted(self.sc.get("trips", []), key=lambda t: -t["days_ago"]):
            d = self._date(t["days_ago"]).isoformat()
            dist = t["distance_km"]
            self.db.insert_trip(
                driver_id=driver, start_date=d, start_time="08:00", end_date=d,
                end_time="17:30", start_point=t["start_point"],
                destination=t["destination"], vehicle=self.sc.get("vehicle", ""),
                reason=t["reason"], distance_km=dist, start_km=km, end_km=km + dist)
            km += dist

    def load_all(self):
        self.load_invoices()
        self.load_expenses()
        self.load_trips()


def load_demo_scenario(db, today=None):
    """Szenario laden (Aufruf nach ``load_test_seed_data(include_invoices=False)``)."""
    DemoScenario(db, today).load_all()
