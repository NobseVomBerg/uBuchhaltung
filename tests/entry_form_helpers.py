# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests der gespeicherten Form eines Buchungssatzes (``Bookings.EntryForm``).

Alle Namen, Nummern und Beträge sind erfunden. Gemeinsam sind: lesende Abfragen auf die Datenbank,
die Auswertungen (EÜR, DATEV-Zeilen, Dashboard), ein Textexport im Format des WISO-Bewegungsdaten-
Exports und eine synthetische WISO-Datenbank.
"""
import csv
from contextlib import closing
from decimal import Decimal

from export import datev
from money import to_minor
from tests.fdb_builder import Column, FdbBuilder, INT64, LONG, SHORT, TEXT, TIMESTAMP, VARYING

D1, D2 = '2026-01-01', '2026-12-31'
CSV_HEADER = "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"


def query(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        return conn.execute(sql, params).fetchall()


def execute(db, sql, params=()):
    """Roh schreiben (und committen) – für Zustände, die die App selbst nie erzeugt."""
    with closing(db._get_connection()) as conn:
        conn.execute(sql, params)
        conn.commit()


def coa(db, number):
    """COA-ID zur Kontonummer; ``None`` bleibt ``None`` (leere Seite des Satzes)."""
    return None if number is None else db.get_coa_id_by_account_number(number)


def bank_account(db, name, skr):
    db.insert_account(name, 'Owner', f'DE00{abs(hash(name)) % 10**8:08d}', 'BIC', 'Bank',
                      is_cash=0, skr_account=skr)
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def cash_account_id(db):
    return next(a[0] for a in db.fetch_accounts() if a[6] == 1)      # IsCash


def minor(value):
    return None if value is None else to_minor(Decimal(value))


def form_of(db, booking_id):
    return query(db, 'SELECT EntryForm FROM Bookings WHERE ID = ?', (booking_id,))[0][0]


def numbers(db, booking_id):
    """(Kontonummer, Gegenkontonummer, Betrag in Minor Units) einer Buchung."""
    names = db.get_coa_id_to_number_map()
    coa_id, counter_id, amount = query(
        db, 'SELECT COA_ID, CounterCOA_ID, Amount FROM Bookings WHERE ID = ?', (booking_id,))[0]
    return names.get(coa_id), names.get(counter_id), amount


def children(db, bank_id):
    """Sätze unter einer Bankbewegung: [(ID, Konto, Gegenkonto, Betrag, Form)] nach ID."""
    names = db.get_coa_id_to_number_map()
    return [(row[0], names.get(row[1]), names.get(row[2]), row[3], row[4]) for row in query(
        db, 'SELECT ID, COA_ID, CounterCOA_ID, Amount, EntryForm FROM Bookings '
            'WHERE ParentBooking_ID = ? ORDER BY ID', (bank_id,))]


def euer(db, date_from=D1, date_to=D2):
    return {nr: Decimal(str(total)) for nr, _name, total in db.get_euer_data(date_from, date_to)}


def datev_rows(db, date_from=D1, date_to=D2):
    """Zeilen des DATEV-Stapels als {Belegfeld 1: (Umsatz, S/H, Konto, Gegenkonto, BU)} – direkt über
    den Export, ohne die Sperre bei unstimmigen Splits."""
    bookings = db.fetch_bookings_range(date_from, date_to)
    csv_bytes, _ids = datev.export_to_datev(
        bookings, db.get_coa_id_to_number_map(), date_from, date_to,
        liquid_coa_ids=db.get_payment_coa_ids())
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line]
    rows = {}
    for line in lines[2:]:
        fields = next(csv.reader([line], delimiter=';', quotechar='"'))
        rows[fields[10]] = (fields[0], fields[1], fields[6], fields[7], fields[8])
    return rows


def csv_export(*lines):
    """Bewegungsdaten-Export; lines: (Konto, Gegenkonto, Brutto, Referenz, Schlüssel[, Datum[, Text]])."""
    rows = []
    for number, line in enumerate(lines, start=1):
        konto, gegen, brutto, ref, key, *rest = line
        date = rest[0] if rest else '02.01.2026'
        text = rest[1] if len(rest) > 1 else f'Posten {ref}'
        rows.append(f'{number};{date};{konto};{gegen};{text};{ref};{brutto};{key};')
    return (CSV_HEADER + '\n'.join(rows) + '\n').encode('utf-8')


def wiso_database(path, rows, extra_chart=(), key_column=False, systems=None):
    """Synthetische WISO-Datenbank: Kontenrahmen (SKR03 → SKR04) und Buchungssätze ``rows``.

    ``key_column``: Die Tabelle der Buchungen hat die Spalte ``DATEVVATKEY`` (BU-Schlüssel; ein Satz nennt ihn als
    ``DATEVVATKEY=401``). ``systems``: Sätze der Fibu-Schnittstellen (``SYS_FINACCSYSTEMS``: ``NAME``,
    ``CONSULTANT_NO``, ``CLIENT_NO``, dazu ``USERPASSWORD`` als Zugangsdaten, die der Leser nie anfasst); ohne die
    Angabe gibt es die Tabelle nicht."""
    builder = FdbBuilder(page_size=16384)
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4), Column('BOOKINGYEAR', SHORT, 2),
        Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in ((1000, 1600, 'Kasse'), (1200, 1800, 'Bank'),
                               (1360, 1460, 'Verrechnungskonto'),
                               (4980, 6850, 'Betriebsbedarf'), (8400, 4400, 'Erlöse 19 %'),
                               *extra_chart):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2026, ACCOUNTTEXT=text)
    bookings = builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4), Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('TAXRATE', INT64, 8, scale=-2), Column('ACCOUNTNO', LONG, 4),
        Column('CONTRA_ACCOUNTNO', LONG, 4), Column('ACCOUNTING_TEXT', TEXT, 60),
        Column('REFERENCENO', TEXT, 20)] + ([Column('DATEVVATKEY', SHORT, 2)] if key_column else []))
    for number, row in enumerate(rows, start=1):
        row = dict(row)
        group = row.pop('ACCOUNTINGID', 700 + number)
        date = row.pop('ACCOUNTING_DATE', '2026-01-02 00:00:00')
        bookings.add(ID=number, ACCOUNTINGID=group, ACCOUNTING_DATE=date, **row)
    if systems is not None:
        table = builder.table('SYS_FINACCSYSTEMS', [
            Column('ID', LONG, 4), Column('NAME', VARYING, 42), Column('CONSULTANT_NO', VARYING, 12),
            Column('CLIENT_NO', VARYING, 12), Column('USERPASSWORD', VARYING, 32)])
        for number, system in enumerate(systems, start=1):
            table.add(ID=number, **system)
    return builder.write(str(path))


def wiso_row(account, counter, gross, ref, text=None, **extra):
    """Ein Satz der WISO-Datenbank (SKR03-Nummern); ``extra`` überschreibt Felder."""
    row = dict(AMOUNTGROSS=gross, ACCOUNTNO=account, CONTRA_ACCOUNTNO=counter,
               ACCOUNTING_TEXT=text or f'Posten {ref}', REFERENCENO=ref)
    row.update(extra)
    return row
