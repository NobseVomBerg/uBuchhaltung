# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Steuersätze aus den Stammdaten: das Beispiel im Demo-Bestand, die Beispiele für die Prüfung der E-Rechnung und der Hinweis des DATEV-Exports (2.8.0, Etappe 7a).

* **Demo** (``demo_history.py``, ``demo_seed.py``, ``seed_data/test/demo_history.json``): Der Bestand des Profils ``full`` bekommt zwei Einträge in den Stammdaten und EINE Rechnung
  „19 % und steuerfrei“, in zwei Raten bezahlt – die Tests dazu stehen in ``tests/test_demo_history.py``. Hier nur das, was die Ladeschicht für sich leistet: Die Einträge legt sie
  nur an, wo die Vorgaben welche nennen.
* **Beispiele** (``tools/make_einvoice_samples.py``): drei neue Paare – 19 % und 5,5 % (mit einer Position über 0,09 € netto, an der zweistufiges Runden einen Cent zu viel ergäbe),
  19 % und steuerfrei, ganz steuerfrei –, gespeichert über den Weg der Maske (``taxRateId``), ohne Lücke in der E-Rechnung und mit der Probe, die Mustang in der CI macht, soweit ohne
  Validator möglich: je Aufschlüsselung Steuer = Basis × Satz einstufig auf den Cent gerundet, Summen BT-106/109/110/112/115, Kategorie E mit Grund und ohne Code. Die CI-Prüfung selbst
  (Mustang) läuft nur dort.
* **DATEV-Hinweis** (``server/handlers.handle_datev_export``, ``export/datev.export_to_datev``): Wie viele der exportierten Buchungssätze einen Steuersatz über 0 tragen, für den der Export
  keinen BU-Schlüssel ableitet (5,5 % etwa) – nicht blockierend, der Stapel bleibt Byte für Byte derselbe.

Alle Namen, Gründe, Konten und Beträge sind erfunden; ein „heute“ steht nie in einer Modulkonstante.
"""
import datetime
import os
import tempfile
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

import pdfplumber
import pytest

import demo
import demo_history
import demo_seed
import server.handlers as handlers
import userctx
from export import datev
from export.einvoice_model import build_einvoice, einvoice_gaps, gap_texts
from money import to_minor
from server.pages_miscellaneous import MSG_MAX_LENGTH
from tests.datev_helpers import datev_post, is_confirm_redirect
from tests.invoice_world import build_world, columns, make_payload, query_rows, save
from tests.single_rate_helpers import embedded_xml
from tests.susa_helpers import account_id
from tests.test_mixed_tax_documents import _samples_module
from tests.test_mixed_tax_reports import mixed_world, with_banks
from tests.test_tax_entries_reports import entry_world

D = Decimal
USER = 'anna'
DAY = datetime.date(2026, 11, 3)          # ein fester Tag für die Ladeschicht der Demo (nie die Uhr)
NEW_SAMPLES = ('satz-5-5', 'steuerfrei-gemischt', 'steuerfrei')
OLD_SAMPLES = ('normalfall', 'kleinunternehmer', 'berichtigt', 'storno', 'ersatz', 'skonto', 'steuernummer', 'steuernummer-kleinunternehmer', 'mischsatz')


# ═════════════════════════════════════════════════════════════════════════════
# Demo: die Einträge legt nur die Ladeschicht an, und nur, wo die Vorgaben welche nennen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_loader_creates_the_entries_of_the_specification_in_order_and_resolves_their_accounts(tmp_db):
    tmp_db.load_test_seed_data(include_invoices=False, include_dated=False)
    entries = demo_history.build_history(DAY, 'full')['tax_entries']
    scenario = demo_seed.DemoScenario(tmp_db, today=DAY, extra={'tax_entries': entries})

    scenario.load_tax_entries()

    stored = {entry.id: entry for entry in tmp_db.fetch_tax_rates()}
    assert set(scenario.tax_entries) == {'exempt', 'reduced'}
    exempt, reduced = stored[scenario.tax_entries['exempt']], stored[scenario.tax_entries['reduced']]
    assert (exempt.bp, exempt.kind, exempt.reason, exempt.revenue_coa_id) == (0, 'exempt', 'Steuerfreie Beispielleistung (Demodaten)', tmp_db.get_coa_id_by_account_number(4100))
    assert (reduced.bp, reduced.kind, reduced.revenue_coa_id) == (550, 'standard', tmp_db.get_coa_id_by_account_number(4180))
    assert exempt.selectable and reduced.selectable and [entry.bp for entry in tmp_db.fetch_tax_rates()] == [1900, 700, 0, 550]


def test_without_entries_in_the_specification_the_loader_adds_nothing(tmp_db):
    """Ein echter Bestand und jede Vorgabe ohne ``tax_entries`` (das Profil ``test``, das Szenario allein) behalten genau die Vorbelegung: 19 und 7 Prozent."""
    tmp_db.load_test_seed_data(include_invoices=False, include_dated=False)

    demo_seed.DemoScenario(tmp_db, today=DAY).load_tax_entries()
    demo_seed.DemoScenario(tmp_db, today=DAY, extra={}).load_tax_entries()
    demo_seed.DemoScenario(tmp_db, today=DAY, extra=demo_history.build_history(DAY, 'test')).load_tax_entries()

    assert [(entry.label, entry.bp) for entry in tmp_db.fetch_tax_rates()] == [('19 %', 1900), ('7 %', 700)]


def test_a_revenue_account_the_chart_does_not_have_stops_the_loading_with_a_clear_sentence(tmp_db):
    tmp_db.load_test_seed_data(include_invoices=False, include_dated=False)
    entry = {'key': 'x', 'label': 'Test (erfunden)', 'kind': 'standard', 'bp': 550, 'reason': None, 'revenue_account': 4999}
    scenario = demo_seed.DemoScenario(tmp_db, today=DAY, extra={'tax_entries': [entry]})

    with pytest.raises(RuntimeError, match='4999'):
        scenario.load_tax_entries()

    assert [entry.bp for entry in tmp_db.fetch_tax_rates()] == [1900, 700]


# ═════════════════════════════════════════════════════════════════════════════
# Beispiele für die Prüfung der E-Rechnung (tools/make_einvoice_samples.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    return build_world(tmp_db)


def item_rows(db, invoice_id):
    """Je Position: (Position, Art, Satz der Position, Satz in Hundertstel Prozent, Grund, Erlöskonto) – was die Position an Momentaufnahme trägt."""
    return [(row['Position'], row['TaxCategory'], row['TaxRate'], row['TaxRateBp'], row['TaxExemptReason'], row['RevenueCoaId'])
            for row in query_rows(db, 'SELECT Position, TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position',
                                  invoice_id)]


def test_the_entry_samples_are_drafts_from_the_form_with_their_snapshots_and_sums_and_no_gap_in_the_einvoice(world):
    samples = _samples_module()
    template = save(make_payload(world, invoiceNumber='26R233'))['invoice_id']
    db = world.db

    drafts = samples._entry_drafts(db, template)

    free, reduced = db.get_coa_id_by_account_number(4100), db.get_coa_id_by_account_number(4180)
    reason = samples.FREE_REASON
    assert list(drafts) == list(NEW_SAMPLES)
    expected = {
        # Nummer, Kennzeichen, Kopf, (netto, Steuer, brutto), Positionen (Position, Art, Satz der Position, Satz in Hundertstel Prozent, Grund, Erlöskonto)
        'satz-5-5': ('SA-DEMO-1', 1, 0.19, ('310.09', '58.90', '368.99'),
                     [(1, 'S', 19.0, None, None, None), (2, 'S', 19.0, None, None, None), (3, 'S', 5.5, 550, None, reduced)]),
        'steuerfrei-gemischt': ('SG-DEMO-1', 1, 0.19, ('270.00', '36.10', '306.10'),
                                [(1, 'S', 19.0, None, None, None), (2, 'E', 0.0, 0, reason, free)]),
        'steuerfrei': ('SF-DEMO-1', None, 0.0, ('155.50', '0.00', '155.50'),
                       [(1, 'E', 0.0, 0, reason, free), (2, 'E', 0.0, 0, reason, free)]),
    }
    for name, draft_id in drafts.items():
        number, flag, head, (net, tax, gross), items = expected[name]
        stored = columns(db, draft_id)
        assert (stored['InvoiceNumber'], stored['Status'], stored['MixedTaxRates'], stored['TaxRate'], stored['BuyerRouteID']) == (number, 'draft', flag, head, samples.LEITWEG_ID), name
        assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (to_minor(net), to_minor(tax), to_minor(gross)), name
        assert item_rows(db, draft_id) == items, name
        model = build_einvoice(db, db.get_invoice_by_id(draft_id), db.get_invoice_items(draft_id))
        assert gap_texts(einvoice_gaps(model)) == [], name


def test_the_entries_of_the_samples_have_no_exemption_code_and_use_accounts_of_the_standard_chart(world):
    """Steuerfrei mit Grund (Text allein, BT-120) und 5,5 %, beide mit einem Konto des Standard-Kontenrahmens – keiner mit Code (es gibt keine Spalte dafür)."""
    samples = _samples_module()
    db = world.db

    ids = samples._tax_entries(db)

    entries = {entry.id: entry for entry in db.fetch_tax_rates()}
    free, reduced = entries[ids['free']], entries[ids['reduced']]
    assert (free.bp, free.kind, free.reason, free.revenue_coa_id) == (0, 'exempt', samples.FREE_REASON, db.get_coa_id_by_account_number(4100))
    assert (reduced.bp, reduced.kind, reduced.reason, reduced.revenue_coa_id) == (550, 'standard', None, db.get_coa_id_by_account_number(4180))
    assert free.selectable and reduced.selectable
    assert not [row for row in query_rows(db, "SELECT name FROM pragma_table_info('TaxRates')") if 'Code' in row['name']]


def test_the_normal_case_of_the_samples_is_never_a_document_with_a_snapshot_of_the_master_data(world):
    """Auch ein Beleg mit 19 % im Kopf, ohne das Kennzeichen, kann Positionen mit der Momentaufnahme eines Eintrags tragen (19 % mit eigenem Erlöskonto): Sein Kopf sähe wie der
    Normalfall aus – das Merkmal ist der Satz in Hundertstel Prozent an der Position."""
    samples = _samples_module()
    db = world.db
    entry_id = db.insert_tax_rate(label='19 % mit Sonderkonto (erfunden)', bp=1900, kind='standard', revenue_coa_id=db.get_coa_id_by_account_number(4100))
    plain = save(make_payload(world, invoiceNumber='26R241', invoiceDate='2026-06-08'))['invoice_id']
    snap = save(make_payload(world, invoiceNumber='26R242', invoiceDate='2026-06-12', taxRateId=entry_id))
    assert snap['success'] is True, snap
    snap = snap['invoice_id']

    assert columns(db, snap)['TaxRate'] == 0.19 and columns(db, snap)['MixedTaxRates'] is None
    assert {row[3] for row in item_rows(db, snap)} == {1900}
    assert db.fetch_invoices()[0][0] == snap                              # er ist der jüngste Beleg mit 19 % im Kopf …
    assert samples._normal_invoice(db)[0] == plain                        # … und trotzdem nie der Normalfall der Beispiele


def test_the_copy_of_the_samples_takes_the_snapshot_along_but_the_paragraph_19_copy_never_carries_one(world):
    samples = _samples_module()
    db = world.db
    entry_id = db.insert_tax_rate(label='19 % mit Sonderkonto (erfunden)', bp=1900, kind='standard', revenue_coa_id=db.get_coa_id_by_account_number(4100))
    snap = save(make_payload(world, invoiceNumber='26R243', taxRateId=entry_id))['invoice_id']

    clone = samples._clone_for_correction(db, snap, 'KP-3')
    exempt = samples._clone_as_exempt(db, snap, 'KU-3')

    assert item_rows(db, clone) == item_rows(db, snap) and {row[3] for row in item_rows(db, clone)} == {1900}
    assert [row[1:] for row in item_rows(db, exempt)] == [('E', 0.0, None, None, None)] * 2             # § 19: die Art ‚E‘, keine Momentaufnahme
    assert columns(db, exempt)['TaxRate'] == -1


def test_the_mixed_sample_goes_through_the_shared_draft_helper_and_is_stored_as_before(world):
    """``_mixed_rates_draft`` geht jetzt über ``_form_draft``: dasselbe Speichern wie vorher (Nummer, Kennzeichen, Leitweg-ID, Positionen ohne Momentaufnahme)."""
    samples = _samples_module()
    template = save(make_payload(world, invoiceNumber='26R234'))['invoice_id']

    draft = samples._mixed_rates_draft(world.db, template)

    stored = columns(world.db, draft)
    assert (stored['InvoiceNumber'], stored['Status'], stored['MixedTaxRates'], stored['BuyerRouteID']) == ('MS-DEMO-1', 'draft', 1, samples.LEITWEG_ID)
    assert [row[1:] for row in item_rows(world.db, draft)] == [('S', 19.0, None, None, None), ('S', 7.0, None, None, None), ('S', 7.0, None, None, None), ('S', 19.0, None, None, None)]


def test_a_form_that_the_server_refuses_stops_the_tool_with_the_label_and_the_reason(world):
    samples = _samples_module()
    template = save(make_payload(world, invoiceNumber='26R235'))['invoice_id']
    items = [{'position': 1, 'description': 'Position', 'quantity': 1, 'unit': 'C62', 'unitPrice': 1.0, 'taxRate': 0, 'taxRateId': 424242}]

    with pytest.raises(SystemExit) as stopped:
        samples._form_draft(world.db, template, 'XX-DEMO-1', items, 'probe', taxRate=0.19, taxMode='mixed')

    assert str(stopped.value).startswith('probe: ') and 'nicht mehr wählbar' in str(stopped.value)


# ── die Prüfung der Norm, soweit ohne Validator möglich ─────────────────────────────────────────────────────────────────────────

def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _below(element, name):
    return [child for child in element.iter() if _local(child) == name]


def _text(element, name):
    found = [child for child in element if _local(child) == name]
    return None if not found else (found[0].text or '')


def ubl_model(xml_bytes):
    """Aufschlüsselungen, Zeilen und Summen eines UBL – Beträge als ``Decimal`` aus dem Text, nichts über den Code der App gerechnet."""
    root = ET.fromstring(xml_bytes)
    breakdowns = []
    for subtotal in _below(root, 'TaxSubtotal'):
        category = [c for c in subtotal if _local(c) == 'TaxCategory'][0]
        breakdowns.append({'category': _text(category, 'ID'), 'percent': D(_text(category, 'Percent')), 'basis': D(_text(subtotal, 'TaxableAmount')),
                           'tax': D(_text(subtotal, 'TaxAmount')), 'reason': _text(category, 'TaxExemptionReason'), 'code': _text(category, 'TaxExemptionReasonCode')})
    lines = []
    for line in _below(root, 'InvoiceLine'):
        classified = _below(line, 'ClassifiedTaxCategory')[0]
        lines.append({'category': _text(classified, 'ID'), 'percent': D(_text(classified, 'Percent')), 'net': D(_text(line, 'LineExtensionAmount'))})
    monetary = _below(root, 'LegalMonetaryTotal')[0]
    tax_total = [t for t in root if _local(t) == 'TaxTotal'][0]
    sums = {'BT-106': D(_text(monetary, 'LineExtensionAmount')), 'BT-109': D(_text(monetary, 'TaxExclusiveAmount')), 'BT-110': D(_text(tax_total, 'TaxAmount')),
            'BT-112': D(_text(monetary, 'TaxInclusiveAmount')), 'BT-115': D(_text(monetary, 'PayableAmount'))}
    return {'breakdowns': breakdowns, 'lines': lines, 'sums': sums}


def cii_model(xml_bytes):
    root = ET.fromstring(xml_bytes)
    settlement = _below(root, 'ApplicableHeaderTradeSettlement')[0]
    breakdowns = [{'category': _text(tax, 'CategoryCode'), 'percent': D(_text(tax, 'RateApplicablePercent')), 'basis': D(_text(tax, 'BasisAmount')),
                   'tax': D(_text(tax, 'CalculatedAmount')), 'reason': _text(tax, 'ExemptionReason'), 'code': _text(tax, 'ExemptionReasonCode')}
                  for tax in settlement if _local(tax) == 'ApplicableTradeTax']
    lines = []
    for line in _below(root, 'IncludedSupplyChainTradeLineItem'):
        trade_tax = _below(line, 'ApplicableTradeTax')[0]
        lines.append({'category': _text(trade_tax, 'CategoryCode'), 'percent': D(_text(trade_tax, 'RateApplicablePercent')),
                      'net': D(_text(_below(line, 'SpecifiedTradeSettlementLineMonetarySummation')[0], 'LineTotalAmount'))})
    summation = _below(settlement, 'SpecifiedTradeSettlementHeaderMonetarySummation')[0]
    sums = {'BT-106': D(_text(summation, 'LineTotalAmount')), 'BT-109': D(_text(summation, 'TaxBasisTotalAmount')), 'BT-110': D(_text(summation, 'TaxTotalAmount')),
            'BT-112': D(_text(summation, 'GrandTotalAmount')), 'BT-115': D(_text(summation, 'DuePayableAmount'))}
    return {'breakdowns': breakdowns, 'lines': lines, 'sums': sums}


def norm_problems(model):
    """Was an einem Modell (``ubl_model``/``cii_model``) den Regeln der Norm widerspricht – leer, wenn alles stimmt: Steuer = Basis × Satz auf zwei Stellen, EINMAL gerundet (BR-CO-17); Basis =
    Summe der Zeilen derselben Kategorie und desselben Satzes; BT-106 = Σ Zeilen, BT-109 = BT-106, BT-110 = Σ Steuer, BT-112 = BT-109 + BT-110, BT-115 = BT-112; Kategorie E: Satz 0, Steuer 0,
    Grund (BT-120), kein Code (BT-121); jede Zeile hat ihre Aufschlüsselung."""
    problems, sums, lines, breakdowns = [], model['sums'], model['lines'], model['breakdowns']
    for b in breakdowns:
        one_step = (b['basis'] * b['percent'] / 100).quantize(D('0.01'), rounding=ROUND_HALF_UP)
        if b['tax'] != one_step:
            problems.append(f'Steuer {b["tax"]} statt {one_step} ({b["basis"]} × {b["percent"]} %)')
        line_sum = sum((line['net'] for line in lines if (line['category'], line['percent']) == (b['category'], b['percent'])), D(0))
        if b['basis'] != line_sum:
            problems.append(f'Basis {b["basis"]} statt Σ Zeilen {line_sum}')
        if b['category'] == 'E' and (b['percent'] != 0 or b['tax'] != 0 or not (b['reason'] or '').strip() or b['code']):
            problems.append(f'Kategorie E unvollständig: {b}')
        if b['category'] == 'S' and b['percent'] <= 0:
            problems.append('Kategorie S mit Satz 0')
    known = {(b['category'], b['percent']) for b in breakdowns}
    problems += [f'Zeile ohne Aufschlüsselung: {line}' for line in lines if (line['category'], line['percent']) not in known]
    problems += [f'Zeile der Kategorie E mit Satz {line["percent"]}' for line in lines if line['category'] == 'E' and line['percent'] != 0]
    total_tax = sum((b['tax'] for b in breakdowns), D(0))
    for label, ok in (('BT-106', sums['BT-106'] == sum((line['net'] for line in lines), D(0))), ('BT-109', sums['BT-109'] == sums['BT-106']),
                      ('BT-110', sums['BT-110'] == total_tax), ('BT-112', sums['BT-112'] == sums['BT-109'] + sums['BT-110']), ('BT-115', sums['BT-115'] == sums['BT-112'])):
        if not ok:
            problems.append(f'{label} stimmt nicht: {sums}')
    return problems


def test_the_check_of_the_norm_finds_what_it_is_for():
    """Die Probe schlägt an – an einem Beleg mit zweistufig gerundeter Steuer (0,09 € zu 5,5 %: 0,01 statt 0,00), einer Kategorie E ohne Grund und einer Summe, die nicht aufgeht."""
    line = {'category': 'S', 'percent': D('5.5'), 'net': D('0.09')}
    good = {'breakdowns': [{'category': 'S', 'percent': D('5.5'), 'basis': D('0.09'), 'tax': D('0.00'), 'reason': None, 'code': None}], 'lines': [line],
            'sums': {'BT-106': D('0.09'), 'BT-109': D('0.09'), 'BT-110': D('0.00'), 'BT-112': D('0.09'), 'BT-115': D('0.09')}}
    assert norm_problems(good) == []
    two_step = {**good, 'breakdowns': [{**good['breakdowns'][0], 'tax': D('0.01')}], 'sums': {**good['sums'], 'BT-110': D('0.01'), 'BT-112': D('0.10'), 'BT-115': D('0.10')}}
    assert norm_problems(two_step) == ['Steuer 0.01 statt 0.00 (0.09 × 5.5 %)']
    free = {'category': 'E', 'percent': D(0), 'net': D('10.00')}
    no_reason = {'breakdowns': [{'category': 'E', 'percent': D(0), 'basis': D('10.00'), 'tax': D('0.00'), 'reason': '  ', 'code': None}], 'lines': [free],
                 'sums': {'BT-106': D('10.00'), 'BT-109': D('10.00'), 'BT-110': D(0), 'BT-112': D('10.00'), 'BT-115': D('10.00')}}
    assert len(norm_problems(no_reason)) == 1 and 'Kategorie E unvollständig' in norm_problems(no_reason)[0]
    assert any('BT-115' in problem for problem in norm_problems({**good, 'sums': {**good['sums'], 'BT-115': D('0.08')}}))


@pytest.fixture(scope='module')
def samples_dir(tmp_path_factory):
    """Das Werkzeug des CI-Jobs ``einvoice`` einmal ganz laufen lassen (Demo-Bestand, alle Beispiele): in einem Ordner unter ``tmp_path_factory`` – sein temporäres Datenverzeichnis auch.
    Was das Werkzeug umstellt (Arbeitsverzeichnis, Datenwurzel, die Variable des Demo-Modus, der angemeldete Benutzer), stellt dieser Block danach zurück."""
    root = tmp_path_factory.mktemp('einvoice_samples')
    (root / 'tmp').mkdir()
    out = root / 'out'
    samples = _samples_module()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(tempfile, 'tempdir', str(root / 'tmp'))
        patch.setenv(demo.ENV_VAR, '1')              # das Werkzeug setzt sie selbst; so wird sie danach entfernt
        patch.chdir(root)
        samples.main(str(out))
    userctx.set_user(None)
    userctx._mode_cache.clear()
    return out


def read(folder, name):
    with open(os.path.join(folder, name), 'rb') as f:
        return f.read()


def test_the_tool_runs_to_the_end_and_writes_the_old_files_and_the_three_new_pairs(samples_dir):
    names = sorted(os.listdir(samples_dir))

    for name in (*NEW_SAMPLES, *OLD_SAMPLES):
        assert f'zugferd-{name}.pdf' in names and f'xrechnung-{name}.xml' in names, name
    assert 'zugferd-zahlungshinweis-raute.pdf' in names and 'xrechnung-zahlungshinweis-raute.xml' not in names     # ein „#“ im Zahlungshinweis: kein UBL (BR-DE-18)


@pytest.mark.parametrize('name', (*NEW_SAMPLES, 'mischsatz', 'normalfall', 'kleinunternehmer'))
def test_the_samples_pass_the_checks_of_the_norm_in_the_ubl_and_in_the_embedded_cii_and_both_say_the_same(samples_dir, name):
    ubl = ubl_model(read(samples_dir, f'xrechnung-{name}.xml'))
    cii = cii_model(embedded_xml(read(samples_dir, f'zugferd-{name}.pdf')))

    assert norm_problems(ubl) == [] and norm_problems(cii) == []

    def facts(model):
        return (sorted((b['category'], b['percent'], b['basis'], b['tax'], b['reason'] or '') for b in model['breakdowns']),
                sorted((line['category'], line['percent'], line['net']) for line in model['lines']), model['sums'])
    assert facts(ubl) == facts(cii)


def test_the_sample_with_5_5_percent_has_the_single_step_tax_of_zero_on_the_9_cent_group(samples_dir):
    """0,09 € × 5,5 % = 0,00495 €: einstufig 0,00 € (BR-CO-17); zweistufig über den Zwischenwert in Minor Units (0,0050 €) wären es 0,01 € – einen Cent zu viel.
    **Diesen Test gibt es, weil Mustang die Regel nicht festhält:** Es nimmt auch die zweistufige Steuer von 0,01 € an (gemessen mit Mustang 2.26.0 an einer Kopie des UBL
    mit stimmenden Summen: 0,01 € und 0,05 € Abweichung gelten als gültig, 5,00 € Abweichung und eine falsche Basis nicht – die Prüfung von BR-CO-17 und BR-S-09 hat eine
    Toleranz)."""
    ubl = ubl_model(read(samples_dir, 'xrechnung-satz-5-5.xml'))

    assert sorted((b['category'], b['percent'], b['basis'], b['tax']) for b in ubl['breakdowns']) == [('S', D('5.5'), D('0.09'), D('0.00')), ('S', D('19'), D('310.00'), D('58.90'))]
    assert (ubl['sums']['BT-106'], ubl['sums']['BT-110'], ubl['sums']['BT-112']) == (D('310.09'), D('58.90'), D('368.99'))


def test_the_free_samples_carry_the_reason_of_the_entry_as_text_and_never_a_code_or_the_paragraph_19_text(samples_dir):
    samples = _samples_module()
    for name, categories in (('steuerfrei-gemischt', ['E', 'S']), ('steuerfrei', ['E'])):
        ubl = ubl_model(read(samples_dir, f'xrechnung-{name}.xml'))
        free = [b for b in ubl['breakdowns'] if b['category'] == 'E']

        assert sorted(b['category'] for b in ubl['breakdowns']) == categories, name
        assert [(b['reason'], b['code'], b['percent'], b['tax']) for b in free] == [(samples.FREE_REASON, None, D(0), D(0))], name
        assert 'Kleinunternehmer' not in free[0]['reason'] and '§ 19' not in free[0]['reason']
    assert ubl_model(read(samples_dir, 'xrechnung-steuerfrei.xml'))['sums']['BT-110'] == D(0)           # ganz steuerfrei: Steuer 0


def test_the_pdfs_of_the_new_samples_carry_the_line_of_the_free_share_the_reason_and_the_5_5_percent_group(samples_dir):
    def page_text(name):
        with pdfplumber.open(os.path.join(samples_dir, name)) as pdf:
            return '\n'.join(page.extract_text() or '' for page in pdf.pages)

    mixed = page_text('zugferd-steuerfrei-gemischt.pdf')
    assert 'steuerfrei auf 80,00 EUR:' in mixed and 'Steuerfreie Beispielleistung (erfundener Grund)' in mixed
    assert 'zzgl. 5,5 % MwSt. auf 0,09 EUR:' in page_text('zugferd-satz-5-5.pdf')
    assert 'Steuerfreie Beispielleistung (erfundener Grund)' in page_text('zugferd-steuerfrei.pdf')


# ═════════════════════════════════════════════════════════════════════════════
# DATEV: der Hinweis auf Sätze ohne BU-Schlüssel
# ═════════════════════════════════════════════════════════════════════════════

FIRST, LAST = '2025-01-01', '2025-12-31'
PLURAL = '{n} Buchungssätze tragen einen Steuersatz ohne BU-Schlüssel – der Satz steht nur in der Spalte ‚EU-Steuersatz‘ bzw. gar nicht im Stapel; bitte mit dem Steuerbüro klären.'
SINGULAR = '1 Buchungssatz trägt einen Steuersatz ohne BU-Schlüssel – der Satz steht nur in der Spalte ‚EU-Steuersatz‘ bzw. gar nicht im Stapel; bitte mit dem Steuerbüro klären.'


@pytest.fixture
def db(tmp_db):
    return with_banks(tmp_db)


def expense(db, day, gross, account_number, rate, document, key=None):
    """Eine Ausgabe über ``gross`` € auf das Aufwandskonto ``account_number``: die Bankbewegung (``Probe-Giro``) mit EINEM Satz darunter (Zweckkonto vorn, Gegenkonto Bank, Betrag negativ,
    Steuersatz als Bruch und die enthaltene Steuer – ohne Satz bei ``rate`` 0). ``key``: ein gespeicherter BU-Schlüssel, so, wie ein Import ihn mitbringt (``update_booking_fields``)."""
    bank = db.insert_booking(day, -D(gross), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Lieferant (erfunden)', text='Ausgabe',
                             document_number=document)
    tax = (D(gross) - D(gross) / (1 + D(str(rate)))).quantize(D('0.01')) if rate else None
    entry_id = db.insert_booking(day, -D(gross), coa_id=db.get_coa_id_by_account_number(account_number), counter_coa_id=db.get_coa_id_by_account_number(1800),
                                 tax_rate=rate or None, tax_amount=-tax if tax else None, text='Ausgabe', document_number=document, booking_type='entry',
                                 parent_booking_id=bank)
    if key:
        db.update_booking_fields(entry_id, datev_key=key)
    return entry_id


def exported(db, **more):
    """Der Export für 2025, wie ihn der Handler liefert: ``(Stapel, Dateiname)`` – nie eine Weiterleitung (der Hinweis fragt nicht zurück)."""
    result = handlers.handle_datev_export(db, datev_post(FIRST, LAST, **more))
    assert not is_confirm_redirect(result) and isinstance(result[0], bytes), result
    return result


def test_the_hint_counts_the_rows_with_a_rate_without_a_key_in_a_world_with_a_5_5_percent_expense_and_a_5_5_percent_invoice(db):
    """H (19 % und 5,5 %, in zwei Raten bezahlt, ohne Forderung: je Rate eine Zeile „Erlöskonto an Bank“ zu 5,5 %) und eine Ausgabe zu 5,5 % (Vorsteuer): drei Zeilen. Die Zeilen zu
    19 % tragen den Schlüssel 101, die Rechnungskette (F, G) nie einen und die steuerfreien keinen Satz – sie zählen nicht."""
    entry_world(db)
    expense(db, '2025-06-10', '10.55', 5170, 0.055, 'A-55')

    stack, name = exported(db)

    assert stack.hint == PLURAL.format(n=3) and name.endswith('.csv')
    assert 'Lieferant' not in stack.hint and 'Käufer' not in stack.hint and 'A-55' not in stack.hint        # nur die Zahl – nie Text oder Empfänger


def test_the_hint_is_singular_for_one_row_and_empty_for_none(db):
    expense(db, '2025-06-10', '10.55', 5170, 0.055, 'A-55')

    assert exported(db)[0].hint == SINGULAR
    assert [handlers._datev_unkeyed_hint(count) for count in (0, 1, 2)] == ['', SINGULAR, PLURAL.format(n=2)]


def test_no_hint_in_a_world_with_only_the_rates_19_7_16_5_and_0_percent(db):
    """Die vier Sätze mit Schlüssel (hier 19 und 7 % der Rechnung und der Ausgaben, 16 und 5 % als Ausgabe) und Zeilen ohne Satz (0 %): kein Hinweis."""
    mixed_world(db)
    for number, (gross, account_number, rate) in enumerate([('119.00', 6815, 0.19), ('107.00', 6850, 0.07), ('116.00', 6300, 0.16), ('105.00', 6845, 0.05), ('50.00', 6815, 0)], 1):
        expense(db, '2025-06-10', gross, account_number, rate, f'A-{number}')

    stack, _name = exported(db)

    assert stack.hint == ''


def test_a_row_with_a_stored_key_does_not_count_even_with_a_rate_outside_the_four(db):
    """Der Import bringt den Schlüssel mit (``Bookings.DatevKey``): Er steht im Stapel, wie er ist – der Export leitet nichts ab, es gibt nichts zu klären."""
    expense(db, '2025-06-10', '10.55', 5170, 0.055, 'A-KEY', key=402)

    assert exported(db)[0].hint == ''

    expense(db, '2025-06-11', '21.10', 5170, 0.055, 'A-NOKEY')

    assert exported(db)[0].hint == SINGULAR


def test_the_hint_never_asks_back_and_the_result_is_the_pair_it_was(db):
    """Mit dem Hinweis liefert der Handler die Antwort wie sonst – die Datei und ihren Namen: keine Weiterleitung, keine Bestätigung (``confirm_export``)."""
    entry_world(db)

    result = handlers.handle_datev_export(db, datev_post(FIRST, LAST))

    assert len(result) == 2 and isinstance(result[0], bytes) and result[1] == 'DATEV_Buchungsstapel_2025_20250101-20251231.csv'
    assert result[0].hint == PLURAL.format(n=2)


def test_the_stack_is_byte_for_byte_the_same_with_the_hint_as_the_export_function_writes_it_alone(db):
    """Der Hinweis ändert den Stapel nicht: dieselben Zeilen wie ``export_to_datev`` ohne die Liste für die Zählung (nur der Zeitstempel im Vorlauf ist von der Sekunde)."""
    entry_world(db)
    expense(db, '2025-06-10', '10.55', 5170, 0.055, 'A-55')
    omitted = db.duplicate_transfer_rows()
    rows = [row for row in db.fetch_bookings_range(FIRST, LAST) if row[0] not in omitted]
    consultant_no, client_no = db.get_datev_numbers()
    kwargs = dict(liquid_coa_ids=db.get_payment_coa_ids(), waiting_numbers=db.get_waiting_account_numbers(), invoices=db.get_datev_invoice_data(),
                  consultant_no=consultant_no, client_no=client_no)
    alone, alone_ids = datev.export_to_datev(rows, db.get_coa_id_to_number_map(), FIRST, LAST, **kwargs)
    counted = []
    with_list, with_ids = datev.export_to_datev(rows, db.get_coa_id_to_number_map(), FIRST, LAST, rates_without_key=counted, **kwargs)

    stack, _name = exported(db)

    def without_stamp(data):
        lines = data.split(b'\r\n')
        head = lines[0].split(b';')
        head[5] = b''                                         # der Zeitstempel des Vorlaufs
        return [b';'.join(head), *lines[1:]]
    assert without_stamp(with_list) == without_stamp(alone) == without_stamp(bytes(stack)) and with_ids == alone_ids and len(counted) == 3
    assert type(stack) is handlers.DatevStack and bytes(stack) == stack


# ── die Zählung in der Funktion, die den Stapel schreibt (``export_to_datev(rates_without_key=…)``) ────────────────────────────────

def booking_row(booking_id, coa, counter, amount, rate, form, key=None):
    """Eine Buchungszeile, wie ``export_to_datev`` sie liest (``SELECT * FROM Bookings``): 0 ID, 1 Datum, 8 Konto, 9 Gegenkonto, 11 Betrag, 12 Währung, 13 Satz, 14 Steuer, 15 Text,
    16 Beleg-Nr., 22 Form, 23 Schlüssel."""
    row = [None] * 24
    row[0], row[1], row[8], row[9], row[11], row[12], row[13], row[14], row[15], row[16], row[22], row[23] = (
        booking_id, '2025-06-01', coa, counter, amount, 'EUR', rate, None if rate is None else amount * rate, 'Text', 'B-1', form, key)
    return tuple(row)


NUMBERS = {1: 4345, 2: 4300, 3: 5170, 4: 1800, 5: 4180}              # COA-ID → Kontonummer: Wartekonto, Erlöskonto 7 %, Aufwand 5,5 %, Bank, Erlöskonto 5,5 %


def counted_of(rows):
    counted = []
    datev.export_to_datev(rows, NUMBERS, FIRST, LAST, liquid_coa_ids={4}, waiting_numbers=(4345,), rates_without_key=counted)
    return counted


def test_the_count_takes_the_kind_vat_and_input_with_a_rate_outside_the_four_and_no_stored_key():
    expense_row = booking_row(1, 3, 4, -100.0, 0.055, 'purpose_first')                     # Vorsteuer, 5,5 %
    revenue_row = booking_row(2, 5, 4, 100.0, 0.055, 'purpose_first')                      # Umsatzsteuer (Erlöskonto), 5,5 %: Spalte 40 trägt den Satz, der Schlüssel fehlt
    keyed = booking_row(3, 3, 4, -100.0, 0.055, 'purpose_first', key=402)                  # gespeicherter Schlüssel: zählt nicht
    no_rate = booking_row(4, 3, 4, -100.0, None, 'purpose_first')                          # ohne Satz
    four = [booking_row(10 + number, 3, 4, -100.0, rate, 'purpose_first') for number, rate in enumerate((0.19, 0.07, 0.16, 0.05))]
    chain = booking_row(5, 1, 2, 100.0, 0.074, 'transfer')                                 # Rechnungskette (Wartekonto 4345 an Erlöskonto): nie ein Schlüssel – auch nicht bei 7,4 %

    assert counted_of([expense_row, revenue_row, keyed, no_rate, *four, chain]) == [1, 2]
    assert counted_of([booking_row(6, 2, 4, 107.0, 0.07, 'purpose_first')]) == []         # 7 %: Schlüssel 102
    assert counted_of([booking_row(7, 2, 4, 107.0, 0.074, 'purpose_first')]) == [7]        # 7,4 %: keiner


def test_a_stored_key_that_is_not_a_key_counts_as_none_like_the_export_reads_it():
    """Der Export liest nur 1 bis 9999 als Schlüssel (``parse_key``); ``0`` und Text gelten als keiner – die Zeile wird abgeleitet und zählt."""
    rows = [booking_row(1, 3, 4, -100.0, 0.055, 'purpose_first', key=0), booking_row(2, 3, 4, -100.0, 0.055, 'purpose_first', key='kein Schlüssel'),
            booking_row(3, 3, 4, -100.0, 0.055, 'purpose_first', key=401)]

    assert counted_of(rows) == [1, 2]


def test_the_longest_hint_fits_a_message_of_the_page():
    assert len(handlers.DATEV_UNKEYED_RATES_HINT.format(n=10 ** 12)) < MSG_MAX_LENGTH


# ── Der feste Hinweis unter dem DATEV-Formular ───────────────────────────────

def test_the_page_names_a_rate_without_a_key_as_soon_as_the_master_data_carry_one(tmp_db):
    """Der Export ist ein Download – nach ihm zeigt die Seite nichts. Der Hinweis steht deshalb fest unter dem Formular, sobald die Stammdaten einen Satz über 0
    außerhalb der Sätze mit Schlüssel führen (19, 16, 7, 5 %); ohne einen solchen Eintrag steht er nicht da, auch nicht für einen steuerfreien Eintrag."""
    from html import escape
    from server.pages_miscellaneous import DATEV_RATE_NOTE, PageMiscellaneous
    note = escape(DATEV_RATE_NOTE, quote=True)
    revenue = tmp_db.get_revenue_coas()[0][0]

    assert note not in PageMiscellaneous(tmp_db)                                    # nur die Vorbelegung 19 % und 7 %
    tmp_db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind='exempt', reason='Erfundener Grund', revenue_coa_id=revenue)
    assert note not in PageMiscellaneous(tmp_db)                                    # steuerfrei trägt keinen Satz
    entry = tmp_db.insert_tax_rate(label='Fünfeinhalb (erfunden)', bp=550, kind='standard', revenue_coa_id=revenue)
    assert PageMiscellaneous(tmp_db).count(note) == 1
    tmp_db.delete_tax_rate(entry)
    assert note not in PageMiscellaneous(tmp_db)
