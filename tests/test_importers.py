# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""todo #2: Abstraktionsschicht für den Datenimport.

Geprüft werden das gemeinsame Datenmodell, die Erkennung des zuständigen
Importmoduls und die Plausibilitätsprüfung – nicht die bankspezifische
Textauswertung (die deckt tests/test_document_parser.py ab).
"""
from datetime import datetime

import importers
from importers.base import BankStatement, StatementTransaction
from importers.dkb import DkbImporter
from importers.plausibility import check_statement, check_transaction
from importers.vbr import VbrImporter, statement_year


def _txn(**kw):
    base = dict(date='2026-03-01', amount=-12.34, recipient='Bueroshop',
                reference='Rechnung 123', foreign_iban='DE02120300000000202051')
    base.update(kw)
    return StatementTransaction(**base)


# ── Datenmodell ──────────────────────────────────────────────────────────────

def test_transaction_roundtrip_keeps_the_canonical_columns(tmp_path):
    raw = {'date': '2026-03-01', 'amount': -12.34, 'recipient': 'Bueroshop',
           'reference': 'Rechnung 123', 'foreign_iban': 'DE0212030000',
           'unbekannt': 'wird verworfen'}
    t = StatementTransaction.from_dict(raw)
    out = t.as_dict()
    assert set(out) == {'date', 'amount', 'recipient', 'reference',
                        'foreign_iban', 'warnings'}
    for key in importers.CANONICAL_FIELDS:
        assert out[key] == raw[key]


def test_transaction_from_dict_tolerates_missing_fields():
    t = StatementTransaction.from_dict({})
    assert t.date is None and t.amount is None
    assert t.recipient == '' and t.reference == '' and t.foreign_iban == ''


def test_statement_serialises_for_the_preview():
    s = BankStatement(bank_code='VBR', iban='DE0212030000',
                      document_date=datetime(2026, 3, 31),
                      transactions=[_txn()])
    out = s.as_dict()
    assert out['bank_code'] == 'VBR' and out['iban'] == 'DE0212030000'
    assert out['document_date'] == datetime(2026, 3, 31)
    assert len(out['transactions']) == 1


# ── Modul-Erkennung ──────────────────────────────────────────────────────────

def test_detection_picks_the_right_module():
    assert importers.find_importer('auszug.pdf', 'Volksbank Rottweil eG') is VbrImporter
    assert importers.find_importer('VBR_2026.pdf', 'irgendwas') is VbrImporter
    assert importers.find_importer('x.pdf', 'Deutsche Kreditbank AG') is DkbImporter
    assert importers.find_importer('Kontoauszug_007.pdf', 'nichts') is DkbImporter


def test_unknown_document_has_no_module():
    assert importers.find_importer('vertrag.pdf', 'Mietvertrag ueber Raeume') is None


def test_every_registered_module_is_complete():
    for mod in importers.IMPORTERS:
        assert mod.bank_code and mod.name
        assert mod.detect('', '') in (True, False)


def test_statement_year_from_header():
    assert statement_year('Kontoauszug Nr. 3/2024 Blatt 1') == 2024
    assert statement_year('erstellt am 05.01.2023') == 2023
    assert statement_year('ohne Jahr') == datetime.now().year


# ── Plausibilität ────────────────────────────────────────────────────────────

def test_clean_transaction_has_no_findings():
    assert check_transaction(_txn(), datetime(2026, 3, 31)) == []


def test_missing_amount_and_date_are_found():
    assert 'amount' in check_transaction(_txn(amount=None))
    assert 'amount' in check_transaction(_txn(amount=0))
    assert 'date' in check_transaction(_txn(date=None))
    assert 'date' in check_transaction(_txn(date='31.03.2026'))   # nicht ISO


def test_implausible_amount_is_found():
    assert 'huge' in check_transaction(_txn(amount=-2_500_000.0))
    assert 'huge' not in check_transaction(_txn(amount=-999_999.0))


def test_year_slip_is_found():
    """Ein Datum weit neben dem Belegdatum ist fast immer ein Jahresdreher."""
    doc = datetime(2026, 3, 31)
    assert 'daterange' in check_transaction(_txn(date='2024-03-01'), doc)
    assert 'daterange' not in check_transaction(_txn(date='2026-02-01'), doc)
    # Ohne Belegdatum kein Befund – es fehlt der Bezug
    assert 'daterange' not in check_transaction(_txn(date='2019-01-01'))


def test_empty_content_is_found():
    assert 'empty' in check_transaction(_txn(recipient='', reference=''))


def test_header_and_footer_junk_is_found():
    """Genau der Fall aus der Aufgabe: Layout-Text statt Buchungsinhalt."""
    for junk in ('Seite 2 von 5', 'Blatt 3', 'Kontoauszug Nr. 4/2026',
                 'Uebertrag auf Blatt 2', 'Neuer Kontostand', 'K00009283',
                 'Bitte beachten Sie die Hinweise', '-----',
                 'BLZ 64290120', 'Telefon 07421 000'):
        found = check_transaction(_txn(recipient=junk, reference=''))
        assert 'boiler' in found, junk


def test_real_content_is_not_mistaken_for_junk():
    """Fachbegriffe kommen in echten Verwendungszwecken vor – nur ein
    komplettes Feld aus Layout-Text zaehlt."""
    for echt in ('Kontoauszug-Gebuehr Januar', 'Seitenbacher GmbH',
                 'IBAN-Umstellung Beitrag 2026', 'Blattwerk Gartenbau'):
        assert check_transaction(_txn(recipient=echt, reference='')) == []


def test_junk_recipient_with_real_reference_is_kept():
    """Ein brauchbarer Verwendungszweck rettet die Zeile."""
    found = check_transaction(_txn(recipient='Seite 1 von 2',
                                   reference='Miete Maerz Objekt 4'))
    assert 'boiler' not in found


def test_check_statement_annotates_and_reports():
    s = BankStatement(bank_code='VBR', iban=None,
                      document_date=datetime(2026, 3, 31),
                      transactions=[_txn(), _txn(amount=None)])
    check_statement(s)
    assert s.transactions[0].warnings == []
    assert 'amount' in s.transactions[1].warnings
    assert 'no_iban' in s.warnings and 'no_transactions' not in s.warnings


def test_empty_statement_is_reported():
    s = BankStatement(bank_code='VBR', iban='DE0212030000')
    check_statement(s)
    assert 'no_transactions' in s.warnings


def test_preview_shows_the_plausibility_findings(tmp_db):
    """Die Vorschau übernimmt die Befunde der Importschicht, statt sie
    ein zweites Mal (und knapper) selbst zu ermitteln."""
    from server.import_preview import build_import_preview

    tmp_db.insert_account('Testbank', 'Nina Nutzer', 'DE02120300000000202051',
                          'BIC', 'Bank', is_cash=0, skr_account=1800)
    statement = BankStatement(
        bank_code='VBR', iban='DE02120300000000202051',
        document_date=datetime(2026, 3, 31),
        transactions=[_txn(), _txn(recipient='Seite 2 von 5', reference='')])
    check_statement(statement)

    preview = build_import_preview(
        tmp_db, {'files': [dict(statement.as_dict(), filename='auszug.pdf')]})
    f = preview['files'][0]
    assert f['status'] == 'warn'
    assert f['problems'][0]['warn'] == ['boiler']


def test_preview_still_checks_older_import_files(tmp_db):
    """Vor der Abstraktionsschicht abgelegte Dateien haben keine Befunde –
    dann prüft die Vorschau nach."""
    from server.import_preview import build_import_preview

    tmp_db.insert_account('Testbank', 'Nina Nutzer', 'DE02120300000000202051',
                          'BIC', 'Bank', is_cash=0, skr_account=1800)
    alt = {'files': [{'filename': 'alt.pdf', 'iban': 'DE02120300000000202051',
                      'transactions': [{'date': '2026-03-01', 'amount': 0,
                                        'recipient': '', 'reference': ''}]}]}
    f = build_import_preview(tmp_db, alt)['files'][0]
    assert set(f['problems'][0]['warn']) == {'amount', 'empty'}


def test_document_date_as_string_still_bounds_the_range():
    """Aus der abgelegten Import-Datei kommt das Belegdatum als Text zurueck."""
    s = BankStatement(bank_code='VBR', iban='DE02', document_date='2026-03-31',
                      transactions=[_txn(date='2020-01-01')])
    check_statement(s)
    assert 'daterange' in s.transactions[0].warnings
