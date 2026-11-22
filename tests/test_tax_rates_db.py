# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Steuersätze der Stammdaten, Datenbankschicht (2.8.0, Etappe E2): ``db/tax_rates.py``.

* Die Vorbelegung (19 und 7 Prozent) ist wählbar und lässt sich ohne Verlust in den bisherigen Payload übersetzen (``legacy_expressible``); die Momentaufnahme
  einer Position (``entry_snapshot``) hat genau die Schlüssel der Positionsspalten.
* Jede Regel eines Eintrags (``tax_groups.check_tax_entry``, mit den Konten dieser Datenbank) lehnt mit genau ihrem festen Satz ab, beim Anlegen wie beim Ändern –
  und es ist nichts geschrieben, kein Zähler verbraucht.
* Die Steuerkonten (Umsatzsteuer 3800–3849, Vorsteuer 1400–1439, über die Gruppen der BWA) sind freiwillig und nie bei Satz 0 und bei 19, 16, 7 und 5 Prozent.
* Ändern wirkt nur auf neue Belege (keine Position verweist auf einen Eintrag), Löschen ist immer erlaubt – auch mit Belegen, die die Momentaufnahme tragen –,
  nur der letzte wählbare Eintrag bleibt: Löschen (``DeleteRefused``) und Abschalten (``TaxRateRefused``) sind dort abgelehnt.
* Gelesen wird streng: Was ein Nutzer über die SQL-Konsole an eine Zeile schreibt und die Regeln verletzt, macht sie ungültig – nie in einer Auswahl,
  in der Liste benannt, löschbar und per Formular zu reparieren.
* Jeder Schreibweg ist EINE Transaktion, schließt seine Verbindung auch im Fehlerfall und geht nach dem Commit ins SQL-Protokoll (das sich auf einer zweiten
  Datenbank zur selben Tabelle abspielen lässt). Konten, die ein Eintrag nennt, lassen sich im Kontenrahmen nicht löschen.

Alle Bezeichnungen, Gründe, Konten und Nummern sind erfunden.
"""
import contextlib
import os
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import userctx
from db import Database, DeleteRefused, TaxRateRefused, bwa, coa_id, susa
from db.invoices import ITEM_SNAPSHOT_KEYS
from db.tax_rates import (FIXED_TAX_BPS, INPUT_TAX_GROUPS, TAX_RATE_NOT_FOUND, VAT_GROUPS, TaxEntry, entry_snapshot, has_default_revenue,
                          legacy_expressible)
from tax_groups import (EXEMPT, STANDARD, TAX_ENTRY_EXEMPT_RATE, TAX_ENTRY_INPUT_TAX_ACCOUNT, TAX_ENTRY_KIND, TAX_ENTRY_LABEL, TAX_ENTRY_LAST_ACTIVE,
                        TAX_ENTRY_RATE, TAX_ENTRY_REASON, TAX_ENTRY_REVENUE_NOT_INCOME, TAX_ENTRY_REVENUE_REQUIRED, TAX_ENTRY_TAX_ACCOUNT_FIXED,
                        TAX_ENTRY_TAX_ACCOUNT_ZERO, TAX_ENTRY_VAT_ACCOUNT)
from tests.document_helpers import add_contacts, add_document, state

REASON = 'Beispielhafter Befreiungsgrund (erfunden)'
COLUMNS = 'ID, Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder'
PRESET = [(1, '19 %', 1900, 'standard', None, None, None, None, 1, 10),
          (2, '7 %', 700, 'standard', None, None, None, None, 1, 20)]


# ── Hilfen ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def rows(db):
    """Die Einträge roh (ohne Zeitstempel), wie ``COLUMNS`` sie nennt."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT {COLUMNS} FROM TaxRates ORDER BY ID').fetchall()


def console(db, sql, *args):
    """Ein Schreibbefehl wie der der SQL-Konsole: ohne die Regeln der Anwendung (und ohne Fremdschlüssel)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, args)
        con.commit()


def new_account(db, number, name='Testkonto (erfunden)'):
    """Ein Konto, das der Kontenrahmen nicht führt, als Nicht-Standardkonto; liefert seine COA-ID."""
    assert db.insert_chart_of_accounts(4, number, name, 'Testkonten', is_standard=0)
    return coa_id(4, number)


@pytest.fixture
def ids(tmp_db):
    """Die Konten der Tests: aus dem Kontenrahmen (``revenue`` 4400, ``waiting`` 4405 – ein Wartekonto, kein Erlöskonto –, ``expense`` 6815, ``input_ok`` 1406,
    ``vat_ok`` 3801) und angelegte (``test_input`` 1430, ``test_vat`` 3845 – im Bereich; ``outside_input`` 1440, ``outside_vat`` 3850 – knapp davor und dahinter)."""
    db = tmp_db
    return SimpleNamespace(
        revenue=db.get_coa_id_by_account_number(4400), waiting=db.get_coa_id_by_account_number(4405), expense=db.get_coa_id_by_account_number(6815),
        input_ok=db.get_coa_id_by_account_number(1406), vat_ok=db.get_coa_id_by_account_number(3801),
        vat_collected=db.get_coa_id_by_account_number(3806), test_input=new_account(db, 1430, 'Vorsteuer Testsatz'),
        test_vat=new_account(db, 3845, 'Umsatzsteuer Testsatz'), outside_input=new_account(db, 1440), outside_vat=new_account(db, 3850),
        missing=999_999)


def five_and_a_half(ids, **over):
    """Die Felder eines gültigen Eintrags über 5,5 % mit Erlöskonto und beiden Steuerkonten – ``over`` ändert einzelne."""
    return {**dict(label='Ermäßigt (erfunden)', bp=550, kind=STANDARD, reason=None, revenue_coa_id=ids.revenue, vat_coa_id=ids.test_vat,
                   input_tax_coa_id=ids.test_input), **over}


def tax_free(ids, **over):
    return {**dict(label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason=REASON, revenue_coa_id=ids.revenue), **over}


def log_lines():
    path = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return [line for line in handle.read().splitlines() if 'TaxRates' in line]


def assert_not_locked(db):
    """Nach einer Ablehnung oder einem Fehler hält keine Verbindung mehr die Schreibsperre: Ein anderer Schreiber kommt sofort durch."""
    con = sqlite3.connect(db.db_name, timeout=0.5)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()
    finally:
        con.close()


# ── Lesen: die Vorbelegung ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_fresh_database_offers_the_two_preset_entries_as_selectable_in_their_order(tmp_db):
    entries = tmp_db.fetch_tax_rates()

    assert [(e.id, e.label, e.bp, e.kind, e.reason, e.revenue_coa_id, e.vat_coa_id, e.input_tax_coa_id, e.active, e.sort_order) for e in entries] == [
        (1, '19 %', 1900, 'standard', None, None, None, None, True, 10), (2, '7 %', 700, 'standard', None, None, None, None, True, 20)]
    assert all(e.valid and e.selectable for e in entries)
    assert tmp_db.fetch_tax_rates(active_only=True) == entries
    assert tmp_db.get_tax_rate(2) == entries[1]
    assert tmp_db.get_tax_rate(99) is None


def test_the_entries_are_ordered_by_sort_order_then_id(tmp_db, ids):
    first = tmp_db.insert_tax_rate(**five_and_a_half(ids, label='B (erfunden)', sort_order=5))
    second = tmp_db.insert_tax_rate(**five_and_a_half(ids, label='C (erfunden)', sort_order=5))
    third = tmp_db.insert_tax_rate(**five_and_a_half(ids, label='A (erfunden)', sort_order=0))

    assert [e.id for e in tmp_db.fetch_tax_rates()] == [third, first, second, 1, 2]


def test_an_entry_without_a_sort_order_goes_to_the_end_ten_above_the_largest(tmp_db, ids):
    first = tmp_db.insert_tax_rate(**five_and_a_half(ids, label='Erster (erfunden)'))
    second = tmp_db.insert_tax_rate(**five_and_a_half(ids, label='Zweiter (erfunden)'))

    assert [e.sort_order for e in tmp_db.fetch_tax_rates()] == [10, 20, 30, 40]
    assert [e.id for e in tmp_db.fetch_tax_rates()] == [1, 2, first, second]


def test_inactive_entries_are_in_the_list_but_not_in_the_selection(tmp_db, ids):
    tmp_db.set_tax_rate_active(2, False)

    assert [(e.id, e.active, e.valid, e.selectable) for e in tmp_db.fetch_tax_rates()] == [(1, True, True, True), (2, False, True, False)]
    assert [e.id for e in tmp_db.fetch_tax_rates(active_only=True)] == [1]


def test_get_tax_rate_reads_on_the_connection_it_is_given_and_leaves_it_open(tmp_db):
    conn = tmp_db._get_connection()
    try:
        assert tmp_db.get_tax_rate(1, conn=conn).label == '19 %'
        assert conn.execute('SELECT COUNT(*) FROM TaxRates').fetchone()[0] == 2           # noch offen
    finally:
        conn.close()


def test_there_is_no_exemption_code_in_the_table_or_the_entry(tmp_db):
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info(TaxRates)')]

    assert columns == ['ID', 'Label', 'RateBp', 'Kind', 'ExemptReason', 'RevenueCoaId', 'VatCoaId', 'InputTaxCoaId', 'Active', 'SortOrder', 'CreatedAt']
    assert not [name for name in TaxEntry._fields if 'code' in name.lower()]


# ── has_default_revenue, legacy_expressible, entry_snapshot ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('bp, kind, expected', [
    (1900, STANDARD, True), (700, STANDARD, True), (550, STANDARD, False), (0, STANDARD, False), (1600, STANDARD, False), (500, STANDARD, False),
    (1900, EXEMPT, False), (0, EXEMPT, False), (None, STANDARD, False), ('1900', STANDARD, False), (19.0, STANDARD, False), (True, STANDARD, False),
    (1900, '?', False)])
def test_only_the_standard_entries_of_19_and_7_percent_have_a_default_revenue_account(bp, kind, expected):
    assert has_default_revenue(bp, kind) is expected


def entry_of(**over):
    return TaxEntry(**{**dict(id=1, label='Satz (erfunden)', bp=1900, kind=STANDARD, reason=None, revenue_coa_id=None, vat_coa_id=None, input_tax_coa_id=None,
                              active=True, sort_order=10, valid=True), **over})


@pytest.mark.parametrize('over, expected', [
    (dict(bp=1900), True), (dict(bp=700), True),                                                      # die Vorbelegung ohne Konto
    (dict(bp=1900, revenue_coa_id=17), False), (dict(bp=700, revenue_coa_id=17), False),              # mit Erlöskonto trägt der Satz allein nicht mehr alles
    (dict(bp=550), False), (dict(bp=1950), False),                                                    # ein Satz mit Nachkommastellen
    (dict(bp=0, kind=EXEMPT, reason=REASON, revenue_coa_id=17), False),                               # steuerfrei: Grund und Konto
    (dict(bp=0, revenue_coa_id=17), False),                                                           # 0 % der Art „Standard“
    (dict(bp=1600, revenue_coa_id=17), False)])
def test_the_legacy_payload_carries_exactly_the_entries_it_loses_nothing_of(over, expected):
    assert legacy_expressible(entry_of(**over)) is expected


def test_the_preset_entries_read_from_the_database_are_expressible_and_an_entry_with_an_account_is_not(tmp_db, ids):
    assert [legacy_expressible(e) for e in tmp_db.fetch_tax_rates()] == [True, True]
    five = tmp_db.get_tax_rate(tmp_db.insert_tax_rate(**five_and_a_half(ids)))
    exempt = tmp_db.get_tax_rate(tmp_db.insert_tax_rate(**tax_free(ids)))
    zero = tmp_db.get_tax_rate(tmp_db.insert_tax_rate(label='Nullsatz (erfunden)', bp=0, revenue_coa_id=ids.revenue))
    with_account = tmp_db.get_tax_rate(tmp_db.insert_tax_rate(label='Regelsatz mit Konto (erfunden)', bp=1900, revenue_coa_id=ids.revenue))

    assert [legacy_expressible(e) for e in (five, exempt, zero, with_account)] == [False, False, False, False]


def test_the_snapshot_of_a_standard_entry_is_category_s_with_its_rate_and_account_and_no_reason():
    assert entry_snapshot(entry_of(bp=1900)) == {'tax_category': 'S', 'tax_rate_bp': 1900, 'tax_exempt_reason': None, 'revenue_coa_id': None}
    assert entry_snapshot(entry_of(bp=550, revenue_coa_id=17)) == {'tax_category': 'S', 'tax_rate_bp': 550, 'tax_exempt_reason': None, 'revenue_coa_id': 17}
    assert entry_snapshot(entry_of(bp=0, revenue_coa_id=17)) == {'tax_category': 'S', 'tax_rate_bp': 0, 'tax_exempt_reason': None, 'revenue_coa_id': 17}
    assert entry_snapshot(entry_of(bp=700, reason='ein Rest aus der SQL-Konsole'))['tax_exempt_reason'] is None      # der Grund zählt nur bei steuerfrei


def test_the_snapshot_of_a_tax_free_entry_is_category_e_with_rate_0_its_reason_and_account():
    assert entry_snapshot(entry_of(bp=0, kind=EXEMPT, reason=REASON, revenue_coa_id=17)) == {
        'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': REASON, 'revenue_coa_id': 17}


def test_the_snapshot_has_exactly_the_keys_of_the_position_columns_in_their_order():
    assert list(entry_snapshot(entry_of())) == list(ITEM_SNAPSHOT_KEYS.values())


def test_a_snapshot_is_what_insert_invoice_item_writes_to_the_columns_of_a_position(tmp_db, ids):
    own, customer = add_contacts(tmp_db)
    document = add_document(tmp_db, '26R001', 'invoice', own, customer)
    entry = tmp_db.get_tax_rate(tmp_db.insert_tax_rate(**tax_free(ids)))

    tmp_db.insert_invoice_item({'invoice_id': document, 'position': 2, 'description': 'Beratung', 'quantity': 1, 'unit': 'Std.', 'price_per_unit': 80.0,
                                'total_net': 80.0, 'tax_rate': 0, **entry_snapshot(entry)})

    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        stored = con.execute('SELECT TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE Position = 2').fetchone()
    assert stored == ('E', 0, REASON, ids.revenue)


# ── Anlegen ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_entries_of_every_kind_are_saved_as_given_and_the_id_comes_back(tmp_db, ids):
    five = tmp_db.insert_tax_rate(**five_and_a_half(ids, sort_order=30))
    exempt = tmp_db.insert_tax_rate(**tax_free(ids, sort_order=40, input_tax_coa_id=None))
    zero = tmp_db.insert_tax_rate(label='Nullsatz (erfunden)', bp=0, revenue_coa_id=ids.revenue, sort_order=50, active=False)
    preset = tmp_db.insert_tax_rate(label='Regelsatz zusätzlich (erfunden)', bp=1900, sort_order=60)

    assert (five, exempt, zero, preset) == (3, 4, 5, 6)
    assert rows(tmp_db)[2:] == [
        (3, 'Ermäßigt (erfunden)', 550, 'standard', None, ids.revenue, ids.test_vat, ids.test_input, 1, 30),
        (4, 'Steuerfrei (erfunden)', 0, 'exempt', REASON, ids.revenue, None, None, 1, 40),
        (5, 'Nullsatz (erfunden)', 0, 'standard', None, ids.revenue, None, None, 0, 50),
        (6, 'Regelsatz zusätzlich (erfunden)', 1900, 'standard', None, None, None, None, 1, 60)]


def test_the_reason_is_kept_only_for_a_tax_free_entry(tmp_db, ids):
    standard = tmp_db.insert_tax_rate(**five_and_a_half(ids, reason='Dieser Text gehört zu keiner Befreiung'))
    exempt = tmp_db.insert_tax_rate(**tax_free(ids))

    assert tmp_db.get_tax_rate(standard).reason is None
    assert tmp_db.get_tax_rate(exempt).reason == REASON
    tmp_db.update_tax_rate(exempt, **tax_free(ids, kind=STANDARD, bp=550))                       # ein steuerfreier Eintrag, der es nicht mehr ist
    assert tmp_db.get_tax_rate(exempt).reason is None


def test_two_entries_may_share_a_rate_and_a_label(tmp_db, ids):
    first = tmp_db.insert_tax_rate(**five_and_a_half(ids))
    second = tmp_db.insert_tax_rate(**five_and_a_half(ids))
    third = tmp_db.insert_tax_rate(**tax_free(ids, reason='Ein anderer erfundener Grund'))
    fourth = tmp_db.insert_tax_rate(**tax_free(ids))

    assert len({first, second, third, fourth}) == 4


def base_fields(ids):
    return five_and_a_half(ids)


# Jede Regel mit genau ihrem Satz: (Änderung am gültigen Eintrag über 5,5 %, der feste Satz). Beim Anlegen wie beim Ändern.
REFUSALS = [
    pytest.param(lambda i: dict(label=''), TAX_ENTRY_LABEL, id='Bezeichnung leer'),
    pytest.param(lambda i: dict(label='x' * 61), TAX_ENTRY_LABEL, id='Bezeichnung zu lang'),
    pytest.param(lambda i: dict(label='Zeile\numbruch'), TAX_ENTRY_LABEL, id='Bezeichnung mit Zeilenumbruch'),
    pytest.param(lambda i: dict(label=None), TAX_ENTRY_LABEL, id='Bezeichnung fehlt'),
    pytest.param(lambda i: dict(bp=None), TAX_ENTRY_RATE, id='Satz fehlt'),
    pytest.param(lambda i: dict(bp=50), TAX_ENTRY_RATE, id='Satz unter 1 %'),
    pytest.param(lambda i: dict(bp=10001), TAX_ENTRY_RATE, id='Satz über 100 %'),
    pytest.param(lambda i: dict(bp='550'), TAX_ENTRY_RATE, id='Satz als Text'),
    pytest.param(lambda i: dict(bp=5.5), TAX_ENTRY_RATE, id='Satz als Kommazahl'),
    pytest.param(lambda i: dict(kind='free'), TAX_ENTRY_KIND, id='Art unbekannt'),
    pytest.param(lambda i: dict(kind='Standard'), TAX_ENTRY_KIND, id='Art in anderer Schreibweise'),
    pytest.param(lambda i: dict(kind=EXEMPT, reason=REASON), TAX_ENTRY_EXEMPT_RATE, id='steuerfrei mit Satz'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason=''), TAX_ENTRY_REASON, id='steuerfrei ohne Grund'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason=None), TAX_ENTRY_REASON, id='steuerfrei, Grund fehlt'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason='x' * 301), TAX_ENTRY_REASON, id='Grund zu lang'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason='Grund\nmit Umbruch'), TAX_ENTRY_REASON, id='Grund mit Zeilenumbruch'),
    pytest.param(lambda i: dict(revenue_coa_id=None), TAX_ENTRY_REVENUE_REQUIRED, id='5,5 % ohne Erlöskonto'),
    pytest.param(lambda i: dict(bp=0, revenue_coa_id=None, vat_coa_id=None, input_tax_coa_id=None), TAX_ENTRY_REVENUE_REQUIRED, id='Standard 0 % ohne Erlöskonto'),
    pytest.param(lambda i: dict(revenue_coa_id=i.expense), TAX_ENTRY_REVENUE_NOT_INCOME, id='Aufwandskonto als Erlöskonto'),
    pytest.param(lambda i: dict(revenue_coa_id=i.waiting), TAX_ENTRY_REVENUE_NOT_INCOME, id='Wartekonto als Erlöskonto'),
    pytest.param(lambda i: dict(revenue_coa_id=i.input_ok), TAX_ENTRY_REVENUE_NOT_INCOME, id='Vorsteuerkonto als Erlöskonto'),
    pytest.param(lambda i: dict(revenue_coa_id=i.missing), TAX_ENTRY_REVENUE_NOT_INCOME, id='Erlöskonto gibt es nicht'),
    pytest.param(lambda i: dict(revenue_coa_id='404400'), TAX_ENTRY_REVENUE_NOT_INCOME, id='Erlöskonto als Text'),
    pytest.param(lambda i: dict(revenue_coa_id=True), TAX_ENTRY_REVENUE_NOT_INCOME, id='Erlöskonto als Wahrheitswert'),
    pytest.param(lambda i: dict(bp=0, vat_coa_id=i.test_vat, input_tax_coa_id=None), TAX_ENTRY_TAX_ACCOUNT_ZERO, id='Standard 0 % mit Umsatzsteuerkonto'),
    pytest.param(lambda i: dict(bp=0, vat_coa_id=None, input_tax_coa_id=i.test_input), TAX_ENTRY_TAX_ACCOUNT_ZERO, id='Standard 0 % mit Vorsteuerkonto'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason=REASON, vat_coa_id=i.test_vat), TAX_ENTRY_TAX_ACCOUNT_ZERO, id='steuerfrei mit Umsatzsteuerkonto'),
    pytest.param(lambda i: dict(kind=EXEMPT, bp=0, reason=REASON, input_tax_coa_id=i.test_input), TAX_ENTRY_TAX_ACCOUNT_ZERO, id='steuerfrei mit Vorsteuerkonto'),
    pytest.param(lambda i: dict(bp=1900, input_tax_coa_id=None), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='19 % mit Umsatzsteuerkonto'),
    pytest.param(lambda i: dict(bp=1900, vat_coa_id=None), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='19 % mit Vorsteuerkonto'),
    pytest.param(lambda i: dict(bp=1600), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='16 % mit Steuerkonten'),
    pytest.param(lambda i: dict(bp=700), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='7 % mit Steuerkonten'),
    pytest.param(lambda i: dict(bp=500), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='5 % mit Steuerkonten'),
    pytest.param(lambda i: dict(bp=1900, vat_coa_id=i.input_ok), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='19 %: das feste Konto geht vor der Kontoart'),
    pytest.param(lambda i: dict(vat_coa_id=i.input_ok), TAX_ENTRY_VAT_ACCOUNT, id='Vorsteuerkonto als Umsatzsteuerkonto'),
    pytest.param(lambda i: dict(vat_coa_id=i.revenue), TAX_ENTRY_VAT_ACCOUNT, id='Erlöskonto als Umsatzsteuerkonto'),
    pytest.param(lambda i: dict(vat_coa_id=i.outside_vat), TAX_ENTRY_VAT_ACCOUNT, id='Umsatzsteuerkonto knapp hinter dem Bereich (3850)'),
    pytest.param(lambda i: dict(vat_coa_id=i.missing), TAX_ENTRY_VAT_ACCOUNT, id='Umsatzsteuerkonto gibt es nicht'),
    pytest.param(lambda i: dict(vat_coa_id=str(i.test_vat)), TAX_ENTRY_VAT_ACCOUNT, id='Umsatzsteuerkonto als Text'),
    pytest.param(lambda i: dict(input_tax_coa_id=i.vat_ok), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='Umsatzsteuerkonto als Vorsteuerkonto'),
    pytest.param(lambda i: dict(input_tax_coa_id=i.revenue), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='4400 als Vorsteuerkonto'),
    pytest.param(lambda i: dict(input_tax_coa_id=i.outside_input), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='Vorsteuerkonto knapp hinter dem Bereich (1440)'),
    pytest.param(lambda i: dict(input_tax_coa_id=i.missing), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='Vorsteuerkonto gibt es nicht'),
    pytest.param(lambda i: dict(input_tax_coa_id=True), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='Vorsteuerkonto als Wahrheitswert'),
    pytest.param(lambda i: dict(vat_coa_id=i.input_ok, input_tax_coa_id=i.vat_ok), TAX_ENTRY_VAT_ACCOUNT, id='beide falsch: das Umsatzsteuerkonto zuerst'),
    pytest.param(lambda i: dict(label='', bp=None, kind='?', revenue_coa_id=i.missing, vat_coa_id=i.missing), TAX_ENTRY_LABEL, id='mehrere Verstöße: die erste Regel'),
]


@pytest.mark.parametrize('change, sentence', REFUSALS)
def test_every_rule_refuses_an_insert_with_exactly_its_sentence_and_nothing_is_written(tmp_db, ids, change, sentence):
    before = state(tmp_db, 'TaxRates')

    with pytest.raises(TaxRateRefused) as refused:
        tmp_db.insert_tax_rate(**five_and_a_half(ids, **change(ids)))

    assert str(refused.value) == sentence
    assert state(tmp_db, 'TaxRates') == before
    assert tmp_db.insert_tax_rate(**five_and_a_half(ids)) == 3                  # kein Zähler verbraucht: die abgelehnte Zeile hat keine ID bekommen
    assert_not_locked(tmp_db)


@pytest.mark.parametrize('change, sentence', REFUSALS)
def test_every_rule_refuses_an_update_with_exactly_its_sentence_and_the_entry_stays_as_it_was(tmp_db, ids, change, sentence):
    entry = tmp_db.insert_tax_rate(**five_and_a_half(ids))
    before = state(tmp_db, 'TaxRates')

    with pytest.raises(TaxRateRefused) as refused:
        tmp_db.update_tax_rate(entry, **five_and_a_half(ids, **change(ids)))

    assert str(refused.value) == sentence
    assert state(tmp_db, 'TaxRates') == before
    assert_not_locked(tmp_db)


def test_the_tax_accounts_of_a_rate_without_fixed_accounts_are_saved_as_chosen(tmp_db, ids):
    five = tmp_db.insert_tax_rate(**five_and_a_half(ids))
    chosen_from_the_chart = tmp_db.insert_tax_rate(**five_and_a_half(ids, bp=1070, label='10,7 % (erfunden)', vat_coa_id=ids.vat_ok, input_tax_coa_id=ids.input_ok))
    collected = tmp_db.insert_tax_rate(**five_and_a_half(ids, bp=250, label='2,5 % (erfunden)', vat_coa_id=ids.vat_collected, input_tax_coa_id=None))

    assert [(e.vat_coa_id, e.input_tax_coa_id) for e in map(tmp_db.get_tax_rate, (five, chosen_from_the_chart, collected))] == [
        (ids.test_vat, ids.test_input), (ids.vat_ok, ids.input_ok), (ids.vat_collected, None)]


def test_the_tax_accounts_are_free_a_rate_may_have_none_or_only_one(tmp_db, ids):
    none = tmp_db.insert_tax_rate(**five_and_a_half(ids, vat_coa_id=None, input_tax_coa_id=None))
    vat_only = tmp_db.insert_tax_rate(**five_and_a_half(ids, input_tax_coa_id=None))
    input_only = tmp_db.insert_tax_rate(**five_and_a_half(ids, vat_coa_id=None))

    assert [(e.vat_coa_id, e.input_tax_coa_id) for e in map(tmp_db.get_tax_rate, (none, vat_only, input_only))] == [
        (None, None), (ids.test_vat, None), (None, ids.test_input)]


def test_the_rates_with_fixed_tax_accounts_are_the_four_of_the_trial_balance_derived_from_its_one_table():
    assert FIXED_TAX_BPS == frozenset({1900, 1600, 700, 500})
    assert FIXED_TAX_BPS == frozenset(int(round(rate * 100)) for rate in susa.TAX_ACCOUNTS[susa.TAX_VAT])
    assert FIXED_TAX_BPS == frozenset(int(round(rate * 100)) for rate in susa.TAX_ACCOUNTS[susa.TAX_INPUT])


def test_the_account_groups_are_those_of_the_bwa():
    assert INPUT_TAX_GROUPS == ('input_vat',) and VAT_GROUPS == ('vat_collected', 'vat_settlement')
    assert all(group in bwa.GROUPS for group in (*INPUT_TAX_GROUPS, *VAT_GROUPS))


def test_the_tax_account_choices_are_the_accounts_of_those_groups_by_number(tmp_db, ids):
    choices = tmp_db.get_tax_account_choices()

    assert [number for _id, number, _name in choices['input']] == [1400, 1401, 1406, 1430, 1433]
    assert [number for _id, number, _name in choices['vat']] == [3800, 3801, 3802, 3804, 3806, 3820, 3830, 3841, 3845]
    assert (ids.test_input, 1430, 'Vorsteuer Testsatz') in choices['input'] and (ids.test_vat, 3845, 'Umsatzsteuer Testsatz') in choices['vat']
    assert ids.outside_input not in [row[0] for row in choices['input']] and ids.outside_vat not in [row[0] for row in choices['vat']]


def test_the_tax_account_choices_follow_the_groups_for_every_account_of_the_chart(tmp_db, ids):
    chart = [(row[0], row[2], row[3]) for row in tmp_db.fetch_chart_of_accounts()]
    choices = tmp_db.get_tax_account_choices()

    assert [row for row in chart if bwa.group_of(row[1]) in VAT_GROUPS] == sorted(choices['vat'], key=lambda row: (row[1], row[0]))
    assert [row for row in chart if bwa.group_of(row[1]) in INPUT_TAX_GROUPS] == sorted(choices['input'], key=lambda row: (row[1], row[0]))


# ── Ändern ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_update_writes_every_field_and_a_missing_account_clears_it(tmp_db, ids):
    entry = tmp_db.insert_tax_rate(**five_and_a_half(ids))

    tmp_db.update_tax_rate(entry, label='Geändert (erfunden)', bp=1070, kind=STANDARD, revenue_coa_id=ids.revenue, sort_order=77, active=False)

    assert rows(tmp_db)[2] == (entry, 'Geändert (erfunden)', 1070, 'standard', None, ids.revenue, None, None, 0, 77)


def test_an_update_without_sort_order_and_active_leaves_them(tmp_db, ids):
    entry = tmp_db.insert_tax_rate(**five_and_a_half(ids, sort_order=33, active=False))

    tmp_db.update_tax_rate(entry, **five_and_a_half(ids, label='Anders (erfunden)'))

    assert rows(tmp_db)[2][1:] == ('Anders (erfunden)', 550, 'standard', None, ids.revenue, ids.test_vat, ids.test_input, 0, 33)


def test_an_entry_can_turn_from_standard_to_tax_free_and_back(tmp_db, ids):
    entry = tmp_db.insert_tax_rate(**five_and_a_half(ids))

    tmp_db.update_tax_rate(entry, **tax_free(ids))
    assert rows(tmp_db)[2][1:5] == ('Steuerfrei (erfunden)', 0, 'exempt', REASON)
    tmp_db.update_tax_rate(entry, **five_and_a_half(ids))
    assert rows(tmp_db)[2][1:5] == ('Ermäßigt (erfunden)', 550, 'standard', None)


def test_an_update_of_an_entry_that_does_not_exist_is_refused_with_a_fixed_sentence(tmp_db, ids):
    before = state(tmp_db, 'TaxRates')

    with pytest.raises(TaxRateRefused) as refused:
        tmp_db.update_tax_rate(99, **five_and_a_half(ids))

    assert str(refused.value) == TAX_RATE_NOT_FOUND == 'Diesen Steuersatz gibt es nicht (mehr).'
    assert state(tmp_db, 'TaxRates') == before


def test_changing_an_entry_changes_no_position_that_carries_its_snapshot(tmp_db, ids):
    own, customer = add_contacts(tmp_db)
    document = add_document(tmp_db, '26R001', 'invoice', own, customer)
    entry = tmp_db.insert_tax_rate(**tax_free(ids))
    tmp_db.insert_invoice_item({'invoice_id': document, 'position': 2, 'description': 'Beratung', 'quantity': 1, 'unit': 'Std.', 'price_per_unit': 80.0,
                                'total_net': 80.0, 'tax_rate': 0, **entry_snapshot(tmp_db.get_tax_rate(entry))})
    before = state(tmp_db, 'InvoiceItems', 'Invoices')

    tmp_db.update_tax_rate(entry, **tax_free(ids, label='Umbenannt (erfunden)', reason='Ein ganz anderer erfundener Grund'))
    tmp_db.delete_tax_rate(entry)

    assert state(tmp_db, 'InvoiceItems', 'Invoices') == before


# ── Abschalten, Löschen, der letzte wählbare Eintrag ─────────────────────────────────────────────────────────────────────────────

def test_an_entry_is_switched_off_and_on_and_stays_in_the_list(tmp_db):
    tmp_db.set_tax_rate_active(2, False)
    assert [r[8] for r in rows(tmp_db)] == [1, 0]
    tmp_db.set_tax_rate_active(2, True)
    assert [r[8] for r in rows(tmp_db)] == [1, 1]


def test_the_last_selectable_entry_cannot_be_switched_off_deleted_or_updated_to_inactive(tmp_db, ids):
    tmp_db.delete_tax_rate(2)
    before = state(tmp_db, 'TaxRates')

    with pytest.raises(TaxRateRefused) as off:
        tmp_db.set_tax_rate_active(1, False)
    with pytest.raises(DeleteRefused) as deleted:
        tmp_db.delete_tax_rate(1)
    with pytest.raises(TaxRateRefused) as updated:
        tmp_db.update_tax_rate(1, label='19 %', bp=1900, kind=STANDARD, active=False)

    assert str(off.value) == str(deleted.value) == str(updated.value) == TAX_ENTRY_LAST_ACTIVE == 'Der letzte wählbare Steuersatz lässt sich nicht löschen oder abschalten.'
    assert state(tmp_db, 'TaxRates') == before
    assert_not_locked(tmp_db)


def test_the_refusal_to_delete_is_a_delete_refused_and_the_one_to_switch_off_a_tax_rate_refused():
    assert issubclass(DeleteRefused, ValueError) and issubclass(TaxRateRefused, ValueError)
    assert not issubclass(TaxRateRefused, DeleteRefused) and not issubclass(DeleteRefused, TaxRateRefused)


def test_with_a_second_selectable_entry_any_of_the_two_can_go(tmp_db, ids):
    tmp_db.set_tax_rate_active(1, False)
    tmp_db.update_tax_rate(1, label='19 %', bp=1900, kind=STANDARD, active=True)
    tmp_db.set_tax_rate_active(1, False)
    tmp_db.delete_tax_rate(1)
    assert [r[0] for r in rows(tmp_db)] == [2]


def test_inactive_entries_do_not_count_as_selectable(tmp_db, ids):
    tmp_db.set_tax_rate_active(2, False)

    with pytest.raises(TaxRateRefused):
        tmp_db.set_tax_rate_active(1, False)                                    # 19 % ist jetzt der letzte wählbare
    with pytest.raises(DeleteRefused):
        tmp_db.delete_tax_rate(1)
    tmp_db.delete_tax_rate(2)                                                   # der inaktive geht immer
    assert [r[0] for r in rows(tmp_db)] == [1]


def test_switching_on_is_always_allowed_and_a_second_entry_is_selectable_once_it_is_on(tmp_db, ids):
    five = tmp_db.insert_tax_rate(**five_and_a_half(ids, active=False))
    tmp_db.set_tax_rate_active(2, False)
    tmp_db.set_tax_rate_active(five, True)

    tmp_db.set_tax_rate_active(1, False)                                        # 5,5 % ist wählbar: 19 % darf gehen

    assert [e.id for e in tmp_db.fetch_tax_rates(active_only=True)] == [five]


def test_deleting_is_allowed_for_an_entry_that_carries_a_snapshot_in_documents(tmp_db, ids):
    own, customer = add_contacts(tmp_db)
    document = add_document(tmp_db, '26R001', 'invoice', own, customer)
    entry = tmp_db.insert_tax_rate(**tax_free(ids))
    tmp_db.insert_invoice_item({'invoice_id': document, 'position': 2, 'description': 'Beratung', 'quantity': 1, 'unit': 'Std.', 'price_per_unit': 80.0,
                                'total_net': 80.0, 'tax_rate': 0, **entry_snapshot(tmp_db.get_tax_rate(entry))})
    before = state(tmp_db, 'InvoiceItems', 'Invoices')

    tmp_db.delete_tax_rate(entry)

    assert entry not in [r[0] for r in rows(tmp_db)]
    assert state(tmp_db, 'InvoiceItems', 'Invoices') == before


def test_an_unknown_entry_cannot_be_switched_and_deleting_it_is_no_error(tmp_db):
    with pytest.raises(TaxRateRefused) as refused:
        tmp_db.set_tax_rate_active(99, True)

    assert str(refused.value) == TAX_RATE_NOT_FOUND
    tmp_db.delete_tax_rate(99)
    assert rows(tmp_db) == PRESET


def test_a_deleted_entry_does_not_come_back_nor_its_id_back(tmp_db, ids):
    five = tmp_db.insert_tax_rate(**five_and_a_half(ids))
    tmp_db.delete_tax_rate(five)

    assert tmp_db.insert_tax_rate(**five_and_a_half(ids)) == five + 1


# ── Streng gelesen ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

GARBAGE = [
    pytest.param('Kind', 'free', id='Art unbekannt'),
    pytest.param('Kind', 'Standard', id='Art in anderer Schreibweise'),
    pytest.param('Kind', '', id='Art leer'),
    pytest.param('Kind', 'exempt', id='steuerfrei mit Satz 7 %'),
    pytest.param('RateBp', 5.5, id='Satz als Kommazahl'),
    pytest.param('RateBp', 'abc', id='Satz als Text'),
    pytest.param('RateBp', -1, id='Satz unter 0'),
    pytest.param('RateBp', 50, id='Satz unter 1 %'),
    pytest.param('RateBp', 10001, id='Satz über 100 %'),
    pytest.param('RateBp', 1600, id='16 % ohne Erlöskonto'),
    pytest.param('Active', 2, id='Aktiv-Kennzeichen 2'),
    pytest.param('Active', -1, id='Aktiv-Kennzeichen −1'),
    pytest.param('Active', 'ja', id='Aktiv-Kennzeichen als Text'),
    pytest.param('Label', '', id='Bezeichnung leer'),
    pytest.param('Label', 'x' * 61, id='Bezeichnung zu lang'),
    pytest.param('Label', 'zwei\nZeilen', id='Bezeichnung mit Zeilenumbruch'),
    pytest.param('RevenueCoaId', 'waiting', id='Wartekonto als Erlöskonto'),
    pytest.param('RevenueCoaId', 999_999, id='Erlöskonto gibt es nicht'),
    pytest.param('RevenueCoaId', 'text', id='Erlöskonto als Text'),
    pytest.param('VatCoaId', 'test_vat', id='Umsatzsteuerkonto bei 7 %'),
    pytest.param('InputTaxCoaId', 'test_input', id='Vorsteuerkonto bei 7 %'),
]


@pytest.mark.parametrize('column, value', GARBAGE)
def test_a_row_the_rules_do_not_allow_is_invalid_never_selectable_and_can_be_deleted(tmp_db, ids, column, value):
    value = getattr(ids, value) if isinstance(value, str) and hasattr(ids, value) else value
    console(tmp_db, f'UPDATE TaxRates SET {column} = ? WHERE ID = 2', value)

    entry = tmp_db.get_tax_rate(2)
    assert entry.valid is False and entry.selectable is False
    assert entry in tmp_db.fetch_tax_rates()
    assert [e.id for e in tmp_db.fetch_tax_rates(active_only=True)] == [1]
    tmp_db.delete_tax_rate(2)                                                    # ein ungültiger Eintrag zählt nicht als der letzte wählbare
    assert [r[0] for r in rows(tmp_db)] == [1]


def test_a_row_the_rules_do_not_allow_is_not_the_last_selectable_and_the_valid_one_is(tmp_db):
    console(tmp_db, "UPDATE TaxRates SET Kind = 'free' WHERE ID = 2")

    with pytest.raises(DeleteRefused):
        tmp_db.delete_tax_rate(1)
    with pytest.raises(TaxRateRefused):
        tmp_db.set_tax_rate_active(1, False)


def test_a_row_without_any_selectable_entry_left_can_still_be_cleaned_up(tmp_db):
    console(tmp_db, "UPDATE TaxRates SET Kind = 'free'")

    assert tmp_db.fetch_tax_rates(active_only=True) == []
    tmp_db.delete_tax_rate(1)
    tmp_db.delete_tax_rate(2)
    assert rows(tmp_db) == []


def test_an_invalid_row_is_repaired_by_saving_it_with_values_the_rules_allow(tmp_db):
    console(tmp_db, "UPDATE TaxRates SET Kind = 'free', RateBp = 5.5, Active = 'ja' WHERE ID = 2")
    assert tmp_db.get_tax_rate(2).valid is False

    tmp_db.update_tax_rate(2, label='7 %', bp=700, kind=STANDARD, active=True)

    assert tmp_db.get_tax_rate(2).valid is True and tmp_db.get_tax_rate(2).selectable is True


def test_a_row_with_the_flag_0_is_valid_just_not_active_and_a_sort_order_that_is_no_number_counts_as_0(tmp_db):
    console(tmp_db, "UPDATE TaxRates SET Active = 0, SortOrder = 'mitte' WHERE ID = 2")

    entry = tmp_db.get_tax_rate(2)

    assert (entry.valid, entry.active, entry.selectable, entry.sort_order) == (True, False, False, 0)


def test_the_garbage_of_a_row_is_shown_as_the_database_has_it(tmp_db):
    console(tmp_db, "UPDATE TaxRates SET Kind = '<b>x</b>', RateBp = 'abc', Label = ? WHERE ID = 2", '<script>x</script>')

    entry = tmp_db.get_tax_rate(2)

    assert (entry.kind, entry.bp, entry.label) == ('<b>x</b>', 'abc', '<script>x</script>')       # maskiert wird erst bei der Ausgabe


# ── Das Wartekonto, nur zur Anzeige ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_waiting_account_of_a_rate_is_read_from_the_table_of_the_invoices_for_exactly_this_rate(tmp_db):
    table = tmp_db.OPEN_REVENUE_ACCOUNTS

    assert tmp_db.tax_rate_waiting_account(1900) == table[19]
    assert tmp_db.tax_rate_waiting_account(700) == table[7]
    assert tmp_db.tax_rate_waiting_account(0) == table[0]
    assert [number for number, _name in map(tmp_db.tax_rate_waiting_account, (1900, 700, 0))] == [4405, 4345, 4340]


@pytest.mark.parametrize('bp', [550, 1070, 1950, 1000, 1600, 500, 100, 10000, -100, None, '1900', 19.0, True])
def test_any_other_rate_has_no_waiting_account_it_is_not_rounded_to_a_whole_percent(tmp_db, bp):
    assert tmp_db.tax_rate_waiting_account(bp) is None


# ── Transaktionen, Verbindungen, SQL-Protokoll ───────────────────────────────────────────────────────────────────────────────

class FailingConnection(sqlite3.Connection):
    """Eine Verbindung, deren Schreibbefehl scheitert (wie „database is locked“) und die merkt, ob sie geschlossen wurde."""
    closed = False

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
            raise sqlite3.OperationalError('simulierter Fehler beim Schreiben')
        return super().execute(sql, *args)

    def close(self):
        self.closed = True
        super().close()


WRITES = [
    pytest.param(lambda db, ids: db.insert_tax_rate(label='Neu (erfunden)', bp=700), id='anlegen'),
    pytest.param(lambda db, ids: db.update_tax_rate(2, label='7 % neu', bp=700, kind=STANDARD), id='ändern'),
    pytest.param(lambda db, ids: db.set_tax_rate_active(2, False), id='abschalten'),
    pytest.param(lambda db, ids: db.delete_tax_rate(2), id='löschen'),
]


@pytest.mark.parametrize('write', WRITES)
def test_a_failing_write_closes_its_connection_leaves_no_lock_and_changes_nothing(tmp_db, ids, monkeypatch, write):
    opened = []

    def connect():
        conn = sqlite3.connect(tmp_db.db_name, factory=FailingConnection)
        conn.execute('PRAGMA foreign_keys = ON')
        opened.append(conn)
        return conn

    monkeypatch.setattr(tmp_db, '_get_connection', connect)
    with pytest.raises(sqlite3.OperationalError, match='simulierter Fehler') as info:
        write(tmp_db, ids)

    assert opened and all(conn.closed for conn in opened)
    assert info.value is not None                      # hält die Rahmen am Leben: Bliebe eine Verbindung offen, hielte sie hier noch die Sperre
    assert_not_locked(tmp_db)
    assert rows(tmp_db) == PRESET
    assert log_lines() == []


@pytest.mark.parametrize('write', [
    pytest.param(lambda db, ids: db.insert_tax_rate(label='', bp=700), id='anlegen'),
    pytest.param(lambda db, ids: db.update_tax_rate(2, label='', bp=700, kind=STANDARD), id='ändern'),
    pytest.param(lambda db, ids: db.set_tax_rate_active(99, False), id='abschalten'),
])
def test_a_refusal_closes_its_connection_logs_nothing_and_changes_nothing(tmp_db, ids, monkeypatch, write):
    opened = []
    original = tmp_db._get_connection

    def connect():
        conn = original()
        opened.append(conn)
        return conn

    monkeypatch.setattr(tmp_db, '_get_connection', connect)
    with pytest.raises(TaxRateRefused):
        write(tmp_db, ids)

    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')                                           # geschlossen
    assert_not_locked(tmp_db)
    assert rows(tmp_db) == PRESET
    assert log_lines() == []


def test_every_write_goes_into_the_sql_log_after_the_commit_and_replays_to_the_same_table(tmp_db, ids, tmp_path):
    entry = tmp_db.insert_tax_rate(**tax_free(ids, label="Steuerfrei 'Probe' (erfunden)", reason="Grund mit Apostroph ' und Umlaut ä (erfunden)"))
    tmp_db.update_tax_rate(entry, **tax_free(ids, label='Umbenannt (erfunden)', sort_order=15))
    tmp_db.set_tax_rate_active(2, False)
    tmp_db.set_tax_rate_active(2, True)
    tmp_db.insert_tax_rate(**five_and_a_half(ids))
    tmp_db.delete_tax_rate(2)
    with pytest.raises(TaxRateRefused):
        tmp_db.insert_tax_rate(label='', bp=700)                                # abgelehnt: kommt nicht ins Protokoll

    lines = log_lines()
    assert [line.split(' (')[0].split(' SET')[0].split(' WHERE')[0] for line in lines] == [
        'INSERT INTO TaxRates', 'UPDATE TaxRates', 'UPDATE TaxRates', 'UPDATE TaxRates', 'INSERT INTO TaxRates', 'DELETE FROM TaxRates']
    assert not any('?' in line for line in lines)                              # die Werte stehen als Literale da

    # Das Protokoll spielt sich auf einer zweiten Datenbank im selben Anfangsstand zur selben Tabelle ab – bis auf die Konten, die dort fehlen (Fremdschlüssel
    # aus): Es kommt auf die Zeilen an
    replay = Database(db_name=str(tmp_path / 'replay.db'))
    with contextlib.closing(sqlite3.connect(replay.db_name)) as con:
        for line in lines:
            con.execute(line)
        con.commit()
        replayed = con.execute(f'SELECT {COLUMNS} FROM TaxRates ORDER BY ID').fetchall()
    assert replayed == rows(tmp_db)


# ── Konten, die ein Eintrag nennt ────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_account_an_entry_names_cannot_be_deleted_from_the_chart_of_accounts_until_the_entry_is_gone(tmp_db, ids):
    entry = tmp_db.insert_tax_rate(**five_and_a_half(ids, revenue_coa_id=new_account(tmp_db, 4711, 'Erlöse Testsatz')))
    revenue, vat, input_tax = coa_id(4, 4711), ids.test_vat, ids.test_input

    assert [tmp_db.coa_is_referenced(account) for account in (revenue, vat, input_tax)] == [True, True, True]
    assert tmp_db.coa_is_referenced(ids.outside_vat) is False
    tmp_db.delete_tax_rate(entry)
    assert [tmp_db.coa_is_referenced(account) for account in (revenue, vat, input_tax)] == [False, False, False]


def test_the_handler_of_the_chart_of_accounts_refuses_to_delete_an_account_an_entry_names(tmp_db, ids):
    from server import handlers
    tmp_db.insert_tax_rate(**five_and_a_half(ids))

    status, location = handlers.handle_delete_skr(tmp_db, ids.test_vat)

    assert status == 303
    assert parse_qs(urlsplit(location).query) == {'msg': ['Konto wird in Buchungen, Anlagen, Steuersätzen oder Rechnungen verwendet und kann nicht gelöscht '
                                                          'werden.'], 'type': ['error']}
    assert tmp_db.get_coa_id_to_number_map().get(ids.test_vat) == 3845          # das Konto steht noch im Kontenrahmen
