# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für das neutrale E-Rechnungsmodell (Summen, Steuer, Einheiten)."""
from decimal import Decimal

import pytest

from export.einvoice_model import build_einvoice, unit_code, rate_to_pct
from tests.golden_cases import make_case


def _model(db, case):
    iid = make_case(db, case)
    return build_einvoice(db, db.get_invoice_by_id(iid), db.get_invoice_items(iid))


def test_totals_are_exactly_consistent(tmp_db):
    m = _model(tmp_db, 'std19')
    t = m.totals
    assert t.line_total == sum(l.net for l in m.lines)
    assert t.tax_total == sum(b.amount for b in m.tax_breakdown)
    assert t.grand_total == t.tax_basis + t.tax_total
    assert t.payable == t.grand_total - t.prepaid
    assert m.warnings == []                      # gespeicherte Summen stimmen


def test_tax_breakdown_is_list_per_rate(tmp_db):
    m = _model(tmp_db, 'std7')
    assert [(b.category, b.rate) for b in m.tax_breakdown] == [('S', Decimal('7'))]


def test_kleinunternehmer_is_exempt_with_reason(tmp_db):
    m = _model(tmp_db, 'ku')
    assert m.is_exempt
    b = m.tax_breakdown[0]
    assert (b.category, b.rate, b.amount) == ('E', Decimal('0'), 0)
    assert '§ 19 UStG' in b.exemption_reason
    assert all(l.tax_category == 'E' for l in m.lines)


@pytest.mark.parametrize('text, code', [
    ('Stk.', 'C62'), ('Std.', 'HUR'), ('HUR', 'HUR'), ('C62', 'C62'),
    ('Tag', 'DAY'), ('pauschal', 'LS'), ('STD.', 'HUR'), ('m²', 'MTK'), ('kg', 'KGM'), ('', 'C62'), (None, 'C62'), ('unbekannt', 'C62'),
    ('Fl.', 'XBO'), ('fl.', 'XBO'), ('FL.', 'XBO'), ('Fl', 'XBO'), ('fl', 'XBO'), ('Flasche', 'XBO')])
def test_unit_code(text, code):
    assert unit_code(text) == code


@pytest.mark.parametrize('value, expected', [
    (19, Decimal('19')), (0.19, Decimal('19')), (7, Decimal('7')),
    (0, Decimal('0')), (None, Decimal('19'))])
def test_rate_to_pct(value, expected):
    assert rate_to_pct(value) == expected


def test_unknown_unit_is_visible(tmp_db, capsys):
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE InvoiceItems SET Unit='Zauberstab' WHERE InvoiceId=?", (iid,))
    m = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert m.lines[0].unit_code == 'C62'
    assert any('Zauberstab' in w for w in m.warnings)
    assert 'Zauberstab' in capsys.readouterr().out


def test_control_characters_do_not_break_ubl(tmp_db):
    import xml.etree.ElementTree as ET
    from export.xrechnung_invoice import XRechnungGenerator
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET BuyerCompany=? WHERE ID=?", ('Muster\x07 AG\x00', iid))
        c.execute("UPDATE InvoiceItems SET Description=? WHERE InvoiceId=?", ('Buch\x1b', iid))
    xml = XRechnungGenerator().generate_xml(
        tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid), db=tmp_db)
    assert ET.fromstring(xml.encode('utf-8')) is not None
    assert 'Muster AG' in xml and '>Buch<' in xml


@pytest.mark.parametrize('stored, shown', [
    ('Std.', 'Std.'), ('Stk.', 'Stk.'), ('Tag', 'Tag'), ('h', 'h'),       # Freitext bleibt
    ('HUR', 'Std.'), ('C62', 'Stk.'), ('DAY', 'Tag'), ('KMT', 'km'), ('KGM', 'kg'),
    ('MTR', 'm'), ('MTK', 'm²'), ('LTR', 'l'), ('LS', 'pausch.'), ('XBO', 'Fl.'),
    ('ZZZ', 'ZZZ'),                                                        # unbekannter Code
    ('', 'Stk.'), (None, 'Stk.')])
def test_unit_display(stored, shown):
    from export.einvoice_model import unit_display
    assert unit_display(stored) == shown


def test_pdf_shows_readable_unit_but_xml_keeps_code(tmp_db, monkeypatch, tmp_path):
    import io
    import pdfplumber
    from export.pdf_invoice import generate_invoice_pdf
    from export.cii_invoice import generate_cii
    monkeypatch.chdir(tmp_path)
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE InvoiceItems SET Unit='HUR' WHERE InvoiceId=?", (iid,))
    pdf, _ = generate_invoice_pdf(tmp_db, iid)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        words = p.pages[0].extract_text().split()
    assert 'Std.' in words and 'HUR' not in words
    xml = generate_cii(tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid), db=tmp_db)
    assert b'unitCode="HUR"' in xml


# ── Das Dokument zeigt nie den Stand der Zahlungen (2.1.2) ───────────────────

def _partly_paid(db, case='std19', paid=50.00):
    """Rechnung des Falls mit einer Teilzahlung: ``AmountDue`` ist kleiner als der Rechnungsbetrag."""
    iid = make_case(db, case)
    booking = db.insert_booking('2026-04-01', paid, booking_type='bank')
    db.link_invoice_to_transaction(iid, booking, paid)
    assert db.get_invoice_by_id(iid)[39] < db.get_invoice_by_id(iid)[38]
    return iid


def test_the_model_shows_the_invoice_amount_not_the_open_amount(tmp_db):
    iid = _partly_paid(tmp_db)
    model = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert model.totals.prepaid == 0
    assert model.totals.payable == model.totals.grand_total == 1779100      # 177,91 € in Minor Units
    assert model.warnings == []


def test_cii_and_ubl_of_a_partly_paid_invoice_state_no_prepaid_amount(tmp_db):
    from export.cii_invoice import generate_cii
    from export.xrechnung_invoice import XRechnungGenerator
    iid = _partly_paid(tmp_db)
    inv, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    cii = generate_cii(inv, items, db=tmp_db).decode('utf-8')
    ubl = XRechnungGenerator().generate_xml(inv, items, db=tmp_db)
    assert 'TotalPrepaidAmount' not in cii and 'PrepaidAmount' not in ubl
    assert '<ram:DuePayableAmount>177.91</ram:DuePayableAmount>' in cii
    assert '<cbc:PayableAmount currencyID="EUR">177.91</cbc:PayableAmount>' in ubl


def test_the_documents_do_not_change_with_the_payments(tmp_db):
    """Dasselbe Dokument vor und nach der Zahlung: Ein Dokument ist die Rechnung, die der Kunde bekam."""
    from export.cii_invoice import generate_cii
    from export.xrechnung_invoice import XRechnungGenerator
    iid = make_case(tmp_db, 'std19')
    inv, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    before = (generate_cii(inv, items, db=tmp_db), XRechnungGenerator().generate_xml(inv, items, db=tmp_db))
    booking = tmp_db.insert_booking('2026-04-01', 50.00, booking_type='bank')
    tmp_db.link_invoice_to_transaction(iid, booking, 50.00)
    inv = tmp_db.get_invoice_by_id(iid)
    after = (generate_cii(inv, items, db=tmp_db), XRechnungGenerator().generate_xml(inv, items, db=tmp_db))
    assert after == before


# ── Steuernummer und USt-IdNr. (2.3.5) ───────────────────────────────────────
# Je Land der Tabelle ``_VAT_ID_RULES``: gültige und ungültige Beispiele. Eine USt-IdNr. ist die Angabe nur, wenn sie mit dem Länderkürzel
# beginnt UND der Rest zu den Nummern des Landes passt; sonst ist sie eine Steuernummer – auch wenn sie so aussieht.
VAT_ID_EXAMPLES = {
    'AT': (['ATU12345678'], ['ATU1234567', 'AT12345678', 'ATU123456789']),
    'BE': (['BE0123456789', 'BE123456789'], ['BE12345678', 'BE01234567890']),
    'BG': (['BG123456789', 'BG1234567890'], ['BG12345678', 'BG12345678901']),
    'CY': (['CY12345678X'], ['CY123456789', 'CY12345678']),
    'CZ': (['CZ12345678', 'CZ123456789', 'CZ1234567890'], ['CZ1234567', 'CZ12345678901']),
    'DE': (['DE123456789'], ['DE12345678', 'DE1234567890']),
    'DK': (['DK12345678'], ['DK1234567', 'DK123456789']),
    'EE': (['EE123456789'], ['EE12345678']),
    'EL': (['EL123456789'], ['EL12345678', 'GR123456789']),
    'ES': (['ESX1234567R', 'ESB12345678', 'ES12345678A'], ['ES1234567', 'ESXX234567R']),
    'FI': (['FI12345678'], ['FI1234567']),
    'FR': (['FR12345678901', 'FRXX123456789', 'FR1A123456789'], ['FRIO123456789', 'FR1234567890']),
    'HR': (['HR12345678901'], ['HR1234567890']),
    'HU': (['HU12345678'], ['HU1234567']),
    'IE': (['IE1234567T', 'IE1234567TA', 'IE1A23456B', 'IE1+23456B'], ['IE123456', 'IE12345678', 'IE1234567']),
    'IT': (['IT12345678901'], ['IT1234567890']),
    'LT': (['LT123456789', 'LT123456789012'], ['LT12345678901']),
    'LU': (['LU12345678'], ['LU1234567']),
    'LV': (['LV12345678901'], ['LV1234567890']),
    'MT': (['MT12345678'], ['MT1234567']),
    'NL': (['NL123456789B01'], ['NL123456789A01', 'NL123456789B1']),
    'PL': (['PL1234567890'], ['PL123456789']),
    'PT': (['PT123456789'], ['PT12345678']),
    'RO': (['RO12', 'RO1234567890'], ['RO1', 'RO12345678901']),
    'SE': (['SE123456789012'], ['SE12345678901']),
    'SI': (['SI12345678'], ['SI1234567']),
    'SK': (['SK1234567890'], ['SK123456789']),
    'XI': (['XI123456789', 'XI123456789012', 'XIGD123', 'XIHA123'], ['XI12345678', 'XIGD12', 'XIGD1234']),
    'GB': (['GB123456789', 'GB123456789012', 'GBGD123', 'GBHA123'], ['GB12345678', 'GBHA12']),
    'CHE': (['CHE123456789', 'CHE123456789MWST', 'CHE123456789TVA', 'CHE123456789IVA'],
            ['CHE12345678', 'CHE123456789XYZ', 'CHE123456789MVA']),
    'NO': (['NO123456789', 'NO123456789MVA'], ['NO12345678', 'NO123456789MWST']),
}


def _spaced(value):
    """Dieselbe Nummer, in Dreiergruppen mit Leerzeichen, Bindestrichen und Punkten geschrieben und klein."""
    groups = [value[i:i + 3] for i in range(0, len(value), 3)]
    return ' '.join(groups[:2]) + '-' + '.'.join(groups[2:]) if len(groups) > 2 else '-'.join(groups)


@pytest.mark.parametrize('prefix', sorted(VAT_ID_EXAMPLES))
def test_tax_id_kind_knows_the_numbers_of_each_country(prefix):
    from export.einvoice_model import tax_id_kind
    valid, invalid = VAT_ID_EXAMPLES[prefix]
    for example in valid:
        assert tax_id_kind(example) == ('vat', example), example
        assert tax_id_kind(_spaced(example).lower()) == ('vat', example), example      # Leerzeichen, Bindestriche, Punkte, klein
        assert tax_id_kind(f'USt-IdNr. {example}') == ('vat', example), example         # mit Bezeichnung davor
    for example in invalid:
        assert tax_id_kind(example) == ('tax', example), example                        # Steuernummer, wie eingegeben


def test_every_country_of_the_table_has_examples_and_nothing_else_is_known():
    from export.einvoice_model import _VAT_ID_RULES
    assert set(_VAT_ID_RULES) == set(VAT_ID_EXAMPLES)


@pytest.mark.parametrize('text, kind, value', [
    # Die Fälle der fachlichen Prüfung: Diese Angaben gingen früher falsch hinaus
    ('StNr. 1338150815', 'tax', '1338150815'),                 # „ST“ + „NR…“ galt als USt-IdNr.
    ('BY 123 456 78901', 'tax', 'BY 123 456 78901'),           # das Bundesland ist kein Länderkürzel
    ('NW 133 8150 8150', 'tax', 'NW 133 8150 8150'),
    ('HRB 12345', 'tax', 'HRB 12345'),                         # eine Registernummer
    ('USt-IdNr. DE123456789', 'vat', 'DE123456789'),           # galt als Steuernummer
    ('DE12345678', 'tax', 'DE12345678'),                       # zu wenige Ziffern: keine USt-IdNr.
    ('DE 12/345/67890', 'tax', 'DE 12/345/67890'),
    ('Finanzamt München 143/123/45678', 'tax', 'Finanzamt München 143/123/45678'),
    # Schreibweisen
    ('DE-123 456 789', 'vat', 'DE123456789'), ('de.123.456.789', 'vat', 'DE123456789'), ('  DE123456789  ', 'vat', 'DE123456789'),
    ('12/345/67890', 'tax', '12/345/67890'), ('123 456 78901', 'tax', '123 456 78901'),
    ('  12 / 345 / 67890  ', 'tax', '12 / 345 / 67890'), ('12-345-67890', 'tax', '12-345-67890'),
    ('AT U 123-456-78', 'vat', 'ATU12345678'), ('CHE-123.456.789 MWST', 'vat', 'CHE123456789MWST'),
    ('NO 123 456 789 MVA', 'vat', 'NO123456789MVA'),
    # nichts oder nur eine Bezeichnung
    ('  ', '', ''), ('', '', ''), (None, '', ''), ('USt-IdNr.', '', ''), ('Steuernummer:', '', ''),
])
def test_tax_id_kind(text, kind, value):
    from export.einvoice_model import tax_id_kind
    assert tax_id_kind(text) == (kind, value)


@pytest.mark.parametrize('label', [
    'USt-IdNr.', 'USt-IdNr', 'USt-Id-Nr.', 'USt.-IdNr.', 'USt-ID', 'UStID', 'USt ID', 'ustidnr', 'USTIDNR', 'UID', 'UID-Nr.',
    'VAT', 'VAT-ID', 'VAT ID', 'VAT No.', 'VAT No', 'vat-id',
    'Umsatzsteuer-ID', 'Umsatzsteuer-Identifikationsnummer', 'Umsatzsteuer-IdNr.', 'umsatzsteuer identifikationsnummer'])
@pytest.mark.parametrize('separator', [' ', ': ', ':', '. ', '  '])
def test_a_label_before_a_vat_id_is_cut_off(label, separator):
    from export.einvoice_model import tax_id_kind
    assert tax_id_kind(f'{label}{separator}DE123456789') == ('vat', 'DE123456789')


@pytest.mark.parametrize('label', [
    'St.-Nr.', 'St.-Nr', 'StNr', 'StNr.', 'St Nr.', 'Steuer-Nr.', 'Steuer-Nr', 'Steuer Nr', 'Steuernummer', 'Steuernr.', 'steuernummer',
    'STEUERNUMMER', 'Steuer-Nummer'])
@pytest.mark.parametrize('separator', [' ', ': ', ':', '. '])
def test_a_label_before_a_tax_number_is_cut_off(label, separator):
    from export.einvoice_model import tax_id_kind
    assert tax_id_kind(f'{label}{separator}12/345/67890') == ('tax', '12/345/67890')


@pytest.mark.parametrize('text, kind, value', [
    ('VAT NO123456789MVA', 'vat', 'NO123456789MVA'),           # das „NO“ ist Norwegen, keine Bezeichnung „No“
    ('VAT No NO123456789MVA', 'vat', 'NO123456789MVA'),
    ('Finanzamt St.-Nr. 12345', 'tax', 'Finanzamt St.-Nr. 12345'),     # eine Bezeichnung zählt nur vorn
    ('x USt-IdNr. DE123456789', 'tax', 'x USt-IdNr. DE123456789'),
    ('USt-IdNr. DE123456789 (Hauptsitz)', 'tax', 'DE123456789 (Hauptsitz)'),   # mit Zusatz keine USt-IdNr.
    ('Stadtsparkasse 12345', 'tax', 'Stadtsparkasse 12345'),           # „St…“ allein ist keine Bezeichnung
])
def test_a_label_is_only_a_label_at_the_start_and_as_a_whole_word(text, kind, value):
    from export.einvoice_model import tax_id_kind
    assert tax_id_kind(text) == (kind, value)


# ── Skonto (2.3.5) ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('days, percent, expected', [
    (7, 2, (7, Decimal('2.00'))),
    (7, 2.0, (7, Decimal('2.00'))),                    # die Spalte ist REAL: 2.0 ist 2 %, nicht 1.99999…
    (14, 2.5, (14, Decimal('2.50'))),
    (7.0, 0.5, (7, Decimal('0.50'))),
    ('7', '2.5', (7, Decimal('2.50'))),                # auch Text (SQL-Konsole)
    (' 7 ', ' 2.5 ', (7, Decimal('2.50'))),            # nach strip
    ('007', '99.99', (7, Decimal('99.99'))),           # bis zu drei Ziffern
    (1, 99.99, (1, Decimal('99.99'))),
    (365, 12, (365, Decimal('12.00'))),                # die Obergrenze der Tage
    (7, Decimal('2.5'), (7, Decimal('2.50'))),
])
def test_parse_skonto_reads_the_stored_values(days, percent, expected):
    from export.einvoice_model import parse_skonto
    assert parse_skonto(days, percent) == expected


@pytest.mark.parametrize('days, percent', [
    (None, None), ('', ''), (0, 0), (0, None), (None, 0), (0.0, 0.0), ('  ', '0'), (' ', Decimal('0.00')), ('0', '0.00'), ('000', 0.0)])
def test_empty_and_zero_mean_no_skonto(days, percent):                     # so schickt es das Formular
    from export.einvoice_model import parse_skonto
    assert parse_skonto(days, percent) is None


PERCENT_MESSAGE = 'Skonto: Prozent größer 0 und kleiner 100, höchstens zwei Nachkommastellen.'
DAYS_MESSAGE = 'Skonto: Tage als ganze Zahl von 1 bis 365.'
PAIR_MESSAGE = 'Skonto braucht Prozent und Tage.'


@pytest.mark.parametrize('days, percent, message', [
    (7, 2.333, PERCENT_MESSAGE), (7, '2.333', PERCENT_MESSAGE), (7, 100, PERCENT_MESSAGE), (7, '100', PERCENT_MESSAGE),
    (7, -1, PERCENT_MESSAGE), (7, '-1', PERCENT_MESSAGE), (7, 'viel', PERCENT_MESSAGE), (7, float('nan'), PERCENT_MESSAGE),
    (7, float('inf'), PERCENT_MESSAGE), (7, True, PERCENT_MESSAGE), (7, '2,5', PERCENT_MESSAGE), (7, '1e1', PERCENT_MESSAGE),
    (7, '5.', PERCENT_MESSAGE), (7, '.5', PERCENT_MESSAGE), (7, '012', PERCENT_MESSAGE), (7, '+2', PERCENT_MESSAGE),
    (7, [2], PERCENT_MESSAGE), (7, {}, PERCENT_MESSAGE), (7, Decimal('NaN'), PERCENT_MESSAGE),
    (7, Decimal('sNaN'), PERCENT_MESSAGE), (7, Decimal('Infinity'), PERCENT_MESSAGE), (7, Decimal('-Infinity'), PERCENT_MESSAGE),
    (7, Decimal('100'), PERCENT_MESSAGE), (7, Decimal('2.333'), PERCENT_MESSAGE),
    (1.5, 2, DAYS_MESSAGE), (-3, 2, DAYS_MESSAGE), ('-3', 2, DAYS_MESSAGE), ('+7', 2, DAYS_MESSAGE), ('sieben', 2, DAYS_MESSAGE),
    (True, 2, DAYS_MESSAGE), (366, 2, DAYS_MESSAGE), ('366', 2, DAYS_MESSAGE), ('1000', 2, DAYS_MESSAGE), ('7.0', 2, DAYS_MESSAGE),
    ('7,0', 2, DAYS_MESSAGE), ('1e1', 2, DAYS_MESSAGE), ('٣', 2, DAYS_MESSAGE), ('１２', 2, DAYS_MESSAGE),      # keine ASCII-Ziffern
    (Decimal('7'), 2, DAYS_MESSAGE), ([7], 2, DAYS_MESSAGE),                                                # kein angenommener Typ
    (0, 2, PAIR_MESSAGE),                              # Tage 0 gelten als leer: Prozent allein genügt nicht
    (7, 0, PAIR_MESSAGE), (7, None, PAIR_MESSAGE), (None, 2, PAIR_MESSAGE),
])
def test_parse_skonto_refuses_what_cannot_be_a_skonto(days, percent, message):
    from export.einvoice_model import parse_skonto
    with pytest.raises(ValueError) as refusal:
        parse_skonto(days, percent)
    assert str(refusal.value) == message


# Werte, die ein Client oder die SQL-Konsole schicken kann: Sie kommen sofort zurück. ``int()`` auf den Text ``1e9999999`` (über
# ``Decimal``) rechnete minutenlang – geprüft wird jetzt, BEVOR etwas in eine Zahl umgewandelt wird.
HOSTILE = ['1e9999999', '1e400', '1e3', 3650000, 10 ** 15, 10 ** 400, float('inf'), float('-inf'), float('nan'), 1e308, 1e400,
           '9' * 100000, '1' + '0' * 100000, ' ' * 10 + '7' + ' ' * 10 + 'x', 'Infinity', 'NaN', '-Infinity', '0x10', '1_0']


@pytest.mark.parametrize('field', ['days', 'percent'])
def test_hostile_values_are_refused_at_once_and_never_converted(field):
    import time
    from export.einvoice_model import parse_skonto
    started = time.perf_counter()
    for value in HOSTILE:
        args = (value, 2) if field == 'days' else (7, value)
        with pytest.raises(ValueError):
            parse_skonto(*args)
    assert time.perf_counter() - started < 1.0                         # Millisekunden; die Grenze ist großzügig


def test_a_hostile_text_is_never_handed_to_int_or_decimal(monkeypatch):
    """Der Weg, der die Prüfung zuerst trifft: ein Text wird nach Länge und Zeichen geprüft, nicht umgewandelt."""
    import export.einvoice_model as model

    def forbidden(*args, **kwargs):
        raise AssertionError('umgewandelt, bevor geprüft wurde')

    monkeypatch.setattr(model, 'Decimal', forbidden)
    for value in ('1e9999999', '1e400', '9' * 1000):
        with pytest.raises(ValueError):
            model.parse_skonto(value, '2')                              # Tage
    for value in ('1e9999999', '1e400', '9' * 1000):
        with pytest.raises(ValueError):
            model.parse_skonto('7', value)                              # Prozent


@pytest.mark.parametrize('gross, percent, expected', [
    (1779100, Decimal('2'), 35600),                    # 177,91 € × 2 % = 3,5582 € → 3,56 €
    (9900, Decimal('0.5'), 0),                         # 0,99 € × 0,5 % = 0,00495 € → 0,00 € (zweistufig gerundet: 0,01 €)
    (1001000, Decimal('2.5'), 25000),                  # 100,10 € × 2,5 % = 2,5025 € → 2,50 €
    (1000000, Decimal('2.5'), 25000),                  # genau 2,50 €
    (1010000, Decimal('0.5'), 5100),                   # 101,00 € × 0,5 % = 0,505 € → 0,51 € (halb auf)
    (1009900, Decimal('0.5'), 5000),                   # 100,99 € × 0,5 % = 0,50495 € → 0,50 €
    (1000000, Decimal('0.05'), 500),                   # 100,00 € × 0,05 % = 0,05 €
    (1779100, 2.5, 44500),                             # 177,91 € × 2,5 % = 4,44775 € → 4,45 €; ein float ist kein Fehler
    (0, Decimal('2'), 0),
])
def test_the_skonto_amount_is_rounded_once_to_the_cent(gross, percent, expected):
    from export.einvoice_model import skonto_amount
    assert skonto_amount(gross, percent) == expected
    assert skonto_amount(gross, percent) % 100 == 0                     # immer ein ganzer Cent


def test_a_credit_note_has_no_skonto_even_with_an_old_value_in_its_row(tmp_db):
    iid = make_case(tmp_db, 'skonto')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET InvoiceKind = 'storno' WHERE ID = ?", (iid,))
    model = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert model.skonto is None and model.skonto_unreadable is False


def test_the_model_reads_the_skonto_period_from_the_invoice_date(tmp_db):
    from datetime import date
    model = _model(tmp_db, 'skonto')
    assert (model.skonto.days, model.skonto.percent, model.skonto.until) == (7, Decimal('2.00'), date(2026, 3, 11))


def test_an_unusable_stored_skonto_is_no_skonto_but_is_flagged(tmp_db):
    iid = make_case(tmp_db, 'skonto')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET SkontoPercent = 2.333 WHERE ID = ?", (iid,))
    model = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert model.skonto is None and model.skonto_unreadable is True


def test_a_period_beyond_the_calendar_is_flagged_not_raised(tmp_db):
    iid = make_case(tmp_db, 'skonto')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET SkontoDays = 99999999 WHERE ID = ?", (iid,))
    model = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert model.skonto is None and model.skonto_unreadable is True


@pytest.mark.parametrize('factor', [-1, 0], ids=['credit', 'zero'])
@pytest.mark.parametrize('percent', [2, 2.333], ids=['usable', 'unusable'])
def test_a_document_of_no_positive_amount_has_no_skonto_and_no_gap(tmp_db, factor, percent):
    """Eine Gutschrift und eine 0-€-Rechnung haben nichts abzuziehen: kein Skonto im Modell, und das ist keine Lücke – auch
    nicht, wenn in der Zeile ein unbrauchbarer Altwert steht (wie bei der Stornorechnung)."""
    from export.einvoice_model import einvoice_gaps
    iid = make_case(tmp_db, 'skonto')
    with tmp_db._get_connection() as c:
        c.execute('UPDATE InvoiceItems SET TotalNet = TotalNet * ? WHERE InvoiceId = ?', (factor, iid))
        c.execute('UPDATE Invoices SET SkontoPercent = ? WHERE ID = ?', (percent, iid))
    model = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert model.totals.grand_total <= 0
    assert model.skonto is None and model.skonto_unreadable is False
    assert not [gap for gap in einvoice_gaps(model) if 'Skonto' in gap.text]


def test_the_skonto_sentence_names_the_due_date_only_when_the_period_does_not_end_after_it():
    """Ein Altbestand kann eine Skontofrist nach der Fälligkeit tragen: Das Dokument widerspricht sich dann nicht."""
    from datetime import date
    from export.einvoice_model import Skonto, skonto_sentence
    due = date(2026, 3, 18)
    before = skonto_sentence(Skonto(7, Decimal('2.00'), date(2026, 3, 11)), 1779100, due)
    on_the_day = skonto_sentence(Skonto(14, Decimal('2.00'), date(2026, 3, 18)), 1779100, due)
    after = skonto_sentence(Skonto(30, Decimal('2.00'), date(2026, 4, 3)), 1779100, due)
    assert before.endswith(' Ohne Abzug zahlbar bis zum 18.03.2026.')
    assert on_the_day.endswith(' Ohne Abzug zahlbar bis zum 18.03.2026.')
    assert after == ('Bei Zahlung bis zum 03.04.2026 (innerhalb von 30 Tagen ab Rechnungsdatum) gewähren wir 2 % Skonto: '
                     'abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen.')
    assert 'Ohne Abzug' not in skonto_sentence(Skonto(7, Decimal('2.00'), date(2026, 3, 11)), 1779100, None)


@pytest.mark.parametrize('typed', ['Fl.', 'fl.', 'Fl', 'fl', 'Flasche'])
def test_a_bottle_goes_to_the_e_invoice_as_xbo_and_comes_back_as_fl(typed):
    """„Fl.“ (Flasche) steht als UN/ECE-Code ``XBO`` in der E-Rechnung und wird für Menschen wieder „Fl.“ – der Rundweg Text → Code → Anzeige."""
    from export.einvoice_model import unit_display
    unknown = []
    assert unit_code(typed, unknown) == 'XBO' and unknown == []                      # kein Fallback auf Stück: nichts wird als unbekannt gesammelt
    assert unit_display(unit_code(typed)) == 'Fl.'
    assert unit_display('Fl.') == 'Fl.'                                              # der Freitext selbst bleibt, wie er gespeichert ist
