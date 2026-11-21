# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen der Tests zur Summen- und Saldenliste (``test_susa.py``, ``test_susa_pdf.py``): eine kleine Welt, in der jeder Satz über einen Weg der App
entsteht – und Hilfen, um Bestände von Hand zu schreiben und die Liste mit der EÜR und den Bankbewegungen zu vergleichen.

Die „SuSa-Welt“ (``build_susa_world``) ist so gebaut, dass die Abstimmungsproben darin **exakt** aufgehen: Es gibt keinen Satz, der keiner Bankbewegung
zugeordnet ist (eine Kasse und das Verrechnungskonto ausgenommen), keinen Satz außerhalb der vier Steuersätze, keine Umbuchung ohne Zahlung. Was die
Liste und die EÜR trennt (Sätze ohne Bankbewegung, ``resolved``, Sätze auf 6884–6892, Steuersätze außer 5, 7, 16 und 19 %), baut jeder Test selbst auf.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (Stichtage sind feste Tage).
"""
import datetime
import sqlite3
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

from db import susa
from db.bwa import bwa_periods
from db.reporting import bank_skr_numbers, euer_section
from money import to_minor
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped

DEMO_TODAY = datetime.date(2026, 11, 3)         # ein fester Tag, nie die Uhr (tests/test_clock_at_import.py)
STORNO_TODAY = datetime.date(2026, 11, 21)


def coa(db, number):
    return db.get_coa_id_by_account_number(number)


def account_id(db, name):
    return next(account[0] for account in db.fetch_accounts() if account[1] == name)


def mask(db, account, number, amount, date, rate=None, tax=None, document=''):
    """Eine Zahlung auf ein Zweckkonto über die Buchungsmaske (Bankbewegung und ein Satz darunter); ``account`` ist der Name des Kontos."""
    post = {'transaction_id': ['0'], 'date': [date], 'amount': [amount], 'account': [str(account_id(db, account))],
            'coa_id': [str(coa(db, number))], 'document_nr': [document]}
    if rate:
        post.update({'tax_rate': [rate], 'tax_amount': [tax]})
    status, location = handle_add_transaction(db, post)
    assert status == 303, (status, location)


def invoice(db, number, gross, day, rate=0.19):
    """Eine versendete Rechnung über ``gross`` € zum Datum ``day`` (nur der Beleg; Forderung und Zahlung buchen die Aufrufer)."""
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': rate,
        'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': 'sent'})


def paid_invoice(db, number, gross, day, paid_on, account, rate=0.19, revenue_coa_id=None):
    """Eine Rechnung mit Forderung (Debitoren an Wartekonto), am Tag ``paid_on`` auf ``account`` voll gezahlt – so, wie die App es bucht."""
    invoice_id = invoice(db, number, gross, day, rate)
    db.ensure_receivable_booking(invoice_id)
    bank = db.insert_booking(paid_on, Decimal(str(gross)), account_id=account_id(db, account), booking_type='bank', recipient_client='Käufer AG')
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, revenue_coa_id=revenue_coa_id)
    assert ok, error
    return invoice_id, bank


def build_susa_world(db):
    """Die SuSa-Welt: zwei Bankkonten (1800 „Probe-Giro“, 1810 „Probe-Zweit“) und die Kasse (1600).

    * 2025: eine Einnahme und eine Ausgabe mit 19 %, eine Rechnung vom Dezember mit Forderung, die im Januar 2026 bezahlt wird (das Wartekonto trägt über
      den Jahreswechsel), eine Privatentnahme;
    * 2026: Einnahmen und Ausgaben mit 19 % und 7 %, eine Erstattung eines Einkaufs, Privatentnahme unter einer Bankbewegung, die Abschreibung einer Anlage,
      ein Kassensatz (unter einer Bewegung der Kasse und einer ohne), ein Satz „Aufwand an Verrechnung“ (1460); je eine Rechnung, die im selben Monat und
      im Folgemonat bezahlt wird, eine mit 7 %, eine storniert und erstattet; ein Übertrag 1800 → 1810 mit Spiegel auf beiden Bewegungen am selben Tag
      (der Eingangs-Satz fällt weg), einer mit satzloser Gegenseite (gedeckt) und eine Bankbewegung ohne Satz (nicht gedeckt).
    """
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    giro, zweit, kasse = (account_id(db, name) for name in ('Probe-Giro', 'Probe-Zweit', 'Kasse'))

    # ── 2025 ──
    mask(db, 'Probe-Giro', 4400, '119.00', '2025-03-10', '19', '19.00')
    mask(db, 'Probe-Giro', 6815, '-59.50', '2025-03-12', '19', '-9.50')
    mask(db, 'Probe-Giro', 2100, '-100.00', '2025-06-05')
    paid_invoice(db, 'R25-1', 238.00, '2025-12-15', '2026-01-10', 'Probe-Giro')           # die Forderung steht im Dezember, die Zahlung im Januar

    # ── 2026 ──
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-01-20', '19', '19.00')
    mask(db, 'Probe-Giro', 4300, '107.00', '2026-02-03', '7', '7.00')
    mask(db, 'Probe-Giro', 6815, '-119.00', '2026-02-05', '19', '-19.00')
    mask(db, 'Probe-Zweit', 6850, '-53.50', '2026-02-07', '7', '-3.50')
    mask(db, 'Probe-Giro', 6815, '23.80', '2026-02-12', '19', '3.80')                       # eine Erstattung (Betrag positiv auf einem Aufwandskonto)
    mask(db, 'Probe-Giro', 2100, '-80.00', '2026-02-14')
    mask(db, 'Kasse', 6805, '-12.00', '2026-02-16', '19', '-1.92')
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
    db.insert_booking('2026-03-03', Decimal('-5.95'), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1600), booking_type='entry', tax_rate=0.19,
                      tax_amount=Decimal('-0.95'), document_number='KS-1')                   # ein Kassensatz ohne Bankbewegung
    db.insert_booking('2026-03-04', Decimal('-20.00'), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1460), booking_type='entry',
                      document_number='VR-1')                                              # Aufwand an Verrechnung
    paid_invoice(db, 'R26-1', 119.00, '2026-03-05', '2026-03-20', 'Probe-Giro')           # Forderung und Zahlung im selben Monat
    paid_invoice(db, 'R26-2', 107.00, '2026-03-25', '2026-04-08', 'Probe-Zweit', rate=0.07)       # Zahlung im Folgemonat
    storno_invoice, _bank = paid_invoice(db, 'R26-3', 119.00, '2026-05-02', '2026-05-10', 'Probe-Giro')
    storno = db.cancel_invoice(storno_invoice, '2026-06-02', 'tester', today=STORNO_TODAY)
    assert storno.ok, storno.message
    refund = db.insert_booking('2026-06-05', Decimal('-119.00'), account_id=giro, booking_type='bank', recipient_client='Käufer AG')
    assert db.assign_storno_refund(storno.storno_id, refund).ok

    # Überträge: mit Spiegel auf beiden Bewegungen (derselbe Tag), mit satzloser Gegenseite, und eine Bewegung ohne Satz
    mask(db, 'Probe-Giro', 1810, '-500.00', '2026-07-01')
    mask(db, 'Probe-Zweit', 1800, '500.00', '2026-07-01')
    mask(db, 'Probe-Giro', 1810, '-300.00', '2026-07-08')
    db.insert_booking('2026-07-08', Decimal('300.00'), account_id=zweit, booking_type='bank')
    db.insert_booking('2026-08-12', Decimal('119.00'), account_id=giro, booking_type='bank', recipient_client='Unbekannt')
    return SimpleNamespace(db=db, giro=giro, zweit=zweit, kasse=kasse)


def raw(db, statement, *args):
    """Eine Anweisung an der App vorbei (wie die SQL-Konsole oder ein Bestand aus einer anderen Zeit)."""
    with closing(sqlite3.connect(db.db_name)) as con:
        cursor = con.execute(statement, args)
        con.commit()
        return cursor.lastrowid


def raw_entry(db, date, coa_number, counter_number, amount, form='purpose_first', parent=None, rate=None, tax=None, document='', status=None,
              account=None):
    """Ein Buchungssatz, wie ihn nur ein Eingriff von Hand schreibt: ``form`` ohne Prüfung (``None`` = keine), Beträge in Minor Units – auch
    ein Wert, der keine ganze Zahl ist (``amount`` darf ein ``str`` oder ``float`` sein)."""
    return raw(db, "INSERT INTO Bookings (DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, DocumentNumber, BookingType, "
                   "ParentBooking_ID, EntryForm, Status, Currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, ?, ?, 'EUR')",
               date, account, coa(db, coa_number) if coa_number else None, coa(db, counter_number) if counter_number else None, amount, rate, tax,
               document, parent, form, status)


def raw_bank(db, date, account, amount):
    """Eine Bankbewegung ``amount`` Minor Units auf ``account`` (Accounts.ID)."""
    return raw(db, "INSERT INTO Bookings (DateBooking, Account_ID, Amount, BookingType, Currency) VALUES (?, ?, ?, 'bank', 'EUR')", date, account, amount)


def minor(euro):
    """Ein Betrag in Euro (als Text) in Minor Units."""
    return to_minor(Decimal(euro))


# ── Der Bericht als Tabelle ─────────────────────────────────────────────────

def accounts_of(report):
    """Alle Konten des Berichts als ``{Schlüssel: SusaAccount}`` (auch die nicht sichtbaren)."""
    return {account.key: account for group in report.groups for account in group.accounts}


def total_row(report):
    t = report.total
    return t.opening, t.month_soll, t.month_haben, t.ytd_soll, t.ytd_haben, t.closing


def assert_balanced(report, where=''):
    """Probe (a): Summe Soll gleich Summe Haben (Monat, aufgelaufen), Summe der Anfangssalden und der Endsalden null – auf den Minor-Wert."""
    opening, month_soll, month_haben, ytd_soll, ytd_haben, closing = total_row(report)
    assert (opening, month_soll - month_haben, ytd_soll - ytd_haben, closing) == (0, 0, 0, 0), (where, total_row(report))
    for group in report.groups:                       # die Gruppen summieren sich zur Gesamtsumme, jede Gruppe aus ihren Konten
        assert group.opening == sum(account.opening for account in group.accounts), (where, group.group.key)
        assert group.closing == sum(account.closing for account in group.accounts), (where, group.group.key)
    assert report.total.opening == sum(group.opening for group in report.groups)


def euer_values(db, span):
    """Die Zeilen der EÜR, die in einem Abschnitt stehen, als ``{Kontonummer: Minor Units}`` (die zwei Zeilen 3806 addiert) – ``to_minor`` je Zeile."""
    banks = bank_skr_numbers(db.fetch_accounts())
    result = {}
    for number, _name, total in db.get_euer_data(*span):
        if euer_section(number, banks) is not None:
            result[number] = result.get(number, 0) + to_minor(total)
    return result


def check_erfolgskonten_against_euer(db, report, where=''):
    """Probe (c): Für jede EÜR-Zeile eines Abschnitts außer 3806 ist ``to_minor`` der EÜR gleich (Haben − Soll) der Liste im selben Zeitraum;
    die Umsatzsteuer der EÜR (3806, die echte und die virtuelle Zeile) ist (H − S) von 3801 plus 3806. Und umgekehrt: Kein Erfolgskonto der Liste
    hat einen Wert, den die EÜR nicht kennt. Returns die Abweichungen als Liste (leer = alles stimmt)."""
    accounts = accounts_of(report)
    deviations = []
    for period, span in (('month', report.periods['month']), ('ytd', report.periods['ytd'])):
        def net(number):
            found = accounts.get(number)
            if found is None:
                return 0
            return found.month_haben - found.month_soll if period == 'month' else found.ytd_haben - found.ytd_soll
        expected = euer_values(db, span)
        for number, value in expected.items():
            got = net(3801) + net(3806) if number == 3806 else net(number)
            if got != value:
                deviations.append((where, period, number, value, got))
        for number, found in accounts.items():
            if isinstance(number, int) and 4000 <= number <= 7999 and number not in susa.OPEN_ITEM_NUMBERS and net(number) and number not in expected:
                deviations.append((where, period, number, 0, net(number)))
    return deviations


def check_bank_accounts(db, report, where=''):
    """Probe (b): Je eigenem Bankkonto ist der Endsaldo der Liste die Summe aller Bankbewegungen bis Monatsende und (Soll − Haben) aufgelaufen die
    Summe der Bankbewegungen im Jahr bis Monatsende. Returns die Abweichungen (leer = alles stimmt)."""
    deviations = []
    year_start = report.periods['ytd'][0]
    end = report.periods['ytd'][1]
    accounts = accounts_of(report)
    with closing(sqlite3.connect(db.db_name)) as con:
        for account_id_, name, is_cash, number in con.execute('SELECT ID, Name, IsCash, SKRAccount FROM Accounts'):
            if is_cash == 1 or number is None:
                continue
            total = con.execute("SELECT COALESCE(SUM(Amount), 0) FROM Bookings WHERE BookingType = 'bank' AND Account_ID = ? AND DateBooking <= ?",
                                (account_id_, end)).fetchone()[0]
            year = con.execute("SELECT COALESCE(SUM(Amount), 0) FROM Bookings WHERE BookingType = 'bank' AND Account_ID = ? AND DateBooking >= ? "
                               "AND DateBooking <= ?", (account_id_, year_start, end)).fetchone()[0]
            found = accounts.get(number)
            closing_balance = found.closing if found else 0
            ytd = (found.ytd_soll - found.ytd_haben) if found else 0
            if closing_balance != total:
                deviations.append((where, name, 'Endsaldo', total, closing_balance))
            if ytd != year:
                deviations.append((where, name, 'aufgelaufen', year, ytd))
    return deviations


def every_month(year):
    return [(year, month) for month in range(1, 13)]


def span_of(year, month):
    return bwa_periods(year, month)['month']
