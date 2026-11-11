# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ein Bestand, in dem jeder Schreibweg einmal vorkommt – und die Auswertungen darüber.

Grundlage des Migrationstests (test_entry_form_migration.py): ``build`` legt in einer neuen Datenbank an,
was die App an Buchungssätzen kennt (Maske neu und bearbeitet, Buchungssatz-Editor mit Umbuchung,
Rechnung mit Forderung und Zahlung – voll, in Teilen und ohne Forderung –, AfA, WISO-Textexport,
WISO-Datenbank, Spiegel beider Konventionen) und dazu Zeilen, wie 2.0.x sie geschrieben hat und die
der heutige Code nicht mehr erzeugt (Konten im Bereich 1000–1099 als Geldkonto). ``evaluate`` liest die
Auswertungen, die sich durch die Migration nicht ändern dürfen: EÜR, DATEV-Zeilen, Dashboard und die
Prüfung auf unstimmige Splits.

``evaluate`` benutzt nur, was es schon in 2.0.x gab. Die Sollwerte im Test stammen daher nicht aus dem
Code dieser Version, sondern aus einem Lauf von ``evaluate`` mit dem Stand 2.0.8 über dieselbe Datenbank
(als hätte 2.0.8 sie geschrieben: Spalte ``EntryForm`` entfernt, Stand 2000).
Alle Namen, Nummern und Beträge sind erfunden.
"""
import datetime
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal

D1, D2 = '2026-01-01', '2026-12-31'
CASH_NUMBERS = (1600, 1460, None)           # Kasse mit 1600, mit dem Verrechnungskonto, ohne Nummer


def _coa(db, number):
    return None if number is None else db.get_coa_id_by_account_number(number)


def _csv(*lines):
    header = "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
    rows = []
    for number, (konto, gegen, brutto, ref, key, date) in enumerate(lines, start=1):
        rows.append(f'{number};{date};{konto};{gegen};Posten {ref};{ref};{brutto};{key};')
    return (header + '\n'.join(rows) + '\n').encode('utf-8')


def _account_id(db, name):
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def _post(db, coa_number, amount, account, rate=None, tax='', transaction_id=0, date='2026-03-01',
          document='', text=''):
    post = {'transaction_id': [str(transaction_id)], 'date': [date], 'amount': [amount],
            'currency': ['EUR'], 'coa_id': [str(_coa(db, coa_number)) if coa_number else ''],
            'tax_rate': [f'{rate * 100:g}' if rate else ''], 'tax_amount': [tax],
            'document_nr': [document], 'text': [text]}
    if account:
        post['account'] = [str(_account_id(db, account))]
    return post


def _editor(db, bank, rows):
    """Post einer bestehenden Bankbewegung mit Zeilen des Editors: [(id, Zweckkonto, Betrag, Gegenkonto)]."""
    with closing(db._get_connection()) as conn:
        amount, account = conn.execute('SELECT Amount, Account_ID FROM Bookings WHERE ID = ?',
                                       (bank,)).fetchone()
    return {'transaction_id': [str(bank)], 'date': ['2026-03-05'],
            'amount': [str(Decimal(amount) / 10000)], 'currency': ['EUR'],
            'account': [str(account)],
            'split_id': [str(r[0] or '') for r in rows],
            'split_coa': [str(_coa(db, r[1]) or '') for r in rows],
            'split_amount': [r[2] for r in rows],
            'split_tax_rate': ['19' if r[1] == 6815 else '' for r in rows],
            'split_tax_amount': [r[4] if len(r) > 4 else '' for r in rows],
            'split_docnr': ['' for _ in rows], 'split_text': ['' for _ in rows],
            'split_counter_coa': [str(_coa(db, r[3]) or '') for r in rows]}


def _invoice(db, number, gross, rate=0.19):
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': rate,
        'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross,
        'amount_due': gross, 'status': 'sent'})


def _raw_entry(db, date, coa_number, counter_number, amount, document, parent=None,
               tax_rate=None, tax_amount=None, auto_mirror=0):
    """Eine Zeile, wie 2.0.x sie geschrieben hat: ohne Form, Betrag nach der damaligen Konvention."""
    with closing(db._get_connection()) as conn:
        cursor = conn.execute(
            "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, "
            "Text, DocumentNumber, BookingType, ParentBooking_ID, AutoMirror) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, ?)",
            (date, _coa(db, coa_number), _coa(db, counter_number), int(Decimal(amount) * 10000),
             tax_rate, None if tax_amount is None else int(Decimal(tax_amount) * 10000),
             f'Altbestand {document}', document, parent, auto_mirror))
        conn.commit()
        return cursor.lastrowid


def _save(db, post):
    from server.handlers import handle_add_transaction
    status, location = handle_add_transaction(db, post)
    assert status == 303, (status, location)


def build(db_path, cash_number=1600):
    """Legt den Bestand in einer neuen Datenbank an und gibt die ``Database`` zurück.

    ``cash_number``: SKR-Nummer der Kasse (1600, 1460 oder ``None``) – vor der ersten Buchung gesetzt."""
    from db import Database
    from server.handlers import link_booking_to_invoice_capped
    from tests.entry_form_helpers import wiso_database, wiso_row

    db = Database(db_name=db_path)
    cash = next(a for a in db.fetch_accounts() if a[6] == 1)
    db.update_account(cash[0], cash[1], cash[2], cash[3], cash[4], cash[5], skr_account=cash_number)
    db.insert_account('Giro Alpha', 'Owner', 'DE00ALPHA', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    db.insert_account('Giro Beta', 'Owner', 'DE00BETA', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    for number, name in ((1050, 'Altkonto Vorräte'), (1650, 'Nebenkonto')):
        db.insert_chart_of_accounts(4, number, name, 'Test')
    alpha = _account_id(db, 'Giro Alpha')
    cash_name = cash[1]

    # ── Buchungsmaske: neu, bearbeitet, mit der Kasse als Zweckkonto, ohne Bankkonto
    _save(db, _post(db, 6815, '-59.50', 'Giro Alpha', rate=0.19, tax='-9.50',
                                     document='MA-1'))
    first = db.fetch_bookings_grouped(D1, D2)[0]['booking'][0]
    _save(db, _post(db, 6850, '-59.50', 'Giro Alpha', rate=0.19, tax='-9.50',
                                     transaction_id=first, document='MA-1'))
    _save(db, _post(db, 1600, '-100.00', 'Giro Alpha', document='MA-2',
                                     date='2026-03-02'))
    _save(db, _post(db, 6815, '-12.00', None, document='MA-3', date='2026-03-03'))
    standalone = db.insert_booking('2026-03-03', Decimal('-8.00'), coa_id=_coa(db, 6815),
                                   booking_type='entry', document_number='MA-4')
    _save(db, _post(db, 6850, '-8.00', None, transaction_id=standalone,
                                     document='MA-4', date='2026-03-03'))

    # ── Buchungssatz-Editor: Zahlungen, Privatanteil (Umbuchung), spätere Änderung
    split = db.insert_booking('2026-03-05', Decimal('-100.00'), account_id=alpha,
                              booking_type='bank', recipient_client='Stadtwerke')
    _save(db, _editor(db, split, [(None, 6815, '-70.00', None, '-11.18'),
                                                   (None, 6850, '-30.00', None),
                                                   (None, 6805, '7.50', 2100)]))
    with closing(db._get_connection()) as conn:
        rows = conn.execute('SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                            (split,)).fetchall()
    _save(db, _editor(db, split, [(rows[0][0], 6815, '-70.00', None, '-11.18'),
                                                   (rows[1][0], 6850, '-30.00', None),
                                                   (rows[2][0], 6805, '7.50', 2100)]))

    # ── Rechnungen: voll, in Teilen, ohne Forderung (5 %)
    one = _invoice(db, 'RE-1', 119.00)
    db.ensure_receivable_booking(one)
    bank = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=alpha, booking_type='bank',
                             recipient_client='Käufer AG')
    link_booking_to_invoice_capped(db, one, bank)
    two = _invoice(db, 'RE-2', 238.00)
    db.ensure_receivable_booking(two)
    for amount, date in (('100.00', '2026-02-10'), ('138.00', '2026-02-20')):
        bank = db.insert_booking(date, Decimal(amount), account_id=alpha, booking_type='bank',
                                 recipient_client='Käufer AG')
        link_booking_to_invoice_capped(db, two, bank)
    three = _invoice(db, 'RE-3', 105.00, rate=0.05)
    bank = db.insert_booking('2026-02-25', Decimal('105.00'), account_id=alpha, booking_type='bank',
                             recipient_client='Käufer AG')
    link_booking_to_invoice_capped(db, three, bank, revenue_coa_id=_coa(db, 4300))
    four = _invoice(db, 'RE-4', 59.50)                      # nur die Forderung, keine Zahlung
    db.ensure_receivable_booking(four)

    # ── AfA
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    db.book_depreciation(asset, 2026, coa_id_expense=_coa(db, 6220), coa_id_asset=_coa(db, 520))

    # ── WISO-Textexport: Kasse, Spiegel (Kasse/Verrechnung), Bank, Umbuchung, Konto ohne Geldkonto
    db.import_wiso_csv(_csv(
        ('6815', '1600', '18,35', 'CSV-KA', '401', '10.04.2026'),
        ('1600', '4400', '50,00', 'CSV-KE', '401', '10.04.2026'),
        ('1600', '1800', '100,00', 'CSV-AB', '', '11.04.2026'),
        ('1800', '1600', '40,00', 'CSV-EZ', '', '11.04.2026'),
        ('1460', '1800', '70,00', 'CSV-VK', '', '12.04.2026'),
        ('6815', '1800', '33,00', 'CSV-BA', '401', '12.04.2026'),
        ('1800', '4400', '119,00', 'CSV-BE', '401', '13.04.2026'),
        ('4405', '4400', '119,00', 'CSV-UM', '401', '13.04.2026'),
        ('6815', '1650', '10,00', 'CSV-TR', '', '14.04.2026'),
    ))
    for amount, date in (('-70.00', '2026-04-12'), ('-33.00', '2026-04-12'),
                         ('119.00', '2026-04-13'), ('-100.00', '2026-04-11')):
        db.insert_booking(date, Decimal(amount), account_id=alpha, booking_type='bank')
    db.link_bank_to_entries()

    # ── WISO-Datenbank
    workdir = tempfile.mkdtemp(prefix='ubuch_world_')
    try:
        path = wiso_database(os.path.join(workdir, 'DB1.FDB'), [
            wiso_row(4980, 1000, 21.40, 'FD-KA', AMOUNTNET=17.98, TAXRATE=19.0,
                     ACCOUNTING_DATE='2026-05-04 00:00:00'),
            wiso_row(1000, 8400, 60.0, 'FD-KE', AMOUNTNET=50.42, TAXRATE=19.0,
                     ACCOUNTING_DATE='2026-05-04 00:00:00'),
            wiso_row(1000, 1200, 90.0, 'FD-AB', ACCOUNTING_DATE='2026-05-05 00:00:00'),
            wiso_row(1200, 1000, 45.0, 'FD-EZ', ACCOUNTING_DATE='2026-05-05 00:00:00'),
            wiso_row(1360, 1200, 55.0, 'FD-VK', ACCOUNTING_DATE='2026-05-06 00:00:00'),
            wiso_row(4980, 1200, 77.0, 'FD-BA', AMOUNTNET=64.71, TAXRATE=19.0,
                     ACCOUNTING_DATE='2026-05-06 00:00:00'),
        ])
        db.import_wiso_fdb(path, with_assets=False, with_invoices=False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    for amount, date in (('-55.00', '2026-05-06'), ('-77.00', '2026-05-06')):
        db.insert_booking(date, Decimal(amount), account_id=alpha, booking_type='bank')
    db.link_bank_to_entries()

    # ── Zeilen, wie 2.0.x sie geschrieben hat und der heutige Code nicht mehr erzeugt
    #    (Konten im Bereich 1000–1099 galten als Geldkonto; Spiegel in beiden Konventionen)
    _raw_entry(db, '2026-06-01', 6815, 1050, '-10.00', 'ALT-1')                # Zweckkonto vorne
    _raw_entry(db, '2026-06-01', 1050, 4400, '20.00', 'ALT-2')                 # Geldkonto vorne
    _raw_entry(db, '2026-06-02', 1050, 1800, '30.00', 'ALT-3')                 # Spiegel, Rohbetrag
    _raw_entry(db, '2026-06-02', 1800, 1050, '-30.00', 'ALT-4')                # Spiegel, Maskenkonvention
    _raw_entry(db, '2026-06-03', 1600, 1800, '-25.00', 'ALT-5')                # Abhebung, Maskenkonvention
    _raw_entry(db, '2026-06-03', 1800, 1600, '25.00', 'ALT-6')                 # Einzahlung, Rohbetrag
    _raw_entry(db, '2026-06-04', 1460, 1800, '15.00', 'ALT-7')                 # Verrechnung an Bank
    _raw_entry(db, '2026-06-04', 6815, 1460, '-9.00', 'ALT-8', tax_rate=0.19, tax_amount='-1.44')
    _raw_entry(db, '2026-06-05', 6815, 4400, '5.00', 'ALT-9')                  # Umbuchung
    _raw_entry(db, '2026-06-05', 6815, None, '-6.00', 'ALT-10')                # ohne Gegenkonto
    _raw_entry(db, '2026-06-05', None, 4400, '7.00', 'ALT-11')                 # ohne Konto
    _raw_entry(db, '2026-06-05', None, None, '8.00', 'ALT-12')                 # nichts kontiert
    legacy_bank = db.insert_booking('2026-06-06', Decimal('-40.00'), account_id=alpha,
                                    booking_type='bank')
    _raw_entry(db, '2026-06-06', 6850, 1800, '-30.00', 'ALT-13', parent=legacy_bank)
    _raw_entry(db, '2026-06-06', 6850, 1050, '-10.00', 'ALT-14', parent=legacy_bank)
    _raw_entry(db, '2026-06-07', 1800, 1810, '-50.00', 'ALT-15')
    return db


def to_state_2000(db_path):
    """Die Datenbank in den Stand zurückversetzen, den 2.0.x geschrieben hat: Spalte ``EntryForm`` und
    eindeutiger Index auf den Zahlungszuordnungen entfernt, ``PRAGMA user_version`` = 2000."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute('DROP INDEX IF EXISTS ux_invoicepayments_invoice_booking')
        conn.execute('ALTER TABLE Bookings DROP COLUMN EntryForm')
        conn.execute('PRAGMA user_version = 2000')
        conn.commit()


def evaluate(db_path, date_from=D1, date_to=D2):
    """Die Auswertungen, die eine Migration nicht ändern darf – als JSON-fähiges Dict, nur mit
    Funktionen, die es schon in 2.0.x gab. Öffnet die Datenbank über ``Database`` (eine
    Datenbank mit älterem Stand wird dabei migriert)."""
    from db import Database
    from export import datev

    db = Database(db_name=db_path)
    accounts = db.fetch_accounts()
    cash_ids = [a[0] for a in accounts if a[6] == 1]
    bank_ids = [a[0] for a in accounts if a[6] != 1]

    def euer(account_ids):
        return [[number, name, f'{total:.2f}']
                for number, name, total in db.get_euer_data(date_from, date_to, account_ids)]

    bookings = db.fetch_bookings_range(date_from, date_to)
    csv_bytes, ids = datev.export_to_datev(
        bookings, db.get_coa_id_to_number_map(), date_from, date_to,
        liquid_coa_ids=db.get_payment_coa_ids())
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    datev_rows = []
    for booking_id, line in zip(ids, lines):
        fields = line.replace('"', '').split(';')
        datev_rows.append([booking_id, fields[0], fields[1], fields[6], fields[7], fields[8],
                           fields[10]])

    def dashboard(account_ids):
        totals = db.get_dashboard_totals(date_from, date_to, account_ids)
        monthly = [[m['year_month'], f"{m['income']:.2f}", f"{m['private']:.2f}",
                    f"{m['expense']:.2f}"]
                   for m in db.get_dashboard_monthly(date_from, date_to, account_ids)]
        return {'totals': {key: str(value) for key, value in sorted(totals.items())},
                'monthly': monthly}

    return {
        'euer': euer(None), 'euer_cash': euer(cash_ids), 'euer_bank': euer(bank_ids[:1]),
        'datev': datev_rows,
        'dashboard': dashboard(None), 'dashboard_cash': dashboard(cash_ids),
        'dashboard_bank': dashboard(bank_ids[:1]),
        'unbalanced': [[row[0], row[1], str(row[2]), str(row[3])]
                       for row in db.find_unbalanced_splits(date_from, date_to)],
    }
