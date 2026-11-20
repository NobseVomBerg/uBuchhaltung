# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erweiterte Demodaten (nur Demo-Modus): mehrere Jahre Buchhaltung, relativ zu heute.

Das von Hand gepflegte Szenario steht in ``seed_data/test/demo_scenario.json`` (rund 22 Monate); den großen Bestand über
mehrere Jahre erzeugt ``demo_history.build_history`` aus Tabellen und reicht ihn als ``extra`` an ``DemoScenario``
weiter, das ihn an die Listen des Szenarios anhängt (``load_demo_scenario``). Ohne ``extra`` entsteht genau das Szenario.
Alles läuft über die App-eigenen Codepfade, nicht per Roh-INSERT – damit EÜR, Offene Posten und DATEV-Export dieselbe
Logik zeigen wie bei echten Daten:

* Rechnungsereignisse laufen über EINE Warteschlange nach Datum (``heapq``, gleiche Tage in der Reihenfolge ihrer
  Anlage): Rechnung anlegen und versenden, Zahlung, Storno, Ersatzrechnung, Erstattung, Angebot umwandeln. So entstehen
  die Nummern je Jahr in Datumsfolge über die App (auch die der Storno- und Ersatzrechnungen).
* Rechnungen aus dem Szenario: Nummernkreis (``get_next_number``), Status *versendet* → Forderungsbuchung (Debitoren
  an Wartekonto), PDF/XRechnung beim Versenden. Die des Erzeugers (``via: form``) gehen den Weg der Maske: Formulardaten
  → ``handlers._invoice_fields`` → ``allocate_invoice_number`` → ``insert_invoice`` in einer Transaktion → Versenden über
  ``set_invoice_status``.
* Zahlungen: Bankbewegung + ``link_booking_to_invoice_capped`` → Buchungssätze *Zahlung* und *Umbuchung* Wartekonto→
  Erlöskonto, Status *bezahlt*/*Teilzahlung*. Keine Zahlung vor dem Rechnungsdatum; Zahlungen in der Zukunft entfallen
  (Rechnung bleibt offen bzw. überfällig).
* Storno: Eine Rechnung mit ``cancel_after_days`` wird nach so vielen Tagen über ``Database.cancel_invoice``
  storniert (Stornorechnung mit eigener Nummer, Gegenbuchung der Forderung). Mit ``replacement`` folgt die niedrigere
  Ersatzrechnung (die Zahlung zieht beim Versenden um) und die Erstattung des Rests (``assign_storno_refund``).
* Ausgaben/Splits: Bankbewegung mit Buchungssätzen als Kinder (``ParentBooking_ID``), Splits wie im Split-Editor; Kasse,
  Überträge zum Tagesgeld, Bargeld, Anlagen samt AfA, Angebote, Arbeitszeiten und offene Bankbewegungen kommen aus dem
  Erzeuger.
* Dokumente (PDF/E-Rechnung) entstehen mit ``documents_days`` nur für Rechnungen der letzten Tage – für ältere gilt die
  Regel „eingefroren, aber keine Datei vorhanden“ („PDF + E-Rechnung“ erzeugt sie später).

Die fest datierten Testrechnungen aus ``test_invoices.json`` werden im Demo-Modus bewusst nicht geladen
(``load_test_seed_data(include_invoices=False)``): sie sind als bezahlt markiert, haben aber keine Zahlungen; ebenso die fest
datierten Buchungen, Anlagen, Angebote und Arbeitszeiten (``include_dated=False``).
"""
import calendar
import contextlib
import datetime
import heapq
import itertools
import json
import os
from types import SimpleNamespace

import money

SCENARIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "seed_data", "test", "demo_scenario.json")

#: Für Rechnungen der letzten so vielen Tage entstehen PDF und E-Rechnung (``load_demo_scenario``)
DOCUMENT_DAYS = 30


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
    ``update_account``; IBAN/BIC bleiben). ``renames``: {alter Name:
    {"name": …, "bank_name": …, "skr_account": …}}. Idempotent – fehlende alte Namen
    werden übersprungen.

    ``skr_account`` stellt das Konto auf eine andere SKR-Nummer um (die Demo führt die
    Kasse der Testdaten statt auf dem Verrechnungskonto 1460 als Nebenkasse auf 1610 –
    1600 trägt schon die automatisch angelegte Kasse);
    Buchungen, die auf der Kasse stehen und die alte Nummer führen, ziehen mit."""
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
        new_skr = new.get("skr_account", skr)
        db.update_account(acc_id, new["name"], owner, number, bic,
                          new.get("bank_name", bank_name), new_skr)
        if new_skr != skr:
            _move_account_bookings(db, acc_id, skr, new_skr)


def _move_account_bookings(db, account_id, old_skr, new_skr):
    """Buchungen eines Kontos, die dessen alte SKR-Nummer führen, auf die neue umstellen."""
    old_coa = db.get_coa_id_by_account_number(old_skr) if old_skr else None
    new_coa = db.get_coa_id_by_account_number(new_skr) if new_skr else None
    if old_coa is None or new_coa is None:
        return
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ID, COA_ID, CounterCOA_ID FROM Bookings WHERE Account_ID = ? "
                "AND (COA_ID = ? OR CounterCOA_ID = ?)", (account_id, old_coa, old_coa))
    rows = cur.fetchall()
    conn.close()
    for booking_id, coa, counter in rows:
        db.update_booking_fields(booking_id, coa_id=new_coa if coa == old_coa else coa,
                                 counter_coa_id=new_coa if counter == old_coa else counter,
                                 log_description="Demo-Szenario")


@contextlib.contextmanager
def _without_sync(db):
    """Beim Laden wartet kein Commit auf die Platte (``PRAGMA synchronous = OFF`` an jeder Verbindung der Datenbank): Mit
    tausenden Commits dauerte das Laden auf einer langsamen Platte (Docker-Volume, NAS) sonst ein Vielfaches. Gerade
    erst angelegte Demodaten gehen bei einem Abbruch ohnehin verloren (der Marker fehlt dann); am Ende wird die Datei
    einmal auf die Platte geschrieben."""
    connect = db._get_connection

    def unsynced():
        conn = connect()
        conn.execute("PRAGMA synchronous = OFF")
        return conn
    db._get_connection = unsynced
    try:
        yield
    finally:
        del db._get_connection
        try:
            with open(db.db_name, "r+b") as f:
                os.fsync(f.fileno())
        except OSError:
            pass


class DemoScenario:
    #: Die Listen des Szenarios, an die ``extra`` anhängt
    EXTRA_LISTS = ("invoices", "monthly", "quarterly", "one_off", "splits", "trips")

    def __init__(self, db, today=None, scenario=None, extra=None, documents_days=None):
        """``extra``: die Vorgaben des Erzeugers (``demo_history.build_history``) – sie hängen an die Listen des Szenarios an,
        das Szenario selbst bleibt unverändert; ohne ``extra`` entsteht genau das Szenario. ``documents_days``: PDF und
        E-Rechnung nur für Rechnungen der letzten so vielen Tage (``None``: für alle)."""
        self.db = db
        self.today = today or datetime.date.today()
        if scenario is None:
            with open(SCENARIO_FILE, encoding="utf-8") as f:
                scenario = json.load(f)
        self.extra = extra or {}
        self.documents_days = documents_days
        self.sc = {**scenario, **{key: list(scenario.get(key, [])) + list(self.extra[key])
                                  for key in self.EXTRA_LISTS if key in self.extra}}
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
        self.savings_id = self.savings_coa = self.cash_id = self.cash_coa = None
        names = self.extra.get("accounts", {})
        if "savings" in names:
            self.savings_id, self.savings_coa = self._account(names["savings"])
        if "cash" in names:
            self.cash_id, self.cash_coa = self._account(names["cash"])
        self._contact_rows = None

    def _account(self, name):
        """ID und SKR-Konto (COA-ID) eines Kontos der Demo – aus dem Bestand, nicht geraten."""
        conn = self.db._get_connection()
        try:
            row = conn.execute("SELECT ID, SKRAccount FROM Accounts WHERE Name = ?", (name,)).fetchone()
        finally:
            conn.close()
        if row is None or row[1] is None:
            raise RuntimeError(f"Demo-Konto ‚{name}‘ fehlt oder hat keine SKR-Nummer")
        return row[0], self.db.get_coa_id_by_account_number(row[1])

    def _coa_numbers(self):
        nums = set()
        for key in ("monthly", "quarterly", "one_off"):
            nums |= {e["coa"] for e in self.sc.get(key, [])}
        for s in self.sc.get("splits", []):
            nums |= {r["coa"] for r in s["rows"]}
        for a in self.extra.get("assets", []):
            nums |= {a["coa"], a["afa_coa"]}
            if a.get("sale"):
                nums.add(a["sale"]["coa"])
        return nums

    def _contacts(self):
        """Kunden, Lieferanten und die eigene Firma, einmal gelesen."""
        if self._contact_rows is None:
            db = self.db
            own = db.fetch_contacts(contact_type='own')
            self._contact_rows = SimpleNamespace(
                customers={c[2]: c for c in db.fetch_contacts(contact_type='customer')},
                suppliers={c[2]: c for c in db.fetch_contacts(contact_type='supplier')},
                own=own[0] if own else None)
        return self._contact_rows

    def _date(self, days_ago):
        return self.today - datetime.timedelta(days=days_ago)

    def _wants_documents(self, day):
        return self.documents_days is None or day >= self.today - datetime.timedelta(days=self.documents_days)

    # ── Ausgaben / Splits ────────────────────────────────────────────────
    def _bank(self, date, amount, recipient, text, doc=None, contact_id=None):
        return self.db.insert_booking(
            date_booking=date.isoformat(), amount=amount, account_id=self.account_id,
            recipient_client=recipient, contact_id=contact_id, text=text, document_number=doc,
            booking_type='bank', log_description="Demo-Szenario")

    def _entry(self, parent_id, date, amount, coa, tax_rate, recipient, text, doc=None, tax_amount=None):
        self.db.insert_booking(
            date_booking=date.isoformat(), amount=amount, coa_id=self.coa[coa],
            counter_coa_id=self.bank_coa, tax_rate=tax_rate,
            tax_amount=tax_amount if tax_amount is not None else _tax_amount(amount, tax_rate),
            recipient_client=recipient, text=text, document_number=doc, booking_type='entry',
            parent_booking_id=parent_id, log_description="Demo-Szenario")

    def _expense(self, date, e, seq):
        text = e["text"].format(mm=f"{date.month:02d}", yyyy=date.year)
        doc = f"E{date:%y%m}-{seq:02d}"
        if e.get("cash"):
            self._cash_expense(date, e, text, doc)
            return
        bid = self._bank(date, e["amount"], e["recipient"], text, doc)
        self._entry(bid, date, e["amount"], e["coa"], e.get("tax_rate"),
                    e["recipient"], text, doc, tax_amount=e.get("tax_amount"))
        if e.get("receipt"):
            self._receipt(bid, doc, date, text)

    def _cash_expense(self, date, e, text, doc):
        """Ein eigenständiger Satz auf der Kasse (ohne Bankbewegung): Aufwandskonto an Kasse."""
        self.db.insert_booking(
            date_booking=date.isoformat(), amount=e["amount"], account_id=self.cash_id, coa_id=self.coa[e["coa"]],
            counter_coa_id=self.cash_coa, tax_rate=e.get("tax_rate"), tax_amount=e.get("tax_amount"),
            recipient_client=e["recipient"], text=text, document_number=doc, booking_type='entry',
            log_description="Demo-Szenario")

    def _receipt(self, booking_id, number, date, text):
        """Ein Beleg als Metadaten (ohne Datei, wie in den Test-Seeddaten), an die Bankbewegung geknüpft."""
        self.db.insert_receipt(number=number, date=date.isoformat(), filename=f"{number}.pdf", path="", info=text)
        receipt = self.db.get_receipt_by_number(number)
        if receipt:
            self.db.link_booking_to_document(booking_id, receipt[0], "receipt")

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
                            s["recipient"], r["text"], doc, tax_amount=r.get("tax_amount"))

    # ── Rechnungen + Zahlungen ───────────────────────────────────────────
    def load_invoices(self):
        """Alle Rechnungsereignisse über EINE Warteschlange, sortiert nach (Datum, laufende Nr.): Ein Ereignis legt die
        folgenden an (Zahlung, Storno, Ersatzrechnung, Erstattung) – was nach dem Szenariodatum läge, entfällt."""
        from server import handlers
        self._handlers = handlers
        self._events = []
        self._order = itertools.count()
        plans = [self._plan(spec) for spec in sorted(self.sc.get("invoices", []), key=lambda s: -s["days_ago"])]
        for plan in plans:
            self._push(plan.day, "invoice", plan)
        self._load_quotes()
        year = None
        while self._events:
            day, _order, kind, plan, arg = heapq.heappop(self._events)
            if self.extra and day.year != year:
                year = day.year
                print(f"Demo: Rechnungen {year} …")
            getattr(self, f"_on_{kind}")(day, plan, arg)
        if self.extra:
            self._mark_overdue()

    def _push(self, day, kind, plan, arg=None):
        heapq.heappush(self._events, (day, next(self._order), kind, plan, arg))

    def _plan(self, spec):
        """Eine Rechnung der Vorgaben als Ablaufplan: ihr Tag, ihre Zahlungen und – sobald sie angelegt ist – ihre IDs."""
        if spec.get("via") == "form":
            payments = list(spec.get("payments", []))
        elif spec.get("pay_after_days") is not None:
            payments = [{"after_days": spec["pay_after_days"], "share": spec.get("partial_share")}]
        else:
            payments = []
        return SimpleNamespace(spec=spec, form=spec.get("via") == "form", day=self._date(spec["days_ago"]),
                               payments=payments, invoice_id=None, number=None, customer=None, paid=0,
                               storno_id=None, storno_number=None)

    def _on_invoice(self, day, plan, _arg):
        if plan.form:
            self._create_form_invoice(plan)
        else:
            self._create_legacy_invoice(plan)
        spec = plan.spec
        for payment in plan.payments:
            pay_day = plan.day + datetime.timedelta(days=payment["after_days"])
            if pay_day <= self.today:
                self._push(pay_day, "pay", plan, payment)
        after = spec.get("cancel_after_days")
        if after is not None and spec.get("final_status") != "finalized":
            cancel_day = plan.day + datetime.timedelta(days=after)
            if cancel_day <= self.today:
                self._push(cancel_day, "cancel", plan)

    def _create_legacy_invoice(self, plan):
        """Eine Rechnung des Szenarios: Seed-Weg (``insert_seed_invoices``), Versenden wie im UI."""
        handlers, db, spec, inv_date = self._handlers, self.db, plan.spec, plan.day
        customer = self._contacts().customers[spec["customer_number"]]
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
        if self._wants_documents(inv_date):
            try:
                handlers._ensure_sent_documents(db, invoice_id, 'finalized', 'sent')
            except Exception as exc:                       # PDF ist Beiwerk
                print(f"Demo: PDF zu {number} nicht erzeugt: {exc}")
        plan.invoice_id, plan.number, plan.customer = invoice_id, number, customer

    def _form_data(self, customer, day, due, number, spec, items, skonto=None, bank_account_id=None, delivery=None):
        """Die Formulardaten einer Rechnung, wie die Maske sie schickt (``/invoice/save``): Summen rechnet der Server."""
        show_tax = spec["show_tax"]
        rate = max((item["tax_rate"] for item in items), default=0)
        form_items = [{"position": index + 1, "description": item["description"], "quantity": item["quantity"],
                       "unit": item["unit"], "unitPrice": item["price_per_unit"],
                       "taxRate": item["tax_rate"] if show_tax else 0} for index, item in enumerate(items)]
        return {
            "invoiceNumber": number, "invoiceDate": day.isoformat(), "customerId": customer[0],
            "ownCompanyId": self._contacts().own[0], "paymentTerms": spec["payment_terms"], "dueDate": due,
            "bankAccountId": bank_account_id or self.account_id, "taxRate": rate / 100 if show_tax else 0,
            "showTax": show_tax, "currency": "EUR", "items": form_items, "deliveryDate": delivery or day.isoformat(),
            "discountPercentage": skonto["percent"] if skonto else None,
            "discountDays": skonto["days"] if skonto else None}

    def _create_form_invoice(self, plan):
        """Eine Rechnung des Erzeugers über den Formularweg (``handle_invoice_save``) in EINER Transaktion, danach
        Versenden (``set_invoice_status`` bucht die Forderung) und – für die letzten Tage – das Dokument."""
        handlers, db, spec = self._handlers, self.db, plan.spec
        customer = self._contacts().customers[spec["customer_number"]]
        due = (plan.day + datetime.timedelta(days=spec["due_days"])).isoformat()
        data = self._form_data(customer, plan.day, due, "neu", spec, spec["items"], spec.get("skonto"))
        invoice_data, item_rows = handlers._invoice_fields(db, data, None)
        with db.transaction() as tx:
            invoice_data["invoice_number"] = db.allocate_invoice_number(tx.conn, invoice_data["invoice_date"], None)
            invoice_data["status"] = "finalized" if spec.get("final_status") == "finalized" else "draft"
            invoice_id = db.insert_invoice(invoice_data, tx=tx)
            for item in item_rows:
                db.insert_invoice_item({**item, "invoice_id": invoice_id}, tx=tx)
        number = invoice_data["invoice_number"]
        plan.invoice_id, plan.number, plan.customer = invoice_id, number, customer
        if spec.get("final_status") == "finalized":
            return
        self._send(invoice_id, number, plan.day, "draft")

    def _send(self, invoice_id, number, day, current_status):
        """Versenden wie in der Oberfläche: Status und Forderung in einer Transaktion, danach das Dokument."""
        change = self.db.set_invoice_status(invoice_id, 'sent', 'demo')
        if not change.ok:
            raise RuntimeError(f"Demo-Rechnung {number} nicht versendet: {change.message}")
        if self._wants_documents(day):
            warning = self._handlers._ensure_sent_documents(self.db, invoice_id, current_status, 'sent')
            if warning:
                print(f"Demo: Dokument zu {number} nicht erzeugt: {warning}")
        return change

    def _on_pay(self, day, plan, payment):
        db = self.db
        invoice = db.get_invoice_by_id(plan.invoice_id)
        if plan.form:
            gross = money.to_minor(invoice[38])
            minor = gross - plan.paid if payment.get("closing") else \
                money.round_minor(money.prorate(gross, payment["percent"], 100), 2)
            plan.paid += minor
            amount = money.from_minor(minor)
        else:
            gross = float(invoice[38])
            share = payment.get("share")
            amount = round(gross * share, 2) if share else gross
        customer = plan.customer
        bid = db.insert_booking(
            date_booking=day.isoformat(), amount=amount, account_id=self.account_id,
            recipient_client=customer[3], contact_id=customer[0],
            text=f"{plan.number} {customer[3]}", booking_type='bank',
            log_description="Demo-Szenario Zahlungseingang")
        ok, err = self._handlers.link_booking_to_invoice_capped(db, plan.invoice_id, bid)
        if not ok:
            raise RuntimeError(f"Zahlung zu {plan.number}: {err}")

    def _on_cancel(self, day, plan, _arg):
        """Storno wie im UI: Stornorechnung samt Gegenbuchung, danach ihr PDF/XRechnung. Nur an einem Tag, der nicht
        in der Zukunft liegt – gemessen am Szenariodatum, das für die Prüfung des Stornodatums auch ``today`` der
        Datenbankschicht ist (ein Szenario mit späterem Datum als dem des Servers bleibt möglich)."""
        result = self.db.cancel_invoice(plan.invoice_id, day.isoformat(), "demo", today=self.today)
        if not result.ok:
            raise RuntimeError(f"Storno zu {plan.number}: {result.message}")
        plan.storno_id, plan.storno_number = result.storno_id, result.storno_number
        if self._wants_documents(day):
            from export.invoice_files import generate_invoice_documents
            try:
                generate_invoice_documents(self.db, result.storno_id, overwrite_sent=True)
            except Exception as exc:                           # PDF ist Beiwerk
                print(f"Demo: PDF zu {result.storno_number} nicht erzeugt: {exc}")
        chain = plan.spec.get("replacement")
        if chain:
            replace_day = day + datetime.timedelta(days=chain["after_days"])
            if replace_day <= self.today:
                self._push(replace_day, "replace", plan, chain)

    def _on_replace(self, day, plan, chain):
        """„Stornieren und neu ausstellen“ wie in der Oberfläche: Entwurf der Ersatzrechnung (``create_replacement_draft``),
        niedrigere Positionen speichern wie die Maske, versenden – dabei zieht die Zahlung der stornierten Rechnung auf
        sie um; der Rest geht später als Erstattung zurück."""
        db, handlers = self.db, self._handlers
        draft = db.create_replacement_draft(plan.invoice_id, "demo", today=day)
        if not draft.ok:
            raise RuntimeError(f"Ersatzrechnung zu {plan.number}: {draft.message}")
        conn = db._get_connection()
        try:
            from db.invoice_lock import invoice_row
            row = invoice_row(conn, draft.invoice_id)
        finally:
            conn.close()
        skonto = {"days": row["SkontoDays"], "percent": row["SkontoPercent"]} if row["SkontoDays"] else None
        data = self._form_data(plan.customer, day, row["PaymentDueDate"], row["InvoiceNumber"], plan.spec, chain["items"],
                               skonto, bank_account_id=row["BankAccountId"], delivery=row["DeliveryDate"])
        invoice_data, item_rows = handlers._invoice_fields(db, data, db.get_document(draft.invoice_id, "invoice"))
        with db.transaction() as tx:
            db.update_invoice_fields(tx, draft.invoice_id, invoice_data, item_rows)
        change = self._send(draft.invoice_id, draft.invoice_number, day, "draft")
        if not change.moved:
            raise RuntimeError(f"Ersatzrechnung {draft.invoice_number}: Die Zahlung zog nicht um ({change.payment_hint})")
        after = chain.get("refund_after_days")
        if after is not None and day + datetime.timedelta(days=after) <= self.today:
            self._push(day + datetime.timedelta(days=after), "refund", plan)

    def _on_refund(self, day, plan, _arg):
        """Was nach dem Umzug als Guthaben an der stornierten Rechnung bleibt, geht per ausgehender Bankbewegung zurück."""
        db, customer = self.db, plan.customer
        credit = db.get_storno_credit(plan.invoice_id)
        if not credit or credit <= 0:
            raise RuntimeError(f"Erstattung zu {plan.storno_number}: kein Guthaben")
        bid = db.insert_booking(
            date_booking=day.isoformat(), amount=-credit, account_id=self.account_id,
            recipient_client=customer[3], contact_id=customer[0],
            text=f"Erstattung {plan.storno_number} {customer[3]}", booking_type='bank',
            log_description="Demo-Szenario Erstattung")
        refund = db.assign_storno_refund(plan.storno_id, bid)
        if not refund.ok:
            raise RuntimeError(f"Erstattung zu {plan.storno_number}: {refund.message}")

    def _on_convert(self, day, quote_id, _arg):
        """Ein Angebot wird zum Rechnungsentwurf (``convert_quote_to_invoice``: Nummer und Datum des Tages ``day``)."""
        if not self.db.convert_quote_to_invoice(quote_id, today=day):
            raise RuntimeError(f"Angebot {quote_id} nicht umgewandelt")

    def _mark_overdue(self):
        """Unbezahlte Rechnungen nach der Fälligkeit stehen auf „überfällig“ (``set_invoice_status``, wie von Hand)."""
        conn = self.db._get_connection()
        try:
            rows = conn.execute(
                "SELECT ID, InvoiceNumber FROM Invoices WHERE Status = 'sent' AND InvoiceKind IS NOT 'storno' "
                "AND (DocumentType = 'invoice' OR DocumentType IS NULL) AND PaymentDueDate < ? "
                "ORDER BY InvoiceDate, ID", (self.today.isoformat(),)).fetchall()
        finally:
            conn.close()
        for invoice_id, number in rows:
            change = self.db.set_invoice_status(invoice_id, 'overdue', 'demo')
            if not change.ok:
                raise RuntimeError(f"Demo-Rechnung {number} nicht auf überfällig gesetzt: {change.message}")

    # ── Angebote ─────────────────────────────────────────────────────────
    def _load_quotes(self):
        for spec in self.extra.get("quotes", []):
            quote_id = self._create_quote(spec)
            convert = spec.get("convert")
            if convert and self._date(convert["days_ago"]) <= self.today:
                self._push(self._date(convert["days_ago"]), "convert", quote_id)

    def _create_quote(self, spec):
        """Ein Angebot, wie ``handle_quote_save`` es speichert (Summen serverseitig exakt, Fließtext über die Positivliste).
        Ein Angebot, das später umgewandelt wird, entsteht als „angenommen“ – ``convert_quote_to_invoice`` setzt es um."""
        from richtext import sanitize_richtext
        handlers, db = self._handlers, self.db
        contacts = self._contacts()
        customer, own = contacts.customers[spec["customer_number"]], contacts.own
        show_tax = spec["show_tax"]
        rate = max((item["tax_rate"] for item in spec["items"]), default=0)
        items = [{"position": index + 1, "description": item["description"], "quantity": item["quantity"],
                  "unit": item["unit"], "unitPrice": item["price_per_unit"],
                  "taxRate": item["tax_rate"] if show_tax else 0} for index, item in enumerate(spec["items"])]
        net, tax, gross, line_nets = handlers.recompute_invoice_totals(items, handlers._rate_to_pct(rate / 100 if show_tax else 0))
        doc = {
            'invoice_number': spec["number"], 'invoice_date': spec["date"], 'own_company_id': own[0],
            'seller_name': own[4] or own[3] or '', 'seller_company': own[4] or '', 'seller_street': own[5] or '',
            'seller_postal_code': own[6] or '', 'seller_city': own[7] or '', 'seller_country': own[8] or 'DE',
            'seller_vat_id': own[11] or '', 'seller_email': own[9] or '', 'seller_phone': own[10] or '',
            'customer_id': customer[0], **handlers.buyer_snapshot(customer), 'buyer_vat_id': customer[11] or '',
            'currency': 'EUR', 'tax_category': 'S', 'tax_rate': rate / 100 if show_tax else -1,
            'sum_net': net, 'tax_amount': tax, 'sum_gross': gross, 'amount_due': gross,
            'status': "accepted" if spec["status"] == "converted" else spec["status"],
            'document_type': 'quote', 'valid_until': spec["valid_until"],
            'intro_text': sanitize_richtext(spec.get("intro")) or None,
            'closing_text': sanitize_richtext(spec.get("closing")) or None,
        }
        with db.transaction() as tx:
            quote_id = db.insert_invoice(doc, tx=tx)
            for index, item in enumerate(items):
                db.insert_invoice_item({
                    'invoice_id': quote_id, 'position': item['position'], 'article_id': None,
                    'description': item['description'], 'quantity': item['quantity'], 'unit': item['unit'],
                    'price_per_unit': item['unitPrice'], 'total_net': line_nets[index], 'tax_category': 'S',
                    'tax_rate': item['taxRate']}, tx=tx)
        return quote_id

    # ── Fahrten ──────────────────────────────────────────────────────────
    def load_trips(self):
        # Fahrer = dieselbe Person wie bei den Arbeitszeiten (Seed: K022)
        persons = {c[2]: c[0] for c in self.db.fetch_contacts(contact_type='customer')}
        driver = persons.get(self.sc.get("driver_number"))
        if not driver:
            return
        # Die Fahrten des Erzeugers liegen vor der ersten des Szenarios: Die Tachostände rechnen rückwärts vom Startstand
        km = self.sc.get("start_km", 0) - sum(t["distance_km"] for t in self.extra.get("trips", []))
        if km < 0:
            raise RuntimeError("Demo-Fahrten: Der Tachostand zu Beginn wäre negativ")
        for t in sorted(self.sc.get("trips", []), key=lambda t: -t["days_ago"]):
            d = self._date(t["days_ago"]).isoformat()
            dist = t["distance_km"]
            self.db.insert_trip(
                driver_id=driver, start_date=d, start_time="08:00", end_date=d,
                end_time="17:30", start_point=t["start_point"],
                destination=t["destination"], vehicle=self.sc.get("vehicle", ""),
                reason=t["reason"], distance_km=dist, start_km=km, end_km=km + dist)
            km += dist

    # ── Anlagen, Überträge, Arbeitszeiten, offene Bankbewegungen ──────────
    def load_assets(self):
        """Kauf als Bankbewegung mit dem Satz „Anlagekonto an Bank“, die Anlage, ``book_depreciation`` für jedes abgeschlossene
        Jahr (das laufende bleibt geplant) und – wo es einen gibt – der Abgang samt Erlös über das Erlöskonto."""
        db, contacts = self.db, self._contacts()
        categories = {row[1]: row[0] for row in db.fetch_asset_categories()}
        for spec in sorted(self.extra.get("assets", []), key=lambda a: a["days_ago"], reverse=True):
            day = self._date(spec["days_ago"])
            doc = f"AN{day:%y%m%d}"
            bank_id = self._bank(day, -spec["gross"], spec["recipient"], spec["text"], doc)
            self._entry(bank_id, day, -spec["gross"], spec["coa"], spec["tax_rate"], spec["recipient"], spec["text"], doc,
                        tax_amount=-spec["tax_amount"] if spec["tax_amount"] is not None else None)
            supplier = contacts.suppliers.get(spec["supplier_number"])
            asset_id = db.insert_asset(
                name=spec["name"], purchase_date=day.isoformat(), purchase_price=spec["price"],
                useful_life_years=spec["years"], description=spec["text"], asset_category_id=categories.get(spec["category"]),
                coa_id=self.coa[spec["coa"]], depreciation_method='linear', serial_number=spec["serial_number"],
                location=spec["location"], supplier_id=supplier[0] if supplier else None, booking_id=bank_id)
            for entry in db.depreciation_plan(asset_id):
                if entry["year"] < self.today.year and not entry["booked"] and entry["bookable"]:
                    db.book_depreciation(asset_id, entry["year"], coa_id_expense=self.coa[spec["afa_coa"]],
                                         coa_id_asset=self.coa[spec["coa"]])
            sale = spec.get("sale")
            if sale:
                sold = self._date(sale["days_ago"])
                buyer = contacts.customers[sale["buyer_number"]]
                db.sell_asset(asset_id, sold.isoformat(), sale["price"])
                doc = f"VK{sold:%y%m%d}"
                bank_id = self._bank(sold, sale["gross"], buyer[3], sale["text"], doc, contact_id=buyer[0])
                self._entry(bank_id, sold, sale["gross"], sale["coa"], sale["tax_rate"], buyer[3], sale["text"], doc,
                            tax_amount=sale["tax_amount"])

    def _mirror(self, bank_id, date, amount, coa_id, counter_coa_id, recipient, text):
        """Der Spiegelsatz unter einer Bankbewegung zwischen eigenen Konten (beide Seiten Geldkonten)."""
        self.db.insert_booking(
            date_booking=date.isoformat(), amount=amount, coa_id=coa_id, counter_coa_id=counter_coa_id,
            recipient_client=recipient, text=text, booking_type='entry', parent_booking_id=bank_id,
            log_description="Demo-Szenario")

    def load_transfers(self):
        """Übertrag zum Tagesgeld: beide Bankbewegungen am selben Tag mit ihrem Spiegel kontiert. Bargeld: die Bankbewegung
        mit dem Spiegel Kasse an Bank."""
        for spec in sorted(self.extra.get("transfers", []), key=lambda t: t["days_ago"], reverse=True):
            day, amount = self._date(spec["days_ago"]), spec["amount"]
            bank_id = self._bank(day, -amount, spec["recipient"], spec["text"])
            if spec["kind"] == "savings":
                self._mirror(bank_id, day, -amount, self.savings_coa, self.bank_coa, spec["recipient"], spec["text"])
                incoming = self.db.insert_booking(
                    date_booking=day.isoformat(), amount=amount, account_id=self.savings_id,
                    recipient_client=spec["recipient"], text=spec["text"], booking_type='bank',
                    log_description="Demo-Szenario")
                self._mirror(incoming, day, amount, self.bank_coa, self.savings_coa, spec["recipient"], spec["text"])
            else:
                self._mirror(bank_id, day, -amount, self.cash_coa, self.bank_coa, spec["recipient"], spec["text"])

    def load_worktimes(self):
        contacts = self._contacts()
        person = contacts.customers.get(self.sc.get("driver_number"))
        if not person:
            return
        own_city = (contacts.own[7] if contacts.own else '') or ''
        for w in self.extra.get("worktimes", []):
            customer = contacts.customers.get(w.get("customer_number")) if w.get("customer_number") else None
            city = own_city if w["location_mode"] == "own" else ((customer[7] if customer else '') or '')
            self.db.insert_worktime(
                person_id=person[0], date=w["date"], kind=w["kind"], customer_id=customer[0] if customer else None,
                start_time=w.get("start_time", ""), end_time=w.get("end_time", ""),
                pause_minutes=w.get("pause_minutes", 0), location_mode=w["location_mode"], location_city=city,
                note=w.get("note", ""))

    def load_open_bank(self):
        """Bankbewegungen ohne Buchungssatz im laufenden Monat – so sieht die Demo „noch zu buchen“."""
        for spec in self.extra.get("open_bank", []):
            self._bank(self._date(spec["days_ago"]), spec["amount"], spec["recipient"], spec["text"])

    def load_all(self):
        with _without_sync(self.db):
            self.load_invoices()
            self.load_expenses()
            self.load_trips()
            if self.extra:
                self.load_assets()
                self.load_transfers()
                self.load_worktimes()
                self.load_open_bank()


def load_demo_scenario(db, today=None, profile="full"):
    """Das Szenario samt dem Bestand des Erzeugers laden (Aufruf nach
    ``load_test_seed_data(include_invoices=False, include_dated=False)``). ``profile``: ``full`` (drei volle Vorjahre und das
    laufende Jahr) oder ``test`` (ein Vorjahr und das laufende Jahr)."""
    import demo_history
    today = today or datetime.date.today()
    extra = demo_history.build_history(today, profile)
    DemoScenario(db, today, extra=extra, documents_days=DOCUMENT_DAYS).load_all()
