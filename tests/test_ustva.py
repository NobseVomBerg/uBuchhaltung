# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Umsatzsteuer-Voranmeldung (2.10.5, Stufe 1): die Kennzahlen 81, 86, 66 und 83 aus den Buchungen – Seite ``/ustva`` (das PDF: ``test_ustva_pdf.py``).

Sechs Teile.

* Die reinen Regeln (``db/ustva.py``): ``assign`` mit je einem Fall für jede Zeile der Zuordnungstabelle (Vorprüfung, T1–T11, Z1–Z15, R1–R7, Herkunft
  und Datum, dazu die Grenze von zwei Cent zwischen Steuer und Netto × Satz), die Rundung der Bemessungsgrundlage an EINER Stelle, Zeitraum, Bezeichnung und
  Dateiname, ``build_ustva`` mit von Hand gebauten Sätzen.
* **Die Invarianten** (Pflicht, **nicht lockern**): In der SuSa-Welt, in zufälligen Beständen (Seeds 1–5) und im Demobestand steht jeder gelesene Satz in
  genau einer Kennzahl, einem Grund unter „Nicht zugeordnet“ oder bei „neutral“; Betrag und Steuer gehen dabei weder verloren noch doppelt ein, und keine
  Steuer steht bei „neutral“. Die Zahl der Sätze wird mit eigener SQL gegengezählt.
* Feste Werte der SuSa-Welt (von Hand gerechnet), Wartekonten, Skonto, das Datum der Bankbewegung, jeder Grund unter „Nicht zugeordnet“; dazu Sätze auf Steuerkonten,
  Vorsteuer zu anderen Sätzen, die Höhe der Steuer (Wege der App), die Forderung als einziger ausgeblendeter eigenständiger Satz, Bankbewegungen, deren Sätze nicht ihren
  Betrag ergeben, und die Abstimmung, wenn die EÜR keine ganze Zahl liefert.
* ``get_euer_data`` ist durch das Herausziehen von ``_euer_entries``/``_euer_totals`` unverändert (SHA-256 am Stand VOR dem Umbau – ändert sich ein
  Wert, ist der Code zu ändern, nie der Schnappschuss).
* Die Seite: Ist-Versteuerung, Soll und „nicht festgelegt“, Aufbau, Maskierung, Skript.
* Adresse, Route, Diagnose-MCP: streng gelesen, kein Cookie beim PDF, nichts geschrieben, in keiner Aktionsmenge.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da (die Seite bekommt ein festes ``today``).
"""
import calendar
import dataclasses
import datetime
import hashlib
import html as html_lib
import importlib.util
import json
import re
import sqlite3
import sys
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

import auth
import demo_seed
import userctx
from db import Database, ustva
from db.ustva import (KZ_66, KZ_81, KZ_86, NEUTRAL, RECON_UNKNOWN, TAX_ACCOUNT_NUMBERS, TAX_TOLERANCE_MINOR, UNASSIGNED_REASONS, UNBALANCED_LABEL, UNREADABLE,
                      USTVA_DATE_TEXT, USTVA_HINT_TEXT, USTVA_NOT_SHOWN_TEXT, USTVA_OPEN_PERIOD_TEXT, USTVA_PDF_NOT_IST_TEXT, USTVA_RECON_TEXT,
                      USTVA_ROUNDING_TEXT, USTVA_SIGN_TEXT, USTVA_SMALL_BUSINESS_TEXT, USTVA_SOLL_TEXT, USTVA_UNASSIGNED_TEXT, USTVA_UNSET_TEXT, Assignment,
                      UstvaEntry, assign, build_ustva, format_recon, format_whole, is_calendar_quarter, period_label, quarter_end_month, ustva_filename,
                      ustva_period, whole_euros)
from money import from_minor, to_minor
from server.pages import Header1
from server.pages_bwa import REPORT_PAGES, reports_submenu
from server.pages_ustva import INVALID_PERIOD_ERROR, PageUstva, UstvaQuery, read_kind, read_ustva_pdf_query
from server.period import MONTHS_ALWAYS, period_filter_widget
from tests import susa_helpers as helpers
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump, tree
from tests.liquidity_helpers import random_books, two_banks
from tests.susa_helpers import account_id, coa, mask, minor, paid_invoice, raw, raw_bank, raw_entry
from tests.test_period_menu_row import header3
from tests.test_skonto_mixed_booking import mixed_invoice
from tests.test_skonto_reports import invoice_of, pay as pay_invoice

FIXED = datetime.date(2026, 11, 3)                # ein fester Tag, nie die Uhr (tests/test_clock_at_import.py)
PERIOD = ('2026-03-01', '2026-03-31')
BRANDS = ('DATEV', 'WISO', 'ELSTER')
HOSTILE = '<script>alert(1)</script>"\'&x'
TOOL_PATH = Path(__file__).resolve().parent.parent / 'tools' / 'mcp_ubuchhaltung.py'
WAITING = (4340, 4345, 4405)


# ═══ Hilfen ═══════════════════════════════════════════════════════════════════
def entry(form, coa_number, counter_number, amount, tax='0', rate=None, **fields):
    """Ein Satz für ``assign``: Beträge in Euro als Text (``None`` = nicht lesbar), Quelle ``euer``, Datum im März 2026."""
    values = {'booking_id': 1, 'source': 'euer', 'entry_date': '2026-03-10', 'parent_date': None, 'asset_transfer': False}
    values.update(fields)
    return UstvaEntry(form=form, coa_number=coa_number, counter_number=counter_number, rate=rate, **values,
                      amount=None if amount is None else minor(amount), tax=None if tax is None else minor(tax))


def expect(target, account, base, tax):
    """Die erwartete Zuordnung, Beträge in Euro als Text."""
    return Assignment(target, account, minor(base), minor(tax))


def numbers_of(value):
    """Alle Zahlen eines Berichts (verschachtelte Datenklassen, Tupel), ohne Texte und Wahrheitswerte."""
    found = []

    def walk(item):
        if dataclasses.is_dataclass(item):
            for field in dataclasses.fields(item):
                walk(getattr(item, field.name))
        elif isinstance(item, (tuple, list)):
            for part in item:
                walk(part)
        elif not isinstance(item, (str, bool)) and item is not None:
            found.append(item)
    walk(value)
    return found


def check_invariants(report, where=''):
    """Die Invarianten – Pflicht, nicht lockern: Jeder gelesene Satz steht in genau einer Kennzahl, einem Grund (auch ``unreadable``) oder bei ``neutral``;
    Betrag und Steuer der lesbaren Sätze gehen ohne Rest und nicht doppelt ein (die Basis von Kz 66 steht als −Netto: eine Ausgabe ist dort positiv);
    bei ``neutral`` steht nie eine Steuer; und: ``unreadable_count`` ist die Zahl des Grundes ``unreadable`` (er steht in ``unassigned``, damit die Seite
    ihn nennt – eine zweite Zählung daneben gäbe es doppelt)."""
    def count(figure):
        return sum(line.count for line in figure.accounts)
    figure_81, figure_86 = report.figures
    placed = count(figure_81) + count(figure_86) + count(report.input_tax) + sum(group.count for group in report.unassigned) + report.neutral_count
    assert placed == report.read_count, (where, 'Zahl', placed, report.read_count)
    net = figure_81.base + figure_86.base - report.input_tax.base + report.unassigned_net + report.neutral_net
    assert net == report.read_net, (where, 'Netto', net, report.read_net)
    tax = figure_81.tax + figure_86.tax - report.input_tax.tax + report.unassigned_tax + report.neutral_tax
    assert tax == report.read_tax, (where, 'Steuer', tax, report.read_tax)
    assert report.neutral_tax == 0, (where, 'neutral trägt Steuer')
    unreadable = [group.count for group in report.unassigned if group.key == UNREADABLE]
    assert report.unreadable_count == (unreadable[0] if unreadable else 0), where
    # Gruppen und Konten: die Anzahl, das Netto und die Steuer eines Grundes sind die Summen seiner Konten
    for group in report.unassigned:
        assert group.count == sum(line.count for line in group.accounts) and group.net == sum(line.base for line in group.accounts), (where, group.key)
        assert group.tax == sum(line.tax for line in group.accounts), (where, group.key)
    for figure in (figure_81, figure_86, report.input_tax):
        assert figure.base == sum(line.base for line in figure.accounts) and figure.tax == sum(line.tax for line in figure.accounts), (where, figure.key)
    assert report.vat_total == figure_81.tax + figure_86.tax and report.prepayment == report.vat_total - report.input_tax.tax
    assert report.recon_page == report.vat_total - report.input_tax.tax
    assert (report.recon_euer is None) == (report.recon_difference is None), where                # die Abstimmung steht ganz da oder gar nicht
    assert report.recon_euer is None or report.recon_difference == report.recon_page - report.recon_euer, where
    assert all(isinstance(number, int) and not isinstance(number, bool) for number in numbers_of(report)), (where, 'nicht nur ganze Zahlen')


def every_period(year):
    """Jeden Monat und jedes Quartal eines Jahres als ``(Jahr, Monat, Art)``."""
    return [(year, month, 'monat') for month in range(1, 13)] + [(year, month, 'quartal') for month in (3, 6, 9, 12)]


def in_places(report):
    """Der Bericht als einfache Werte (Euro als Text): ``{Kz: (Basis, Steuer)}``, die Gründe ``[(Grund, Anzahl, Netto, Steuer)]``."""
    def euro(value):
        return str(from_minor(value))
    result = {figure.kz: (euro(figure.base), euro(figure.tax)) for figure in (*report.figures, report.input_tax)}
    result['groups'] = [(group.key, group.count, euro(group.net), euro(group.tax)) for group in report.unassigned]
    return result


def bank_with_child(db, bank_date, bank_amount, child_date, child_amount, number, counter=1800, form='purpose_first', account='Probe-Giro', **extra):
    """Eine Bankbewegung auf ``account`` mit EINEM Satz darunter (Kontonummern; Beträge in Euro als Text); ``(Bankbewegung, Satz)`` als IDs."""
    bank = db.insert_booking(bank_date, Decimal(bank_amount), account_id=account_id(db, account), booking_type='bank')
    child = db.insert_booking(child_date, Decimal(child_amount), coa_id=coa(db, number), counter_coa_id=coa(db, counter), booking_type='entry',
                              parent_booking_id=bank, entry_form=form, **extra)
    return bank, child


def own_company(db, vat_method='ist', name='Eigene Firma GmbH'):
    """Die eigene Firma mit ihrer Besteuerungsart."""
    return db.insert_contact(contact_type='own', entity_type='company', display_name=name, company_name=name, vat_method=vat_method)


@pytest.fixture
def db(tmp_db):
    """Eine leere Datenbank mit den zwei Bankkonten „Probe-Giro“ (1800) und „Probe-Zweit“ (1810) neben der Kasse (1600)."""
    two_banks(tmp_db)
    return tmp_db


@pytest.fixture(scope='module')
def worlds(tmp_path_factory):
    """Die SuSa-Welt, die zufälligen Bestände (Seeds 1–5) und der Demobestand, einmal gebaut; die Datenwurzel liegt unter ``tmp_path_factory`` – nichts
    landet im echten data/ (``pytest.MonkeyPatch.context``, weil eine modulweite Fixture den ``monkeypatch`` je Test nicht nutzen darf). In der SuSa-Welt
    ist die Besteuerungsart der eigenen Firma Ist-Versteuerung (für die Seite)."""
    root = tmp_path_factory.mktemp('ustva_worlds')
    built = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        built['susa'] = helpers.build_susa_world(Database(str(root / 'susa.db'))).db
        own_company(built['susa'])
        for seed in (1, 2, 3, 4, 5):
            built[f'random{seed}'] = Database(str(root / f'random{seed}.db'))
            random_books(built[f'random{seed}'], seed)
        demo = Database(str(root / 'demo.db'))
        demo.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(demo, today=FIXED, profile='test')
        built['demo'] = demo
    userctx._mode_cache.clear()
    return built


WORLD_NAMES = ['susa', 'random1', 'random2', 'random3', 'random4', 'random5', 'demo']


# ═══ Die Zuordnung eines Satzes ═══════════════════════════════════════════════
# (Kennung, Satz, erwartete Zuordnung): Beträge in Euro, Netto = Betrag − Steuer. Jede Zeile der Tabellen in ``docs/regeln/auswertungen.md`` hat mindestens einen Fall.
ASSIGNMENTS = [
    # Vorprüfung
    ('V-unreadable-amount', entry('purpose_first', 4400, 1800, None, '19.00', 0.19), Assignment(UNREADABLE, None, 0, 0)),
    ('V-unreadable-tax', entry('purpose_first', 4400, 1800, '119.00', None, 0.19), Assignment(UNREADABLE, None, 0, 0)),
    ('V-zero-and-zero', entry('purpose_first', 4400, 1800, '0', '0', 0.19), expect(NEUTRAL, None, '0', '0')),
    ('V-tax-without-amount', entry('purpose_first', 4400, 1800, '0', '19.00', 0.19), expect('rate_tax_mismatch', 4400, '-19.00', '19.00')),
    # Spiegel und Umbuchungen
    ('T1-mirror-coa', entry('mirror_coa', 1800, 1810, '-500.00'), expect(NEUTRAL, None, '-500.00', '0')),
    ('T1-mirror-counter', entry('mirror_counter', 1800, 1810, '500.00'), expect(NEUTRAL, None, '500.00', '0')),
    ('T1-mirror-legacy', entry('mirror_legacy', 1810, 1800, '12.00'), expect(NEUTRAL, None, '12.00', '0')),
    ('T2-mirror-with-tax', entry('mirror_counter', 1800, 1810, '-500.00', '-95.00', 0.19), expect('tax_other', 1810, '-405.00', '-95.00')),
    ('T3-waiting-4405-to-4400', entry('transfer', 4405, 4400, '119.00', '19.00', 0.19), expect(KZ_81, 4400, '100.00', '19.00')),
    ('T3-waiting-4345-to-4300', entry('transfer', 4345, 4300, '107.00', '7.00', 0.07), expect(KZ_86, 4300, '100.00', '7.00')),
    ('T3-debtor-side', entry('transfer', 10000, 4400, '119.00', '19.00', 0.19), expect(KZ_81, 4400, '100.00', '19.00')),
    ('T3-4340-to-4185', entry('transfer', 4340, 4185, '100.00'), expect('account_4185', 4185, '100.00', '0')),
    ('T3-4340-to-4184', entry('transfer', 4340, 4184, '100.00'), expect('zero_rate', 4184, '100.00', '0')),
    ('T3-other-rate', entry('transfer', 4405, 4400, '116.00', '16.00', 0.16), expect('other_rate', 4400, '100.00', '16.00')),
    ('T4-reverse-direction', entry('transfer', 4400, 4405, '-119.00', '-19.00', 0.19), expect('transfer_other', 4400, '-100.00', '-19.00')),
    ('T5-chain-without-tax', entry('transfer', 10000, 4405, '119.00'), expect(NEUTRAL, None, '119.00', '0')),
    ('T6-chain-with-tax', entry('transfer', 10000, 4405, '119.00', '19.00', 0.19), expect('transfer_other', 4405, '100.00', '19.00')),
    ('T7-asset-transfer', entry('transfer', 6220, 520, '400.00', asset_transfer=True), expect(NEUTRAL, None, '400.00', '0')),
    ('T8-asset-transfer-with-tax', entry('transfer', 6220, 520, '400.00', '64.00', 0.16, asset_transfer=True), expect('tax_other', 520, '336.00', '64.00')),
    ('T9-private-to-loss', entry('transfer', 2100, 4645, '119.00', '19.00', 0.19), expect('private_transfer', 4645, '100.00', '19.00')),
    ('T9-loss-to-private', entry('transfer', 6815, 2100, '-50.00'), expect('private_transfer', 6815, '-50.00', '0')),
    ('T10-tax', entry('transfer', 3150, 3160, '100.00', '19.00', 0.19), expect('transfer_other', 3160, '81.00', '19.00')),
    ('T10-sales-on-the-coa-side', entry('transfer', 4400, 3150, '-100.00'), expect('transfer_other', 3150, '-100.00', '0')),
    ('T10-sales-on-the-counter-side', entry('transfer', 3150, 4300, '-100.00'), expect('transfer_other', 4300, '-100.00', '0')),
    ('T10-no-counter', entry('transfer', 4400, None, '-100.00'), expect('transfer_other', 4400, '-100.00', '0')),
    ('T11-plain-transfer', entry('transfer', 3150, 3160, '100.00'), expect(NEUTRAL, None, '100.00', '0')),
    # Zahlungen
    ('Z1-no-account-first', entry('purpose_first', None, 1800, '50.00'), expect('no_account', None, '50.00', '0')),
    ('Z1-no-account-second', entry('liquid_first', 1800, None, '-50.00', '-8.00'), expect('no_account', None, '-42.00', '-8.00')),
    ('Z2-waiting-4405', entry('purpose_first', 4405, 1800, '119.00', '19.00', 0.19), expect('waiting', 4405, '100.00', '19.00')),
    ('Z2-waiting-4340', entry('liquid_first', 1800, 4340, '100.00'), expect('waiting', 4340, '100.00', '0')),
    ('Z2-waiting-4345', entry('purpose_first', 4345, 1800, '107.00', '7.00', 0.07), expect('waiting', 4345, '100.00', '7.00')),
    ('Z3-debtor', entry('liquid_first', 1800, 10000, '119.00'), expect(NEUTRAL, None, '119.00', '0')),
    ('Z4-debtor-with-tax', entry('purpose_first', 10000, 1800, '119.00', '19.00', 0.19), expect('tax_other', 10000, '100.00', '19.00')),
    ('Z6-private', entry('liquid_first', 1800, 2100, '-80.00'), expect(NEUTRAL, None, '-80.00', '0')),
    ('Z7-private-with-tax', entry('purpose_first', 2100, 1800, '-119.00', '-19.00', 0.19), expect('private_tax', 2100, '-100.00', '-19.00')),
    ('Z8-expense-without-tax', entry('purpose_first', 6815, 1800, '-50.00'), expect(NEUTRAL, None, '-50.00', '0')),
    ('Z8-asset-without-tax', entry('purpose_first', 520, 1800, '-1000.00'), expect(NEUTRAL, None, '-1000.00', '0')),
    ('Z9-tax-without-rate', entry('purpose_first', 6815, 1800, '-58.00', '-8.00'), expect('tax_without_rate', 6815, '-50.00', '-8.00')),
    ('Z10-input-tax-at-5-5-percent', entry('purpose_first', 6815, 1800, '-105.50', '-5.50', 0.055), expect('input_other_rate', 6815, '-100.00', '-5.50')),
    ('Z10-input-tax-at-20-percent', entry('purpose_first', 6815, 1800, '-120.00', '-20.00', 0.2), expect('input_other_rate', 6815, '-100.00', '-20.00')),
    ('Z10-input-tax-at-16-percent', entry('purpose_first', 6815, 1800, '-116.00', '-16.00', 0.16), expect('input_other_rate', 6815, '-100.00', '-16.00')),
    ('Z10-input-tax-at-5-percent-money-first', entry('liquid_first', 1800, 6850, '-105.00', '-5.00', 5), expect('input_other_rate', 6850, '-100.00', '-5.00')),
    ('Z10-asset-purchase-at-16-percent', entry('purpose_first', 520, 1800, '-1160.00', '-160.00', 0.16), expect('input_other_rate', 520, '-1000.00', '-160.00')),
    ('Z10-refund-at-another-rate', entry('purpose_first', 6815, 1800, '23.00', '3.00', 0.15), expect('input_other_rate', 6815, '20.00', '3.00')),
    ('Z11-signs-differ', entry('purpose_first', 6815, 1800, '-119.00', '19.00', 0.19), expect('rate_tax_mismatch', 6815, '-138.00', '19.00')),
    ('Z11-tiny-amount-opposite-signs', entry('purpose_first', 6815, 1800, '-0.04', '0.01', 0.19), expect('rate_tax_mismatch', 6815, '-0.05', '0.01')),
    ('Z11-input-tax-too-low', entry('purpose_first', 6815, 1800, '-119.00', '-7.00', 0.19), expect('rate_tax_mismatch', 6815, '-112.00', '-7.00')),
    ('Z11-input-tax-equals-the-amount', entry('purpose_first', 6815, 1800, '-19.00', '-19.00', 0.19), expect('rate_tax_mismatch', 6815, '0', '-19.00')),
    ('Z11-input-tax-of-7-percent-too-high', entry('purpose_first', 6850, 1800, '-107.00', '-17.00', 0.07), expect('rate_tax_mismatch', 6850, '-90.00', '-17.00')),
    ('Z11-input-tax-3-cents-more', entry('purpose_first', 6815, 1800, '-119.03', '-19.03', 0.19), expect('rate_tax_mismatch', 6815, '-100.00', '-19.03')),
    ('Z11-input-tax-3-cents-less', entry('purpose_first', 6815, 1800, '-118.97', '-18.97', 0.19), expect('rate_tax_mismatch', 6815, '-100.00', '-18.97')),
    ('Z11-input-tax-of-7-percent-3-cents-more', entry('purpose_first', 6850, 1800, '-107.03', '-7.03', 0.07), expect('rate_tax_mismatch', 6850, '-100.00', '-7.03')),
    ('Z11-input-tax-just-over-2-cents', entry('purpose_first', 6815, 1800, '-119.0201', '-19.0201', 0.19), expect('rate_tax_mismatch', 6815, '-100.00', '-19.0201')),
    ('Z12-input-tax', entry('purpose_first', 6815, 1800, '-119.00', '-19.00', 0.19), expect(KZ_66, 6815, '100.00', '19.00')),
    ('Z12-input-tax-money-first', entry('liquid_first', 1800, 6850, '-53.50', '-3.50', 0.07), expect(KZ_66, 6850, '50.00', '3.50')),
    ('Z12-asset-purchase', entry('purpose_first', 520, 1800, '-1190.00', '-190.00', 19), expect(KZ_66, 520, '1000.00', '190.00')),
    ('Z12-refund-reduces', entry('purpose_first', 6815, 1800, '23.80', '3.80', 0.19), expect(KZ_66, 6815, '-20.00', '-3.80')),
    ('Z12-input-tax-2-cents-more', entry('purpose_first', 6815, 1800, '-119.02', '-19.02', 0.19), expect(KZ_66, 6815, '100.00', '19.02')),
    ('Z12-input-tax-2-cents-less', entry('purpose_first', 6815, 1800, '-118.98', '-18.98', 0.19), expect(KZ_66, 6815, '100.00', '18.98')),
    ('Z12-input-tax-of-7-percent-2-cents-more', entry('purpose_first', 6850, 1800, '-107.02', '-7.02', 0.07), expect(KZ_66, 6850, '100.00', '7.02')),
    ('Z12-input-tax-just-under-2-cents', entry('purpose_first', 6815, 1800, '-119.0199', '-19.0199', 0.19), expect(KZ_66, 6815, '100.00', '19.0199')),
    ('Z12-refund-2-cents-more', entry('purpose_first', 6815, 1800, '119.02', '19.02', 0.19), expect(KZ_66, 6815, '-100.00', '-19.02')),
    ('Z13-tax-account-1406', entry('purpose_first', 1406, 1800, '-19.00'), expect('tax_account', 1406, '-19.00', '0')),
    ('Z13-tax-account-1401-money-first', entry('liquid_first', 1800, 1401, '-7.00'), expect('tax_account', 1401, '-7.00', '0')),
    ('Z13-tax-account-3806', entry('purpose_first', 3806, 1800, '19.00'), expect('tax_account', 3806, '19.00', '0')),
    ('Z13-tax-account-3801-money-first', entry('liquid_first', 1800, 3801, '7.00'), expect('tax_account', 3801, '7.00', '0')),
    ('Z13-tax-account-with-tax', entry('purpose_first', 3801, 1800, '7.00', '0.49', 0.07), expect('tax_account', 3801, '6.51', '0.49')),
    ('Z14-other-account', entry('purpose_first', 3150, 1800, '5000.00'), expect(NEUTRAL, None, '5000.00', '0')),
    ('Z14-money-account', entry('purpose_first', 1810, 1800, '10.00'), expect(NEUTRAL, None, '10.00', '0')),
    ('Z14-settlement-account-3820', entry('purpose_first', 3820, 1800, '-500.00'), expect(NEUTRAL, None, '-500.00', '0')),          # Zahllast: kein Steuerkonto der Liste
    ('Z14-settlement-account-3830', entry('liquid_first', 1800, 3830, '-500.00'), expect(NEUTRAL, None, '-500.00', '0')),
    ('V-zero-and-zero-on-a-tax-account', entry('purpose_first', 1406, 1800, '0', '0'), expect(NEUTRAL, None, '0', '0')),
    ('Z15-other-account-with-tax', entry('purpose_first', 3150, 1800, '5010.00', '10.00', 0.19), expect('tax_other', 3150, '5000.00', '10.00')),
    ('Z15-account-above-7999', entry('purpose_first', 8000, 1800, '119.00', '19.00', 0.19), expect('tax_other', 8000, '100.00', '19.00')),
    ('Z15-tax-account-with-tax-but-without-an-amount', entry('purpose_first', 1406, 1800, '0', '19.00', 0.19), expect('tax_other', 1406, '-19.00', '19.00')),
    ('Z15-settlement-account-with-tax', entry('purpose_first', 3820, 1800, '-119.00', '-19.00', 0.19), expect('tax_other', 3820, '-100.00', '-19.00')),
    # Erlöse
    ('R1-4185-without-tax', entry('purpose_first', 4185, 1800, '50.00'), expect('account_4185', 4185, '50.00', '0')),
    ('R1-4185-with-19-percent', entry('purpose_first', 4185, 1800, '119.00', '19.00', 0.19), expect('account_4185', 4185, '100.00', '19.00')),
    ('R2-tax-without-rate', entry('purpose_first', 4400, 1800, '105.00', '5.00'), expect('tax_without_rate', 4400, '100.00', '5.00')),
    ('R3-4120', entry('purpose_first', 4120, 1800, '30.00'), expect('zero_rate', 4120, '30.00', '0')),
    ('R3-4184', entry('purpose_first', 4184, 1800, '40.00', rate=-1), expect('zero_rate', 4184, '40.00', '0')),
    ('R3-4730', entry('purpose_first', 4730, 1800, '-2.00'), expect('zero_rate', 4730, '-2.00', '0')),
    ('R3-4100', entry('purpose_first', 4100, 1800, '10.00', rate=0), expect('zero_rate', 4100, '10.00', '0')),
    ('R4-16-percent', entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16), expect('other_rate', 4400, '100.00', '16.00')),
    ('R4-5-percent', entry('purpose_first', 4400, 1800, '105.00', '5.00', 5), expect('other_rate', 4400, '100.00', '5.00')),
    ('R4-5-5-percent', entry('purpose_first', 4400, 1800, '105.50', '5.50', 0.055), expect('other_rate', 4400, '100.00', '5.50')),
    ('R5-19-percent-without-tax', entry('purpose_first', 4400, 1800, '119.00', '0', 0.19), expect('rate_tax_mismatch', 4400, '119.00', '0')),
    ('R5-19-percent-opposite-signs', entry('purpose_first', 4400, 1800, '119.00', '-19.00', 0.19), expect('rate_tax_mismatch', 4400, '138.00', '-19.00')),
    ('R5-7-percent-without-tax', entry('purpose_first', 4300, 1800, '107.00', '0', 0.07), expect('rate_tax_mismatch', 4300, '107.00', '0')),
    ('R5-tiny-amount-without-tax', entry('purpose_first', 4400, 1800, '0.10', '0', 0.19), expect('rate_tax_mismatch', 4400, '0.10', '0')),            # unter der Grenze von zwei Cent
    ('R5-tiny-amount-opposite-signs', entry('purpose_first', 4400, 1800, '0.04', '-0.01', 0.19), expect('rate_tax_mismatch', 4400, '0.05', '-0.01')),
    ('R5-19-percent-tax-too-low', entry('purpose_first', 4400, 1800, '119.00', '7.00', 0.19), expect('rate_tax_mismatch', 4400, '112.00', '7.00')),
    ('R5-19-percent-amount-equals-tax', entry('purpose_first', 4400, 1800, '19.00', '19.00', 0.19), expect('rate_tax_mismatch', 4400, '0', '19.00')),
    ('R5-7-percent-tax-too-high', entry('purpose_first', 4300, 1800, '107.00', '17.00', 0.07), expect('rate_tax_mismatch', 4300, '90.00', '17.00')),
    ('R5-refund-tax-too-low', entry('purpose_first', 4400, 1800, '-119.00', '-7.00', 0.19), expect('rate_tax_mismatch', 4400, '-112.00', '-7.00')),
    ('R5-19-percent-3-cents-more', entry('purpose_first', 4400, 1800, '119.03', '19.03', 0.19), expect('rate_tax_mismatch', 4400, '100.00', '19.03')),
    ('R5-19-percent-3-cents-less', entry('purpose_first', 4400, 1800, '118.97', '18.97', 0.19), expect('rate_tax_mismatch', 4400, '100.00', '18.97')),
    ('R5-7-percent-3-cents-more', entry('purpose_first', 4300, 1800, '107.03', '7.03', 0.07), expect('rate_tax_mismatch', 4300, '100.00', '7.03')),
    ('R5-19-percent-just-over-2-cents', entry('purpose_first', 4400, 1800, '119.0201', '19.0201', 0.19), expect('rate_tax_mismatch', 4400, '100.00', '19.0201')),
    ('R5-chain-tax-does-not-fit', entry('transfer', 4405, 4400, '119.00', '7.00', 0.19), expect('rate_tax_mismatch', 4400, '112.00', '7.00')),
    ('R6-4400', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19), expect(KZ_81, 4400, '100.00', '19.00')),
    ('R6-rate-as-percent', entry('purpose_first', 4400, 1800, '119.00', '19.00', 19), expect(KZ_81, 4400, '100.00', '19.00')),
    ('R6-granted-discount-4736', entry('purpose_first', 4736, 1800, '-2.38', '-0.38', 0.19), expect(KZ_81, 4736, '-2.00', '-0.38')),
    ('R6-refund', entry('purpose_first', 4400, 1800, '-119.00', '-19.00', 0.19), expect(KZ_81, 4400, '-100.00', '-19.00')),
    ('R6-19-percent-2-cents-more', entry('purpose_first', 4400, 1800, '119.02', '19.02', 0.19), expect(KZ_81, 4400, '100.00', '19.02')),
    ('R6-19-percent-2-cents-less', entry('purpose_first', 4400, 1800, '118.98', '18.98', 0.19), expect(KZ_81, 4400, '100.00', '18.98')),
    ('R6-19-percent-just-under-2-cents', entry('purpose_first', 4400, 1800, '119.0199', '19.0199', 0.19), expect(KZ_81, 4400, '100.00', '19.0199')),
    ('R6-refund-2-cents-more', entry('purpose_first', 4400, 1800, '-119.02', '-19.02', 0.19), expect(KZ_81, 4400, '-100.00', '-19.02')),
    ('R6-chain-with-rounded-tax', entry('transfer', 4405, 4400, '119.02', '19.02', 0.19), expect(KZ_81, 4400, '100.00', '19.02')),
    ('R6-4640', entry('purpose_first', 4640, 1800, '119.00', '19.00', 0.19), expect(KZ_81, 4640, '100.00', '19.00')),
    ('R6-4845', entry('purpose_first', 4845, 1800, '119.00', '19.00', 0.19), expect(KZ_81, 4845, '100.00', '19.00')),
    ('R6-6885-sale-of-an-asset', entry('purpose_first', 6885, 1800, '119.00', '19.00', 0.19), expect(KZ_81, 6885, '100.00', '19.00')),
    ('R7-4300', entry('purpose_first', 4300, 1800, '107.00', '7.00', 0.07), expect(KZ_86, 4300, '100.00', '7.00')),
    ('R7-7-percent-2-cents-more', entry('purpose_first', 4300, 1800, '107.02', '7.02', 0.07), expect(KZ_86, 4300, '100.00', '7.02')),
    ('R7-7-percent-2-cents-less', entry('purpose_first', 4300, 1800, '106.98', '6.98', 0.07), expect(KZ_86, 4300, '100.00', '6.98')),
    ('R7-4731', entry('liquid_first', 1800, 4731, '-1.07', '-0.07', 0.07), expect(KZ_86, 4731, '-1.00', '-0.07')),
    # Herkunft und Datum
    ('S-standalone-revenue', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, source='standalone'), expect('no_bank', 4400, '100.00', '19.00')),
    ('S-standalone-input-tax-is-not-negated', entry('purpose_first', 6815, 1800, '-119.00', '-19.00', 0.19, source='standalone'),
     expect('no_bank', 6815, '-100.00', '-19.00')),
    ('S-standalone-private-transfer', entry('transfer', 2100, 4645, '119.00', '19.00', 0.19, source='standalone'), expect('private_transfer', 4645, '100.00', '19.00')),
    ('S-standalone-private-transfer-wins-over-the-chain', entry('transfer', 2100, 4405, '119.00', '19.00', 0.19, source='standalone'),
     expect('private_transfer', 4405, '100.00', '19.00')),
    ('S-standalone-chain-with-revenue', entry('transfer', 4405, 4400, '119.00', '19.00', 0.19, source='standalone'), expect('no_bank', 4400, '100.00', '19.00')),
    ('S-standalone-neutral-stays', entry('purpose_first', 6815, 1800, '-50.00', source='standalone'), expect(NEUTRAL, None, '-50.00', '0')),
    ('S-standalone-tax-account', entry('purpose_first', 1406, 1800, '-19.00', source='standalone'), expect('no_bank', 1406, '-19.00', '0')),
    ('S-standalone-input-other-rate', entry('purpose_first', 6815, 1800, '-120.00', '-20.00', 0.2, source='standalone'), expect('no_bank', 6815, '-100.00', '-20.00')),
    ('D-other-date-source', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, source='other_date', entry_date='2026-04-02'),
     expect('other_date', 4400, '100.00', '19.00')),
    ('D-other-date-keeps-input-tax-as-booked', entry('purpose_first', 6815, 1800, '-119.00', '-19.00', 0.19, source='other_date'),
     expect('other_date', 6815, '-100.00', '-19.00')),
    ('D-other-date-neutral-stays', entry('purpose_first', 6815, 1800, '-50.00', source='other_date'), expect(NEUTRAL, None, '-50.00', '0')),
    ('D-movement-before', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date='2026-02-28'), expect('other_date', 4400, '100.00', '19.00')),
    ('D-movement-after', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date='2026-04-01'), expect('other_date', 4400, '100.00', '19.00')),
    ('D-movement-on-the-first-day', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date='2026-03-01'), expect(KZ_81, 4400, '100.00', '19.00')),
    ('D-movement-on-the-last-day', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date='2026-03-31'), expect(KZ_81, 4400, '100.00', '19.00')),
    ('D-other-day-inside-the-period-counts', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date='2026-03-31', entry_date='2026-03-02'),
     expect(KZ_81, 4400, '100.00', '19.00')),
    ('D-no-date-of-the-movement-counts', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date=None), expect(KZ_81, 4400, '100.00', '19.00')),
    ('D-movement-neutral-stays', entry('purpose_first', 6815, 1800, '-50.00', parent_date='2026-04-01'), expect(NEUTRAL, None, '-50.00', '0')),
    ('D-movement-tax-account', entry('purpose_first', 1406, 1800, '-19.00', parent_date='2026-04-01'), expect('other_date', 1406, '-19.00', '0')),
    ('D-movement-without-a-real-date', entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, parent_date=''), expect('other_date', 4400, '100.00', '19.00')),
]


@pytest.mark.parametrize('case, item, expected', ASSIGNMENTS, ids=[case[0] for case in ASSIGNMENTS])
def test_assign_places_every_row_of_the_table(case, item, expected):
    assert assign(item, *PERIOD) == expected


def test_every_target_of_the_table_is_reached_by_a_case():
    """Jede Kennzahl, jeder Grund und ``neutral`` kommt in der Tabelle vor – kein Grund ist tot."""
    reached = {expected.target for _case, _item, expected in ASSIGNMENTS}
    assert reached == {KZ_81, KZ_86, KZ_66, NEUTRAL, *UNASSIGNED_REASONS}
    assert len({case for case, _item, _expected in ASSIGNMENTS}) == len(ASSIGNMENTS)


def test_the_reasons_stand_in_the_order_of_the_display_and_a_nonreadable_amount_is_the_last():
    assert list(UNASSIGNED_REASONS) == ['zero_rate', 'other_rate', 'input_other_rate', 'account_4185', 'tax_without_rate', 'rate_tax_mismatch', 'private_tax',
                                        'other_date', 'private_transfer', 'no_bank', 'transfer_other', 'tax_other', 'tax_account', 'waiting', 'no_account',
                                        'unreadable']
    assert UNASSIGNED_REASONS['account_4185'] == 'Erlöse auf Konto 4185' and UNASSIGNED_REASONS['unreadable'] == 'Betrag nicht lesbar'
    assert UNASSIGNED_REASONS['input_other_rate'] == 'Vorsteuer zu einem anderen Steuersatz'
    assert UNASSIGNED_REASONS['tax_account'] == 'Satz direkt auf einem Steuerkonto'
    assert UNBALANCED_LABEL == 'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben'
    assert UNREADABLE == 'unreadable' and ustva.WAITING_NUMBERS == frozenset(WAITING)


def test_the_tax_accounts_are_the_ones_of_the_susa_and_the_tolerance_is_two_cents():
    """Als Steuerkonto zählen die Konten, auf denen die Summen- und Saldenliste die Steuer bucht (``susa.TAX_ACCOUNTS``) – nicht die Zahllast- und Finanzamtskonten.
    Die Grenze zwischen gebuchter Steuer und Netto × Satz sind zwei Cent in Minor Units (``money.SCALE`` = 4 Stellen)."""
    from db import susa
    assert TAX_ACCOUNT_NUMBERS == frozenset({1401, 1406, 3801, 3806})
    assert TAX_ACCOUNT_NUMBERS == {number for by_rate in susa.TAX_ACCOUNTS.values() for number in by_rate.values()}
    assert TAX_ACCOUNT_NUMBERS.isdisjoint({3800, 3802, 3804, 3820, 3830, 3840, 3841})
    assert TAX_TOLERANCE_MINOR == minor('0.02') == 200


@pytest.mark.parametrize('rate', [0.19, 19, 19.0, 0.1900001])
def test_a_rate_of_19_percent_counts_whatever_way_it_is_stored(rate):
    assert assign(entry('purpose_first', 4400, 1800, '119.00', '19.00', rate), *PERIOD).target == KZ_81


@pytest.mark.parametrize('rate', [None, 0, '', 'x', -1, float('nan'), float('inf')])
def test_no_readable_rate_is_no_rate(rate):
    assert assign(entry('purpose_first', 4400, 1800, '5.00', '5.00', rate), *PERIOD).target == 'tax_without_rate'
    assert assign(entry('purpose_first', 4400, 1800, '5.00', '0', rate), *PERIOD).target == 'zero_rate'


@pytest.mark.parametrize('amount, tax, rate, fits', [
    ('119.00', '19.00', 0.19, True), ('119.02', '19.02', 0.19, True), ('118.98', '18.98', 0.19, True), ('119.0200', '19.0200', 0.19, True),
    ('119.0201', '19.0201', 0.19, False), ('118.9799', '18.9799', 0.19, False), ('119.03', '19.03', 0.19, False), ('118.97', '18.97', 0.19, False),
    ('107.00', '7.00', 0.07, True), ('107.02', '7.02', 0.07, True), ('106.98', '6.98', 0.07, True),
    ('107.03', '7.03', 0.07, False), ('106.97', '6.97', 0.07, False),
    ('119.00', '7.00', 0.19, False), ('19.00', '19.00', 0.19, False), ('107.00', '17.00', 0.07, False),
    ('0.10', '0.02', 0.19, True), ('0.05', '0.01', 0.19, True),
])
def test_the_tax_must_fit_net_times_rate_within_two_cents_for_revenue_and_input_tax_alike(amount, tax, rate, fits):
    """Netto = Betrag − Steuer; die gebuchte Steuer weicht von Netto × Satz höchstens um zwei Cent ab (genau auf die Minor Unit) – bei Erlös (Kz 81/86) und bei
    Vorsteuer (Kz 66), je als Zahlung und als Erstattung; sonst steht der Satz unter ``rate_tax_mismatch``. Die Beträge stehen ohne Vorzeichen da."""
    revenue_account, expense_account, figure = (4400, 6815, KZ_81) if rate == 0.19 else (4300, 6850, KZ_86)
    flip = lambda text: f'-{text}'          # noqa: E731
    cases = [(entry('purpose_first', revenue_account, 1800, amount, tax, rate), figure),
             (entry('purpose_first', revenue_account, 1800, flip(amount), flip(tax), rate), figure),
             (entry('purpose_first', expense_account, 1800, flip(amount), flip(tax), rate), KZ_66),
             (entry('purpose_first', expense_account, 1800, amount, tax, rate), KZ_66)]
    for item, target in cases:
        assert assign(item, *PERIOD).target == (target if fits else 'rate_tax_mismatch'), (item.coa_number, item.amount, item.tax)


# ═══ Rundung der Bemessungsgrundlage ══════════════════════════════════════════
def test_the_base_is_cut_to_whole_euros_toward_zero_and_the_cents_of_the_tax_stay():
    assert whole_euros(minor('12345.6789')) == minor('12345') and whole_euros(minor('-12.99')) == minor('-12')
    assert whole_euros(minor('0.99')) == 0 and whole_euros(minor('-0.99')) == 0 and whole_euros(minor('100.00')) == minor('100.00')
    assert format_whole(minor('12345.6789')) == '12.345' and format_whole(minor('-12.99')) == '-12' and format_whole(minor('-0.50')) == '0'
    report = build_ustva(2026, 3, 'monat', [entry('purpose_first', 4400, 1800, '120.18', '19.19', 0.19)], {}, 0, 0, (0, 0))
    figure = report.figures[0]
    assert (figure.base, figure.base_whole, figure.tax) == (minor('100.99'), minor('100.00'), minor('19.19'))      # die Steuer wird nicht neu gerechnet


def test_the_direction_of_the_rounding_is_set_in_exactly_one_place(monkeypatch):
    """``BASE_ROUNDING`` ist die EINE Stelle: Ändert man sie, ändern sich ``whole_euros``, ``format_whole`` und die ganzen Euro im Bericht – sonst nichts."""
    items = [entry('purpose_first', 4400, 1800, '120.18', '19.19', 0.19)]
    before = build_ustva(2026, 3, 'monat', items, {}, 0, 0, (0, 0))
    monkeypatch.setattr(ustva, 'BASE_ROUNDING', ROUND_HALF_UP)
    assert whole_euros(minor('12345.6789')) == minor('12346') and format_whole(minor('12345.6789')) == '12.346'
    after = build_ustva(2026, 3, 'monat', items, {}, 0, 0, (0, 0))
    assert after.figures[0].base_whole == minor('101.00') and before.figures[0].base_whole == minor('100.00')
    assert dataclasses.replace(after.figures[0], base_whole=before.figures[0].base_whole) == before.figures[0]       # nichts anderes ändert sich


def test_the_whole_euros_come_from_the_sum_never_from_each_account():
    """Zwei Konten mit 100,60 € ergeben 201 € (aus der Summe 201,20 €), nicht 200 €."""
    items = [entry('purpose_first', 4400, 1800, '119.71', '19.11', 0.19), entry('purpose_first', 4640, 1800, '119.71', '19.11', 0.19)]
    figure = build_ustva(2026, 3, 'monat', items, {}, 0, 0, (0, 0)).figures[0]
    assert [line.base for line in figure.accounts] == [minor('100.60')] * 2 and figure.base == minor('201.20') and figure.base_whole == minor('201.00')


# ═══ Zeitraum, Bezeichnung, Dateiname ═════════════════════════════════════════
@pytest.mark.parametrize('year, month, kind, expected', [
    (2026, 3, 'monat', ('2026-03-01', '2026-03-31')), (2026, 4, 'monat', ('2026-04-01', '2026-04-30')), (2024, 2, 'monat', ('2024-02-01', '2024-02-29')),
    (2026, 2, 'monat', ('2026-02-01', '2026-02-28')), (2100, 2, 'monat', ('2100-02-01', '2100-02-28')), (2000, 2, 'monat', ('2000-02-01', '2000-02-29')),
    (2026, 12, 'monat', ('2026-12-01', '2026-12-31')), (1, 1, 'monat', ('0001-01-01', '0001-01-31')), (9999, 12, 'monat', ('9999-12-01', '9999-12-31')),
    (2026, 3, 'quartal', ('2026-01-01', '2026-03-31')), (2026, 6, 'quartal', ('2026-04-01', '2026-06-30')),
    (2026, 9, 'quartal', ('2026-07-01', '2026-09-30')), (2026, 12, 'quartal', ('2026-10-01', '2026-12-31')),
])
def test_the_period_of_a_month_or_a_quarter(year, month, kind, expected):
    assert ustva_period(year, month, kind) == expected


@pytest.mark.parametrize('year, month, kind', [
    (0, 3, 'monat'), (10000, 3, 'monat'), (-1, 3, 'monat'), ('2026', 3, 'monat'), (2026.0, 3, 'monat'), (None, 3, 'monat'),
    (2026, 0, 'monat'), (2026, 13, 'monat'), (2026, -1, 'monat'), (2026, '3', 'monat'), (2026, None, 'monat'),
    (2026, 3, 'x'), (2026, 3, None), (2026, 3, 'Monat'), (2026, 3, ''), (2026, 3, 'jahr'),
    (2026, 1, 'quartal'), (2026, 2, 'quartal'), (2026, 4, 'quartal'), (2026, 5, 'quartal'), (2026, 8, 'quartal'), (2026, 10, 'quartal'), (2026, 11, 'quartal'),
])
def test_a_period_out_of_range_is_refused(year, month, kind):
    for function in (ustva_period, period_label, ustva_filename):
        with pytest.raises(ValueError):
            function(year, month, kind)


def test_the_quarter_of_a_month():
    assert [quarter_end_month(month) for month in range(1, 13)] == [3] * 3 + [6] * 3 + [9] * 3 + [12] * 3


@pytest.mark.parametrize('first, last, expected', [
    ('2026-01-01', '2026-03-31', True), ('2026-04-01', '2026-06-30', True), ('2026-07-01', '2026-09-30', True), ('2026-10-01', '2026-12-31', True),
    ('2024-01-01', '2024-03-31', True), ('2024-04-01', '2024-06-30', True),
    ('2026-01-01', '2026-03-30', False), ('2026-01-02', '2026-03-31', False), ('2026-02-01', '2026-04-30', False), ('2026-01-01', '2026-02-28', False),
    ('2026-03-01', '2026-03-31', False), ('2026-01-01', '2026-12-31', False), ('2025-10-01', '2026-03-31', False), ('2026-04-01', '2026-06-29', False),
    ('2026-03-31', '2026-01-01', False), ('2026-01-01', '2026-01-01', False),
    ('2026-1-1', '2026-3-31', False), ('20260101', '20260331', False), ('2026-13-01', '2026-15-31', False), ('2026-02-30', '2026-04-30', False),
    ('', '', False), (None, None, False), (5, 6, False), ('x', 'y', False), ('0000-01-01', '0000-03-31', False),
])
def test_a_calendar_quarter_is_exactly_one_quarter_of_one_year(first, last, expected):
    assert is_calendar_quarter(first, last) is expected


def test_the_label_and_the_file_name():
    assert period_label(2026, 3, 'monat') == 'März 2026' and period_label(2026, 12, 'monat') == 'Dezember 2026'
    assert [period_label(2026, month, 'quartal') for month in (3, 6, 9, 12)] == ['1. Quartal 2026', '2. Quartal 2026', '3. Quartal 2026', '4. Quartal 2026']
    assert ustva_filename(2026, 3, 'monat') == 'USt-Voranmeldung_2026-03.pdf' and ustva_filename(2026, 12, 'monat') == 'USt-Voranmeldung_2026-12.pdf'
    assert [ustva_filename(2026, month, 'quartal') for month in (3, 6, 9, 12)] == [f'USt-Voranmeldung_2026-Q{number}.pdf' for number in (1, 2, 3, 4)]


# ═══ build_ustva mit von Hand gebauten Sätzen ═════════════════════════════════
def test_an_empty_report_has_the_figures_81_and_86_and_no_unassigned_group():
    report = build_ustva(2026, 3, 'monat', [], {}, 0, 0, (0, 0))
    assert [(figure.key, figure.kz, figure.base, figure.base_whole, figure.tax, figure.accounts) for figure in report.figures] == [
        ('kz81', '81', 0, 0, 0, ()), ('kz86', '86', 0, 0, 0, ())]
    assert (report.input_tax.key, report.input_tax.kz, report.input_tax.tax, report.input_tax.base_whole) == ('kz66', '66', 0, 0)
    assert report.unassigned == () and (report.vat_total, report.prepayment, report.recon_page, report.recon_euer, report.recon_difference) == (0, 0, 0, 0, 0)
    assert (report.read_count, report.neutral_count, report.unreadable_count, report.unbooked_count) == (0, 0, 0, 0) and not report.has_small_business
    assert (report.year, report.month, report.kind, report.date_from, report.date_to) == (2026, 3, 'monat', '2026-03-01', '2026-03-31')
    check_invariants(report)


def test_the_report_of_a_quarter_names_its_period_and_a_wrong_one_is_refused():
    report = build_ustva(2026, 6, 'quartal', [], {}, 0, 0, (0, 0))
    assert (report.date_from, report.date_to, report.kind, report.month) == ('2026-04-01', '2026-06-30', 'quartal', 6)
    for arguments in ((2026, 5, 'quartal'), (2026, 13, 'monat'), (2026, 3, 'x')):
        with pytest.raises(ValueError):
            build_ustva(*arguments, [], {}, 0, 0, (0, 0))


MIXED = [
    entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19), entry('purpose_first', 4640, 1800, '59.50', '9.50', 0.19),
    entry('liquid_first', 1800, 4300, '107.00', '7.00', 0.07),
    entry('purpose_first', 6815, 1800, '-119.00', '-19.00', 0.19), entry('purpose_first', 520, 1800, '-1190.00', '-190.00', 0.19),
    entry('purpose_first', 6815, 1800, '-21.42', '-3.42', 0.19, booking_id=9),
    entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16), entry('purpose_first', 4185, 1800, '50.00'), entry('purpose_first', 4184, 1800, '40.00'),
    entry('purpose_first', 4120, 1800, '30.00'), entry('purpose_first', 2100, 1800, '-119.00', '-19.00', 0.19),
    entry('purpose_first', 4405, 1800, '10.00'), entry('purpose_first', None, 1800, '1.00'), entry('purpose_first', 4400, 1800, None, '1.00'),
    entry('transfer', 2100, 4645, '119.00', '19.00', 0.19, source='standalone'), entry('purpose_first', 4400, 1800, '1.19', '0.19', 0.19, source='other_date'),
    entry('mirror_coa', 1800, 1810, '-100.00'), entry('purpose_first', 6815, 1800, '-1.00'),
    entry('purpose_first', 1406, 1800, '-19.00'), entry('purpose_first', 6815, 1800, '-120.00', '-20.00', 0.2),
]
NAMES = {4400: 'Erlöse 19% USt', 4640: 'Erlöse 4640', 4300: 'Erlöse 7% USt', 6815: 'Bürobedarf', 520: 'Anlage', 4185: 'Erlöse ohne Steuer'}


def test_the_sums_are_whole_numbers_and_every_entry_stands_in_one_place():
    report = build_ustva(2026, 3, 'monat', MIXED, NAMES, minor('33.00'), minor('-100.00'), (minor('12.34'), 2))
    check_invariants(report, 'MIXED')
    figure_81, figure_86 = report.figures
    assert (figure_81.base, figure_81.tax, figure_86.base, figure_86.tax) == (minor('150.00'), minor('28.50'), minor('100.00'), minor('7.00'))
    assert (report.input_tax.tax, report.input_tax.base) == (minor('212.42'), minor('1118.00'))                  # 19 + 190 + 3,42; 100 + 1000 + 18 (eine Ausgabe ist positiv)
    assert report.read_count == len(MIXED) and report.unreadable_count == 1 and report.neutral_count == 2


def test_the_groups_stand_in_the_order_of_the_reasons_and_only_with_entries():
    report = build_ustva(2026, 3, 'monat', list(reversed(MIXED)), NAMES, 0, 0, (0, 0))
    keys = [group.key for group in report.unassigned]
    assert keys == [key for key in UNASSIGNED_REASONS if key in keys] and len(keys) == len(set(keys))
    assert set(keys) == {'zero_rate', 'other_rate', 'input_other_rate', 'account_4185', 'private_tax', 'other_date', 'private_transfer', 'tax_account', 'waiting',
                         'no_account', 'unreadable'}
    assert all(group.count > 0 and group.label == UNASSIGNED_REASONS[group.key] for group in report.unassigned)
    check_invariants(report, 'reversed')


def test_the_accounts_are_sorted_by_number_with_the_ones_without_a_number_last():
    items = [entry('purpose_first', 4640, 1800, '119.00', '19.00', 0.19), entry('purpose_first', None, 1800, '1.00'), entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19),
             entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19), entry('purpose_first', 4399, 1800, '119.00', '19.00', 0.19)]
    report = build_ustva(2026, 3, 'monat', items, {4400: 'A', 4399: 'B'}, 0, 0, (0, 0))
    assert [(line.number, line.name, line.count) for line in report.figures[0].accounts] == [(4399, 'B', 1), (4400, 'A', 2), (4640, '', 1)]
    assert [(line.number, line.count) for line in report.unassigned[0].accounts] == [(None, 1)] and report.unassigned[0].key == 'no_account'


def test_the_small_business_note_depends_on_account_4184_with_no_tax_only():
    def has(*items):
        return build_ustva(2026, 3, 'monat', list(items), {}, 0, 0, (0, 0)).has_small_business
    assert has(entry('purpose_first', 4184, 1800, '40.00'))
    assert not has(entry('purpose_first', 4185, 1800, '40.00')) and not has(entry('purpose_first', 4120, 1800, '40.00')) and not has()
    assert not has(entry('purpose_first', 4184, 1800, '40.00', '4.00'))                       # mit Steuer ohne Satz: ein anderer Grund
    assert not has(entry('purpose_first', 4184, 1800, '119.00', '19.00', 0.19, source='standalone'))


def test_the_reconciliation_compares_the_tax_of_the_figures_with_the_euer_and_the_bank_line_stays_outside_the_invariants():
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19), entry('purpose_first', 6815, 1800, '-59.50', '-9.50', 0.19)]
    report = build_ustva(2026, 3, 'monat', items, {}, minor('19.00'), minor('-9.50'), (minor('-77.00'), 3))
    assert (report.recon_page, report.recon_euer, report.recon_difference) == (minor('9.50'), minor('9.50'), 0)
    assert (report.unbooked_count, report.unbooked_amount) == (3, minor('-77.00')) and report.unassigned == ()      # die Bewegungen ohne Satz sind kein Satz
    check_invariants(report)
    other = build_ustva(2026, 3, 'monat', items, {}, minor('35.00'), minor('-9.50'), (0, 0))
    assert other.recon_euer == minor('25.50') and other.recon_difference == minor('-16.00')


# ═══ Die Invarianten (Pflicht, nicht lockern) ═════════════════════════════════
def counted_by_sql(db, report):
    """Die Zahl der Sätze, die der Bericht gelesen haben muss – gegengezählt mit eigener SQL über die Kontonummern, ohne die Filter der App zu kopieren: die Sätze der EÜR
    (``_euer_entries``), dazu (a) die Kinder einer Bankbewegung im Zeitraum mit eigenem Datum außerhalb und (b) die eigenständigen Sätze im Zeitraum, die nicht schon in
    der EÜR stehen – bis auf die Forderung einer Rechnung und die Gegenbuchung ihrer Stornorechnung (eine Umbuchung zwischen einem Debitor, 10000 bis 69999, und einem
    Wartekonto, 4340, 4345 oder 4405)."""
    span = (report.date_from, report.date_to)
    connection = db._get_connection()
    try:
        in_euer = set(db._euer_entries(connection.cursor(), *span).entries)
    finally:
        connection.close()
    with closing(sqlite3.connect(db.db_name)) as con:
        other_date = con.execute(
            "SELECT COUNT(*) FROM Bookings e JOIN Bookings p ON p.ID = e.ParentBooking_ID WHERE e.BookingType = 'entry' "
            "AND p.DateBooking BETWEEN ? AND ? AND NOT (e.DateBooking BETWEEN ? AND ?) AND (e.Status IS NULL OR e.Status != 'resolved')", [*span, *span]).fetchone()[0]
        standalone = [row[0] for row in con.execute(
            "SELECT e.ID FROM Bookings e LEFT JOIN ChartOfAccounts c ON c.ID = e.COA_ID LEFT JOIN ChartOfAccounts k ON k.ID = e.CounterCOA_ID "
            "WHERE e.BookingType = 'entry' AND e.ParentBooking_ID IS NULL AND e.DateBooking BETWEEN ? AND ? AND (e.Status IS NULL OR e.Status != 'resolved') "
            "AND NOT (e.EntryForm = 'transfer' AND ((c.AccountNumber BETWEEN 10000 AND 69999 AND k.AccountNumber IN (4340, 4345, 4405)) "
            "OR (k.AccountNumber BETWEEN 10000 AND 69999 AND c.AccountNumber IN (4340, 4345, 4405))))", span)]
    return len(in_euer) + other_date + len([booking_id for booking_id in standalone if booking_id not in in_euer])


@pytest.mark.parametrize('name', WORLD_NAMES)
def test_every_entry_stands_in_exactly_one_place_in_every_month_and_quarter(worlds, name):
    """Die Invarianten in der SuSa-Welt, den zufälligen Beständen (Seeds 1–5) und im Demobestand, für jeden Monat und jedes Quartal 2026 (im Demobestand auch
    2025): Zahl, Netto und Steuer gehen ohne Rest ein, ``neutral`` trägt nie Steuer, und die Zahl der gelesenen Sätze stimmt mit der eigenen SQL überein."""
    db = worlds[name]
    for year in (2025, 2026) if name == 'demo' else (2026,):
        for year_, month, kind in every_period(year):
            report = db.get_ustva(year_, month, kind)
            check_invariants(report, (name, year_, month, kind))
            assert report.read_count == counted_by_sql(db, report), (name, year_, month, kind)
            assert report.unreadable_count == 0                                                       # alle Bestände entstehen über Wege der App


@pytest.mark.parametrize('name', WORLD_NAMES)
def test_what_the_app_books_never_lands_in_the_reasons_for_tax_accounts_rates_and_amounts_and_the_unbalanced_ones_are_those_of_the_split_check(worlds, name):
    """In denselben Beständen und Zeiträumen: Was die App selbst bucht, trifft nie ``rate_tax_mismatch`` (die Steuer passt zu Netto × Satz auf zwei Cent), ``input_other_rate``
    oder ``tax_account``; Zahl und Summe der unstimmigen Bankbewegungen sind die der Prüfung der Splits (``find_unbalanced_splits``) über denselben Zeitraum."""
    db = worlds[name]
    for year in (2025, 2026) if name == 'demo' else (2026,):
        for year_, month, kind in every_period(year):
            report = db.get_ustva(year_, month, kind)
            assert not {'rate_tax_mismatch', 'input_other_rate', 'tax_account'} & {group.key for group in report.unassigned}, (name, year_, month, kind)
            unbalanced = db.find_unbalanced_splits(report.date_from, report.date_to)
            assert (report.unbalanced_count, report.unbalanced_amount) == (len(unbalanced), sum(abs(to_minor(row[3])) for row in unbalanced)), (name, year_, month, kind)


def test_the_literal_form_of_the_count_with_the_unreadable_entries_in_its_own_term_holds_where_there_are_none(worlds):
    """Wo es keinen nicht lesbaren Satz gibt, ist die Zählung auch so zu schreiben: Kennzahlen + Gründe + neutral + ``unreadable_count`` = gelesen."""
    report = worlds['susa'].get_ustva(2026, 3, 'quartal')
    placed = sum(line.count for figure in (*report.figures, report.input_tax) for line in figure.accounts) + sum(group.count for group in report.unassigned)
    assert placed + report.neutral_count + report.unreadable_count == report.read_count == 13


@pytest.mark.parametrize('arguments', [(2026, 13, 'monat'), (2026, 0, 'monat'), (0, 3, 'monat'), (10000, 3, 'monat'), (2026, 2, 'quartal'), (2026, 3, 'x'), ('2026', 3, 'monat')])
def test_a_period_out_of_range_is_refused_before_anything_is_read(db, arguments):
    with pytest.raises(ValueError):
        db.get_ustva(*arguments)


def test_a_book_without_any_account_has_no_bank_line_and_no_exception(tmp_db):
    """Ohne ein einziges Konto (auch ohne Kasse) gibt es keine Bankbewegung, die ohne Satz sein könnte – der Bericht ist leer, ohne Fehler."""
    raw(tmp_db, 'DELETE FROM Accounts')
    report = tmp_db.get_ustva(2026, 3, 'monat')
    assert (report.unbooked_count, report.unbooked_amount, report.read_count) == (0, 0, 0)
    check_invariants(report)


def test_the_report_changes_nothing_in_the_books(worlds):
    db = worlds['susa']
    before = dump(db.db_name)
    for year, month, kind in every_period(2026):
        db.get_ustva(year, month, kind)
    assert dump(db.db_name) == before


# ═══ Feste Werte der SuSa-Welt (von Hand gerechnet) ═══════════════════════════
def test_the_first_quarter_of_the_susa_world_in_fixed_values(worlds):
    """1. Quartal 2026: 81 (400,00 €: drei Zahlungen zu 19 %, 76,00 €), 86 (100,00 €, 7,00 €), Vorsteuer 21,57 €, Vorauszahlung 61,43 €; nichts unter „Nicht
    zugeordnet“, die Abstimmung geht auf. Gegen ``get_euer_data`` bestätigt: Umsatzsteuer 83,00 €, Vorsteuer −21,57 €."""
    report = worlds['susa'].get_ustva(2026, 3, 'quartal')
    figure_81, figure_86 = report.figures
    assert (figure_81.base, figure_81.base_whole, figure_81.tax) == (minor('400.00'), minor('400.00'), minor('76.00'))
    assert (figure_86.base, figure_86.base_whole, figure_86.tax) == (minor('100.00'), minor('100.00'), minor('7.00'))
    assert report.input_tax.tax == minor('21.57') and report.vat_total == minor('83.00') and report.prepayment == minor('61.43')
    assert [(line.number, line.name, line.count, line.base, line.tax) for line in figure_81.accounts] == [(4400, 'Erlöse 19% USt', 3, minor('400.00'), minor('76.00'))]
    assert [(line.number, line.count, line.tax) for line in report.input_tax.accounts] == [
        (6805, 1, minor('1.92')), (6815, 3, minor('16.15')), (6850, 1, minor('3.50'))]
    assert report.unassigned == () and (report.unbooked_count, report.unbooked_amount) == (0, 0)
    assert (report.recon_page, report.recon_euer, report.recon_difference) == (minor('61.43'), minor('61.43'), 0)
    euer = {number: to_minor(total) for number, _name, total in worlds['susa'].get_euer_data('2026-01-01', '2026-03-31')}
    assert euer[3806] == minor('83.00') and euer[1406] + euer[1401] == minor('-21.57')                 # die Quelle der Handrechnung


def test_march_of_the_susa_world_in_fixed_values(worlds):
    report = worlds['susa'].get_ustva(2026, 3, 'monat')
    figure_81, figure_86 = report.figures
    assert (figure_81.base, figure_81.tax, figure_86.base, figure_86.tax) == (minor('100.00'), minor('19.00'), 0, 0)
    assert report.input_tax.tax == minor('0.95') and report.prepayment == minor('18.05') and report.unassigned == ()
    assert (report.recon_page, report.recon_difference) == (minor('18.05'), 0)


@pytest.mark.parametrize('year, month, kind, expected', [
    (2026, 1, 'monat', {'81': ('300.0000', '57.0000'), '86': ('0.0000', '0.0000'), '66': ('0.0000', '0.0000')}),
    (2026, 2, 'monat', {'81': ('0.0000', '0.0000'), '86': ('100.0000', '7.0000'), '66': ('140.0800', '20.6200')}),
    (2026, 4, 'monat', {'81': ('0.0000', '0.0000'), '86': ('100.0000', '7.0000'), '66': ('0.0000', '0.0000')}),
    (2026, 6, 'monat', {'81': ('-100.0000', '-19.0000'), '86': ('0.0000', '0.0000'), '66': ('0.0000', '0.0000')}),
    (2026, 3, 'quartal', {'81': ('400.0000', '76.0000'), '86': ('100.0000', '7.0000'), '66': ('145.0800', '21.5700')}),
    (2025, 3, 'monat', {'81': ('100.0000', '19.0000'), '86': ('0.0000', '0.0000'), '66': ('50.0000', '9.5000')}),
])
def test_other_periods_of_the_susa_world(worlds, year, month, kind, expected):
    """Januar: die Zahlung der Rechnung vom Dezember 2025 (200,00 €) und die Einnahme vom 20.1.; Juni: die Erstattung der stornierten Rechnung steht
    negativ in 81; die Erlöse der Rechnung vom März, im April gezahlt, stehen im April."""
    result = in_places(worlds['susa'].get_ustva(year, month, kind))
    assert {key: result[key] for key in ('81', '86', '66')} == expected and result['groups'] == []


def test_the_unbooked_bank_movement_and_the_covered_transfers_of_the_susa_world(worlds):
    """August: eine Bankbewegung ohne Satz (119,00 €) – genannt, aber in keiner Kennzahl; die Überträge vom Juli (Spiegel auf beiden Seiten, satzlose Gegenseite) stehen
    dort nicht (das Dashboard zählt sie als Übertrag)."""
    july, august = worlds['susa'].get_ustva(2026, 7, 'monat'), worlds['susa'].get_ustva(2026, 8, 'monat')
    assert (july.unbooked_count, july.unbooked_amount) == (0, 0) and july.neutral_count == 3
    assert (august.unbooked_count, august.unbooked_amount) == (1, minor('119.00')) and august.read_count == 0 and august.unassigned == ()
    assert worlds['susa'].get_ustva(2026, 9, 'quartal').unbooked_count == 1 and worlds['susa'].get_ustva(2026, 6, 'quartal').unbooked_count == 0


# ═══ Wartekonten ══════════════════════════════════════════════════════════════
def accounts_in_figures(report):
    return {line.number for figure in (*report.figures, report.input_tax) for line in figure.accounts}


def test_the_waiting_accounts_never_stand_in_a_figure(db):
    """Eine bezahlte Rechnung zu 19, 7 und 0 %: Die Umsatzsteuer entsteht mit der Umbuchung auf das Erlöskonto bei der Zahlung – 4405, 4345 und 4340 stehen in keiner
    Kennzahl (und die Zahlung ist kein Posten unter „Nicht zugeordnet“)."""
    paid_invoice(db, 'R-19', 119.00, '2026-03-05', '2026-03-20', 'Probe-Giro')
    paid_invoice(db, 'R-7', 107.00, '2026-03-06', '2026-03-21', 'Probe-Giro', rate=0.07)
    paid_invoice(db, 'R-0', 100.00, '2026-03-07', '2026-03-22', 'Probe-Giro', rate=0.0)
    report = db.get_ustva(2026, 3, 'monat')
    check_invariants(report)
    assert accounts_in_figures(report).isdisjoint(WAITING)
    assert [(figure.base, figure.tax) for figure in report.figures] == [(minor('100.00'), minor('19.00')), (minor('100.00'), minor('7.00'))]
    assert [(group.key, group.count, group.net, group.tax) for group in report.unassigned] == [('account_4185', 1, minor('100.00'), 0)]       # die Rechnung ohne Steuer: 4185
    assert (report.read_count, report.neutral_count, report.recon_difference) == (6, 3, 0)          # je Zahlung der Erlös und „Bank an Debitor“; die Forderungen nicht


def test_a_raw_payment_on_a_waiting_account_is_a_reason_not_a_figure(db):
    giro = account_id(db, 'Probe-Giro')
    bank = raw_bank(db, '2026-03-12', giro, minor('119.00'))
    raw_entry(db, '2026-03-12', 4405, 1800, minor('119.00'), form='purpose_first', parent=bank, rate=0.19, tax=minor('19.00'))
    report = db.get_ustva(2026, 3, 'monat')
    assert in_places(report)['groups'] == [('waiting', 1, str(from_minor(minor('100.00'))), str(from_minor(minor('19.00'))))]
    assert accounts_in_figures(report) == set() and report.unassigned[0].accounts[0].number == 4405
    check_invariants(report)


def test_an_open_receivable_is_read_nowhere_and_is_no_item(db):
    """Die Forderung „10000 an 4405“ einer versendeten Rechnung bewegt kein Geld; sonst stünde jede offene Rechnung unter „Nicht zugeordnet“."""
    before = db.get_ustva(2026, 3, 'monat')
    invoice_id = helpers.invoice(db, 'R-OFFEN', 119.00, '2026-03-05')
    assert db.ensure_receivable_booking(invoice_id) is not None
    report = db.get_ustva(2026, 3, 'monat')
    assert report.read_count == before.read_count == 0 and report.unassigned == () and report.neutral_count == 0
    with closing(sqlite3.connect(db.db_name)) as con:           # die Forderung gibt es, sie steht im März – und ist eine Umbuchung der Kette
        row = con.execute("SELECT EntryForm, DateBooking FROM Bookings WHERE BookingType = 'entry'").fetchall()
    assert row == [('transfer', '2026-03-05')]


# ═══ Skonto ═══════════════════════════════════════════════════════════════════
def test_a_granted_discount_reduces_figure_81_through_its_own_account(db):
    """Rechnung 119,00 €, gezahlt 116,62 € mit Skonto (der Weg der App): 81 hat 98,00 € Basis und 18,62 € Steuer; 4736 steht im Detail mit −2,00 € und −0,38 €."""
    invoice_id = invoice_of(db, 'SK-1', 119.0, day='2026-03-03')
    pay_invoice(db, invoice_id, '116.62', '2026-03-20', skonto=True)
    report = db.get_ustva(2026, 3, 'monat')
    figure = report.figures[0]
    assert (figure.base, figure.base_whole, figure.tax) == (minor('98.00'), minor('98.00'), minor('18.62'))
    assert [(line.number, line.base, line.tax) for line in figure.accounts] == [(4400, minor('100.00'), minor('19.00')), (4736, minor('-2.00'), minor('-0.38'))]
    assert report.unassigned == () and accounts_in_figures(report).isdisjoint(WAITING) and report.recon_difference == 0
    check_invariants(report)


def test_a_granted_discount_at_7_percent_reduces_figure_86(db):
    invoice_id = invoice_of(db, 'SK-7', 107.0, rate=0.07, day='2026-03-03')
    pay_invoice(db, invoice_id, '104.86', '2026-03-20', skonto=True)
    report = db.get_ustva(2026, 3, 'monat')
    assert [(line.number, line.base, line.tax) for line in report.figures[1].accounts] == [(4300, minor('100.00'), minor('7.00')), (4731, minor('-2.00'), minor('-0.14'))]
    assert (report.figures[1].base, report.figures[1].tax) == (minor('98.00'), minor('6.86')) and report.figures[0].base == 0 and report.unassigned == ()


def test_a_discount_without_tax_is_a_zero_rate_item(db):
    invoice_id = invoice_of(db, 'SK-0', 100.0, rate=0.0, day='2026-03-03')
    pay_invoice(db, invoice_id, '98.00', '2026-03-20', skonto=True)
    report = db.get_ustva(2026, 3, 'monat')
    groups = {group.key: group for group in report.unassigned}
    assert set(groups) == {'account_4185', 'zero_rate'} and [(line.number, line.base) for line in groups['zero_rate'].accounts] == [(4730, minor('-2.00'))]
    assert (groups['account_4185'].net, groups['account_4185'].tax) == (minor('100.00'), 0) and report.figures[0].base == 0
    check_invariants(report)


def test_a_received_discount_or_a_refund_reduces_the_input_tax(db):
    mask(db, 'Probe-Giro', 6815, '-119.00', '2026-03-05', '19', '-19.00')
    mask(db, 'Probe-Giro', 6815, '23.80', '2026-03-12', '19', '3.80')
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.input_tax.base, report.input_tax.tax) == (minor('80.00'), minor('15.20')) and report.prepayment == -minor('15.20')
    assert [(line.number, line.count, line.base, line.tax) for line in report.input_tax.accounts] == [(6815, 2, minor('80.00'), minor('15.20'))]
    check_invariants(report)


# ═══ Das Datum der Bankbewegung ═══════════════════════════════════════════════
def test_an_entry_dated_after_its_bank_movement_stands_under_other_date_in_both_months(db):
    """Bankbewegung am 31.3. mit einem Satz „4400“ vom 2.4.: im März (Bewegung im Zeitraum, Satz außerhalb) und im April (Satz im Zeitraum, Bewegung außerhalb)
    unter ``other_date``, in keiner Kennzahl – und in beiden Quartalen ebenso."""
    bank_with_child(db, '2026-03-31', '119.00', '2026-04-02', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    for month, kind in ((3, 'monat'), (4, 'monat'), (3, 'quartal'), (6, 'quartal')):
        report = db.get_ustva(2026, month, kind)
        assert [(group.key, group.count, group.net, group.tax) for group in report.unassigned] == [('other_date', 1, minor('100.00'), minor('19.00'))], (month, kind)
        assert report.figures[0].base == 0 and report.figures[0].accounts == () and report.read_count == 1, (month, kind)
        check_invariants(report, (month, kind))
    assert db.get_ustva(2026, 3, 'monat').recon_euer == 0 and db.get_ustva(2026, 4, 'monat').recon_euer == minor('19.00')    # die EÜR liest ihn nur im April


def test_both_dates_in_the_period_count_normally(db):
    bank_with_child(db, '2026-03-31', '119.00', '2026-03-30', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    bank_with_child(db, '2026-01-31', '119.00', '2026-02-02', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    march, quarter = db.get_ustva(2026, 3, 'monat'), db.get_ustva(2026, 3, 'quartal')
    assert (march.figures[0].base, march.figures[0].tax) == (minor('100.00'), minor('19.00')) and march.unassigned == ()
    assert (quarter.figures[0].base, quarter.figures[0].tax, quarter.figures[0].accounts[0].count) == (minor('200.00'), minor('38.00'), 2) and quarter.unassigned == ()
    assert [group.key for group in db.get_ustva(2026, 1, 'monat').unassigned] == ['other_date'] and [group.key for group in db.get_ustva(2026, 2, 'monat').unassigned] == ['other_date']
    assert march.recon_difference == 0 and quarter.recon_difference == 0


# ═══ Jeder Grund unter „Nicht zugeordnet“ ═════════════════════════════════════
def reasons_world(db):
    """Ein Monat (März 2026) mit jedem Grund einmal oder mehrmals; ``(Zahl der Sätze insgesamt)``."""
    giro = account_id(db, 'Probe-Giro')
    mask(db, 'Probe-Giro', 4185, '50.00', '2026-03-02')                                           # account_4185, ohne Steuer
    mask(db, 'Probe-Giro', 4185, '119.00', '2026-03-02', '19', '19.00')                           # account_4185, zu 19 %
    mask(db, 'Probe-Giro', 4100, '30.00', '2026-03-03')                                           # zero_rate (4120 gibt es im Kontenrahmen nicht)
    mask(db, 'Probe-Giro', 4184, '40.00', '2026-03-03')                                           # zero_rate, Kleinunternehmer
    mask(db, 'Probe-Giro', 4400, '116.00', '2026-03-04', '16', '16.00')                           # other_rate
    mask(db, 'Probe-Giro', 6815, '-120.00', '2026-03-07', '20', '-20.00')                         # input_other_rate
    mask(db, 'Probe-Giro', 1406, '-19.00', '2026-03-09')                                          # tax_account: direkt auf dem Vorsteuerkonto, ohne Steuerbetrag
    bank = raw_bank(db, '2026-03-05', giro, minor('-58.00'))
    raw_entry(db, '2026-03-05', 6815, 1800, minor('-58.00'), form='purpose_first', parent=bank, tax=minor('-8.00'))        # tax_without_rate
    bank = raw_bank(db, '2026-03-06', giro, minor('119.00'))
    raw_entry(db, '2026-03-06', 4400, 1800, minor('119.00'), form='purpose_first', parent=bank, rate=0.19, tax=0)           # rate_tax_mismatch
    mask(db, 'Probe-Giro', 2100, '-119.00', '2026-03-08', '19', '-19.00')                         # private_tax
    raw_entry(db, '2026-03-09', 2100, 4645, minor('119.00'), form='transfer', rate=0.19, tax=minor('19.00'))               # private_transfer, eigenständig
    bank = raw_bank(db, '2026-03-09', giro, 0)
    raw_entry(db, '2026-03-09', 4645, 2100, minor('-119.00'), form='transfer', parent=bank, rate=0.19, tax=minor('-19.00'))  # private_transfer, als Kind
    raw_entry(db, '2026-03-10', 4400, 1800, minor('119.00'), form='purpose_first', rate=0.19, tax=minor('19.00'))          # no_bank
    raw_bank(db, '2026-03-11', giro, minor('77.00'))                                              # Bankbewegung ohne Satz
    bank = raw_bank(db, '2026-03-12', giro, minor('1.00'))
    raw_entry(db, '2026-03-12', 4400, 1800, 'x', form='purpose_first', parent=bank, rate=0.19, tax=minor('0.19'))           # unreadable, von der EÜR gelesen
    raw_entry(db, '2026-03-12', 4400, 1800, 'x', form='purpose_first')                                                       # unreadable, eigenständig
    bank = raw_bank(db, '2026-03-13', giro, minor('119.00'))
    raw_entry(db, '2026-03-13', 4405, 1800, minor('119.00'), form='purpose_first', parent=bank, rate=0.19, tax=minor('19.00'))  # waiting
    bank = raw_bank(db, '2026-03-14', giro, minor('10.00'))
    raw_entry(db, '2026-03-14', None, 1800, minor('10.00'), form='purpose_first', parent=bank)                               # no_account
    bank = raw_bank(db, '2026-03-15', giro, minor('100.00'))
    raw_entry(db, '2026-03-15', 3150, 1800, minor('105.00'), form='purpose_first', parent=bank, rate=0.19, tax=minor('5.00'))  # tax_other
    bank = raw_bank(db, '2026-03-16', giro, minor('10.00'))
    raw_entry(db, '2026-03-16', 3150, 3160, minor('10.00'), form='transfer', parent=bank, rate=0.19, tax=minor('1.00'))     # transfer_other
    bank_with_child(db, '2026-03-31', '119.00', '2026-04-02', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))    # other_date


def test_every_reason_names_its_entries_with_count_net_and_tax(db):
    reasons_world(db)
    report = db.get_ustva(2026, 3, 'monat')
    check_invariants(report)
    euro = lambda text: str(from_minor(minor(text)))          # noqa: E731
    assert in_places(report)['groups'] == [
        ('zero_rate', 2, euro('70.00'), euro('0')), ('other_rate', 1, euro('100.00'), euro('16.00')), ('input_other_rate', 1, euro('-100.00'), euro('-20.00')),
        ('account_4185', 2, euro('150.00'), euro('19.00')), ('tax_without_rate', 1, euro('-50.00'), euro('-8.00')),
        ('rate_tax_mismatch', 1, euro('119.00'), euro('0')), ('private_tax', 1, euro('-100.00'), euro('-19.00')),
        ('other_date', 1, euro('100.00'), euro('19.00')), ('private_transfer', 2, euro('0.00'), euro('0.00')), ('no_bank', 1, euro('100.00'), euro('19.00')),
        ('transfer_other', 1, euro('9.00'), euro('1.00')), ('tax_other', 1, euro('100.00'), euro('5.00')), ('tax_account', 1, euro('-19.00'), euro('0')),
        ('waiting', 1, euro('100.00'), euro('19.00')), ('no_account', 1, euro('10.00'), euro('0')), ('unreadable', 2, euro('0'), euro('0'))]
    assert report.has_small_business and (report.unbooked_count, report.unbooked_amount) == (1, minor('77.00'))
    assert (report.unbalanced_count, report.unbalanced_amount) == (2, minor('15.00'))               # 3150 mit 105,00 € unter 100,00 € und eine Umbuchung unter 10,00 €
    assert report.unassigned_net == sum(group.net for group in report.unassigned) and report.unassigned_tax == sum(group.tax for group in report.unassigned)
    assert report.figures[0].base == 0 and report.figures[1].base == 0 and report.input_tax.tax == 0
    assert report.unreadable_count == 2 and report.neutral_count == 0


def test_the_reasons_name_their_accounts_and_the_private_transfers_their_loss_side(db):
    reasons_world(db)
    groups = {group.key: group for group in db.get_ustva(2026, 3, 'monat').unassigned}
    assert [(line.number, line.count) for line in groups['zero_rate'].accounts] == [(4100, 1), (4184, 1)]
    assert [(line.number, line.count) for line in groups['private_transfer'].accounts] == [(4645, 2)]
    assert [line.number for line in groups['private_tax'].accounts] == [2100] and [line.number for line in groups['waiting'].accounts] == [4405]
    assert [line.number for line in groups['no_account'].accounts] == [None] and [line.number for line in groups['tax_other'].accounts] == [3150]
    assert [line.number for line in groups['transfer_other'].accounts] == [3160] and [line.number for line in groups['no_bank'].accounts] == [4400]
    assert [line.number for line in groups['input_other_rate'].accounts] == [6815] and [line.number for line in groups['tax_account'].accounts] == [1406]


def test_a_sale_of_an_asset_on_6885_counts_in_81_and_the_reconciliation_still_adds_up(db):
    """6885 („Erlöse aus Verkäufen … bei Buchverlust“, 19 %) zählt wie im Export für das Steuerbüro als Umsatz (Kz 81). Die EÜR führt dieselbe Steuer als Vorsteuer (+19,00 €
    auf 1406, nicht auf 3806): Die Einordnung weicht ab, die Summe nicht – die Abstimmung zeigt 0,00 €."""
    mask(db, 'Probe-Giro', 6885, '119.00', '2026-03-10', '19', '19.00')
    report = db.get_ustva(2026, 3, 'monat')
    assert [(line.number, line.base, line.tax) for line in report.figures[0].accounts] == [(6885, minor('100.00'), minor('19.00'))] and report.input_tax.tax == 0
    connection = db._get_connection()
    try:
        cursor = connection.cursor()
        totals = db._euer_totals(cursor, db._euer_entries(cursor, '2026-03-01', '2026-03-31'))
    finally:
        connection.close()
    assert (totals.vat_minor, totals.input_tax_minor) == (0, minor('19.00'))
    assert (report.recon_page, report.recon_euer, report.recon_difference) == (minor('19.00'), minor('19.00'), 0)


def test_the_tax_of_a_group_in_the_euer_but_not_in_a_figure_shows_up_as_the_difference(db):
    """Die Steuer eines Erlöses zu 16 %: Die EÜR rechnet sie in die Umsatzsteuer, die Seite zu „Erlöse zu einem anderen Steuersatz“ – die Abstimmung zeigt sie."""
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    mask(db, 'Probe-Giro', 4400, '116.00', '2026-03-11', '16', '16.00')
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.recon_page, report.recon_euer, report.recon_difference) == (minor('19.00'), minor('35.00'), -minor('16.00'))
    assert report.recon_difference == -report.unassigned_tax


def test_an_unbooked_movement_of_0_00_is_not_counted_and_a_covered_transfer_neither(db):
    giro, zweit = account_id(db, 'Probe-Giro'), account_id(db, 'Probe-Zweit')
    raw_bank(db, '2026-03-02', giro, 0)
    mask(db, 'Probe-Giro', 1810, '-500.00', '2026-03-03')                                           # ein Übertrag mit Spiegel auf beiden Bewegungen
    mask(db, 'Probe-Zweit', 1800, '500.00', '2026-03-03')
    mask(db, 'Probe-Giro', 1810, '-300.00', '2026-03-04')                                           # ein Übertrag mit satzloser Gegenseite
    raw_bank(db, '2026-03-04', zweit, minor('300.00'))
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.unbooked_count, report.unbooked_amount) == (0, 0) and report.unassigned == ()      # die satzlose Gegenseite ist gedeckt, die Bewegung über 0,00 € zählt nicht
    raw_bank(db, '2026-03-05', giro, minor('-12.50'))
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.unbooked_count, report.unbooked_amount) == (1, minor('-12.50'))


def test_a_nonreadable_amount_is_a_reason_and_never_an_exception(db):
    """Text als Betrag (per SQL-Konsole schreibbar): unter einer Bankbewegung – dort bräche die EÜR selbst ab – und eigenständig; ebenso Text als Steuerbetrag, ein
    Bruchteil und Text als Steuersatz. Die Seite nennt den Satz, die Gegenrechnung lässt ihn aus."""
    giro = account_id(db, 'Probe-Giro')
    bank = raw_bank(db, '2026-03-12', giro, minor('1.00'))
    raw_entry(db, '2026-03-12', 4400, None, 'x', form='purpose_first', parent=bank)
    raw_entry(db, '2026-03-13', 4400, None, 'x', form='purpose_first')
    raw_entry(db, '2026-03-14', 4400, 1800, minor('119.00'), form='purpose_first', parent=raw_bank(db, '2026-03-14', giro, 1), tax='y', rate=0.19)
    raw_entry(db, '2026-03-15', 4400, 1800, 1190000.5, form='purpose_first', parent=raw_bank(db, '2026-03-15', giro, 1), rate=0.19, tax=190000)
    raw_entry(db, '2026-03-16', 4400, 1800, minor('119.00'), form='purpose_first', parent=raw_bank(db, '2026-03-16', giro, 1), tax=minor('19.00'), rate='x')
    with pytest.raises(TypeError):
        db.get_euer_data('2026-03-01', '2026-03-31')                              # die EÜR selbst bricht ab – die Voranmeldung nicht
    report = db.get_ustva(2026, 3, 'monat')
    groups = {group.key: group for group in report.unassigned}
    assert groups['unreadable'].count == 4 and (groups['unreadable'].net, groups['unreadable'].tax) == (0, 0) and report.unreadable_count == 4
    assert groups['tax_without_rate'].count == 1 and report.read_count == 5                                 # der Satz mit Text als Steuersatz: ein Steuersatz, der keiner ist
    assert (report.recon_page, report.recon_euer, report.recon_difference) == (0, minor('19.00'), -minor('19.00'))     # der Bruchteil: die EÜR schneidet ihn ab und zählt ihn, die Seite nennt ihn nur
    check_invariants(report)


def test_an_entry_with_a_nonreadable_amount_stays_out_of_the_euer_comparison_only(db):
    """Die Gegenrechnung mit der EÜR lässt einen Satz aus, den die EÜR gar nicht summieren kann; ein lesbarer daneben steht in beiden."""
    giro = account_id(db, 'Probe-Giro')
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    raw_entry(db, '2026-03-12', 4400, None, 'x', form='purpose_first', parent=raw_bank(db, '2026-03-12', giro, minor('1.00')))
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.figures[0].tax, report.recon_euer, report.recon_difference, report.unreadable_count) == (minor('19.00'), minor('19.00'), 0, 1)


# ═══ Steuerkonten, Vorsteuer zu anderen Sätzen, die Höhe der Steuer ═══════════
def groups_of(report):
    """Die Gründe unter „Nicht zugeordnet“ als ``{Grund: (Anzahl, Netto, Steuer)}``, Beträge in Minor Units."""
    return {group.key: (group.count, group.net, group.tax) for group in report.unassigned}


def test_a_payment_directly_on_a_tax_account_is_a_reason_even_without_a_tax_amount(db):
    """Die Maske direkt auf 1406 (Vorsteuer) und auf 3806 (Umsatzsteuer): kein Steuerbetrag, aber ein Betrag – das ist kein neutraler Satz, sondern steht mit dem Betrag
    unter „Satz direkt auf einem Steuerkonto“; in keiner Kennzahl."""
    mask(db, 'Probe-Giro', 1406, '-19.00', '2026-03-10')
    mask(db, 'Probe-Giro', 3806, '7.00', '2026-03-11')
    report = db.get_ustva(2026, 3, 'monat')
    assert groups_of(report) == {'tax_account': (2, minor('-12.00'), 0)} and report.neutral_count == 0
    assert [(line.number, line.count, line.base, line.tax) for line in report.unassigned[0].accounts] == [
        (1406, 1, minor('-19.00'), 0), (3806, 1, minor('7.00'), 0)]
    assert report.figures[0].base == 0 and report.figures[1].base == 0 and report.input_tax.tax == 0 and report.read_count == 2
    check_invariants(report)


def test_a_split_with_an_expense_and_a_booking_on_the_input_tax_account_names_the_booking_on_the_account(db):
    """Split 6815 −100,00 € (Aufwand ohne Steuerbetrag, neutral) und 1406 −19,00 € unter einer Bankbewegung über −119,00 €: Der Anteil auf dem Steuerkonto steht unter
    „Nicht zugeordnet“, die Bewegung geht auf."""
    giro = account_id(db, 'Probe-Giro')
    bank = raw_bank(db, '2026-03-10', giro, minor('-119.00'))
    raw_entry(db, '2026-03-10', 6815, 1800, minor('-100.00'), form='purpose_first', parent=bank)
    raw_entry(db, '2026-03-10', 1406, 1800, minor('-19.00'), form='purpose_first', parent=bank)
    report = db.get_ustva(2026, 3, 'monat')
    assert groups_of(report) == {'tax_account': (1, minor('-19.00'), 0)} and report.neutral_count == 1 and report.read_count == 2
    assert [line.number for line in report.unassigned[0].accounts] == [1406]
    assert (report.unbalanced_count, report.unbooked_count) == (0, 0) and report.input_tax.tax == 0
    check_invariants(report)


@pytest.mark.parametrize('number', [3820, 3830, 3841, 3800])
def test_a_payment_on_a_tax_settlement_account_is_no_payment_on_a_tax_account(db, number):
    """Zahllast- und Finanzamtskonten (3820 ff.) gehören nicht zu den Steuerkonten der Liste: Eine Zahlung ans Finanzamt ohne Steuerbetrag bleibt neutral."""
    mask(db, 'Probe-Giro', number, '-500.00', '2026-03-10')
    report = db.get_ustva(2026, 3, 'monat')
    assert report.unassigned == () and (report.read_count, report.neutral_count) == (1, 1)
    check_invariants(report)


def test_input_tax_at_another_rate_is_a_reason_and_never_kz_66(db):
    """20 % und 5,5 % Vorsteuer stehen mit ihrem Betrag unter „Vorsteuer zu einem anderen Steuersatz“ (mit dem Vorzeichen, wie gebucht); Kz 66 bleibt leer, die
    Abstimmung zeigt die Steuer als Abweichung (die EÜR führt sie auf der Vorsteuer)."""
    mask(db, 'Probe-Giro', 6815, '-120.00', '2026-03-05', '20', '-20.00')
    mask(db, 'Probe-Giro', 6850, '-105.50', '2026-03-06', '5.5', '-5.50')
    mask(db, 'Probe-Giro', 6815, '-119.00', '2026-03-07', '19', '-19.00')
    report = db.get_ustva(2026, 3, 'monat')
    assert groups_of(report) == {'input_other_rate': (2, minor('-200.00'), minor('-25.50'))}
    assert (report.input_tax.base, report.input_tax.tax) == (minor('100.00'), minor('19.00')) and report.prepayment == -minor('19.00')
    assert [(line.number, line.base, line.tax) for line in report.unassigned[0].accounts] == [(6815, minor('-100.00'), minor('-20.00')), (6850, minor('-100.00'), minor('-5.50'))]
    assert report.recon_euer == -minor('44.50') and report.recon_difference == minor('25.50') == -report.unassigned_tax
    check_invariants(report)


def test_the_tax_of_an_entry_that_does_not_fit_its_rate_is_a_reason_for_revenue_and_input_tax(db):
    """4400 zu 19 % mit 7,00 € Steuer und ein Satz, dessen Betrag die Steuer ist: Die Seite zählt sie nicht in 81, die Vorsteuer entsprechend nicht in 66."""
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '7.00')
    mask(db, 'Probe-Giro', 4400, '19.00', '2026-03-11', '19', '19.00')
    mask(db, 'Probe-Giro', 6815, '-119.00', '2026-03-12', '19', '-7.00')
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-13', '19', '19.00')
    report = db.get_ustva(2026, 3, 'monat')
    assert groups_of(report) == {'rate_tax_mismatch': (3, 0, minor('19.00'))}                          # Netto 112,00 + 0,00 − 112,00, Steuer 7,00 + 19,00 − 7,00
    assert [(line.number, line.count, line.base, line.tax) for line in report.unassigned[0].accounts] == [
        (4400, 2, minor('112.00'), minor('26.00')), (6815, 1, -minor('112.00'), -minor('7.00'))]
    assert (report.figures[0].base, report.figures[0].tax) == (minor('100.00'), minor('19.00')) and report.input_tax.tax == 0
    check_invariants(report)


@pytest.mark.parametrize('paid, tax, fits', [('119.02', '19.02', True), ('118.98', '18.98', True), ('119.03', '19.03', False), ('118.97', '18.97', False)])
def test_two_cents_of_rounding_stay_in_kz_81_and_three_do_not(db, paid, tax, fits):
    """Über die Maske: Die Steuer weicht um zwei Cent von Netto × Satz ab (100,00 € netto, 19 %) – das bleibt in 81; bei drei Cent steht der Satz unter „Steuersatz und
    Steuerbetrag passen nicht zusammen“."""
    mask(db, 'Probe-Giro', 4400, paid, '2026-03-10', '19', tax)
    report = db.get_ustva(2026, 3, 'monat')
    if fits:
        assert report.unassigned == () and (report.figures[0].base, report.figures[0].tax) == (minor('100.00'), minor(tax))
    else:
        assert groups_of(report) == {'rate_tax_mismatch': (1, minor('100.00'), minor(tax))} and report.figures[0].base == 0
    check_invariants(report)


def test_partial_payments_discounts_and_several_rates_booked_by_the_app_fit_their_rates(db):
    """Wege der App, die die Steuer anteilig auf den Cent rechnen: eine Teilzahlung vor der schließenden mit Skonto (19 %), dasselbe zu 7 % und eine Rechnung mit zwei Sätzen
    (19 % × 100,00 und 7 % × 50,00), je mit einer Teilzahlung und dem Skonto im Rest. Nichts rutscht unter „Steuersatz und Steuerbetrag passen nicht zusammen“; von Hand
    gerechnet: 81 = 98,00 + 98,00 (Rest nach Skonto) mit 18,62 + 18,62, 86 = 98,00 + 49,00 mit 6,86 + 3,43."""
    full = invoice_of(db, 'TP-19', 119.0, day='2026-03-03')
    pay_invoice(db, full, '33.33', '2026-03-12')
    pay_invoice(db, full, '83.29', '2026-03-20', skonto=True)
    reduced = invoice_of(db, 'TP-07', 107.0, rate=0.07, day='2026-03-04')
    pay_invoice(db, reduced, '33.33', '2026-03-13')
    pay_invoice(db, reduced, '71.53', '2026-03-21', skonto=True)
    mixed = mixed_invoice(db, number='TP-MIX', day='2026-03-05')
    pay_invoice(db, mixed, '100.00', '2026-03-14')
    pay_invoice(db, mixed, '69.05', '2026-03-22', skonto=True)
    report = db.get_ustva(2026, 3, 'monat')
    assert report.unassigned == (), groups_of(report)
    figure_81, figure_86 = report.figures
    assert (figure_81.base, figure_81.tax) == (minor('196.00'), minor('37.24')) and (figure_86.base, figure_86.tax) == (minor('147.00'), minor('10.29'))
    assert report.recon_difference == 0
    check_invariants(report)


# ═══ Eigenständige Sätze: nur die Forderung einer Rechnung ist keiner ════════
def test_only_the_receivable_pair_is_no_standalone_entry_and_every_other_transfer_stays_visible(db):
    """Die Forderung einer Rechnung („10000 an 4405“) und die Gegenbuchung ihrer Stornorechnung (Debitor und Wartekonto, in jeder Richtung, mit und ohne Steuer) bewegen
    kein Geld: Sie sind kein Satz. Jede andere Umbuchung mit Debitor oder Wartekonto bleibt sichtbar – auch „2100 an 4405“ und „6815 an 10000“ mit Steuer."""
    raw_entry(db, '2026-03-10', 10000, 4405, minor('119.00'), form='transfer', rate=0.19, tax=minor('19.00'))              # die Forderung
    raw_entry(db, '2026-03-11', 4405, 10000, minor('-119.00'), form='transfer', rate=0.19, tax=minor('-19.00'))            # die Gegenbuchung
    raw_entry(db, '2026-03-12', 4340, 10000, minor('-50.00'), form='transfer')                                            # dasselbe Paar ohne Steuer
    raw_entry(db, '2026-03-13', 10000, 4345, minor('107.00'), form='transfer', rate=0.07, tax=minor('7.00'))
    hidden = db.get_ustva(2026, 3, 'monat')
    assert (hidden.read_count, hidden.unassigned, hidden.neutral_count) == (0, (), 0)
    raw_entry(db, '2026-03-14', 2100, 4405, minor('119.00'), form='transfer', rate=0.19, tax=minor('19.00'))              # privat an Wartekonto
    raw_entry(db, '2026-03-15', 6815, 10000, minor('-119.00'), form='transfer', rate=0.19, tax=minor('-19.00'))           # Aufwand an Debitor
    raw_entry(db, '2026-03-16', 4340, 4345, minor('10.00'), form='transfer')                                              # Wartekonto an Wartekonto, ohne Steuer
    raw_entry(db, '2026-03-17', 4345, 4405, minor('10.65'), form='transfer', rate=0.07, tax=minor('0.65'))                # Wartekonto an Wartekonto, mit Steuer
    report = db.get_ustva(2026, 3, 'monat')
    assert report.read_count == 4 and report.neutral_count == 1
    assert groups_of(report) == {'private_transfer': (1, minor('100.00'), minor('19.00')), 'no_bank': (2, minor('-90.00'), minor('-18.35'))}
    assert [(line.number, line.count) for line in report.unassigned[1].accounts] == [(4405, 1), (10000, 1)]
    check_invariants(report)
    assert report.read_count == counted_by_sql(db, report)


# ═══ Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben ═════════════
def test_a_bank_movement_whose_entries_do_not_add_up_is_counted_with_its_rest(db):
    """Bankbewegung +238,00 € mit EINEM Kind 4400 (119,00 €, 19 %): Das Kind steht in 81, die fehlenden 119,00 € stehen als Rest unter den unstimmigen Bankbewegungen –
    außerhalb der Invariante, nicht in der Summe „Nicht zugeordnet“."""
    bank, _child = bank_with_child(db, '2026-03-10', '238.00', '2026-03-10', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.unbalanced_count, report.unbalanced_amount) == (1, minor('119.00'))
    assert (report.figures[0].base, report.figures[0].tax) == (minor('100.00'), minor('19.00')) and report.unassigned == ()
    assert (report.unassigned_net, report.unassigned_tax, report.unbooked_count) == (0, 0, 0)
    check_invariants(report)
    assert [(row[0], row[3]) for row in db.find_unbalanced_splits('2026-03-01', '2026-03-31')] == [(bank, Decimal('119.00'))]      # dieselbe Auswahl und Regel


def test_a_bank_movement_that_adds_up_is_not_counted_and_the_sum_of_the_rests_has_no_sign(db):
    bank_with_child(db, '2026-03-10', '119.00', '2026-03-10', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))                  # geht auf
    bank_with_child(db, '2026-03-11', '300.00', '2026-03-11', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))                  # Rest +181,00
    bank_with_child(db, '2026-03-12', '-100.00', '2026-03-12', '-119.00', 6815, tax_rate=0.19, tax_amount=Decimal('-19.00'))               # Rest −19,00 (der Satz ist mehr)
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.unbalanced_count, report.unbalanced_amount) == (2, minor('200.00'))
    check_invariants(report)


def test_the_unbalanced_bank_movements_follow_the_period_of_the_movement_or_of_one_of_its_entries(db):
    """Wie die Prüfung der Splits: Eine Bankbewegung ist betroffen, wenn sie selbst ODER einer ihrer Sätze im Zeitraum liegt – im März (Bewegung) und im April (Satz), im
    Quartal einmal; eine Bewegung außerhalb mit Sätzen außerhalb nirgends."""
    bank_with_child(db, '2026-03-31', '238.00', '2026-04-02', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    bank_with_child(db, '2025-12-10', '238.00', '2025-12-10', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    counts = {(month, kind): db.get_ustva(2026, month, kind).unbalanced_count for month, kind in ((3, 'monat'), (4, 'monat'), (5, 'monat'), (3, 'quartal'), (6, 'quartal'))}
    assert counts == {(3, 'monat'): 1, (4, 'monat'): 1, (5, 'monat'): 0, (3, 'quartal'): 1, (6, 'quartal'): 1}
    assert db.get_ustva(2025, 12, 'monat').unbalanced_count == 1


def test_an_unreadable_amount_under_a_bank_movement_never_breaks_the_count_of_the_unbalanced_ones(db):
    """Text als Betrag eines Satzes (per SQL-Konsole schreibbar) bricht ``find_unbalanced_splits`` ab – die Voranmeldung nicht: Der Satz steht unter „Betrag nicht
    lesbar“, die lesbaren Sätze derselben Bewegung gehen in die Rechnung ein."""
    giro = account_id(db, 'Probe-Giro')
    bank = raw_bank(db, '2026-03-12', giro, minor('238.00'))
    raw_entry(db, '2026-03-12', 4400, 1800, minor('119.00'), form='purpose_first', parent=bank, rate=0.19, tax=minor('19.00'))
    raw_entry(db, '2026-03-12', 4400, 1800, 'x', form='purpose_first', parent=bank)
    with pytest.raises(TypeError):
        db.find_unbalanced_splits('2026-03-01', '2026-03-31')
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.unbalanced_count, report.unbalanced_amount, report.unreadable_count) == (1, minor('119.00'), 1)
    check_invariants(report)


def test_the_page_names_the_unbalanced_movements_with_the_warning_on_top(db):
    own_company(db)
    bank_with_child(db, '2026-03-10', '238.00', '2026-03-10', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    page = page_of(db, 2026, 3, 'monat')
    rows = tables_of(page)[2]
    assert 'Keine Posten.' not in rows
    assert rows[2:] == [f'{UNBALANCED_LABEL} | 1 | 119,00 | ', 'Summe Nicht zugeordnet |  | 0,00 | 0,00']
    assert html_lib.escape(USTVA_UNASSIGNED_TEXT, quote=True) in page and html_lib.escape(UNBALANCED_LABEL, quote=True) in page
    balanced = page_of(db, 2026, 4, 'monat')
    assert UNBALANCED_LABEL not in balanced and html_lib.escape(USTVA_UNASSIGNED_TEXT, quote=True) not in balanced and 'Keine Posten.' in balanced


# ═══ Die Abstimmung, wenn die EÜR keine ganze Zahl liefert ════════════════════
def test_the_reconciliation_is_left_open_when_the_euer_sum_is_no_whole_number():
    """Eine Kommazahl als Steuerbetrag im Bestand (REAL): Die EÜR summiert sie als Kommazahl, die Seite schneidet sie nicht ab – ``recon_euer`` und
    ``recon_difference`` sind ``None``, ``recon_page`` bleibt."""
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19)]
    for vat, input_tax in ((190000.5, 0), (190000, -95000.5), (190000.0, 0), (190000, -95000.0), (float('nan'), 0)):
        report = build_ustva(2026, 3, 'monat', items, {}, vat, input_tax, (0, 0))
        assert (report.recon_page, report.recon_euer, report.recon_difference) == (minor('19.00'), None, None), (vat, input_tax)
        check_invariants(report)
    whole = build_ustva(2026, 3, 'monat', items, {}, 190000, -95000, (0, 0))
    assert (whole.recon_euer, whole.recon_difference) == (95000, minor('19.00') - 95000)
    assert [format_recon(value) for value in (None, 0, minor('1234.5'), -minor('0.05'))] == [RECON_UNKNOWN, '0,00', '1.234,50', '-0,05'] and RECON_UNKNOWN == '–'


@pytest.mark.parametrize('where', ['vat', 'input_tax'])
def test_a_fractional_tax_amount_in_the_books_gives_a_dash_in_the_reconciliation_on_the_page(db, where):
    """Die Abstimmung steht mit „–“ da, wo die EÜR keine ganze Zahl liefert: bei der Umsatzsteuer (Erlös) und bei der Vorsteuer (Aufwand) mit Steuerbetrag 190000,5 Minor
    Units; der Satz selbst steht unter „Betrag nicht lesbar“."""
    own_company(db)
    giro = account_id(db, 'Probe-Giro')
    number, amount, tax = (4400, minor('119.00'), 190000.5) if where == 'vat' else (6815, -minor('119.00'), -190000.5)
    raw_entry(db, '2026-03-12', number, 1800, amount, form='purpose_first', parent=raw_bank(db, '2026-03-12', giro, amount), rate=0.19, tax=tax)
    report = db.get_ustva(2026, 3, 'monat')
    assert (report.recon_page, report.recon_euer, report.recon_difference, report.unreadable_count) == (0, None, None, 1)
    check_invariants(report)
    reconciliation = tables_of(page_of(db, 2026, 3, 'monat'))[1]
    assert reconciliation == ['Abstimmung', 'Steuer der Kennzahlen (81 + 86 − 66) | Umsatzsteuer abzüglich Vorsteuer laut EÜR | Abweichung', '0,00 | – | –']


def test_the_fixed_texts_of_the_page():
    assert USTVA_SIGN_TEXT == 'Vorzeichen unter „Nicht zugeordnet“ wie gebucht.'
    assert USTVA_DATE_TEXT == ('Die Seite rechnet nach dem Datum der Bankbewegung bzw. der Kassenbuchung. Auch die Vorsteuer zählt hier mit der Zahlung, '
                               'nicht mit dem Datum der Rechnung.')


# ═══ get_euer_data ist unverändert ════════════════════════════════════════════
def euer_spans():
    spans = []
    for year in (2025, 2026):
        spans.append((f'{year}-01-01', f'{year}-12-31'))
        spans += [(f'{year}-{month:02d}-01', f'{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}') for month in range(1, 13)]
        spans += [(f'{year}-{last - 2:02d}-01', f'{year}-{last:02d}-{calendar.monthrange(year, last)[1]:02d}') for last in (3, 6, 9, 12)]
    return spans


def euer_digest(db):
    """SHA-256 über ``get_euer_data`` für jeden Monat, jedes Quartal und jedes Jahr 2025 und 2026: alle Zeilen, nur die Umbuchungen Anlage ↔ Erfolgskonto, mit dem
    ersten Konto und mit allen Konten als Filter."""
    ids = [account[0] for account in db.fetch_accounts()]
    payload = [[span, db.get_euer_data(*span), db.get_euer_data(*span, only_asset_transfers=True), db.get_euer_data(*span, account_ids=ids[:1]),
                db.get_euer_data(*span, account_ids=ids)] for span in euer_spans()]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


# Genommen am Stand 2.10.4, VOR dem Herausziehen von ``_euer_entries``/``_euer_totals`` aus ``get_euer_data`` (an einem Abzug des Standes, nicht am Arbeitsbaum). Ändert
# sich ein Wert, ist der Code zu ändern, nie der Schnappschuss.
EUER_DIGESTS = {
    'susa': '9ed23ad3f5eb795a5e048972201eec2d04ce5748d38dc0a01cadbc0792ed85ab',
    'random1': '16dc5b2db3141063651b8d770e7c341150dc2f1f8c3c8eb48db93fd9a2d332f2',
    'random2': 'a7cc513580f8885cf7f222f4441f7284f3af53f3a68903b261545683084d3ca4',
    'random3': 'c591891717289ba197cd91847bb0039da8c5e6973de190afb5453ef352fb87b1',
    'random4': '7aef12a94c8207ca0473b751e53e19fc2f2bfa10a683a9e71f0e035613f154f7',
    'random5': 'cd0147e4ba8886341aae53e8ebd7b9211e93de0271fb2d9b028740b86c6f7354',
}


@pytest.mark.parametrize('name', sorted(EUER_DIGESTS))
def test_the_euer_is_unchanged_by_taking_apart_get_euer_data(worlds, name):
    assert euer_digest(worlds[name]) == EUER_DIGESTS[name]


@pytest.mark.parametrize('name', WORLD_NAMES)
def test_the_totals_carry_the_whole_numbers_of_the_euer(worlds, name):
    """``vat_minor`` ist die virtuelle Zeile 3806 der EÜR (hier ist keine echte 3806 bebucht), ``input_tax_minor`` die Summe ihrer Vorsteuerzeilen 1401, 1406 und 1409 –
    als ganze Zahlen, ohne ``float``."""
    db = worlds[name]
    for span in euer_spans()[:15] if name != 'demo' else euer_spans():
        rows = db.get_euer_data(*span)
        connection = db._get_connection()
        try:
            cursor = connection.cursor()
            totals = db._euer_totals(cursor, db._euer_entries(cursor, *span))
        finally:
            connection.close()
        assert isinstance(totals.vat_minor, int) and isinstance(totals.input_tax_minor, int)
        assert totals.vat_minor == sum(to_minor(total) for number, _name, total in rows if number == 3806), (name, span)
        assert totals.input_tax_minor == sum(to_minor(total) for number, _name, total in rows if number in (1401, 1406, 1409)), (name, span)


def test_the_entries_carry_the_dates_and_the_section(db):
    """``_euer_entries`` hängt jedem Satz Sektion und beide Daten an: Kind einer Bankbewegung (1), Satz der Kasse (2), Umbuchung der Abschreibung (3)."""
    bank_with_child(db, '2026-03-31', '119.00', '2026-03-30', '119.00', 4400, tax_rate=0.19, tax_amount=Decimal('19.00'))
    db.insert_booking('2026-03-03', Decimal('-5.95'), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1600), booking_type='entry', tax_rate=0.19, tax_amount=Decimal('-0.95'))
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
    connection = db._get_connection()
    try:
        scan = db._euer_entries(connection.cursor(), '2026-01-01', '2026-12-31')
    finally:
        connection.close()
    found = {(item.section, item.parent_date, item.entry_date) for item in scan.entries.values()}
    assert found == {(1, '2026-03-31', '2026-03-30'), (2, None, '2026-03-03'), (3, None, '2026-12-31')}
    assert scan.liquid_coa_ids and scan.asset_coa_ids and scan.pl_coa_ids and coa(db, 520) in scan.asset_coa_ids and coa(db, 6220) in scan.pl_coa_ids


# ═══ Die Seite ════════════════════════════════════════════════════════════════
def tables_of(page):
    """Die Tabellen der Seite: je Tabelle die Zeilen, die Zellen durch ``|`` getrennt (der sichtbare Text)."""
    result = []
    for table in re.findall(r'<table[^>]*>(.*?)</table>', page, flags=re.S):
        rows = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table, flags=re.S):
            cells = [html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
            rows.append(' | '.join(cells))
        result.append(rows)
    return result


def visible_text(page):
    return html_lib.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<script>.*?</script>', '', page, flags=re.S)))


def page_of(db, year, month, kind, date_from=None, date_to=None, today=FIXED):
    return PageUstva(db, UstvaQuery(year, month, kind), date_from, date_to, today)


def test_the_page_shows_the_figures_of_a_quarter_the_reconciliation_and_no_item(worlds):
    page = page_of(worlds['susa'], 2026, 3, 'quartal')
    figures, reconciliation, unassigned = tables_of(page)
    assert figures == [
        'Kz | Bezeichnung | Bemessungsgrundlage € | in vollen € | Steuer €',
        '81 | ▶ Steuerpflichtige Umsätze 19 % | 400,00 | 400 | 76,00', ' | 4400 Erlöse 19% USt | 400,00 |  | 76,00',
        '86 | ▶ Steuerpflichtige Umsätze 7 % | 100,00 | 100 | 7,00', ' | 4300 Erlöse 7% USt | 100,00 |  | 7,00',
        ' | Umsatzsteuer (81 und 86) |  |  | 83,00',
        '66 | ▶ Vorsteuer aus Rechnungen anderer Unternehmer |  |  | 21,57', ' | 6805 Telefon |  |  | 1,92', ' | 6815 Bürobedarf |  |  | 16,15',
        ' | 6850 Sonstiger Betriebsbedarf |  |  | 3,50',
        '83 | Verbleibende Vorauszahlung (Überschuss mit Minus) |  |  | 61,43']
    assert reconciliation == ['Abstimmung', 'Steuer der Kennzahlen (81 + 86 − 66) | Umsatzsteuer abzüglich Vorsteuer laut EÜR | Abweichung', '61,43 | 61,43 | 0,00']
    assert unassigned == ['Nicht zugeordnet', 'Grund | Anzahl | Netto € | Steuer €', 'Keine Posten.', 'Summe Nicht zugeordnet |  | 0,00 | 0,00']
    assert 'Umsatzsteuer-Voranmeldung – 1. Quartal 2026 (01.01.2026 – 31.03.2026)' in page
    for text in (USTVA_HINT_TEXT, USTVA_DATE_TEXT, USTVA_ROUNDING_TEXT, USTVA_NOT_SHOWN_TEXT, USTVA_SIGN_TEXT, USTVA_RECON_TEXT):
        assert html_lib.escape(text, quote=True) in page, text
    for text in (USTVA_OPEN_PERIOD_TEXT, USTVA_UNASSIGNED_TEXT, USTVA_SMALL_BUSINESS_TEXT, USTVA_SOLL_TEXT, USTVA_UNSET_TEXT):
        assert html_lib.escape(text, quote=True) not in page, text


def test_the_page_of_a_month_has_its_title_and_the_empty_figures_stand_there_with_zero(db):
    own_company(db)
    page = page_of(db, 2026, 3, 'monat')
    assert 'Umsatzsteuer-Voranmeldung – März 2026 (01.03.2026 – 31.03.2026)' in page
    figures, _reconciliation, unassigned = tables_of(page)
    assert figures[1:] == ['81 | ▶ Steuerpflichtige Umsätze 19 % | 0,00 | 0 | 0,00', '86 | ▶ Steuerpflichtige Umsätze 7 % | 0,00 | 0 | 0,00',
                           ' | Umsatzsteuer (81 und 86) |  |  | 0,00', '66 | ▶ Vorsteuer aus Rechnungen anderer Unternehmer |  |  | 0,00',
                           '83 | Verbleibende Vorauszahlung (Überschuss mit Minus) |  |  | 0,00']
    assert unassigned[2:] == ['Keine Posten.', 'Summe Nicht zugeordnet |  | 0,00 | 0,00']


def test_the_unassigned_table_lists_every_group_the_bank_line_and_the_sum(db):
    own_company(db)
    reasons_world(db)
    page = page_of(db, 2026, 3, 'monat')
    rows = tables_of(page)[2]
    assert rows[:2] == ['Nicht zugeordnet', 'Grund | Anzahl | Netto € | Steuer €']
    assert 'Keine Posten.' not in rows
    assert rows == [
        'Nicht zugeordnet', 'Grund | Anzahl | Netto € | Steuer €',
        '▶ Erlöse ohne Steuer (0 %, steuerfrei, § 19 oder ohne Satz) | 2 | 70,00 | 0,00', '4100 Steuerfreie Umsätze § 4 Nr. 8 ff. UStG | 1 | 30,00 | 0,00',
        '4184 Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG | 1 | 40,00 | 0,00',
        '▶ Erlöse zu einem anderen Steuersatz | 1 | 100,00 | 16,00', '4400 Erlöse 19% USt | 1 | 100,00 | 16,00',
        '▶ Vorsteuer zu einem anderen Steuersatz | 1 | -100,00 | -20,00', '6815 Bürobedarf | 1 | -100,00 | -20,00',
        '▶ Erlöse auf Konto 4185 | 2 | 150,00 | 19,00', '4185 Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG | 2 | 150,00 | 19,00',
        '▶ Steuerbetrag ohne Steuersatz | 1 | -50,00 | -8,00', '6815 Bürobedarf | 1 | -50,00 | -8,00',
        '▶ Steuersatz und Steuerbetrag passen nicht zusammen | 1 | 119,00 | 0,00', '4400 Erlöse 19% USt | 1 | 119,00 | 0,00',
        '▶ Vorsteuer auf Privatkonten | 1 | -100,00 | -19,00', '2100 Privatentnahmen allgemein | 1 | -100,00 | -19,00',
        '▶ Satz mit anderem Datum als seine Bankbewegung (anderer Zeitraum) | 1 | 100,00 | 19,00', '4400 Erlöse 19% USt | 1 | 100,00 | 19,00',
        '▶ Umbuchung Privat ↔ Erfolgskonto ohne Geldfluss | 2 | 0,00 | 0,00',
        '4645 Verwendung von Gegenständen für Zwecke außerhalb des Unternehmens 19 % USt (KFZ-Nutzung) | 2 | 0,00 | 0,00',
        '▶ Satz ohne Bankbewegung, den die EÜR nicht zählt | 1 | 100,00 | 19,00', '4400 Erlöse 19% USt | 1 | 100,00 | 19,00',
        '▶ Umbuchung, die die Seite nicht zuordnet | 1 | 9,00 | 1,00', '3160 Verbindlichkeiten ggü. Kreditinstituten – 1-5 Jahre | 1 | 9,00 | 1,00',
        '▶ Steuer auf einem Konto ohne Umsatz- oder Vorsteuer | 1 | 100,00 | 5,00', '3150 Verbindlichkeiten gegenüber Kreditinstituten | 1 | 100,00 | 5,00',
        '▶ Satz direkt auf einem Steuerkonto | 1 | -19,00 | 0,00', '1406 Abziehbare Vorsteuer 19% | 1 | -19,00 | 0,00',
        '▶ Satz auf einem Wartekonto (4340, 4345, 4405) | 1 | 100,00 | 19,00', '4405 Erlöse 19% USt noch offen | 1 | 100,00 | 19,00',
        '▶ Satz ohne Konto | 1 | 10,00 | 0,00', '– | 1 | 10,00 | 0,00',
        '▶ Betrag nicht lesbar | 2 | 0,00 | 0,00', '– | 2 | 0,00 | 0,00',
        'Bankbewegungen ohne Buchungssatz | 1 | 77,00 | ',                                       # hinter den Gründen, ohne Steuer
        'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben | 2 | 15,00 | ',           # dahinter: Zahl und Summe der Reste
        'Summe Nicht zugeordnet |  | 589,00 | 51,00']                                           # ohne die Bankbewegungen: 70 + 100 − 100 + 150 − 50 + 119 − 100 + 100 + 0 + 100 + 9 + 100 − 19 + 100 + 10
    for text in (USTVA_UNASSIGNED_TEXT, USTVA_SMALL_BUSINESS_TEXT):
        assert html_lib.escape(text, quote=True) in page
    dom = Dom(page)
    groups = {attrs['data-ustva-parent'] for tag, attrs in dom.tags if tag == 'tr' and 'child-row' in attrs.get('class', '') and attrs['data-ustva-parent'].startswith('n-')}
    assert groups == {f'n-{key}' for key in UNASSIGNED_REASONS}                                      # jeder Grund hat seine Konten zum Aufklappen


def test_a_bank_movement_without_an_entry_alone_is_a_reason_for_the_note_and_not_for_the_sum(db):
    own_company(db)
    raw_bank(db, '2026-03-11', account_id(db, 'Probe-Giro'), minor('77.00'))
    page = page_of(db, 2026, 3, 'monat')
    rows = tables_of(page)[2]
    assert 'Keine Posten.' not in rows and 'Bankbewegungen ohne Buchungssatz | 1 | 77,00 | ' in rows and rows[-1] == 'Summe Nicht zugeordnet |  | 0,00 | 0,00'
    assert html_lib.escape(USTVA_UNASSIGNED_TEXT, quote=True) in page


def test_the_open_period_is_named_only_while_it_runs(db):
    own_company(db)
    note = html_lib.escape(USTVA_OPEN_PERIOD_TEXT, quote=True)
    assert note in page_of(db, 2026, 11, 'monat') and note in page_of(db, 2026, 12, 'quartal') and note in page_of(db, 2026, 12, 'monat')
    assert note not in page_of(db, 2026, 10, 'monat') and note not in page_of(db, 2026, 9, 'quartal') and note not in page_of(db, 2026, 3, 'monat')
    assert note in page_of(db, 2026, 11, 'monat', today=datetime.date(2026, 11, 29)) and note not in page_of(db, 2026, 11, 'monat', today=datetime.date(2026, 11, 30))
    assert note in page_of(db, 2026, 3, 'quartal', today=datetime.date(2026, 3, 30)) and note not in page_of(db, 2026, 3, 'quartal', today=datetime.date(2026, 3, 31))


def test_the_page_shows_the_kind_form_with_the_chosen_value_and_the_pdf_button(db):
    own_company(db)
    for kind in ('monat', 'quartal'):
        page = page_of(db, 2026, 3, kind, '2026-03-01', '2026-03-31')
        dom = Dom(page)
        assert dom.one('form', action='/ustva')['method'] == 'get'
        assert [attrs['name'] for attrs in dom.all('select')] == ['art'] and dom.one('select', name='art')['onchange'] == 'this.form.submit()'
        assert dom.one('input', type='hidden', name='from')['value'] == '2026-03-01' and dom.one('input', type='hidden', name='to')['value'] == '2026-03-31'
        options = [attrs for tag, attrs in dom.tags if tag == 'option']
        assert [attrs['value'] for attrs in options] == ['monat', 'quartal'] and [attrs['value'] for attrs in options if 'selected' in attrs] == [kind]
        assert 'Monat</option>' in page and 'Quartal</option>' in page and 'Anmeldezeitraum' in page and 'Anzeigen' not in page
        assert 'onclick="ustvaPdf()"' in page and 'onclick="ustvaToggleAll()"' in page


def test_without_a_common_period_the_menu_row_holds_the_period_of_the_query(db):
    own_company(db)
    assert header3(page_of(db, 2026, 3, 'quartal')) == period_filter_widget('2026-01-01', '2026-03-31', '/ustva', extra_params={'art': 'quartal'}, months=MONTHS_ALWAYS, today=FIXED)
    assert header3(page_of(db, 2026, 3, 'monat')) == period_filter_widget('2026-03-01', '2026-03-31', '/ustva', extra_params={'art': None}, months=MONTHS_ALWAYS, today=FIXED)


@pytest.mark.parametrize('kind, common', [('monat', ('2026-03-01', '2026-03-31')), ('quartal', ('2026-03-01', '2026-03-31')), ('quartal', ('2026-01-01', '2026-03-31')),
                                          ('monat', ('2026-01-01', '2026-12-31'))])
def test_the_menu_row_is_what_the_one_helper_builds(db, kind, common):
    """Wie auf jeder Seite mit Zeitraum: Der Inhalt von ``.header3`` ist genau das, was ``period_filter_widget`` liefert – mit Monatsknöpfen der Art ``always`` und der
    Art im Zusatzparameter (bei ``monat`` fehlt er); die Auswahl „Anmeldezeitraum“ steht nicht dort, sondern im Inhalt."""
    own_company(db)
    page = page_of(db, 2026, 3, kind, *common)
    row = header3(page)
    assert row == period_filter_widget(*common, '/ustva', extra_params={'art': None if kind == 'monat' else 'quartal'}, months=MONTHS_ALWAYS, today=FIXED)
    for forbidden in ('<form', '<select', '<option', 'coloredButton', 'style=', 'Anzeigen'):
        assert forbidden not in row, forbidden
    assert ('art=quartal' in row) is (kind == 'quartal') and len(re.findall(r'>(\d{4})</button>', row)) == 5
    assert page.index('<form method="get" action="/ustva"') > page.index('<div class="rectRounded">')


def test_the_submenu_lists_the_voranmeldung_as_the_fifth_page(db):
    assert [key for key, _path, _label in REPORT_PAGES] == ['bwa', 'euer', 'susa', 'liquiditaet', 'ustva']
    assert REPORT_PAGES[-1] == ('ustva', '/ustva', 'USt-Voranmeldung')
    assert reports_submenu('ustva') == ('<a href="/bwa">BWA</a> | <a href="/euer">EÜR</a> | <a href="/susa">Summen- und Saldenliste</a> | '
                                        '<a href="/liquiditaet">Liquidität</a> | <span id="ActivePage">USt-Voranmeldung</span>')
    page = page_of(db, 2026, 3, 'monat')
    assert reports_submenu('ustva') in page and '<span id="ActivePage">Auswertungen</span>' in Header1('bwa')
    assert page.index('Auswertungen</span>') < page.index('<span id="ActivePage">USt-Voranmeldung</span>') < page.index("<div class='header3'>")
    for key, _path, _label in REPORT_PAGES[:-1]:
        assert '<span id="ActivePage">USt-Voranmeldung</span>' not in reports_submenu(key) and '<a href="/ustva">USt-Voranmeldung</a>' in reports_submenu(key)


@pytest.mark.parametrize('method, text, other', [('soll', USTVA_SOLL_TEXT, USTVA_UNSET_TEXT), (None, USTVA_UNSET_TEXT, USTVA_SOLL_TEXT)])
def test_without_ist_taxation_the_page_shows_one_fixed_sentence_and_no_number(db, monkeypatch, method, text, other):
    """Bei Soll-Versteuerung und ohne festgelegte Besteuerungsart steht nur der feste Satz da, dazu der Satz zum Kleinunternehmer; ``get_ustva`` läuft nicht (der
    Test lässt es scheitern), und es gibt keine Tabelle und keinen Knopf zum PDF."""
    if method:
        own_company(db, method)
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')

    def refuse(*args, **kwargs):
        raise AssertionError('get_ustva darf nicht laufen')
    monkeypatch.setattr(Database, 'get_ustva', refuse)
    page = page_of(db, 2026, 3, 'quartal')
    assert html_lib.escape(text, quote=True) in page and html_lib.escape(other, quote=True) not in page
    assert html_lib.escape(USTVA_SMALL_BUSINESS_TEXT, quote=True) in page and 'class="hint warnColor"' in page
    for absent in ('bwaTable', 'reportTable', 'ustvaPdf', 'ustvaToggle', 'USTVA_DATA', '<script>\nvar', '19,00', '100,00', 'Nicht zugeordnet'):
        assert absent not in page, absent
    assert 'Umsatzsteuer-Voranmeldung – 1. Quartal 2026 (01.01.2026 – 31.03.2026)' in page and reports_submenu('ustva') in page


def test_an_invalid_vat_method_in_the_books_counts_as_not_set(db):
    own_company(db)
    raw(db, "UPDATE Contacts SET VatMethod = 'IST' WHERE ContactType = 'own'")
    page = page_of(db, 2026, 3, 'monat')
    assert html_lib.escape(USTVA_UNSET_TEXT, quote=True) in page and 'bwaTable' not in page


def test_the_small_business_note_stands_with_the_ist_figures_only_when_4184_has_revenue(db):
    own_company(db)
    note = html_lib.escape(USTVA_SMALL_BUSINESS_TEXT, quote=True)
    assert note not in page_of(db, 2026, 3, 'monat')
    mask(db, 'Probe-Giro', 4184, '40.00', '2026-03-03')
    assert note in page_of(db, 2026, 3, 'monat') and note not in page_of(db, 2026, 4, 'monat')


def test_the_page_uses_no_inline_style_and_only_var_and_function(worlds):
    page = page_of(worlds['susa'], 2026, 3, 'quartal')
    own = page[page.index('<div class="rectRounded">'):page.index('<div id="app_confirm"')]
    assert ' style=' not in own and 'innerHTML' not in own
    scripts = Dom(page).scripts
    script = scripts[1]                                                     # hinter dem Skript der Menüzeile (gotoPeriod)
    assert 'gotoPeriod' in scripts[0] and script.startswith('\nvar USTVA_DATA = ')
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval(', 'BWA_DATA', 'LIQUIDITY_DATA'):
        assert forbidden not in script, forbidden
    assert re.findall(r'^function (\w+)\(', script, flags=re.M) == ['ustvaPdf', 'ustvaSetGroup', 'ustvaToggle', 'ustvaToggleAll']
    assert 'data-ustva-parent' in script and 'ustva-icon-' in script and 'substring(11)' in script and len('ustva-icon-') == 11


def test_the_pdf_address_stands_only_in_the_script_block(worlds):
    for kind, month in (('monat', 3), ('quartal', 3)):
        page = page_of(worlds['susa'], 2026, month, kind)
        assert '/ustva/pdf' not in re.sub(r'<script>.*?</script>', '', page, flags=re.S)
        data = re.search(r'var USTVA_DATA = (.*?);\n', Dom(page).scripts[1], re.S).group(1)
        assert json.loads(data) == {'pdf': f'/ustva/pdf?year=2026&month=3&art={kind}'}


def test_the_accounts_are_closed_at_first_and_belong_to_their_line(worlds):
    dom = Dom(page_of(worlds['susa'], 2026, 3, 'quartal'))
    children = [attrs for tag, attrs in dom.tags if tag == 'tr' and 'child-row' in attrs.get('class', '')]
    assert children and all('hidden' in attrs and attrs['data-ustva-parent'] for attrs in children)
    parents = {attrs['data-ustva-parent'] for attrs in children}
    assert parents == {'kz81', 'kz86', 'kz66'}
    heads = {attrs['onclick'] for tag, attrs in dom.tags if tag == 'tr' and 'group-row' in attrs.get('class', '')}
    assert heads == {f"ustvaToggle('{key}')" for key in parents}
    icons = {attrs['id'] for tag, attrs in dom.tags if tag == 'span' and attrs.get('id', '').startswith('ustva-icon-')}
    assert icons == {f'ustva-icon-{key}' for key in parents}
    assert not [attrs for tag, attrs in dom.tags if tag == 'tr' and ('bwa-parent' in str(attrs) or 'liq-parent' in str(attrs))]


def test_names_with_markup_are_escaped(tmp_path):
    """Ein Kontoname mit Markup steht als Text im Kontendetail (Kz 81, Kz 66 und „Nicht zugeordnet“) – und ändert den Aufbau der Seite nicht."""
    pages = {}
    for kind, name in (('hostile', HOSTILE), ('benign', 'Harmloser Name')):
        database = Database(db_name=str(tmp_path / f'{kind}.db'))
        two_banks(database)
        own_company(database)
        for number in (4400, 6815, 4100):
            raw(database, 'UPDATE ChartOfAccounts SET Name = ? WHERE AccountNumber = ?', name, number)
        mask(database, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
        mask(database, 'Probe-Giro', 6815, '-59.50', '2026-03-12', '19', '-9.50')
        mask(database, 'Probe-Giro', 4100, '10.00', '2026-03-13')
        pages[kind] = page_of(database, 2026, 3, 'monat')
    assert HOSTILE not in pages['hostile'] and '<script>alert(1)</script>' not in pages['hostile']
    assert html_lib.escape(HOSTILE, quote=True) in pages['hostile']
    assert_same_structure(pages['hostile'], pages['benign'])
    figures, _reconciliation, unassigned = tables_of(pages['hostile'])
    assert f' | 4400 {HOSTILE} | 100,00 |  | 19,00' in figures and f' | 6815 {HOSTILE} |  |  | 9,50' in figures
    assert f'4100 {HOSTILE} | 1 | 10,00 | 0,00' in unassigned


def test_the_page_names_no_brand(worlds):
    for kind in ('monat', 'quartal'):
        text = visible_text(page_of(worlds['susa'], 2026, 3, kind)).lower()
        for brand in BRANDS:
            assert brand.lower() not in text, brand
    for text in (USTVA_SOLL_TEXT, USTVA_UNSET_TEXT, USTVA_PDF_NOT_IST_TEXT, *UNASSIGNED_REASONS.values()):
        assert not [brand for brand in BRANDS if brand.lower() in text.lower()]


# ═══ Adresse ══════════════════════════════════════════════════════════════════
def qs(**fields):
    return {name: value if isinstance(value, list) else [value] for name, value in fields.items()}


@pytest.mark.parametrize('query, common, expected', [
    (qs(art='quartal'), ('2026-03-01', '2026-03-31'), 'quartal'), (qs(art='monat'), ('2026-01-01', '2026-03-31'), 'monat'),
    (qs(), ('2026-01-01', '2026-03-31'), 'quartal'), (qs(), ('2026-04-01', '2026-06-30'), 'quartal'), (qs(), ('2026-03-01', '2026-03-31'), 'monat'),
    (qs(), ('2026-01-01', '2026-12-31'), 'monat'), (qs(), ('2026-02-01', '2026-04-30'), 'monat'),
    (qs(art='x'), ('2026-01-01', '2026-03-31'), 'quartal'), (qs(art='x'), ('2026-03-01', '2026-03-31'), 'monat'),
    (qs(art=['monat', 'quartal']), ('2026-01-01', '2026-03-31'), 'quartal'), (qs(art=['quartal', 'quartal']), ('2026-03-01', '2026-03-31'), 'monat'),
    (qs(art=''), ('2026-01-01', '2026-03-31'), 'quartal'), (qs(art='Quartal'), ('2026-03-01', '2026-03-31'), 'monat'), (qs(art='QUARTAL'), ('2026-01-01', '2026-03-31'), 'quartal'),
])
def test_the_kind_of_the_page_comes_from_the_address_or_the_period_and_never_complains(query, common, expected):
    assert read_kind(query, *common) == expected


@pytest.mark.parametrize('query, expected', [
    (qs(year='2026', month='3'), UstvaQuery(2026, 3, 'monat')), (qs(year='2026', month='03'), UstvaQuery(2026, 3, 'monat')),
    (qs(year='2026', month='3', art='monat'), UstvaQuery(2026, 3, 'monat')), (qs(year='2026', month='3', art='quartal'), UstvaQuery(2026, 3, 'quartal')),
    (qs(year='2026', month='12', art='quartal'), UstvaQuery(2026, 12, 'quartal')), (qs(year='2026', month='9', art='quartal'), UstvaQuery(2026, 9, 'quartal')),
    (qs(year='2026', month='6', art='quartal'), UstvaQuery(2026, 6, 'quartal')), (qs(year='2026', month='2'), UstvaQuery(2026, 2, 'monat')),
])
def test_the_address_of_the_pdf_is_read_strictly(query, expected):
    assert read_ustva_pdf_query(query) == (expected, None)


@pytest.mark.parametrize('query', [
    qs(), qs(year='2026'), qs(month='3'), qs(year='', month='3'), qs(year='2026', month=''), qs(year='02026', month='3'), qs(year='26', month='3'),
    qs(year='1899', month='3'), qs(year='3000', month='3'), qs(year='2026', month='0'), qs(year='2026', month='13'), qs(year='2026', month='001'),
    qs(year='2026', month='+3'), qs(year='2026', month='1.0'), qs(year='2026', month='-1'), qs(year='2026', month=' 3'), qs(year='２０２６', month='3'),
    qs(year=['2026', '2025'], month='3'), qs(year='2026', month=['3', '4']), qs(year=['2026', '2026'], month='3'),
    qs(year='2026', month='3', art='x'), qs(year='2026', month='3', art=''), qs(year='2026', month='3', art='Quartal'), qs(year='2026', month='3', art=['monat', 'monat']),
    qs(year='2026', month='3', art=['quartal', 'quartal']), qs(year='2026', month='3', art=['monat', 'quartal']),
    qs(year='2026', month='1', art='quartal'), qs(year='2026', month='2', art='quartal'), qs(year='2026', month='4', art='quartal'),
    qs(year='2026', month='5', art='quartal'), qs(year='2026', month='7', art='quartal'), qs(year='2026', month='8', art='quartal'),
    qs(year='2026', month='10', art='quartal'), qs(year='2026', month='11', art='quartal'),
])
def test_the_address_of_the_pdf_refuses_what_it_cannot_read_and_never_corrects_it(query):
    assert read_ustva_pdf_query(query) == (None, INVALID_PERIOD_ERROR) and INVALID_PERIOD_ERROR == 'Ungültiger Zeitraum.'


# ═══ Route ════════════════════════════════════════════════════════════════════
def ist_books(db):
    own_company(db)
    two_banks(db)
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    mask(db, 'Probe-Giro', 6815, '-59.50', '2026-03-12', '19', '-9.50')
    mask(db, 'Probe-Giro', 4300, '107.00', '2026-05-12', '7', '7.00')
    mask(db, 'Probe-Giro', 4400, '119.00', '2025-05-12', '19', '19.00')


def test_the_page_answers_for_a_month_with_a_cookie_and_without_an_address_with_none(server_port, auto_login):
    ist_books(auto_login.db)
    response = get(server_port, '/ustva?from=2026-03-01&to=2026-03-31')
    assert response.status == 200 and response.headers['Content-Type'].startswith('text/html')
    assert response.headers.get_all('Set-Cookie') == ['period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax']
    assert response.headers.get_all('Cache-Control') == ['no-cache']
    assert 'Umsatzsteuer-Voranmeldung – März 2026 (01.03.2026 – 31.03.2026)' in response.text and '81 | ' in ' | '.join(' '.join(row) for row in tables_of(response.text))
    bare = get(server_port, '/ustva')
    assert bare.status == 200 and bare.headers.get('Set-Cookie') is None and 'Umsatzsteuer-Voranmeldung – ' in bare.text and 'Der gewählte Zeitraum' not in bare.text


def test_the_kind_decides_between_the_month_and_its_quarter(server_port, auto_login):
    ist_books(auto_login.db)
    quarter = get(server_port, '/ustva?from=2026-03-01&to=2026-03-31&art=quartal')
    assert quarter.status == 200 and 'Umsatzsteuer-Voranmeldung – 1. Quartal 2026 (01.01.2026 – 31.03.2026)' in quarter.text
    assert 'art=quartal' in header3(quarter.text) and quarter.headers.get_all('Set-Cookie') == ['period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax']
    # ein Zeitraum, der kein Kalendermonat ist, zeigt den Monat seines Endes – 2025, damit der laufende Monat ihn nie abschneidet (``period.period_month``)
    month = get(server_port, '/ustva?from=2025-01-01&to=2025-03-31&art=monat')
    assert 'Umsatzsteuer-Voranmeldung – März 2025 (01.03.2025 – 31.03.2025)' in month.text and 'art=' not in header3(month.text)
    # ein Kalenderquartal ohne ``art`` zeigt das Quartal
    derived = get(server_port, '/ustva?from=2025-04-01&to=2025-06-30')
    assert 'Umsatzsteuer-Voranmeldung – 2. Quartal 2025 (01.04.2025 – 30.06.2025)' in derived.text and 'art=quartal' in header3(derived.text)
    for query in ('art=x', 'art=monat&art=quartal', 'art=', 'art=Quartal'):
        page = get(server_port, f'/ustva?from=2026-03-01&to=2026-03-31&{query}')
        assert page.status == 200 and 'Umsatzsteuer-Voranmeldung – März 2026' in page.text and 'ungültig' not in page.text.lower(), query


def test_the_page_and_the_pdf_write_nothing_into_the_books(server_port, auto_login):
    """Die Seite und das PDF sind GET-Routen, die nur lesen: Datenbank und Datenordner sind danach unverändert. Die Seite setzt – wie jede Seite mit Zeitraum – das
    Cookie des gemeinsamen Zeitraums, wenn der aus der Adresse kam; das PDF setzt nie eines. Das ist der Nachweis für die Positivliste des Diagnose-MCP (``/ustva``)."""
    ist_books(auto_login.db)
    before_db, before_tree = dump(auto_login.db.db_name), tree(userctx.DATA_ROOT)
    cookie = ['period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax']
    for path, cookies in (('/ustva', []), ('/ustva?from=2026-03-01&to=2026-03-31', cookie), ('/ustva?from=2026-03-01&to=2026-03-31&art=quartal', cookie),
                          ('/ustva?year=x', []), ('/ustva?year=2026&month=1', []), ('/ustva?from=x&to=y', []), ('/ustva?art=quartal', [])):
        response = get(server_port, path)
        assert response.status == 200, path
        assert (response.headers.get_all('Set-Cookie') or []) == cookies, path
    for path in ('/ustva/pdf?year=2026&month=3', '/ustva/pdf?year=2026&month=3&art=quartal', '/ustva/pdf?year=bad', '/ustva/pdf?year=2026&month=2&art=quartal',
                 '/ustva/pdf?from=2026-03-01&to=2026-03-31'):
        response = get(server_port, path)
        assert response.headers.get('Set-Cookie') is None, path
        assert response.status in (200, 400), path
    assert dump(auto_login.db.db_name) == before_db
    assert tree(userctx.DATA_ROOT) == before_tree


def test_the_routes_are_plain_get_pages_without_an_action_or_json_entry():
    from server import app
    for name in ('_ACTION_PATHS', '_GENERATE_PATHS', '_JSON_PATHS', '_POST_ONLY_JSON_PATHS', '_SEARCH_PATHS', '_UPLOAD_PATHS', '_LOCK_FREE_ROUTES'):
        for route in ('/ustva', '/ustva/pdf'):
            assert route not in getattr(app, name), (name, route)


def test_the_pdf_of_a_firm_that_is_not_on_actual_taxation_is_refused(server_port, auto_login):
    db = auto_login.db
    two_banks(db)
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    for method in (None, 'soll'):
        if method:
            own_company(db, method)
        response = get(server_port, '/ustva/pdf?year=2026&month=3')
        assert response.status == 400 and response.text == USTVA_PDF_NOT_IST_TEXT == 'Die Umsatzsteuer-Voranmeldung gibt es nur bei Ist-Versteuerung.'
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8' and response.headers.get('Set-Cookie') is None
        page = get(server_port, '/ustva?from=2026-03-01&to=2026-03-31')
        assert page.status == 200 and 'ustvaPdf' not in page.text
    assert get(server_port, '/ustva/pdf?year=2026&month=13').text == 'Ungültiger Zeitraum.'                  # die Adresse wird zuerst gelesen


# ═══ Diagnose-MCP ═════════════════════════════════════════════════════════════
def _mcp():
    spec = importlib.util.spec_from_file_location('mcp_ubuchhaltung_ustva', TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_mcp_may_read_the_page_but_never_the_pdf():
    """Die Seite schreibt nichts (Nachweis: ``test_the_page_and_the_pdf_write_nothing_into_the_books``), das PDF ist ein Dokument: nur die Seite steht in der Positivliste."""
    mcp = _mcp()
    for path in ('/ustva', '/ustva?art=quartal', '/ustva?from=2026-10-01&to=2026-10-31&art=monat', '/ustva?date_from=2026-10-01&date_to=2026-10-31'):
        assert mcp.check_page_path(path) == path
    assert mcp._PAGE_ALLOWLIST['/ustva'] == frozenset({'art', 'from', 'to', 'date_from', 'date_to'}) and '/ustva/pdf' not in mcp._PAGE_ALLOWLIST
    for path in ('/ustva/pdf?year=2026&month=10', '/ustva/pdf', '/Ustva', '/ustva/', '/ustva?variant=jahr', '/ustva?year=2026&month=3'):
        with pytest.raises(ValueError):
            mcp.check_page_path(path)
