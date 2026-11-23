# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import: Eine Zeile über 0,00 € ist ein gewöhnlicher Umsatz.

Der Fall: Die Umsatzliste einer Bank enthält eine Quartalsabrechnung ohne Zinsen über 0,00 €. Die Plausibilitätsprüfung
meldete so eine Zeile als ``amount`` („Betrag?“), obwohl eine Bank solche Zeilen bucht – die Vorschau zeigte eine Auffälligkeit,
wo keine war.

* ``check_transaction`` meldet ``amount`` nur noch, wenn der Betrag fehlt (``None``). ``0``, ``0.0`` und ``-0.0`` sind ein Betrag
  (``-0.0`` liefern die Soll-Seite einer 0,00-€-Zeile im VBR-PDF – ``S`` – und die DKB-CSV mit ``-0,00``).
* In allen Formaten – CAMT, PDF-Text (VBR), CSV (DKB) – bleibt die Zeile erhalten und trägt keinen Befund.
* Vorschau: weder ``incomplete`` noch auffällig, ``importable`` – auch in Vorschau-Dateien, die vor der Importschicht
  entstanden sind (ohne gespeicherte Befunde).
* Import: Die Zeile wird mit dem Betrag 0 angelegt, und ein erneuter Import erkennt sie als Duplikat (Konto + Datum + Betrag,
  zählbasiert) – genau wie bei jedem anderen Betrag.
* Ein fehlender Betrag (``None``) bleibt ein Befund, gilt als unvollständig und wird nicht importiert.

Namen, Beträge und IBANs sind erfunden; die IBAN ist die erlaubte Test-IBAN.
"""
import json
from datetime import datetime

import pytest

from importers import check_statement
from importers.base import BankStatement, StatementTransaction
from importers.camt import parse_xml
from importers.dkb_csv import parse_csv
from importers.plausibility import check_transaction
from importers.vbr import parse_text
from money import to_minor
from server import handlers
from server.import_preview import build_import_preview
from tests.test_confirm_import import (IBAN_A, _bank_bookings, _txn,  # noqa: F401
                                       db_two_accounts, pending_factory)
from tests.test_dkb_csv_import import OWN_IBAN, giro, new_format, new_row, upload  # noqa: F401
from tests.test_import_upload_http import _camt

ZERO_TEXT = 'Quartalsabrechnung ohne Zinsen'
FEE_TEXT = 'Entgelt entfaellt'


# ── Dieselbe 0,00-€-Zeile aus allen drei Formaten: je eine auf der Haben- und eine auf der Soll-Seite ───────────────────

def _camt_statement():
    entries = [('0.00', 'CRDT', '2026-03-05', ZERO_TEXT), ('0.00', 'DBIT', '2026-03-06', FEE_TEXT)]
    (statement,) = parse_xml(_camt('S1', entries))
    return statement


def _pdf_statement():
    """Auszugstext einer Volksbank (``importers.vbr.parse_text``); ``S`` = Soll, ``H`` = Haben."""
    text = (f'15.03. 15.03. Gutschrift 0,00 H\nMuster AG\n{ZERO_TEXT}\n'
            f'16.03. 16.03. Lastschrift 0,00 S\nMuster AG\n{FEE_TEXT}\n')
    rows = parse_text(text, 2026)
    return BankStatement(bank_code='VBR', iban=OWN_IBAN, document_date=datetime(2026, 3, 31),
                         transactions=[StatementTransaction.from_dict(row) for row in rows])


def _csv_statement():
    rows = (new_row('05.03.26', 'Musterbank AG', 'Max Muster', ZERO_TEXT, '0,00'),
            new_row('06.03.26', 'Max Muster', 'Musterbank AG', FEE_TEXT, '-0,00'))
    return parse_csv(new_format(*rows).encode('utf-8-sig'))


FORMATS = pytest.mark.parametrize('build', [_camt_statement, _pdf_statement, _csv_statement], ids=['camt', 'pdf', 'csv'])


def _file(*transactions, iban=IBAN_A, name='auszug.pdf'):
    return {'filename': name, 'bank_code': 'VBR', 'iban': iban, 'document_date': '2025-03-31',
            'transactions': list(transactions)}


def _import(db, pending_factory, *transactions):
    """Eine Datei über den Bestätigungs-Import legen und ihr Ergebnis zurückgeben."""
    status, body = handlers.handle_confirm_import(db, {'import_id': [pending_factory([_file(*transactions)])]})
    assert status == 200, body
    (result,) = json.loads(body)['results']
    return result


# ══ Die Prüfung ══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('amount', [0, 0.0, -0.0], ids=['ganze-Null', 'Null', 'minus-Null'])
def test_a_zero_amount_is_no_finding(amount):
    row = StatementTransaction(date='2026-03-05', amount=amount, recipient='Muster AG', reference=ZERO_TEXT)
    assert check_transaction(row, datetime(2026, 3, 31)) == []


def test_a_missing_amount_is_the_one_finding_of_the_amount_check():
    row = StatementTransaction(date='2026-03-05', amount=None, recipient='Muster AG', reference=ZERO_TEXT)
    assert check_transaction(row, datetime(2026, 3, 31)) == ['amount']


@FORMATS
def test_every_reader_keeps_a_zero_amount_row_and_it_carries_no_finding(build):
    statement = check_statement(build())
    assert len(statement.transactions) == 2 and all(t.amount == 0 for t in statement.transactions)
    assert [t.warnings for t in statement.transactions] == [[], []]


# ══ Die Vorschau ═════════════════════════════════════════════════════════════════════════════════════════════════

@FORMATS
def test_the_preview_shows_a_zero_amount_row_as_an_ordinary_importable_row(db_two_accounts, build):
    statement = check_statement(build())
    (card,) = build_import_preview(db_two_accounts, {'files': [dict(statement.as_dict(), filename='auszug')]})['files']
    assert card['account_name'] == 'Konto A'
    assert [(t['warn'], t['importable'], t['dup']) for t in card['transactions']] == [([], True, False)] * 2
    assert (card['total'], card['new_count'], card['dup_count'], card['problems']) == (2, 2, 0, [])


def test_a_clean_statement_with_a_zero_row_is_ok_not_a_warning(db_two_accounts):
    """Vorher färbte die 0,00-€-Zeile die Karte auf „Hinweise“ – jetzt bleibt sie „OK“."""
    statement = check_statement(_camt_statement())
    assert statement.warnings == []
    (card,) = build_import_preview(db_two_accounts, {'files': [dict(statement.as_dict(), filename='a.xml')]})['files']
    assert card['status'] == 'ok'


def test_an_older_import_file_without_stored_findings_is_checked_with_the_same_rule(db_two_accounts):
    """Eine Datei aus der Zeit vor der Importschicht trägt keine Befunde – dann prüft die Vorschau nach."""
    old = {'files': [{'filename': 'alt.pdf', 'iban': IBAN_A,
                      'transactions': [{'date': '2026-03-05', 'amount': 0, 'recipient': 'Muster AG', 'reference': ZERO_TEXT}]}]}
    (card,) = build_import_preview(db_two_accounts, old)['files']
    (row,) = card['transactions']
    assert (row['warn'], row['importable']) == ([], True) and card['problems'] == [] and card['new_count'] == 1


def test_a_missing_amount_stays_a_finding_and_that_row_is_not_importable(db_two_accounts, pending_factory):
    missing, zero = _txn('2025-03-05', None, 'ohne Betrag'), _txn('2025-03-06', 0.0, ZERO_TEXT)
    (card,) = build_import_preview(db_two_accounts, {'files': [_file(missing, zero)]})['files']
    gone, kept = card['transactions']
    assert set(gone['warn']) == {'amount', 'incomplete'} and gone['importable'] is False
    assert (kept['warn'], kept['importable']) == ([], True)
    assert (card['new_count'], card['dup_count']) == (1, 0) and [t['index'] for t in card['problems']] == [0]

    result = _import(db_two_accounts, pending_factory, missing, zero)
    assert (result['inserted'], result['skipped'], result['skipped_invalid']) == (1, 0, 1)
    assert [row[1] for row in _bank_bookings(db_two_accounts)] == [0]


# ══ Der Import und die Duplikat-Zählung ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('amount', [0.0, -0.0, -12.5], ids=['Null', 'minus-Null', 'gewoehnlicher-Betrag'])
def test_a_zero_amount_is_imported_and_counted_as_a_duplicate_like_any_other_amount(db_two_accounts, pending_factory, amount):
    """Zwei gleiche Umsätze einer Datei bleiben beide erhalten; ein erneuter Import überspringt sie, ein dritter nur zwei von drei."""
    row = _txn('2025-03-05', amount, ZERO_TEXT)
    first = _import(db_two_accounts, pending_factory, row, row)
    assert (first['inserted'], first['skipped'], first['skipped_invalid']) == (2, 0, 0)
    second = _import(db_two_accounts, pending_factory, row, row)
    assert (second['inserted'], second['skipped'], second['skipped_invalid']) == (0, 2, 0)
    third = _import(db_two_accounts, pending_factory, row, row, row)
    assert (third['inserted'], third['skipped'], third['skipped_invalid']) == (1, 2, 0)
    assert [booked[1] for booked in _bank_bookings(db_two_accounts)] == [to_minor(amount)] * 3


def test_the_next_preview_marks_the_zero_row_as_a_duplicate_from_the_books(db_two_accounts, pending_factory):
    row = _txn('2025-03-05', 0.0, ZERO_TEXT)
    _import(db_two_accounts, pending_factory, row)
    (card,) = build_import_preview(db_two_accounts, {'files': [_file(row)]})['files']
    (shown,) = card['transactions']
    assert (shown['dup'], shown['dup_source'], shown['importable'], shown['warn']) == (True, 'db', False, [])
    assert (card['new_count'], card['dup_count']) == (0, 1)


def test_a_dkb_list_with_a_zero_row_goes_through_upload_preview_and_import_and_is_a_duplicate_the_second_time(giro):
    """Der Weg der Umsatzliste: hochladen → Vorschau → bestätigen – die 0,00-€-Zeile ist dabei eine Zeile wie jede andere."""
    rows = (new_row('05.03.26', 'Musterbank AG', 'Max Muster', ZERO_TEXT, '0,00'),
            new_row('06.03.26', 'Max Muster', 'Laden', 'Einkauf', '-4,50'))
    csv = new_format(*rows).encode('utf-8-sig')
    data = upload(giro, ('umsaetze.csv', csv))
    (card,) = data['files']
    assert [t['amount'] for t in card['transactions']] == [0.0, -4.5]
    assert [(t['warn'], t['importable']) for t in card['transactions']] == [([], True)] * 2
    assert card['problems'] == [] and (card['new_count'], card['dup_count']) == (2, 0)

    status, body = handlers.handle_confirm_import(giro, {'import_id': [data['import_id']]})
    assert status == 200
    (result,) = json.loads(body)['results']
    assert (result['inserted'], result['skipped'], result['skipped_invalid']) == (2, 0, 0)
    assert [(booked[1]) for booked in _bank_bookings(giro)] == [0, -45000]

    (again,) = upload(giro, ('umsaetze.csv', csv))['files']
    assert (again['new_count'], again['dup_count']) == (0, 2) and all(t['dup'] for t in again['transactions'])
