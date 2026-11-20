# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""todo #1: Vorschlagsfunktion für noch nicht kontierte Buchungen.

Der Vorschlag liest die Historie desselben Empfängers, füllt SKR-Konto und
Steuersatz und schlägt die nächste Beleg-Nr. vor – ohne zu speichern. War die
Vorlage ein Split, kommen alle Teilbuchungen mit.
"""
import contextlib
import json
import sqlite3
from decimal import Decimal

import pytest

from server.handlers import handle_add_transaction, handle_transaction_suggest
from server.pages_transactions import PageTransactions


def _bank_account(db, name='Vorschlag-Bank', skr=1800):
    db.insert_account(name, 'Owner', 'DE00VOR', 'BIC', 'Bank',
                      is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _suggest(db, booking_id=0, recipient=None):
    qs = {'id': [str(booking_id)]}
    if recipient is not None:
        qs['recipient'] = [recipient]
    status, body = handle_transaction_suggest(db, qs)
    assert status == 200
    return json.loads(body)


# ── Nummernkreis: ansehen ohne verbrauchen ───────────────────────────────────

def test_peek_does_not_consume(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 7, 'Firma')
    first, range_id = tmp_db.peek_next_number('receipt_company', 2026)
    assert first == '26F008'
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F008'

    assert tmp_db.consume_number(range_id, '26F008') is True
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F009'


def test_consume_ignores_a_different_number(tmp_db):
    """Hat der Nutzer die Nummer überschrieben, bleibt der Zähler stehen."""
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 3)
    _, range_id = tmp_db.peek_next_number('receipt_company', 2026)
    assert tmp_db.consume_number(range_id, 'EIGENE-NR') is False
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F004'


def test_consume_lifts_the_counter_to_a_higher_number_of_the_template_but_never_lowers_it(tmp_db):
    """Seit das Löschen eines Belegs den Zähler zurücknehmen kann, kann eine gespeicherte Nummer über dem Zähler stehen: Das Speichern hebt ihn dann
    auf sie (sonst vergäbe der nächste Upload sie noch einmal) – nie nach unten."""
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 7, 'Firma')
    _, range_id = tmp_db.peek_next_number('receipt_company', 2026)
    assert tmp_db.consume_number(range_id, '26F010') is True                       # eine höhere als die nächste
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F011'
    assert tmp_db.consume_number(range_id, '26F010') is False                      # gleich dem Zähler
    assert tmp_db.consume_number(range_id, '26F003') is False                      # darunter
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F011'


@pytest.mark.parametrize('number', ['EIGENE-NR', '25F020', '26G020', '26F020_X', '26F2', '26F', '', None, ' ', '26F-20', 'x26F020'])
def test_consume_leaves_the_counter_for_a_number_that_does_not_fit_the_template(tmp_db, number):
    """Jahr, Buchstabe, Zusatz und eine Zahl mit mindestens drei Stellen müssen zur Vorlage ``{yy}{l}{nnn}{s}`` des Kreises passen."""
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 7, 'Firma')
    _, range_id = tmp_db.peek_next_number('receipt_company', 2026)
    assert tmp_db.consume_number(range_id, number) is False
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F008'


def test_consume_never_takes_a_number_above_the_maximum(tmp_db):
    """Größer als ``MAX_RECEIPT_RUNNING`` wird eine laufende Nummer nie gewertet – auch keine, die kein 64-Bit-Zähler fasste, und keine mit mehr
    Ziffern, als Python in eine Zahl wandelt; der Zähler bleibt, und es gibt keinen Fehler."""
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 7, 'Firma')
    _, range_id = tmp_db.peek_next_number('receipt_company', 2026)
    for number in ('26F1000000000', '26F9223372036854775807', '26F' + '9' * 25, '26F' + '9' * 5000):
        assert tmp_db.consume_number(range_id, number) is False
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F008'
    assert tmp_db.consume_number(range_id, '26F999999999') is True                 # genau die Grenze zählt noch
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F1000000000'


def test_consume_ignores_a_template_without_a_counter_and_an_unknown_range(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 7, 'Firma', number_format='{yy}{l}')
    range_id = tmp_db.fetch_number_ranges('receipt_company')[0][0]
    assert tmp_db.consume_number(range_id, '26F') is False
    assert tmp_db.consume_number(99999, '26F020') is False


def test_consume_counts_from_zero_when_the_counter_is_empty(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 0, 'Firma')
    range_id = tmp_db.fetch_number_ranges('receipt_company')[0][0]
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('UPDATE NumberRanges SET CurrentNumber = NULL')
        con.commit()
    assert tmp_db.consume_number(range_id, '26F002') is True
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F003'


def test_peek_honours_number_format(tmp_db):
    """Die frühere Handrechnung der Belegseite ignorierte das Format."""
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '_X', 0, '',
                               number_format='{yyyy}-{l}{nn}{s}')
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '2026-F01_X'


def test_peek_without_range(tmp_db):
    assert tmp_db.peek_next_number('receipt_company', 2026) == (None, None)


# ── Historie: Kontierung des gleichen Empfängers ─────────────────────────────

def test_suggestion_reuses_last_account_and_tax(tmp_db):
    coa = tmp_db.get_coa_id_by_account_number(6805)
    tmp_db.insert_booking('2026-01-15', -29.99, coa_id=coa,
                          recipient_client='O2 Germany GmbH', tax_rate=0.19,
                          tax_amount=-4.79, booking_type='entry')
    offen = tmp_db.insert_booking('2026-02-15', -29.99,
                                  recipient_client='O2 Germany GmbH',
                                  booking_type='entry')

    d = _suggest(tmp_db, offen)
    assert d['ok'] and not d['is_split']
    assert d['rows'][0]['coa_id'] == coa
    assert d['rows'][0]['tax_rate'] == '19'
    assert d['source_date'] == '2026-01-15'


def test_suggestion_matches_similar_recipient(tmp_db):
    """Kontoauszüge schreiben denselben Partner selten zweimal gleich."""
    coa = tmp_db.get_coa_id_by_account_number(6805)
    tmp_db.insert_booking('2026-01-15', -29.99, coa_id=coa,
                          recipient_client='Telefonica Germany GmbH & Co. OHG',
                          booking_type='entry')
    offen = tmp_db.insert_booking('2026-02-15', -29.99,
                                  recipient_client='TELEFONICA GERMANY',
                                  booking_type='entry')

    d = _suggest(tmp_db, offen)
    assert d['rows'][0]['coa_id'] == coa


def test_suggestion_ignores_uncoded_history(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 0)
    tmp_db.insert_booking('2026-01-15', -10.00, recipient_client='Neuer Partner',
                          booking_type='entry')
    offen = tmp_db.insert_booking('2026-02-15', -10.00,
                                  recipient_client='Neuer Partner',
                                  booking_type='entry')

    d = _suggest(tmp_db, offen)
    assert d['ok'] and d['rows'] == []          # nur die Beleg-Nr.
    assert d['document_nr'] == '26F001'


def test_suggestion_does_not_use_the_booking_itself(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 0)
    coa = tmp_db.get_coa_id_by_account_number(6805)
    selbst = tmp_db.insert_booking('2026-02-15', -10.00, coa_id=coa,
                                   recipient_client='Einzelfall',
                                   booking_type='entry')
    assert _suggest(tmp_db, selbst)['rows'] == []


def test_nothing_to_suggest_at_all(tmp_db):
    """Weder Historie noch Nummernkreis: klare Rückmeldung statt leerem Erfolg."""
    offen = tmp_db.insert_booking('2026-02-15', -10.00,
                                  recipient_client='Unbekannt',
                                  booking_type='entry')
    d = _suggest(tmp_db, offen)
    assert d['ok'] is False and 'Nummernkreis' in d['message']


def test_suggestion_offers_the_whole_split(tmp_db):
    """O2-Muster: der Vorschlag liefert alle Teilbuchungen der Vorlage."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa_tk = tmp_db.get_coa_id_by_account_number(6805)
    coa_priv = tmp_db.get_coa_id_by_account_number(2100)
    vorlage = tmp_db.insert_booking('2026-01-02', -29.99, account_id=acct,
                                    booking_type='bank',
                                    recipient_client='O2 Germany')
    tmp_db.insert_booking('2026-01-02', -15.00, coa_id=coa_tk,
                          counter_coa_id=bank_coa, tax_rate=0.19,
                          tax_amount=-2.39, recipient_client='O2 Germany',
                          text='Anteil Betrieb', booking_type='entry',
                          parent_booking_id=vorlage)
    tmp_db.insert_booking('2026-01-02', 7.50, coa_id=coa_priv,
                          counter_coa_id=coa_tk, recipient_client='O2 Germany',
                          text='Privatanteil', booking_type='entry',
                          parent_booking_id=vorlage)
    tmp_db.insert_booking('2026-01-02', -14.99, coa_id=coa_priv,
                          counter_coa_id=bank_coa, recipient_client='O2 Germany',
                          booking_type='entry', parent_booking_id=vorlage)

    offen = tmp_db.insert_booking('2026-02-02', -29.99, account_id=acct,
                                  booking_type='bank',
                                  recipient_client='O2 Germany')
    d = _suggest(tmp_db, offen)
    assert d['is_split'] and len(d['rows']) == 3
    assert [r['coa_id'] for r in d['rows']] == [coa_tk, coa_priv, coa_priv]
    assert d['rows'][0]['tax_rate'] == '19'
    assert d['rows'][1]['text'] == 'Privatanteil'
    # Der Privatanteil ist eine Umbuchung: sein Gegenkonto muss mitkommen,
    # sonst wuerde die Kopie zur Bankbewegung und die Summe stimmte nicht.
    assert d['rows'][1]['nobank'] is True
    assert d['rows'][1]['counter_coa_id'] == coa_tk
    assert d['rows'][0]['nobank'] is False and d['rows'][0]['counter_coa_id'] == ''


def test_copied_transfer_row_stays_a_transfer(tmp_db):
    """Die kopierte Umbuchung wird als solche gespeichert – nicht als Zahlung."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa_tk = tmp_db.get_coa_id_by_account_number(6805)
    coa_priv = tmp_db.get_coa_id_by_account_number(2100)
    bk = tmp_db.insert_booking('2026-03-02', -29.99, account_id=acct,
                               booking_type='bank', recipient_client='O2')

    handle_add_transaction(tmp_db, {
        'transaction_id': [str(bk)], 'date': ['2026-03-02'],
        'amount': ['-29.99'], 'account': [str(acct)], 'currency': ['EUR'],
        'split_id': ['', '', ''],
        'split_amount': ['-15.00', '7.50', '-14.99'],
        'split_coa': [str(coa_tk), str(coa_priv), str(coa_priv)],
        'split_counter_coa': ['', str(coa_tk), ''],
        'split_tax_rate': ['19', '', ''],
        'split_tax_amount': ['-2.39', '', ''],
        'split_docnr': ['A', 'B', 'C'], 'split_text': ['', '', ''],
    })

    kids = {c[7]: c for c in tmp_db.get_child_bookings_for_bank(bk)}
    assert kids['A'][2] == bank_coa        # Zahlung: Gegenkonto Bank
    assert kids['B'][2] == coa_tk          # Umbuchung: Gegenkonto 6805
    assert kids['C'][2] == bank_coa
    # Und die Split-Summe geht damit auf
    assert tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []


def test_suggestion_skips_doppik_mirror(tmp_db):
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    vorlage = tmp_db.insert_booking('2026-01-02', -50.00, account_id=acct,
                                    booking_type='bank',
                                    recipient_client='Bueroshop')
    tmp_db.insert_booking('2026-01-02', -50.00, coa_id=bank_coa,
                          counter_coa_id=bank_coa, booking_type='entry',
                          parent_booking_id=vorlage)
    tmp_db.insert_booking('2026-01-02', -50.00, coa_id=coa,
                          counter_coa_id=bank_coa, booking_type='entry',
                          parent_booking_id=vorlage)

    offen = tmp_db.insert_booking('2026-02-02', -50.00, account_id=acct,
                                  booking_type='bank',
                                  recipient_client='Bueroshop')
    d = _suggest(tmp_db, offen)
    assert not d['is_split'] and len(d['rows']) == 1
    assert d['rows'][0]['coa_id'] == coa


def test_income_orientation_is_resolved_to_the_purpose_account(tmp_db):
    """Liquide-zuerst gebuchte Einnahme: vorgeschlagen wird das Erlöskonto,
    nicht das Bankkonto."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    erloes = tmp_db.get_coa_id_by_account_number(4400)
    vorlage = tmp_db.insert_booking('2026-01-02', 1190.00, account_id=acct,
                                    booking_type='bank',
                                    recipient_client='Kunde AG')
    tmp_db.insert_booking('2026-01-02', 1190.00, coa_id=bank_coa,
                          counter_coa_id=erloes, booking_type='entry',
                          parent_booking_id=vorlage)

    offen = tmp_db.insert_booking('2026-02-02', 1190.00, account_id=acct,
                                  booking_type='bank',
                                  recipient_client='Kunde AG')
    assert _suggest(tmp_db, offen)['rows'][0]['coa_id'] == erloes


# ── Beleg-Nr. im Vorschlag und beim Speichern ────────────────────────────────

def test_suggestion_includes_next_document_number(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 11)
    offen = tmp_db.insert_booking('2026-02-15', -10.00,
                                  recipient_client='Partner',
                                  booking_type='entry')
    d = _suggest(tmp_db, offen)
    assert d['document_nr'] == '26F012'
    # Nur ansehen: ein zweiter Vorschlag liefert dieselbe Nummer
    assert _suggest(tmp_db, offen)['document_nr'] == '26F012'


def test_saving_consumes_the_suggested_number(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 11)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-02-15', -10.00,
                               recipient_client='Partner', booking_type='entry')
    d = _suggest(tmp_db, bk)

    handle_add_transaction(tmp_db, {
        'transaction_id': [str(bk)], 'date': ['2026-02-15'],
        'amount': ['-10.00'], 'currency': ['EUR'], 'coa_id': [str(coa)],
        'document_nr': [d['document_nr']],
        'suggest_range_id': [str(d['range_id'])],
    })

    assert tmp_db.get_booking_by_id(bk)[16] == '26F012'
    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F013'


def test_saving_an_edited_number_leaves_the_range_alone(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'F', '', 11)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-02-15', -10.00,
                               recipient_client='Partner', booking_type='entry')
    d = _suggest(tmp_db, bk)

    handle_add_transaction(tmp_db, {
        'transaction_id': [str(bk)], 'date': ['2026-02-15'],
        'amount': ['-10.00'], 'currency': ['EUR'], 'coa_id': [str(coa)],
        'document_nr': ['HANDGESETZT'],
        'suggest_range_id': [str(d['range_id'])],
    })

    assert tmp_db.peek_next_number('receipt_company', 2026)[0] == '26F012'


def test_a_number_saved_after_a_deletion_lifts_the_counter_so_the_next_upload_does_not_issue_it_again(tmp_db):
    """Der Vorschlag „26B002“ wird geholt (Zähler 1), dann löscht der Nutzer den Beleg „26B001“ (der Zähler geht auf 0 zurück), dann speichert er die
    Buchung mit „26B002“: Der Zähler steht danach auf 2 – vorher blieb er auf 0, und die Nummer wurde ein zweites Mal vergeben."""
    tmp_db.insert_number_range('receipt_company', 2026, 'B', '', 0)
    receipt_id, first, _path = tmp_db.insert_receipt_with_file('2026-03-04', 'a.pdf', '', b'Inhalt')
    assert first == '26B001'
    booking = tmp_db.insert_booking('2026-03-05', -10.00, recipient_client='Partner', booking_type='entry')
    suggestion = _suggest(tmp_db, booking)
    assert suggestion['document_nr'] == '26B002'
    tmp_db.delete_receipt(receipt_id)
    assert tmp_db.fetch_number_ranges('receipt_company')[0][5] == 0

    coa = tmp_db.get_coa_id_by_account_number(6815)
    handle_add_transaction(tmp_db, {
        'transaction_id': [str(booking)], 'date': ['2026-03-05'], 'amount': ['-10.00'], 'currency': ['EUR'], 'coa_id': [str(coa)],
        'document_nr': [suggestion['document_nr']], 'suggest_range_id': [str(suggestion['range_id'])],
    })

    assert tmp_db.get_booking_by_id(booking)[16] == '26B002'
    assert tmp_db.fetch_number_ranges('receipt_company')[0][5] == 2
    assert tmp_db.insert_receipt_with_file('2026-03-06', 'b.pdf', '', b'Inhalt')[1] == '26B003'      # nicht „26B002“: Die trägt die Buchung


# ── Maske ────────────────────────────────────────────────────────────────────

def test_form_offers_the_suggestion_button(tmp_db):
    bk = tmp_db.insert_booking('2026-02-15', -10.00,
                               recipient_client='Partner', booking_type='entry')
    html = PageTransactions(tmp_db, edit_transaction_id=bk)
    assert 'applySuggestion()' in html
    assert 'Vorschlag</button>' in html
    assert 'name="suggest_range_id"' in html
