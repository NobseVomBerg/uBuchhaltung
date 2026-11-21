# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Summen- und Saldenliste (2.5.17): dieselben Zeilen und dieselbe Lesart wie der Export für das Steuerbüro, je Konto Anfangssaldo, Soll und
Haben (Monat und aufgelaufen) und Endsaldo.

Sieben Teile.

* Die reinen Regeln (``db/susa.py``): Kontenklassen und Vortrag, die Buchungsregel je Satz (Soll und Haben, Steuer, fehlende Seiten) als Tabelle
  und als Eigenschaft (jeder Satz bucht gleich viel ins Soll wie ins Haben; eine Gruppe gleich gelesener Sätze ergibt die Summen der Sätze
  einzeln), die Gruppe gegen ``book_group``, Bericht, Sichtbarkeit und Anzeige.
* Keine dritte Lesart: Die Auslagerung ``matching.ledger_form``/``coa_in_soll`` lässt den Export **Byte für Byte** gleich (SHA-256-Schnappschüsse,
  am Stand VOR der Auslagerung genommen – ändert sich ein Byte, ist der Code zu ändern, nie der Schnappschuss), und für JEDE Zeile der Testwelt und
  des Demobestands bucht die Liste dieselbe Seite wie die Zeile des Exports (Konto, Gegenkonto, Soll/Haben, Betrag).
* Die Abstimmungsproben: (a) Summe Soll gleich Summe Haben, Anfangs- und Endsalden zusammen null – in der Testwelt, der SuSa-Welt, dem Demobestand und
  in zufälligen Beständen (Eigenschaftsprüfung mit festem Seed), in jedem Monat; (b) jedes Bankkonto gegen die Bankbewegungen; (c) die Erfolgskonten
  gegen die EÜR – und was sie trennen darf.
* Vortrag, Übertrag, Bankbewegungen ohne Satz, Steuerkonten, Sichtbarkeit.
* Nicht Lesbares (Roh-SQL): Beträge, Daten, Konten – die Liste geht auf, der Hinweis nennt es. Nichts fällt still weg: Zeilen ohne Buchungsart, mit einem
  Datum, das kein Kalendertag ist, mit Betrag 0 und Steuerbetrag, Sätze mit anderem Datum oder anderem eigenen Konto als ihre Bankbewegung – und die
  Bilanz der Zeilen (jede Zeile ist gebucht oder in genau einem Hinweis genannt, ``row_classes``). Kein Fehler bei Summen über 64 Bit und Namen, die kein
  Text sind.
* Adresse, Seite, Routen: streng gelesen, maskiert, kein Cookie, nichts geschrieben.
* Der Diagnose-MCP und die Menüs.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da.
"""
import csv
import datetime
import hashlib
import html as html_lib
import json
import random
import re
import sqlite3
import types
from contextlib import closing
from decimal import Decimal

import pytest

import auth
import userctx
from db import Database, datev_keys, reporting, susa
from db.bwa import INVALID_PERIOD_TEXT, MONTH_NAMES
from db.matching import (ENTRY_FORMS, FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, FORM_PURPOSE_FIRST,
                         FORM_TRANSFER, SIDE_COA, SIDE_COUNTER, TransferSettlement, coa_in_soll, ledger_form, soll_side)
from db.matching import DoubtfulEntry
from db.susa import Posting
from export import datev
from money import from_minor, to_minor
from server.pages import Header1
from server.pages_bwa import reports_submenu
from server.pages_susa import INVALID_PERIOD_ERROR, SUSA_PDF_FAILED_TEXT, PageSusa, SusaQuery, read_susa_pdf_query
from tests import susa_helpers as helpers
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump, tree
from tests.test_bwa import demo_db, world  # noqa: F401  (Fixtures: die Testwelt und der Demobestand)

HOSTILE = '<script>alert(1)</script>"x'
BRANDS = ('DATEV', 'WISO', 'ELSTER')
WAITING = (4340, 4345, 4405)

# SHA-256 der Export-Bytes (Zeitstempel im Vorlauf fest), genommen am Stand 2.5.16 VOR der Auslagerung von ``ledger_form``/``coa_in_soll``:
# (von, bis, Zeilen, Prüfsumme). Die Zeilen sind die des Handlers: alle Sätze des Zeitraums ohne die weggelassenen Eingangs-Sätze gepaarter Überträge.
EXPORT_SNAPSHOTS = {
    'world': ('2026-01-01', '2026-12-31', 48, '500717a1c49567ac23b0ff87de416fdf7b8ebad09569367705293d35c875f878'),
    'demo_db_2025': ('2025-01-01', '2025-12-31', 284, '57b445f366b8c8a63a6f32618299a15c46706dacfbb6ba919bcb6ed2155c35d9'),
    'demo_db_2026': ('2026-01-01', '2026-10-31', 348, 'c380d3972d18f665d6572d7516b5eb620682e69ee80adb1757d175456568d4f3'),
}


@pytest.fixture(scope='module')
def susa_world(tmp_path_factory):
    """Die SuSa-Welt (``tests/susa_helpers.py``), einmal gebaut; die Datenwurzel liegt unter ``tmp_path_factory`` – nichts landet im echten data/."""
    root = tmp_path_factory.mktemp('susa_world')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        result = helpers.build_susa_world(Database(str(root / 'susa.db')))
    userctx._mode_cache.clear()
    return result


def P(key, soll=0, haben=0):
    return Posting(key, soll, haben)


def tuples(postings):
    """Buchungen als Liste ``(Schlüssel, Soll, Haben)``, nach Schlüssel (Text) und Seite sortiert."""
    return sorted(((p.key, p.soll, p.haben) for p in postings), key=lambda item: (str(item[0]), item[1], item[2]))


# ═══ Kontenklassen und Vortrag ════════════════════════════════════════════════
@pytest.mark.parametrize('key, group', [
    (0, 'fixed_assets'), (999, 'fixed_assets'), (1000, 'current_assets'), (1999, 'current_assets'), (2000, 'equity'), (2999, 'equity'),
    (3000, 'liabilities'), (3999, 'liabilities'), (4000, 'revenue'), (4339, 'revenue'), (4340, 'open_items'), (4341, 'revenue'),
    (4344, 'revenue'), (4345, 'open_items'), (4346, 'revenue'), (4404, 'revenue'), (4405, 'open_items'), (4406, 'revenue'),
    (4999, 'revenue'), (5000, 'expenses'), (7999, 'expenses'), (8000, 'other'), (9999, 'other'), (10000, 'personal'), (70000, 'personal'),
    (999_999_999, 'personal'), (susa.NOT_POSTED, 'not_posted'), (susa.PRIOR_RESULT, 'equity'), (susa.TAX_UNASSIGNED, 'liabilities'),
    (-1, 'not_posted'), (None, 'not_posted'), ('x', 'not_posted'), (True, 'not_posted'),
])
def test_every_key_belongs_to_exactly_one_group(key, group):
    assert susa.group_key(key) == group


def test_the_groups_stand_in_the_order_of_the_list_and_only_the_balance_accounts_carry_forward():
    assert [group.key for group in susa.GROUPS] == [
        'fixed_assets', 'current_assets', 'equity', 'liabilities', 'revenue', 'open_items', 'expenses', 'other', 'personal', 'not_posted']
    assert [group.key for group in susa.GROUPS if not group.carried] == ['revenue', 'expenses']
    for number in (520, 1800, 2100, 3806, 4340, 4345, 4405, 8000, 10000, susa.NOT_POSTED, susa.PRIOR_RESULT, susa.TAX_UNASSIGNED):
        assert susa.is_carried(number), number
    for number in (4000, 4400, 4999, 5000, 6815, 7999):
        assert not susa.is_carried(number), number
    assert susa.OPEN_ITEM_NUMBERS == (4340, 4345, 4405)


def test_every_standard_account_of_the_chart_has_a_group(tmp_db):
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        numbers = [row[0] for row in con.execute('SELECT DISTINCT AccountNumber FROM ChartOfAccounts WHERE AccountNumber IS NOT NULL')]
    assert len(numbers) > 50
    for number in numbers:
        assert susa.group_key(number) in susa.GROUP_BY_KEY and susa.group_key(number) != 'not_posted', number


def test_the_tax_accounts_are_complete_and_the_other_rates_have_none():
    assert susa.TAX_ACCOUNTS == {'input': {19.0: 1406, 16.0: 1406, 7.0: 1401, 5.0: 1401}, 'vat': {19.0: 3806, 16.0: 3806, 7.0: 3801, 5.0: 3801}}
    for rate in (0.19, 19, 19.0, 0.16, 16):
        assert susa.tax_account('input', rate) == 1406 and susa.tax_account('vat', rate) == 3806
    for rate in (0.07, 7, 0.05, 5.0):
        assert susa.tax_account('input', rate) == 1401 and susa.tax_account('vat', rate) == 3801
    for rate in (0.107, 10.7, 0, None, -1, 'abc', float('nan'), 0.2, 21):
        assert susa.tax_account('vat', rate) == susa.TAX_UNASSIGNED and susa.tax_account('input', rate) == susa.TAX_UNASSIGNED, rate
    assert susa.TAX_ACCOUNT_NAMES == {1401: 'Abziehbare Vorsteuer 7 %', 1406: 'Abziehbare Vorsteuer 19 %', 3801: 'Umsatzsteuer 7 %',
                                      3806: 'Umsatzsteuer 19 %'}


# ═══ Die Lesart des Exports: ledger_form und coa_in_soll ══════════════════════
def test_a_stored_form_decides_and_without_one_the_payment_accounts_do():
    assert ledger_form('purpose_first', 1, 2, {1}) == ('purpose_first', SIDE_COUNTER)
    assert ledger_form('purpose_first', 1, 2, None) == ('purpose_first', SIDE_COUNTER)           # die gespeicherte Form gilt auch ohne Geldkonten
    assert ledger_form('liquid_first', 1, 2, None) == ('liquid_first', SIDE_COA)
    assert ledger_form('mirror_counter', 1, 2, set()) == ('mirror_counter', SIDE_COUNTER)
    assert ledger_form('mirror_legacy', 1, 2, set()) == ('mirror_legacy', SIDE_COUNTER)
    assert ledger_form(None, 1, 2, {2}) == ('purpose_first', SIDE_COUNTER)                         # Zeile ohne Form: Geldkonto hinten
    assert ledger_form(None, 1, 2, {1}) == ('liquid_first', SIDE_COA)
    assert ledger_form(None, 1, 2, {1, 2}) == ('mirror_legacy', SIDE_COUNTER)
    assert ledger_form('unbekannt', 1, 2, {1}) == ('liquid_first', SIDE_COA)
    assert ledger_form(None, 1, 2, None) == (None, SIDE_COUNTER)                                    # keine Form, keine Geldkonten: allein das Vorzeichen
    assert ledger_form('unbekannt', 1, 2, None) == (None, SIDE_COUNTER)


@pytest.mark.parametrize('inflow, side, expected', [(True, SIDE_COA, True), (True, SIDE_COUNTER, False), (False, SIDE_COA, False),
                                                    (False, SIDE_COUNTER, True)])
def test_the_account_field_stands_in_the_debit_as_the_export_flags_it(inflow, side, expected):
    assert coa_in_soll(inflow, side) is expected


# ═══ Die Buchungsregel je Satz ════════════════════════════════════════════════
def postings_of(form, amount, coa=4400, counter=1800, tax=0, rate=None, kind=None):
    """Die Buchungen eines Satzes mit der Form, der Soll-Seite und der Art, die der Export dafür liest."""
    side = soll_side(form)
    kind = datev_keys.booking_kind(form, coa, counter, amount > 0, WAITING) if kind is None else kind
    return susa.entry_postings(form, side, kind, coa, counter, amount, tax, rate, WAITING)


GROSS, NET, TAX = 1_190_000, 1_000_000, 190_000          # 119,00 € brutto: 100,00 € netto und 19,00 € Steuer


@pytest.mark.parametrize('form, amount, coa, counter, expected', [
    (FORM_PURPOSE_FIRST, GROSS, 4400, 1800, [(1800, GROSS, 0), (4400, 0, GROSS)]),          # Einnahme: Bank im Soll, Erlös im Haben
    (FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, [(6815, GROSS, 0), (1800, 0, GROSS)]),         # Ausgabe: Aufwand im Soll, Bank im Haben
    (FORM_LIQUID_FIRST, GROSS, 1800, 4400, [(1800, GROSS, 0), (4400, 0, GROSS)]),
    (FORM_LIQUID_FIRST, -GROSS, 1800, 6815, [(6815, GROSS, 0), (1800, 0, GROSS)]),
    (FORM_TRANSFER, GROSS, 6220, 520, [(6220, GROSS, 0), (520, 0, GROSS)]),                  # positiv: Konto im Soll
    (FORM_TRANSFER, -GROSS, 6220, 520, [(520, GROSS, 0), (6220, 0, GROSS)]),
    (FORM_MIRROR_COA, GROSS, 1810, 1800, [(1810, GROSS, 0), (1800, 0, GROSS)]),
    (FORM_MIRROR_COA, -GROSS, 1810, 1800, [(1800, GROSS, 0), (1810, 0, GROSS)]),
    (FORM_MIRROR_COUNTER, GROSS, 1810, 1800, [(1800, GROSS, 0), (1810, 0, GROSS)]),          # der Betrag bezieht sich auf das Gegenkonto
    (FORM_MIRROR_COUNTER, -GROSS, 1810, 1800, [(1810, GROSS, 0), (1800, 0, GROSS)]),
])
def test_every_form_and_sign_books_the_amount_without_sign_on_the_two_sides(form, amount, coa, counter, expected):
    found, notices = postings_of(form, amount, coa, counter)
    assert tuples(found) == sorted(expected, key=lambda item: (str(item[0]), item[1], item[2])) and notices == set()


def test_a_legacy_mirror_and_a_zero_amount_book_nothing():
    assert postings_of(FORM_MIRROR_LEGACY, GROSS, 1810, 1800) == ([], {'legacy_mirror'})
    assert postings_of(FORM_MIRROR_LEGACY, -GROSS, 1810, 1800, tax=TAX) == ([], {'legacy_mirror'})
    for form in ENTRY_FORMS:
        if form != FORM_MIRROR_LEGACY:
            assert postings_of(form, 0, 4400, 1800, tax=TAX) == ([], set()), form


@pytest.mark.parametrize('form, amount, coa, counter, tax, rate, expected', [
    # Zahlung, Zweckkonto vorne: Einnahme (Erlös im Haben samt Umsatzsteuer), Ausgabe (Aufwand im Soll samt Vorsteuer)
    (FORM_PURPOSE_FIRST, GROSS, 4400, 1800, TAX, 0.19, [(1800, GROSS, 0), (4400, 0, NET), (3806, 0, TAX)]),
    (FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, -TAX, 0.19, [(6815, NET, 0), (1406, TAX, 0), (1800, 0, GROSS)]),
    (FORM_PURPOSE_FIRST, 1_070_000, 4300, 1800, 70_000, 0.07, [(1800, 1_070_000, 0), (4300, 0, 1_000_000), (3801, 0, 70_000)]),
    (FORM_PURPOSE_FIRST, -535_000, 6850, 1800, -35_000, 0.07, [(6850, 500_000, 0), (1401, 35_000, 0), (1800, 0, 535_000)]),
    (FORM_PURPOSE_FIRST, 1_160_000, 4400, 1800, 160_000, 0.16, [(1800, 1_160_000, 0), (4400, 0, NET), (3806, 0, 160_000)]),           # 16 % auf 3806
    (FORM_PURPOSE_FIRST, -525_000, 6850, 1800, -25_000, 5, [(6850, 500_000, 0), (1401, 25_000, 0), (1800, 0, 525_000)]),              # 5 % auf 1401
    # eine Erstattung: Betrag und Steuer mit dem Vorzeichen der Geldbewegung – die Steuerkonten laufen in die Gegenrichtung
    (FORM_PURPOSE_FIRST, 238_000, 6815, 1800, 38_000, 0.19, [(1800, 238_000, 0), (6815, 0, 200_000), (1406, 0, 38_000)]),
    (FORM_PURPOSE_FIRST, -238_000, 4400, 1800, -38_000, 0.19, [(4400, 200_000, 0), (3806, 38_000, 0), (1800, 0, 238_000)]),
    # Geldkonto vorne: das Zweckkonto ist das Gegenkonto
    (FORM_LIQUID_FIRST, GROSS, 1800, 4400, TAX, 0.19, [(1800, GROSS, 0), (4400, 0, NET), (3806, 0, TAX)]),
    (FORM_LIQUID_FIRST, -GROSS, 1800, 6815, -TAX, 0.19, [(6815, NET, 0), (1406, TAX, 0), (1800, 0, GROSS)]),
    # „Wartekonto an Erlöskonto“ (die Zahlung einer Rechnung): das Wartekonto hält den Bruttobetrag, der Erlös steht netto da, die Umsatzsteuer dazu
    (FORM_TRANSFER, GROSS, 4405, 4400, TAX, 0.19, [(4405, GROSS, 0), (4400, 0, NET), (3806, 0, TAX)]),
    (FORM_TRANSFER, -GROSS, 4405, 4400, -TAX, 0.19, [(4400, NET, 0), (3806, TAX, 0), (4405, 0, GROSS)]),              # die Erstattung, die Gegenrichtung
    (FORM_TRANSFER, 1_070_000, 4345, 4300, 70_000, 0.07, [(4345, 1_070_000, 0), (4300, 0, 1_000_000), (3801, 0, 70_000)]),
    # eine Umbuchung zwischen einem Aufwands- und einem Anlagekonto trägt die Vorsteuer, wenn sie Steuer hat
    (FORM_TRANSFER, GROSS, 6220, 520, TAX, 0.19, [(6220, GROSS, 0), (520, 0, NET), (1406, 0, TAX)]),
])
def test_the_tax_is_taken_out_of_the_side_of_the_purpose_and_booked_on_the_same_side_on_its_account(form, amount, coa, counter, tax, rate,
                                                                                                     expected):
    found, notices = postings_of(form, amount, coa, counter, tax, rate)
    assert tuples(found) == sorted(expected, key=lambda item: (str(item[0]), item[1], item[2])) and notices == set()


@pytest.mark.parametrize('amount, tax, coa, counter', [(GROSS, TAX, 10000, 4405), (-GROSS, -TAX, 10000, 4405)])
def test_the_receivable_of_an_invoice_and_the_counter_entry_of_its_credit_note_are_never_split_and_say_nothing(amount, tax, coa, counter):
    """Das Wartekonto hält den Bruttobetrag bis zur Zahlung: Die Forderung „10000 an Wartekonto“ und die Gegenbuchung der Stornorechnung (negativ)
    bleiben ganz; es gibt keinen Hinweis."""
    found, notices = postings_of(FORM_TRANSFER, amount, coa, counter, tax, 0.19)
    expected = [(10000, GROSS, 0), (4405, 0, GROSS)] if amount > 0 else [(4405, GROSS, 0), (10000, 0, GROSS)]
    assert tuples(found) == sorted(expected, key=lambda item: (str(item[0]), item[1], item[2])) and notices == set()


@pytest.mark.parametrize('rate', [0.107, 0.1, None, 0, 'abc', 0.2])
def test_a_rate_without_a_tax_account_books_the_tax_on_the_account_without_a_tax_account(rate):
    found, notices = postings_of(FORM_PURPOSE_FIRST, -1_107_000, 6815, 1800, -107_000, rate)
    assert tuples(found) == sorted([(6815, NET, 0), (susa.TAX_UNASSIGNED, 107_000, 0), (1800, 0, 1_107_000)],
                                   key=lambda item: (str(item[0]), item[1], item[2])) and notices == {'tax_unassigned'}
    found, notices = postings_of(FORM_PURPOSE_FIRST, 1_107_000, 4400, 1800, 107_000, rate)
    assert (susa.TAX_UNASSIGNED, 0, 107_000) in tuples(found) and notices == {'tax_unassigned'}


@pytest.mark.parametrize('form, coa, counter', [
    (FORM_LIQUID_FIRST, 1800, 10000),         # „Bank an Debitoren“: der Debitor ist das Zweckkonto, die Art hat keinen Schlüssel
    (FORM_MIRROR_COA, 1810, 1800),            # ein Spiegel
    (FORM_TRANSFER, 2100, 2180),              # eine Umbuchung ohne Erlös-, Aufwands- oder Anlagekonto
    (FORM_TRANSFER, 4405, 6815),              # die Kette „Wartekonto an Aufwand“: keine Erlösseite
])
def test_a_tax_amount_on_a_row_that_cannot_be_split_stays_gross_and_is_reported(form, coa, counter):
    found, notices = postings_of(form, GROSS, coa, counter, TAX, 0.19)
    assert len([p for p in found if p.soll]) == 1 and sum(p.soll for p in found) == GROSS == sum(p.haben for p in found)
    assert notices == {'tax_not_split'}


def test_a_missing_side_stands_on_not_posted_and_a_missing_purpose_account_cannot_be_split():
    found, notices = postings_of(FORM_PURPOSE_FIRST, -5000, None, 1800)
    assert tuples(found) == sorted([(susa.NOT_POSTED, 5000, 0), (1800, 0, 5000)], key=lambda item: (str(item[0]), item[1], item[2]))
    assert notices == {'missing_side'}
    found, notices = postings_of(FORM_PURPOSE_FIRST, -5000, 6815, None)
    assert tuples(found) == sorted([(6815, 5000, 0), (susa.NOT_POSTED, 0, 5000)], key=lambda item: (str(item[0]), item[1], item[2]))
    found, notices = postings_of(FORM_PURPOSE_FIRST, 5000, None, None)
    assert tuples(found) == [(susa.NOT_POSTED, 0, 5000), (susa.NOT_POSTED, 5000, 0)] and notices == {'missing_side'}
    found, notices = postings_of(FORM_PURPOSE_FIRST, -119_000, None, 1800, -19_000, 0.19)
    assert sum(p.soll for p in found) == sum(p.haben for p in found) == 119_000
    assert notices == {'missing_side', 'tax_not_split'}                 # ohne Zweckkonto gibt es nichts, woraus die Steuer herauszurechnen wäre
    found, notices = postings_of(FORM_LIQUID_FIRST, -5000, 1800, None)
    assert tuples(found) == sorted([(susa.NOT_POSTED, 5000, 0), (1800, 0, 5000)], key=lambda item: (str(item[0]), item[1], item[2]))


def test_a_tax_amount_of_zero_or_without_a_value_is_not_split():
    for tax in (0, None):
        found, notices = postings_of(FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, tax, 0.19)
        assert tuples(found) == sorted([(6815, GROSS, 0), (1800, 0, GROSS)], key=lambda item: (str(item[0]), item[1], item[2])) and notices == set()


def test_a_tax_amount_with_the_other_sign_is_taken_as_stored_and_the_books_still_balance():
    """Der gespeicherte Steuerbetrag gilt, auch gegen das Vorzeichen des Betrags: Nichts wird nachgerechnet. Die Seite bekommt dann mehr als den
    Bruttobetrag, das Steuerkonto eine Buchung auf der anderen Seite – Soll und Haben stimmen trotzdem."""
    found, notices = postings_of(FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, TAX, 0.19)
    assert tuples(found) == sorted([(6815, GROSS + TAX, 0), (1406, -TAX, 0), (1800, 0, GROSS)], key=lambda item: (str(item[0]), item[1], item[2]))
    assert sum(p.soll for p in found) == sum(p.haben for p in found) and notices == set()


def test_every_row_books_as_much_into_the_debit_as_into_the_credit():
    """Eigenschaftsprüfung mit festem Seed: Form, Betrag (auch 0), Konten (auch fehlend), Steuer, Satz – in jedem Fall Soll gleich Haben."""
    rng = random.Random(2517)
    numbers = [None, 520, 1050, 1460, 1600, 1800, 1810, 2100, 3806, 4300, 4340, 4345, 4400, 4405, 6815, 6850, 6890, 10000]
    for _ in range(4000):
        form = rng.choice(ENTRY_FORMS)
        coa, counter = rng.choice(numbers), rng.choice(numbers)
        amount = rng.choice([0, rng.randint(-5_000_000, 5_000_000)])
        tax = rng.choice([0, None, rng.randint(-900_000, 900_000)])
        rate = rng.choice([None, 0, 0.19, 0.07, 0.16, 0.05, 0.107, 19, 7])
        found, notices = postings_of(form, amount, coa, counter, tax, rate)
        assert sum(p.soll for p in found) == sum(p.haben for p in found), (form, amount, coa, counter, tax, rate)
        assert notices <= {susa.MISSING_SIDE, susa.LEGACY_MIRROR, susa.TAX_UNASSIGNED_NOTICE, susa.TAX_NOT_SPLIT}
        if form == FORM_MIRROR_LEGACY or amount == 0:
            assert found == []


# ═══ Eine Gruppe gleich gelesener Sätze ═══════════════════════════════════════
CONTEXT = susa.SusaContext(frozenset({2, 7}), {1: 4400, 2: 1800, 3: 6815, 4: 10000, 5: 4405, 6: 520, 7: 1600, 8: None},
                           {10: 1800, 11: None}, WAITING)


def group(form='purpose_first', coa=3, counter=2, inflow=False, rate=0.19, bucket='month', parent=None, count=1, amount=-GROSS, tax=-TAX):
    return susa.GroupRow(form, coa, counter, inflow, rate, bucket, parent, count, amount, tax)


def booked(row, sign=1, context=CONTEXT):
    tally = susa.Tally()
    susa.book_group(tally, context, row, sign)
    return tally


def test_a_group_books_its_sums_in_its_period():
    tally = booked(group(count=3, amount=-3 * GROSS, tax=-3 * TAX, bucket='year'))
    assert tally.postings() == [('year', P(6815, 3 * NET)), ('year', P(1406, 3 * TAX)), ('year', P(1800, 0, 3 * GROSS))]
    assert tally.notices() == []


def test_a_child_of_a_bank_movement_without_a_money_account_is_booked_on_the_account_of_the_movement():
    """„Zweckkonto vorne“ ohne Gegenkonto und „Geldkonto vorne“ ohne Konto unter einer Bankbewegung: Die fehlende Seite ist das Konto der Bewegung –
    EÜR und Split-Summe zählen den Satz genauso (Hinweis ``money_from_parent``)."""
    tally = booked(group(counter=None, parent=10, count=2, amount=-2 * GROSS, tax=-2 * TAX))
    assert tuples(p for _period, p in tally.postings()) == sorted([(6815, 2 * NET, 0), (1406, 2 * TAX, 0), (1800, 0, 2 * GROSS)],
                                                                   key=lambda item: (str(item[0]), item[1], item[2]))
    assert tally.notices() == [susa.SusaNotice('money_from_parent', 2, 2 * GROSS)]
    tally = booked(group('liquid_first', coa=None, counter=3, parent=10, amount=-GROSS, tax=-TAX))
    assert tuples(p for _period, p in tally.postings()) == sorted([(6815, NET, 0), (1406, TAX, 0), (1800, 0, GROSS)],
                                                                   key=lambda item: (str(item[0]), item[1], item[2]))
    assert [notice.key for notice in tally.notices()] == ['money_from_parent']


def test_without_a_number_of_the_movement_or_without_a_movement_the_missing_side_stays_not_posted():
    for parent in (11, None, 999):
        tally = booked(group(counter=None, parent=parent, amount=-5000, tax=0))
        assert tuples(p for _period, p in tally.postings()) == sorted([(6815, 5000, 0), (susa.NOT_POSTED, 0, 5000)],
                                                                       key=lambda item: (str(item[0]), item[1], item[2])), parent
        assert [notice.key for notice in tally.notices()] == ['missing_side'], parent


def test_a_row_without_a_stored_form_is_read_with_the_payment_accounts_and_reported():
    tally = booked(group(None, coa=3, counter=2, amount=-5000, tax=0))                    # Zeile ohne Form: Geldkonto 1800 hinten = Zweckkonto vorne
    assert tuples(p for _period, p in tally.postings()) == sorted([(6815, 5000, 0), (1800, 0, 5000)], key=lambda item: (str(item[0]), item[1], item[2]))
    assert [(notice.key, notice.count, notice.amount) for notice in tally.notices()] == [('no_form', 1, 5000)]
    tally = booked(group('purpose_first', amount=-5000, tax=0))
    assert tally.notices() == []                                                              # eine gespeicherte Form: kein Hinweis
    tally = booked(group('bogus', coa=3, counter=2, amount=-5000, tax=0))                   # eine Form, die es nicht gibt, gilt wie keine
    assert [notice.key for notice in tally.notices()] == ['no_form']


def test_a_legacy_mirror_books_nothing_and_is_reported_once_even_without_a_form():
    tally = booked(group('mirror_legacy', coa=2, counter=7, amount=-5000, tax=0, count=4))
    assert tally.postings() == [] and tally.notices() == [susa.SusaNotice('legacy_mirror', 4, 5000)]
    tally = booked(group(None, coa=2, counter=7, amount=-5000, tax=0))                       # beide Seiten Geldkonten, keine Form: ein Spiegel ohne Lesart
    assert tally.postings() == [] and [notice.key for notice in tally.notices()] == ['legacy_mirror']


def test_the_tax_notices_carry_the_tax_the_others_the_gross_amount():
    tally = booked(group('liquid_first', coa=2, counter=4, inflow=True, amount=GROSS, tax=TAX, count=2))     # Bank an Debitoren mit Steuer
    assert tally.notices() == [susa.SusaNotice('tax_not_split', 2, TAX)]
    tally = booked(group(rate=0.107, amount=-1_107_000, tax=-107_000))
    assert tally.notices() == [susa.SusaNotice('tax_unassigned', 1, 107_000)]


def test_subtracting_a_group_takes_back_exactly_what_it_booked_and_the_notices_with_it():
    rows = [group(counter=None, parent=10, amount=-GROSS, tax=-TAX), group('mirror_legacy', coa=2, counter=7), group(None, coa=3, counter=2),
            group(rate=0.107, amount=-1_107_000, tax=-107_000)]
    tally = susa.Tally()
    for sign in (1, -1):
        for row in rows:
            susa.book_group(tally, CONTEXT, row, sign)
    assert tally.postings() == [] and tally.notices() == []


def test_a_group_gives_the_sums_of_its_rows_booked_one_by_one():
    """Eigenschaftsprüfung mit festem Seed: Die Regel ist linear. Gleich gelesene Sätze (Form, Konten, Richtung des Betrags und der Steuer,
    Satz, Zeitraum, Bewegung) gebucht als Gruppe mit ihren Summen ergeben dieselben Summen und Hinweise wie jeder Satz einzeln."""
    rng = random.Random(25172)
    ids = [None, 1, 2, 3, 4, 5, 6, 7, 8]
    for _ in range(600):
        form = rng.choice([*ENTRY_FORMS, None])
        coa, counter, parent = rng.choice(ids), rng.choice(ids), rng.choice([None, 10, 11])
        inflow, rate = rng.random() < 0.5, rng.choice([None, 0.19, 0.07, 0.16, 0.05, 0.107])
        tax_sign = rng.choice([0, 1, -1])
        count = rng.randint(1, 6)
        amounts = [rng.randint(1, 4_000_000) * (1 if inflow else -1) for _ in range(count)]
        taxes = [0 if tax_sign == 0 else rng.randint(1, 600_000) * tax_sign for _ in range(count)]
        single, together = susa.Tally(), susa.Tally()
        for amount, tax in zip(amounts, taxes):
            susa.book_group(single, CONTEXT, group(form, coa, counter, inflow, rate, 'month', parent, 1, amount, tax))
        susa.book_group(together, CONTEXT, group(form, coa, counter, inflow, rate, 'month', parent, count, sum(amounts), sum(taxes)))
        assert sorted(single.postings(), key=repr) == sorted(together.postings(), key=repr)
        assert sorted(single.notices(), key=lambda n: n.key) == sorted(together.notices(), key=lambda n: n.key)


def test_the_tally_names_an_amount_only_where_the_sentence_has_one():
    tally = susa.Tally()
    tally.notice('not_readable', 2, 50)
    tally.notice('doubtful', 1, 70)
    tally.notice('unbooked', 3, 0, 1000, 400)
    tally.notice('bad_date', 1, 50)
    tally.notice('legacy_mirror', 0)
    found = {notice.key: notice for notice in tally.notices()}
    assert found == {'not_readable': susa.SusaNotice('not_readable', 2), 'doubtful': susa.SusaNotice('doubtful', 1),
                     'unbooked': susa.SusaNotice('unbooked', 3, None, 1000, 400), 'bad_date': susa.SusaNotice('bad_date', 1, 50)}


# ═══ Bericht, Sichtbarkeit, Anzeige ═══════════════════════════════════════════
NAMES = {1800: ('Bank', False), 4400: ('Erlöse 19 % USt', False), 6815: ('Bürobedarf', False), 3806: ('Umsatzsteuer 19%', False)}


def small_report():
    postings = [('prior', P(1800, 10_000_000)), ('prior', P(4400, 0, 10_000_000)),            # Soll 1.000 Bank, Haben 1.000 Erlös im Vorjahr
                ('year', P(6815, 2_000_000)), ('year', P(1800, 0, 2_000_000)),
                ('month', P(6815, 500_000)), ('month', P(1800, 0, 500_000))]
    return susa.build_susa(2026, 3, postings, NAMES, [], [], [])


def test_the_balance_accounts_carry_forward_and_the_profit_accounts_start_at_zero():
    accounts = helpers.accounts_of(small_report())
    assert (accounts[1800].opening, accounts[1800].month_haben, accounts[1800].ytd_haben, accounts[1800].closing) == (
        10_000_000, 500_000, 2_500_000, 7_500_000)
    assert (accounts[6815].opening, accounts[6815].month_soll, accounts[6815].ytd_soll, accounts[6815].closing) == (0, 500_000, 2_500_000, 2_500_000)
    assert 4400 not in accounts                                                                   # nur ein Vorjahr: für die Liste ein Konto ohne Werte
    result = accounts[susa.PRIOR_RESULT]                                                          # Haben 1.000: der Gewinn der Vorjahre
    assert (result.opening, result.closing, result.virtual, result.number, result.group) == (-10_000_000, -10_000_000, True, None, 'equity')
    helpers.assert_balanced(small_report())


def test_the_groups_sum_their_accounts_and_the_total_sums_the_groups():
    report = small_report()
    groups = {item.group.key: item for item in report.groups}
    assert (groups['current_assets'].opening, groups['current_assets'].closing) == (10_000_000, 7_500_000)
    assert (groups['expenses'].month_soll, groups['expenses'].ytd_soll, groups['expenses'].closing) == (500_000, 2_500_000, 2_500_000)
    assert groups['equity'].closing == -10_000_000
    assert helpers.total_row(report) == (0, 500_000, 500_000, 2_500_000, 2_500_000, 0)
    assert [item.group.key for item in report.groups] == [group_.key for group_ in susa.GROUPS]


def test_an_account_stands_there_with_any_value_and_a_group_with_a_visible_account():
    postings = [('month', P(6815, 0, 0)), ('year', P(6850, 500)), ('year', P(6850, 0, 500)), ('prior', P(520, 100))]
    report = susa.build_susa(2026, 3, postings, {}, [], [], [])
    accounts = helpers.accounts_of(report)
    assert not susa.visible(accounts[6815])                                                       # lauter Nullen: fehlt
    assert susa.visible(accounts[6850]) and accounts[6850].closing == 0                           # Soll gleich Haben, nicht null: steht da
    assert susa.visible(accounts[520])
    assert [item.group.key for item in susa.visible_groups(report)] == ['fixed_assets', 'expenses']
    assert [account.key for account in susa.visible_accounts(report.groups[6])] == [6850]


def test_the_accounts_stand_by_number_and_the_virtual_ones_at_the_end_of_their_group():
    postings = [('year', P(2180, 0, 5)), ('year', P(2100, 5)), ('year', P(susa.PRIOR_RESULT, 0, 1)), ('prior', P(2999, 7)),
                ('prior', P(susa.NOT_POSTED, 0, 7)), ('year', P(3806, 0, 3)), ('year', P(susa.TAX_UNASSIGNED, 3)), ('year', P(3801, 1))]
    report = susa.build_susa(2026, 3, postings, {}, [], [], [])
    by_group = {item.group.key: [account.key for account in item.accounts] for item in report.groups}
    assert by_group['equity'] == [2100, 2180, 2999, susa.PRIOR_RESULT]
    assert by_group['liabilities'] == [3801, 3806, susa.TAX_UNASSIGNED]
    assert by_group['not_posted'] == [susa.NOT_POSTED]


def test_an_account_outside_the_chart_carries_the_suffix_and_a_tax_account_its_own_name():
    report = susa.build_susa(2026, 3, [('year', P(3801, 0, 5)), ('year', P(1406, 5)), ('year', P(9999, 1)), ('year', P(1800, 0, 1))],
                             {1406: ('Abziehbare Vorsteuer 19%', False), 1800: ('Bank Alpha', True)}, [], [], [])
    accounts = helpers.accounts_of(report)
    assert accounts[3801].name == 'Umsatzsteuer 7 % (nicht im Kontenrahmen)' and accounts[3801].outside_chart
    assert accounts[1406].name == 'Abziehbare Vorsteuer 19%' and not accounts[1406].outside_chart
    assert accounts[9999].name == ' (nicht im Kontenrahmen)' and accounts[9999].outside_chart
    assert accounts[1800].name == 'Bank Alpha (nicht im Kontenrahmen)'
    assert accounts[susa.NOT_POSTED].name == 'Nicht kontiert (Gegenseite fehlt)' and accounts[susa.PRIOR_RESULT].name.startswith('Ergebnis der Vorjahre')
    assert accounts[susa.TAX_UNASSIGNED].name == 'Steuer ohne Steuerkonto'


@pytest.mark.parametrize('minor, text', [(0, '0,00'), (12_345_600, '1.234,56 S'), (-12_345_600, '1.234,56 H'), (100, '0,01 S'), (-100, '0,01 H'),
                                         (40, '0,00'), (-40, '0,00'), (10_000_000_000, '1.000.000,00 S')])
def test_a_balance_shows_its_side_with_a_letter_and_zero_without(minor, text):
    assert susa.format_balance(minor) == text


def test_a_row_shows_zeros_only_in_the_closing_balance_and_a_sum_row_shows_everything():
    account = susa.SusaAccount(6815, 6815, 'Bürobedarf', 'expenses', False, False, 0, 0, 0, 100_000, 0)
    assert susa.cells(account, False) == ['', '', '', '10,00', '', '10,00 S']
    assert susa.cells(account, True) == ['0,00', '0,00', '0,00', '10,00', '0,00', '10,00 S']
    account = susa.SusaAccount(1800, 1800, 'Bank', 'current_assets', False, False, -12_345_600, 100_000, 50_000, 100_000, 50_000)
    assert susa.cells(account, False) == ['1.234,56 H', '10,00', '5,00', '10,00', '5,00', '1.229,56 H']
    empty = susa.SusaAccount(1, 1, '', 'fixed_assets', False, False)
    assert susa.cells(empty, False) == ['', '', '', '', '', '0,00'] and susa.cells(empty, True) == ['0,00'] * 6
    assert susa.COLUMN_LABELS == ('Anfangssaldo', 'Soll', 'Haben', 'Soll', 'Haben', 'Endsaldo')


def test_the_block_headers_name_the_month_and_the_accumulated_span():
    assert susa.block_headers(2026, 1) == ('Januar 2026', 'Januar 2026 (aufgelaufen)')
    assert susa.block_headers(2026, 10) == ('Oktober 2026', 'Januar bis Oktober 2026')
    assert susa.block_headers(2025, 3) == ('März 2025', 'Januar bis März 2025')
    assert susa.susa_filename(2026, 10) == 'Summen_und_Salden_2026-10.pdf' and susa.susa_filename(2026, 1) == 'Summen_und_Salden_2026-01.pdf'


def test_every_notice_has_a_sentence_for_one_case_and_one_for_several():
    assert set(susa.NOTICE_TEXTS) == set(susa.NOTICE_ORDER) and len(susa.NOTICE_ORDER) == 16
    for key in susa.NOTICE_ORDER:
        singular, plural = susa.NOTICE_TEXTS[key]
        assert singular != plural, key
        for text in (singular, plural):
            assert 'DATEV' not in text and 'WISO' not in text
        one = susa.notice_text(susa.SusaNotice(key, 1, 12_340_000, 5_000_000, 700_000))
        several = susa.notice_text(susa.SusaNotice(key, 3, 12_340_000, 5_000_000, 700_000))
        assert one.startswith('1 ') and several.startswith('3 '), key
        assert '{' not in one and '{' not in several
    assert susa.notice_text(susa.SusaNotice('unbooked', 3, None, 238_000_000, 11_115_000)) == (
        '3 Bankbewegungen ohne Buchungssatz (Eingänge 23.800,00 €, Ausgänge 1.111,50 €) stehen gegen „Nicht kontiert“.')
    assert susa.notice_text(susa.SusaNotice('legacy_mirror', 1, 1_150_000)) == (
        '1 Umbuchung zwischen Geldkonten aus einem älteren Bestand, deren Richtung unbekannt ist (115,00 €), fehlt in der Liste.')


#: Die Hinweise der Liste in der Reihenfolge der Seite – fest, eine eigene Abschrift (die neuen Hinweise stehen bei „nicht lesbar“ und bei „Bankkonto weicht ab“)
NOTICE_KEYS_IN_ORDER = ('unbooked', 'unbooked_no_number', 'missing_side', 'money_from_parent', 'legacy_mirror', 'no_form', 'not_readable', 'bad_date',
                        'unknown_type', 'zero_amount_tax', 'tax_unassigned', 'tax_not_split', 'unbalanced', 'child_other_period', 'child_other_account',
                        'doubtful')


def test_the_notices_stand_in_a_fixed_order_and_the_new_ones_have_their_sentences_in_both_numbers():
    assert susa.NOTICE_ORDER == NOTICE_KEYS_IN_ORDER
    shuffled = [susa.SusaNotice(key, 2, 10_000) for key in reversed(NOTICE_KEYS_IN_ORDER)]
    assert [notice.key for notice in susa.build_susa(2026, 3, [], {}, shuffled, [], []).notices] == list(NOTICE_KEYS_IN_ORDER)
    new = {
        'bad_date': ('1 Zeile mit einem Datum, das kein Kalendertag ist (19,00 €), lässt sich keinem Monat zuordnen.',
                     '2 Zeilen mit einem Datum, das kein Kalendertag ist (19,00 €), lassen sich keinem Monat zuordnen.'),
        'unknown_type': ('1 Zeile ohne Buchungsart (19,00 €) ist nicht enthalten.',
                         '2 Zeilen ohne Buchungsart (19,00 €) sind nicht enthalten.'),
        'zero_amount_tax': ('1 Buchungssatz mit Betrag 0 und einem Steuerbetrag (19,00 €) ist nicht gebucht: '
                            'Ohne Betrag gibt es keine Seite für die Steuer.',
                            '2 Buchungssätze mit Betrag 0 und einem Steuerbetrag (19,00 €) sind nicht gebucht: '
                            'Ohne Betrag gibt es keine Seite für die Steuer.'),
        'child_other_period': ('1 Buchungssatz (19,00 €) trägt ein anderes Datum als seine Bankbewegung, und das Monatsende liegt dazwischen – '
                               'um ihn kann der Abgleich abweichen.',
                               '2 Buchungssätze (19,00 €) tragen ein anderes Datum als ihre Bankbewegung, und das Monatsende liegt dazwischen – '
                               'um sie kann der Abgleich abweichen.'),
        'child_other_account': ('1 Buchungssatz (19,00 €) unter einer Bankbewegung bucht auf ein anderes eigenes Konto als das der Bewegung – '
                                'um ihn kann der Abgleich dieser Konten abweichen.',
                                '2 Buchungssätze (19,00 €) unter einer Bankbewegung buchen auf ein anderes eigenes Konto als das der Bewegung – '
                                'um sie kann der Abgleich dieser Konten abweichen.'),
    }
    for key, (one, several) in new.items():
        assert susa.notice_text(susa.SusaNotice(key, 1, 190_000)) == one and susa.notice_text(susa.SusaNotice(key, 2, 190_000)) == several, key
        assert key in susa.AMOUNT_NOTICES and key not in susa.TAX_NOTICES
    tally = susa.Tally()
    for key in new:
        tally.notice(key, 2, 190_000)
    assert {notice.key: notice.amount for notice in tally.notices()} == {key: 190_000 for key in new}


def test_the_bank_and_euer_explanations_name_the_new_reasons():
    for reason in ('anderen Datum', 'anderen eigenen Konto', 'nicht lesbare Werte'):
        assert reason in susa.BANK_DIFF_TEXT, reason
    for reason in ('6884–6892', 'Vorsteuer', 'Betrag 0 und Steuerbetrag'):
        assert reason in susa.EUER_DIFF_TEXT, reason


def test_the_texts_name_no_brand_and_the_simplifications():
    text = ' '.join([susa.SUSA_HINT_TEXT, susa.SALDO_TEXT, susa.OPENING_TEXT, susa.NOT_POSTED_TEXT, susa.TRANSFER_TEXT, susa.TAX_TEXT,
                    susa.EUER_DIFF_TEXT, susa.BANK_CHECK_TEXT, susa.BANK_DIFF_TEXT, susa.BANK_SKIPPED_TEXT, susa.SUSA_FOOTER])
    for brand in BRANDS:
        assert brand.lower() not in text.lower(), brand
    assert 'Keine Steuerberatung' in susa.SUSA_HINT_TEXT and 'Keine Steuerberatung' in susa.SUSA_FOOTER
    assert 'Eröffnungsbilanz' in susa.OPENING_TEXT and 'Jahresabschluss' in susa.TAX_TEXT and 'nicht verrechnet' in susa.TAX_TEXT


# ═══ Der Export bleibt, wie er war ════════════════════════════════════════════
class FixedClock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 3, 12, 0, 0)


def export_of(db, date_from, date_to, monkeypatch):
    """Der Stapel, wie ``handle_datev_export`` ihn schreibt (alle Sätze des Zeitraums ohne die weggelassenen Eingangs-Sätze gepaarter Überträge),
    mit festem Zeitstempel im Vorlauf: ``(Bytes, IDs, Sätze)``."""
    omitted = db.duplicate_transfer_rows()
    rows = [row for row in db.fetch_bookings_range(date_from, date_to) if row[0] not in omitted]
    consultant_no, client_no = db.get_datev_numbers()
    fake = types.SimpleNamespace(**{key: getattr(datetime, key) for key in dir(datetime) if not key.startswith('__')})
    fake.datetime = FixedClock
    monkeypatch.setattr(datev, 'datetime', fake)
    csv_bytes, ids = datev.export_to_datev(rows, db.get_coa_id_to_number_map(), date_from, date_to, liquid_coa_ids=db.get_payment_coa_ids(),
                                           waiting_numbers=db.get_waiting_account_numbers(), invoices=db.get_datev_invoice_data(),
                                           consultant_no=consultant_no, client_no=client_no)
    return csv_bytes, ids, {row[0]: row for row in rows}


@pytest.mark.parametrize('case', sorted(EXPORT_SNAPSHOTS))
def test_the_export_is_byte_for_byte_what_it_was_before_the_row_reading_moved_to_one_place(case, request, monkeypatch):
    date_from, date_to, count, digest = EXPORT_SNAPSHOTS[case]
    db = request.getfixturevalue(case.rsplit('_20', 1)[0])
    csv_bytes, ids, _rows = export_of(db, date_from, date_to, monkeypatch)
    assert len(ids) == count
    assert hashlib.sha256(csv_bytes).hexdigest() == digest, case


def test_the_ledger_rows_are_the_rows_the_export_reads(world):
    """Die Liste und der Export lesen ihre Zeilen mit demselben ``WHERE``: Eine Anfrage mit ``LEDGER_ROWS_WHERE`` findet genau die Sätze von
    ``fetch_bookings_range`` (ohne die Bankbewegungen)."""
    from db.accounts import LEDGER_ROWS_WHERE
    with closing(sqlite3.connect(world.db_name)) as con:
        found = [row[0] for row in con.execute(f'SELECT e.ID FROM Bookings e WHERE {LEDGER_ROWS_WHERE.format(a="e.")} ORDER BY e.DateBooking',
                                               ('2026-03-01', '2026-06-30'))]
    assert found and sorted(found) == sorted(row[0] for row in world.fetch_bookings_range('2026-03-01', '2026-06-30'))


#: Die Formen, bei denen ein positiver Betrag das Feld „Konto“ in das Soll stellt (Geldkonto vorne, Umbuchung, Spiegel mit dem Betrag auf dem Konto) – eine
#: eigene Abschrift der Tabelle aus ``db/matching.py``, damit der Vergleich auch dann anschlägt, wenn Export UND Liste dieselbe Funktion falsch läsen
KONTO_IM_SOLL_BEI_POSITIVEM_BETRAG = ('liquid_first', 'transfer', 'mirror_coa')


def line_checks(db, date_from, date_to, monkeypatch):
    """Für jede Zeile des Exports: Das Kennzeichen S/H stimmt mit der Tabelle der Formen überein, und die Liste bucht den Bruttobetrag auf dieselben
    Konten und dieselben Seiten. Returns die Zahl der Zeilen und die Abweichungen."""
    csv_bytes, ids, rows = export_of(db, date_from, date_to, monkeypatch)
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    numbers = db.get_coa_id_to_number_map()
    liquid, waiting = db.get_payment_coa_ids(), db.get_waiting_account_numbers()
    deviations = []
    for booking_id, line in zip(ids, lines):
        row = rows[booking_id]
        fields = next(csv.reader([line], delimiter=';', quotechar='"'))
        coa_id, counter_id, amount, rate = row[8], row[9], to_minor(row[11]), row[13]
        form, side = ledger_form(row[22], coa_id, counter_id, liquid)
        coa_number = datev_keys.account_number(numbers.get(coa_id)) if coa_id else None
        counter_number = datev_keys.account_number(numbers.get(counter_id)) if counter_id else None
        kind = datev_keys.booking_kind(form, coa_number, counter_number, amount >= 0, waiting)
        found, _notices = susa.entry_postings(form, side, kind, coa_number, counter_number, amount, 0, rate, waiting)
        if form == FORM_MIRROR_LEGACY or amount == 0:
            if found:
                deviations.append((booking_id, 'bucht, was es nicht dürfte'))
            continue
        konto_im_soll = (amount >= 0) == (form in KONTO_IM_SOLL_BEI_POSITIVEM_BETRAG)
        if fields[1] != ('S' if konto_im_soll else 'H'):
            deviations.append((booking_id, form, 'S/H weicht von der Tabelle der Formen ab', fields[1]))
        konto, gegenkonto = (int(fields[6]) if fields[6] else susa.NOT_POSTED), (int(fields[7]) if fields[7] else susa.NOT_POSTED)
        soll, haben = (konto, gegenkonto) if fields[1] == 'S' else (gegenkonto, konto)
        shown = datev._fmt_amount(float(from_minor(amount)))
        expected = sorted([(soll, abs(amount), 0), (haben, 0, abs(amount))], key=lambda item: (str(item[0]), item[1], item[2]))
        if tuples(found) != expected or fields[0] != shown:
            deviations.append((booking_id, form, fields[:2] + fields[6:8], tuples(found)))
    return len(ids), deviations


@pytest.mark.parametrize('fixture, date_from, date_to', [('world', '2026-01-01', '2026-12-31'), ('demo_db', '2025-01-01', '2025-12-31'),
                                                         ('demo_db', '2026-01-01', '2026-12-31')])
def test_every_row_books_the_side_the_export_writes_in_its_line(fixture, date_from, date_to, request, monkeypatch):
    """Keine dritte Lesart: Auf der Ebene der einzelnen Zeile – nicht der Summen – steht das Konto, das der Export in das Feld „Konto“ schreibt, im
    Soll, wo er S schreibt, sonst im Haben; ein fehlendes Konto steht auf „Nicht kontiert“; der Betrag ist der des Exports."""
    count, deviations = line_checks(request.getfixturevalue(fixture), date_from, date_to, monkeypatch)
    assert count > 40 and deviations == []


# ═══ get_susa: die Testwelt (Probe a) und die Hinweise ════════════════════════
def test_the_books_balance_in_every_month_of_the_test_world(world):
    for year, month in helpers.every_month(2026):
        helpers.assert_balanced(world.get_susa(year, month), (year, month))


def test_the_test_world_names_the_rows_it_cannot_read_the_way_the_export_does(world):
    """Die Testwelt hat Zeilen ohne Form (Roh-SQL), Spiegel des Bestands, Sätze ohne Konto, einen unstimmigen Split und eine Bankbewegung ohne Satz."""
    report = world.get_susa(2026, 12)
    notices = {notice.key: notice for notice in report.notices}
    assert (notices['no_form'].count, notices['legacy_mirror'].count, notices['missing_side'].count) == (11, 4, 5)
    assert (notices['unbalanced'].count, notices['unbalanced'].amount) == (1, minor_of('10.00'))
    assert notices['unbooked'].count == 1 and notices['unbooked'].incoming == minor_of('119.00') and notices['unbooked'].outgoing == 0
    assert [notice.key for notice in report.notices] == [key for key in susa.NOTICE_ORDER if key in notices]
    assert 'legacy_mirror' not in {notice.key for notice in world.get_susa(2026, 5).notices}         # die Zeilen aus Juni liegen noch nicht darin


def minor_of(euro):
    return to_minor(Decimal(euro))


def test_the_list_counts_the_unbooked_movements_the_export_reports(world, demo_db, susa_world):
    """Die Bankbewegungen ohne Satz sind die, die der Export als Hinweis nennt (``check_transfers``), über den ganzen Bestand bis Monatsende."""
    for db, year, month in ((world, 2026, 12), (demo_db, 2026, 11), (demo_db, 2026, 10), (susa_world.db, 2026, 8), (susa_world.db, 2026, 7)):
        end = helpers.span_of(year, month)[1]
        check = db.check_transfers('0000-01-01', end)
        notices = {notice.key: notice for notice in db.get_susa(year, month).notices}
        assert (notices['unbooked'].count if 'unbooked' in notices else 0) == len(check.unbooked), (year, month)
        if check.unbooked:
            assert notices['unbooked'].incoming == sum(to_minor(amount) for _id, _date, amount in check.unbooked if amount > 0)
            assert notices['unbooked'].outgoing == -sum(to_minor(amount) for _id, _date, amount in check.unbooked if amount < 0)


# ═══ get_susa: die SuSa-Welt (Proben a, b, c) ═════════════════════════════════
def test_the_books_balance_and_the_banks_and_the_profit_accounts_agree_in_every_month_of_the_susa_world(susa_world):
    deviations = []
    for year, month in [(2025, 12), *helpers.every_month(2026)]:
        report = susa_world.db.get_susa(year, month)
        helpers.assert_balanced(report, (year, month))
        deviations += helpers.check_bank_accounts(susa_world.db, report, (year, month))
        deviations += helpers.check_erfolgskonten_against_euer(susa_world.db, report, (year, month))
    assert deviations == []


def test_the_susa_world_has_the_cases_it_is_built_for(susa_world):
    db = susa_world.db
    settlement = db.duplicate_transfer_rows()
    assert len(settlement) == 1                                                                   # der Eingangs-Satz des Übertrags am 1. Juli
    covered = db.check_transfers('2026-01-01', '2026-12-31')
    assert [amount for _id, _date, amount in covered.unbooked] == [Decimal('119.00')]            # nur die Bewegung vom August; die vom 8. Juli ist gedeckt
    with closing(sqlite3.connect(db.db_name)) as con:
        forms = {row[0] for row in con.execute('SELECT DISTINCT EntryForm FROM Bookings WHERE EntryForm IS NOT NULL')}
        assert {'purpose_first', 'liquid_first', 'transfer', 'mirror_counter'} <= forms
        storno = con.execute("SELECT COUNT(*) FROM Invoices WHERE InvoiceKind = 'storno'").fetchone()[0]
        refunds = con.execute('SELECT COUNT(*) FROM InvoicePayments WHERE Amount < 0').fetchone()[0]
        depreciation = con.execute('SELECT COUNT(*) FROM AssetDepreciations WHERE Booking_ID IS NOT NULL').fetchone()[0]
    assert (storno, refunds, depreciation) == (1, 1, 1)


def test_the_waiting_account_carries_over_the_year_end_and_is_empty_when_the_invoice_is_paid(susa_world):
    """Die Rechnung vom Dezember 2025 steht über den Jahreswechsel auf dem Wartekonto (und als Forderung); mit der Zahlung im Januar sind beide leer."""
    december, january = susa_world.db.get_susa(2025, 12), susa_world.db.get_susa(2026, 1)
    accounts = helpers.accounts_of(december)
    assert (accounts[4405].closing, accounts[10000].closing) == (-minor_of('238.00'), minor_of('238.00'))
    first = helpers.accounts_of(january)
    assert (first[4405].opening, first[10000].opening) == (-minor_of('238.00'), minor_of('238.00'))          # Anfangssaldo ≠ 0
    assert first[4405].closing == 0 and first[10000].closing == 0                                       # Ende Januar: bezahlt
    assert (first[4405].month_soll, first[10000].month_haben) == (minor_of('238.00'), minor_of('238.00'))
    assert susa.group_key(4405) == 'open_items' and first[4405].group == 'open_items'


def test_the_profit_accounts_start_at_zero_and_the_result_of_the_previous_years_stands_with_the_equity(susa_world):
    december, january = susa_world.db.get_susa(2025, 12), susa_world.db.get_susa(2026, 1)
    accounts = helpers.accounts_of(january)
    for number, account in accounts.items():
        if isinstance(number, int) and not susa.is_carried(number):
            assert account.opening == 0, number                                                    # Erfolgskonten beginnen bei null
    expected = sum(account.closing for number, account in helpers.accounts_of(december).items() if isinstance(number, int) and not susa.is_carried(number))
    assert expected == -minor_of('50.00')                                                           # 100,00 Erlös im Haben, 50,00 Aufwand im Soll
    result = accounts[susa.PRIOR_RESULT]
    assert (result.opening, result.closing, result.group) == (expected, expected, 'equity')
    assert helpers.accounts_of(december)[susa.PRIOR_RESULT].opening == 0                           # im ersten Jahr gibt es keine Vorjahre
    assert accounts[2100].opening == minor_of('100.00')                                               # das Privatkonto trägt vor (kein Abschluss)


def test_a_paired_transfer_counts_once_with_the_date_of_its_outgoing_side(susa_world, monkeypatch):
    """Ein Übertrag 1800 → 1810, auf beiden Bankbewegungen kontiert (1. Juli), und einer mit satzloser Gegenseite (8. Juli): Das Konto 1810 bekommt sein
    Soll aus dem Ausgangs-Satz; der Eingangs-Satz zählt nicht. Ohne die weggelassenen Sätze stünde der erste Übertrag doppelt da, und das
    Bankkonto weichte um ihn ab."""
    db = susa_world.db
    july = helpers.accounts_of(db.get_susa(2026, 7))
    assert (july[1810].month_soll, july[1810].month_haben) == (minor_of('800.00'), 0)             # 500 (1. Juli) + 300 (8. Juli), je einmal
    assert (july[1800].month_haben, july[1800].month_soll) == (minor_of('800.00'), 0)
    real = db._settle_stock
    monkeypatch.setattr(db, '_settle_stock', lambda cursor: real(cursor)._replace(omitted={}))
    doubled = db.get_susa(2026, 7)
    assert helpers.accounts_of(doubled)[1810].month_soll == minor_of('1300.00')
    deviations = {(deviation[1], deviation[2]): deviation[4] - deviation[3] for deviation in helpers.check_bank_accounts(db, doubled)}
    assert deviations == {('Probe-Zweit', 'Endsaldo'): minor_of('500.00'), ('Probe-Zweit', 'aufgelaufen'): minor_of('500.00'),
                          ('Probe-Giro', 'Endsaldo'): -minor_of('500.00'), ('Probe-Giro', 'aufgelaufen'): -minor_of('500.00')}


def test_a_movement_without_a_row_is_booked_against_not_posted_unless_a_transfer_covers_it(susa_world):
    db = susa_world.db
    august = helpers.accounts_of(db.get_susa(2026, 8))
    assert (august[susa.NOT_POSTED].month_haben, august[susa.NOT_POSTED].month_soll) == (minor_of('119.00'), 0)
    assert august[susa.NOT_POSTED].closing == -minor_of('119.00')
    notices = {notice.key: notice for notice in db.get_susa(2026, 8).notices}
    assert notices['unbooked'] == susa.SusaNotice('unbooked', 1, None, minor_of('119.00'), 0)
    july = db.get_susa(2026, 7)
    assert 'unbooked' not in {notice.key for notice in july.notices}                               # die Bewegung vom 8. Juli deckt der Übertrag
    assert helpers.accounts_of(july)[susa.NOT_POSTED].closing == 0 and susa.NOT_POSTED in helpers.accounts_of(july)
    assert not susa.visible(helpers.accounts_of(july)[susa.NOT_POSTED])                              # und die Zeile steht nicht da


def test_the_bank_check_lists_the_banks_with_their_own_number_and_not_the_cash_register(susa_world):
    report = susa_world.db.get_susa(2026, 12)
    assert [(check.name, check.number, check.difference) for check in report.bank_checks] == [
        ('Probe-Giro', 1800, 0), ('Probe-Zweit', 1810, 0)] and report.bank_skipped == []
    assert report.bank_checks[0].movements == report.bank_checks[0].closing


#: Die Umsatzsteuerkonten, die der Standard-Kontenrahmen seit 2.6.1 zusätzlich führt (``seed_data/chart_of_accounts_skr04.json``): Ein Bestand von vorher
#: hat sie nicht. 3801 ist das Konto der Umsatzsteuer zu 7 % und 5 %, auf dem die Liste die Steuer bucht.
SALES_TAX_ACCOUNTS = [(3800, 'Umsatzsteuer'), (3801, 'Umsatzsteuer 7%'), (3802, 'Umsatzsteuer aus innergemeinschaftlichem Erwerb'),
                      (3804, 'Umsatzsteuer aus innergemeinschaftlichem Erwerb 19%'), (3830, 'Umsatzsteuer-Vorauszahlungen 1/11'),
                      (3841, 'Umsatzsteuer Vorjahr')]


def chart_rows(db, *numbers):
    """Die Zeilen ``(Nummer, Name, Gruppe, Standard)`` dieser Konten im Kontenrahmen 4 der Datenbank, nach Nummer."""
    marks = ', '.join('?' * len(numbers))
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT AccountNumber, Name, Description, IsStandard FROM ChartOfAccounts '
                           f'WHERE Framework = 4 AND AccountNumber IN ({marks}) ORDER BY AccountNumber', numbers).fetchall()


def without_accounts(source, path, *numbers):
    """Eine Kopie der Datenbank ``source`` (über die Sicherungsschnittstelle von SQLite), deren Kontenrahmen die Konten ``numbers`` nicht führt – so sieht ein
    Bestand von vor 2.6.1 aus. Es bucht kein Satz auf diese Konten; geöffnet wird die Kopie wie jede Datenbank (der Start legt nie Konten nach)."""
    path = str(path)
    with closing(sqlite3.connect(source.db_name)) as origin, closing(sqlite3.connect(path)) as copy:
        origin.backup(copy)
    marks = ', '.join('?' * len(numbers))
    helpers.raw(Database(path), f'DELETE FROM ChartOfAccounts WHERE Framework = 4 AND AccountNumber IN ({marks})', *numbers)
    db = Database(path)
    assert chart_rows(db, *numbers) == []
    return db


def assert_3801_follows_the_chart(db, year, month):
    """3801 steht im Bericht und auf der Seite mit dem Namen des Kontenrahmens, mit Werten und ohne den Zusatz – und kein Konto der Liste trägt ihn."""
    accounts = helpers.accounts_of(db.get_susa(year, month))
    assert accounts[3801].ytd_haben > 0                                                           # die Umsatzsteuer zu 7 % ist gebucht
    assert (accounts[3801].name, accounts[3801].outside_chart) == ('Umsatzsteuer 7%', False)
    assert not any(account.outside_chart for account in accounts.values())
    rows = table_rows(page_of(db, year, month))
    assert [row.startswith('3801 | Umsatzsteuer 7% | ') for row in rows if row.startswith('3801 | ')] == [True]
    assert not any(susa.NOT_IN_CHART_SUFFIX in row for row in rows)


def test_the_tax_is_booked_on_the_tax_accounts_by_rate_in_the_susa_world(susa_world):
    accounts = helpers.accounts_of(susa_world.db.get_susa(2026, 12))
    assert accounts[3806].ytd_haben > 0 and accounts[3801].ytd_haben > 0 and accounts[1406].ytd_soll > 0 and accounts[1401].ytd_soll > 0
    assert susa.TAX_UNASSIGNED not in accounts or not susa.visible(accounts[susa.TAX_UNASSIGNED])
    assert not any(accounts[number].outside_chart for number in (1401, 1406, 3801, 3806))        # der Standard-Kontenrahmen führt alle vier


def test_a_fresh_book_names_3801_after_the_chart_and_shows_no_suffix(susa_world):
    """Der Standard-Kontenrahmen führt 3801 „Umsatzsteuer 7%“ seit 2.6.1. Ein frischer Bestand zeigt das Konto im Bericht (``get_susa``) und auf der Seite
    (``/susa``) mit diesem Namen und ohne den Zusatz „(nicht im Kontenrahmen)“, der nur noch dort steht, wo der Kontenrahmen das Konto nicht führt."""
    assert_3801_follows_the_chart(susa_world.db, 2026, 12)


def test_the_demo_stock_names_3801_after_the_chart_and_shows_no_suffix(demo_db):
    """Dasselbe im Demobestand: Seine Umsatzsteuer zu 7 % (Kaffee, Tee) steht auf 3801 „Umsatzsteuer 7%“, ohne Zusatz."""
    assert_3801_follows_the_chart(demo_db, 2026, 10)


def test_a_book_whose_chart_lacks_3801_names_it_after_the_table_and_shows_the_suffix(susa_world, tmp_path):
    """Ein Bestand von vor 2.6.1 – oder einer, dessen Nutzer das Konto gelöscht hat – führt 3801 nicht, und es bucht kein Satz darauf: Die Liste nennt das
    Konto weiter mit dem Namen aus ``TAX_ACCOUNT_NAMES`` und dem Zusatz „(nicht im Kontenrahmen)“, im Bericht und auf der Seite. Die übrigen Steuerkonten
    und der Betrag bleiben unberührt."""
    db = without_accounts(susa_world.db, tmp_path / 'ohne3801.db', 3801)
    accounts = helpers.accounts_of(db.get_susa(2026, 12))
    assert accounts[3801].name == 'Umsatzsteuer 7 % (nicht im Kontenrahmen)' and accounts[3801].outside_chart
    assert not any(accounts[number].outside_chart for number in (1401, 1406, 3806))
    assert accounts[3801].ytd_haben == helpers.accounts_of(susa_world.db.get_susa(2026, 12))[3801].ytd_haben > 0
    rows = table_rows(page_of(db, 2026, 12))
    assert any(row.startswith('3801 | Umsatzsteuer 7 % (nicht im Kontenrahmen) | ') for row in rows)
    assert len([row for row in rows if susa.NOT_IN_CHART_SUFFIX in row]) == 1


def test_a_book_from_before_2_6_1_gets_the_sales_tax_accounts_through_the_missing_standard_accounts(susa_world, tmp_path):
    """Der Kontenrahmen eines bestehenden Bestands bleibt beim Öffnen, wie er ist: Einem Bestand von vor 2.6.1 fehlen die sechs Umsatzsteuerkonten des Standard-
    Kontenrahmens (3800, 3801, 3802, 3804, 3830, 3841). ``missing_standard_accounts`` nennt sie mit dem Namen der Seed-Datei, ``add_missing_standard_accounts``
    legt sie an – danach zeigt die Liste 3801 mit dem Namen des Kontenrahmens und ohne Zusatz."""
    numbers = [number for number, _name in SALES_TAX_ACCOUNTS]
    expected = [(number, name, 'Fremdkapitalkonten', 1) for number, name in SALES_TAX_ACCOUNTS]
    assert chart_rows(susa_world.db, *numbers) == expected                                         # ein frischer Bestand führt alle sechs
    db = without_accounts(susa_world.db, tmp_path / 'alt.db', *numbers)
    assert helpers.accounts_of(db.get_susa(2026, 12))[3801].outside_chart                          # vorher: mit Zusatz
    assert db.missing_standard_accounts() == SALES_TAX_ACCOUNTS
    assert db.add_missing_standard_accounts() == len(SALES_TAX_ACCOUNTS)
    assert db.missing_standard_accounts() == []
    assert chart_rows(db, *numbers) == expected
    assert_3801_follows_the_chart(db, 2026, 12)


def assert_the_sales_tax_accounts_change_no_number(db, tmp_path, monkeypatch, spans):
    """Derselbe Bestand mit und ohne die sechs Umsatzsteuerkonten im Kontenrahmen: EÜR (``get_euer_data``), BWA, Dashboard und der Stapel für das Steuerbüro
    (Byte für Byte) sind gleich. Spannen: ``(von, bis, Jahr, Monat)`` – ``von`` und ``bis`` für EÜR, Dashboard und Stapel, Jahr und Monat für die BWA."""
    older = without_accounts(db, tmp_path / 'ohne.db', *(number for number, _name in SALES_TAX_ACCOUNTS))
    for date_from, date_to, year, month in spans:
        where = (date_from, date_to)
        assert older.get_euer_data(date_from, date_to) == db.get_euer_data(date_from, date_to) != [], where
        assert older.get_bwa(year, month, 'vorjahr') == db.get_bwa(year, month, 'vorjahr'), where
        assert older.get_dashboard(date_from, date_to) == db.get_dashboard(date_from, date_to), where
        assert export_of(older, date_from, date_to, monkeypatch)[0] == export_of(db, date_from, date_to, monkeypatch)[0], where


def test_the_sales_tax_accounts_in_the_chart_change_no_number_of_the_susa_world(susa_world, tmp_path, monkeypatch):
    """Das bloße Vorhandensein der Konten im Kontenrahmen ändert keine Zahl: Die Umsatzsteuer der Einnahmen steht in der EÜR auf der virtuellen Zeile 3806,
    gesucht werden nur feste Nummern und die Bereiche der Anlage-, Erfolgs- und Erlöskonten – und es bucht kein Satz auf die neuen Konten."""
    assert_the_sales_tax_accounts_change_no_number(susa_world.db, tmp_path, monkeypatch, [('2026-01-01', '2026-12-31', 2026, 12)])


def test_the_sales_tax_accounts_in_the_chart_change_no_number_of_the_demo_stock(demo_db, tmp_path, monkeypatch):
    assert_the_sales_tax_accounts_change_no_number(demo_db, tmp_path, monkeypatch, [('2025-01-01', '2025-12-31', 2025, 12), ('2026-01-01', '2026-10-31', 2026, 10)])


def test_a_tax_account_of_the_chart_keeps_its_own_name(tmp_path):
    """Ein Konto des Kontenrahmens trägt seinen eigenen Namen – auch ein umbenanntes –, nie den aus ``TAX_ACCOUNT_NAMES``."""
    db = Database(str(tmp_path / 't.db'))
    helpers.raw(db, 'UPDATE ChartOfAccounts SET Name = ? WHERE Framework = 4 AND AccountNumber = 3801', 'Umsatzsteuer 7 % (Kontenrahmen)')
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    helpers.mask(db, 'Probe-Giro', 4300, '107.00', '2026-03-10', '7', '7.00')
    account = helpers.accounts_of(db.get_susa(2026, 3))[3801]
    assert account.name == 'Umsatzsteuer 7 % (Kontenrahmen)' and not account.outside_chart and account.month_haben == minor_of('7.00')


def test_a_month_of_the_susa_world_lists_its_accounts_in_the_order_of_the_groups(susa_world):
    report = susa_world.db.get_susa(2026, 3)
    keys = [account.key for group_ in susa.visible_groups(report) for account in susa.visible_accounts(group_)]
    positions = [[g.key for g in susa.GROUPS].index(susa.group_key(key)) for key in keys]
    assert positions == sorted(positions) and len(keys) == len(set(keys))
    for group_ in susa.visible_groups(report):
        numbers = [account.number for account in susa.visible_accounts(group_) if not account.virtual]
        assert numbers == sorted(numbers)


# ═══ Der Demobestand ══════════════════════════════════════════════════════════
@pytest.mark.parametrize('year, month', [(2025, 12), (2026, 1), (2026, 10), (2026, 11)])
def test_the_demo_books_balance(demo_db, year, month):
    helpers.assert_balanced(demo_db.get_susa(year, month), (year, month))


def test_the_demo_books_balance_in_every_month_of_both_years(demo_db):
    for year, month in helpers.every_month(2025) + helpers.every_month(2026):
        helpers.assert_balanced(demo_db.get_susa(year, month), (year, month))


@pytest.mark.parametrize('year, month', [(2025, 12), (2026, 1), (2026, 10), (2026, 11)])
def test_the_demo_banks_agree_with_the_bank_movements(demo_db, year, month):
    assert helpers.check_bank_accounts(demo_db, demo_db.get_susa(year, month), (year, month)) == []


def test_the_demo_profit_accounts_agree_with_the_euer_in_every_month_of_2025_and_in_the_months_of_2026(demo_db):
    deviations = []
    for year, month in helpers.every_month(2025) + [(2026, 1), (2026, 10), (2026, 11)]:
        deviations += helpers.check_erfolgskonten_against_euer(demo_db, demo_db.get_susa(year, month), (year, month))
    assert deviations == []


def test_the_demo_has_three_open_movements_in_the_current_month_and_none_in_the_month_before(demo_db):
    november = {notice.key: notice for notice in demo_db.get_susa(2026, 11).notices}
    assert november['unbooked'].count == 3
    assert 'unbooked' not in {notice.key for notice in demo_db.get_susa(2026, 10).notices}
    assert not [notice for notice in demo_db.get_susa(2026, 10).notices]                         # ein abgeschlossener Monat: keine Hinweise


# ═══ get_susa liest nur ═══════════════════════════════════════════════════════
def test_the_list_writes_and_stamps_nothing(susa_world):
    db = susa_world.db
    with closing(sqlite3.connect(db.db_name)) as con:
        stamps = con.execute('SELECT ID, DateTax FROM Bookings ORDER BY ID').fetchall()
    before = dump(db.db_name)
    for year, month in [(2025, 12), (2026, 3), (2026, 12)]:
        db.get_susa(year, month)
    assert dump(db.db_name) == before
    with closing(sqlite3.connect(db.db_name)) as con:
        assert con.execute('SELECT ID, DateTax FROM Bookings ORDER BY ID').fetchall() == stamps


@pytest.mark.parametrize('year, month', [(2026, 0), (2026, 13), (2026, -1), (0, 1), (10000, 1), ('2026', 1), (2026, '3'), (2026.0, 3), (None, 3)])
def test_a_month_or_a_year_outside_the_calendar_is_refused(tmp_db, year, month):
    with pytest.raises(ValueError):
        tmp_db.get_susa(year, month)


def test_an_empty_book_has_an_empty_list_with_zero_totals(tmp_db):
    report = tmp_db.get_susa(2026, 3)
    assert helpers.total_row(report) == (0, 0, 0, 0, 0, 0) and susa.visible_groups(report) == [] and report.notices == []
    assert [(check.name, check.closing, check.movements) for check in report.bank_checks] == []         # nur die Kasse – ohne Gegenprobe
    assert report.periods == {'month': ('2026-03-01', '2026-03-31'), 'ytd': ('2026-01-01', '2026-03-31')}
    assert susa.block_headers(report.year, report.month) == ('März 2026', 'Januar bis März 2026')


# ═══ Nicht Lesbares: Roh-SQL ══════════════════════════════════════════════════
@pytest.fixture
def raw_db(tmp_db):
    tmp_db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return tmp_db


def notices_of(report):
    return {notice.key: notice for notice in report.notices}


@pytest.mark.parametrize('amount', [12.5, 'abc', 10 ** 16, -(10 ** 16), 1e300])
def test_an_amount_that_is_no_whole_number_in_the_readable_range_is_not_booked_and_counted(raw_db, amount):
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, amount)
    helpers.raw_entry(raw_db, '2026-03-06', 6850, 1800, -50_000)
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['not_readable'].count == 1
    accounts = helpers.accounts_of(report)
    assert 6815 not in accounts and accounts[6850].month_soll == 50_000
    helpers.assert_balanced(report)


def test_a_tax_amount_that_is_no_whole_number_makes_the_row_unreadable(raw_db):
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, -119_000, rate=0.19, tax=-19_000.5)
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['not_readable'].count == 1 and 6815 not in helpers.accounts_of(report)
    helpers.assert_balanced(report)


@pytest.mark.parametrize('amount', ['abc', 12.5, 10 ** 16])
def test_an_unreadable_amount_under_a_bank_movement_does_not_stop_the_list(raw_db, amount):
    """``find_unbalanced_splits`` bricht bei einem Betrag, der Text ist, mit einem Fehler ab; die Liste rechnet ihre Hinweise zu den Bankbewegungen mit
    den lesbaren Beträgen selbst und zählt den Rest als nicht lesbar."""
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Probe-Giro'), -119_000)
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, -100_000, parent=parent)
    helpers.raw_entry(raw_db, '2026-03-05', 6850, 1800, amount, parent=parent)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert found['not_readable'].count == 1 and (found['unbalanced'].count, found['unbalanced'].amount) == (1, 19_000)
    helpers.assert_balanced(report)


def test_the_unbalanced_notice_is_what_find_unbalanced_splits_reports(world, demo_db, susa_world):
    """Dieselbe Regel, dieselbe Auswahl – nur robust gegen Beträge, die sich nicht lesen lassen: Wo ``find_unbalanced_splits`` rechnen kann, nennt die Liste
    dieselben Bankbewegungen mit derselben Summe der Reste."""
    for db, year, month in ((world, 2026, 12), (world, 2026, 5), (demo_db, 2026, 11), (demo_db, 2025, 6), (susa_world.db, 2026, 12)):
        found = db.find_unbalanced_splits('0000-01-01', helpers.span_of(year, month)[1])
        notices = notices_of(db.get_susa(year, month))
        if found:
            assert (notices['unbalanced'].count, notices['unbalanced'].amount) == (len(found), sum(to_minor(abs(rest)) for *_ignored, rest, _text in found))
        else:
            assert 'unbalanced' not in notices, (year, month)
    assert notices_of(world.get_susa(2026, 12))['unbalanced'].count == 1


@pytest.mark.parametrize('date', ['01.02.2026', '2026-3-5', '2026-03-05 10:00:00', '2026-03-05T10:00'])
def test_a_date_that_is_not_in_the_form_of_the_books_is_not_booked_and_counted_with_its_amount(raw_db, date):
    """Jeder dieser Texte liegt als Text vor ``2027-12-31`` und damit im Zeitraum – gelesen werden kann er nicht."""
    helpers.raw_entry(raw_db, date, 6815, 1800, -50_000)
    helpers.raw_entry(raw_db, '2027-03-06', 6850, 1800, -70_000)
    report = raw_db.get_susa(2027, 12)
    found = notices_of(report)
    assert 'bad_date' in found and found['bad_date'].count == 1 and found['bad_date'].amount == 50_000, date
    assert 6850 in helpers.accounts_of(report) and 6815 not in helpers.accounts_of(report)
    helpers.assert_balanced(report)


def test_an_account_without_a_number_puts_its_side_on_not_posted(raw_db):
    helpers.raw(raw_db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name) VALUES (777777, 4, NULL, 'Konto ohne Nummer')")
    helpers.raw(raw_db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, BookingType, EntryForm) "
                        "VALUES ('2026-03-05', 777777, ?, -50000, 'entry', 'purpose_first')", helpers.coa(raw_db, 1800))
    helpers.raw(raw_db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name) VALUES (777778, 4, -5, 'Konto mit negativer Nummer')")
    helpers.raw(raw_db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, BookingType, EntryForm) "
                        "VALUES ('2026-03-06', 777778, ?, -70000, 'entry', 'purpose_first')", helpers.coa(raw_db, 1800))
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['missing_side'].count == 2 and notices_of(report)['missing_side'].amount == 120_000
    assert helpers.accounts_of(report)[susa.NOT_POSTED].month_soll == 120_000
    helpers.assert_balanced(report)


def test_a_row_without_any_account_stands_on_not_posted_on_both_sides(raw_db):
    helpers.raw_entry(raw_db, '2026-03-05', None, None, 8_000_000, form=None)
    report = raw_db.get_susa(2026, 3)
    account = helpers.accounts_of(report)[susa.NOT_POSTED]
    assert (account.month_soll, account.month_haben, account.closing) == (8_000_000, 8_000_000, 0) and susa.visible(account)
    assert notices_of(report)['missing_side'].count == 1
    helpers.assert_balanced(report)


def test_a_movement_on_an_account_without_a_number_is_counted_and_not_booked(raw_db):
    raw_db.insert_account('Ohne Nummer', 'Ich', 'DE04', 'BIC', 'Bank', is_cash=0, skr_account=None)
    nameless = helpers.account_id(raw_db, 'Ohne Nummer')
    helpers.raw_bank(raw_db, '2026-03-05', nameless, 50_000)
    helpers.raw_bank(raw_db, '2026-03-06', None, -70_000)
    helpers.raw_bank(raw_db, '2026-03-07', helpers.account_id(raw_db, 'Probe-Giro'), 90_000)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert (found['unbooked_no_number'].count, found['unbooked_no_number'].amount) == (2, 120_000)
    assert (found['unbooked'].count, found['unbooked'].incoming) == (1, 90_000)
    assert [(check.name, check.closing, check.movements) for check in report.bank_checks] == [('Probe-Giro', 90_000, 90_000)]
    assert report.bank_skipped == ['Ohne Nummer']
    helpers.assert_balanced(report)


def test_an_unreadable_bank_movement_is_counted_and_a_movement_with_a_bad_date_stays_unbooked_and_named(raw_db):
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    helpers.raw_bank(raw_db, '2026-03-05', giro, 'abc')
    helpers.raw_bank(raw_db, '2026-03-05', giro, 12.5)
    helpers.raw_bank(raw_db, '05.03.2026', giro, 40_000)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert found['not_readable'].count == 2 and found['bad_date'].count == 1 and found['bad_date'].amount == 40_000
    assert 'unbooked' not in found and [(check.closing, check.movements) for check in report.bank_checks] == [(0, 40_000)]   # die Abweichung ist erklärt
    helpers.assert_balanced(report)


def test_a_child_whose_bank_movement_has_no_number_is_not_posted_on_the_money_side(raw_db):
    raw_db.insert_account('Ohne Nummer', 'Ich', 'DE04', 'BIC', 'Bank', is_cash=0, skr_account=None)
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Ohne Nummer'), -50_000)
    helpers.raw_entry(raw_db, '2026-03-05', 6815, None, -50_000, parent=parent)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert 'money_from_parent' not in found and found['missing_side'].count == 1
    assert helpers.accounts_of(report)[susa.NOT_POSTED].month_haben == 50_000
    helpers.assert_balanced(report)


def test_a_child_without_a_money_account_counts_for_the_bank_of_its_movement_and_the_bank_check_agrees(raw_db):
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Probe-Giro'), -119_000)
    helpers.raw_entry(raw_db, '2026-03-05', 6815, None, -119_000, parent=parent, rate=0.19, tax=-19_000)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert (found['money_from_parent'].count, found['money_from_parent'].amount) == (1, 119_000) and 'missing_side' not in found
    accounts = helpers.accounts_of(report)
    assert (accounts[6815].month_soll, accounts[1406].month_soll, accounts[1800].month_haben) == (100_000, 19_000, 119_000)
    assert helpers.check_bank_accounts(raw_db, report) == [] and raw_db.find_unbalanced_splits('2026-03-01', '2026-03-31') == []
    helpers.assert_balanced(report)


def test_a_split_that_does_not_add_up_is_named_and_the_bank_check_shows_the_rest(raw_db):
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Probe-Giro'), -119_000)
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, -100_000, parent=parent)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert (found['unbalanced'].count, found['unbalanced'].amount) == (1, 19_000)
    check = report.bank_checks[0]
    assert (check.closing, check.movements, check.difference) == (-100_000, -119_000, 19_000)
    helpers.assert_balanced(report)


def test_a_standalone_row_on_the_bank_without_a_movement_shows_in_the_bank_check_as_a_difference(raw_db):
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, -50_000)
    report = raw_db.get_susa(2026, 3)
    assert [(check.closing, check.movements, check.difference) for check in report.bank_checks] == [(-50_000, 0, -50_000)]
    helpers.assert_balanced(report)


def test_a_legacy_mirror_is_not_booked_and_the_bank_check_shows_what_it_would_have_moved(raw_db):
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Probe-Giro'), -50_000)
    helpers.raw_entry(raw_db, '2026-03-05', 1810, 1800, -50_000, form='mirror_legacy', parent=parent)
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['legacy_mirror'].count == 1 and [check.difference for check in report.bank_checks] == [50_000, 0]
    helpers.assert_balanced(report)


def test_two_accounts_with_the_same_number_have_no_bank_check(raw_db):
    raw_db.insert_account('Zweites Konto', 'Ich', 'DE05', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    report = raw_db.get_susa(2026, 3)
    assert report.bank_checks == [] and sorted(report.bank_skipped) == ['Probe-Giro', 'Zweites Konto']


# ═══ Nichts fällt still weg: Zeilen ohne Buchungsart, mit unmöglichem Datum, mit Betrag 0 und Steuer ═══
BAD_DATES = [(20260305, 'number'), ('31.12.2026', 'german'), ('Dezember', 'word'), ('2026-02-30', 'no_such_day'),
             ('2026-12-32', 'no_such_day_in_december'), ('', 'empty'), ('-0001-01-01', 'negative_year'), ('0000-06-15', 'year_zero'),
             ('2026-03-05 ', 'trailing_space'), ('2026-03-05T00:00', 'with_time')]


def closing_of(report, key):
    found = helpers.accounts_of(report).get(key)
    return found.closing if found else 0


def test_a_row_without_a_booking_type_is_counted_and_a_row_of_another_type_is_read_like_the_export(raw_db):
    """Der Export liest eine Zeile nur, wenn ``BookingType != 'bank'`` zutrifft – für NULL trifft das nie zu: Ein Satz ohne Buchungsart, als Kind einer
    Bankbewegung ebenso wie eigenständig, stand weder im Stapel noch in der Liste und fiel still weg; ``unknown_type`` nennt ihn jetzt mit Zahl und
    Betrag, in jedem Monat (kein Datum bestimmt, wohin er gehört). Eine Zeile mit einer anderen Buchungsart als ``entry`` und ``bank`` liest der Export
    wie einen Satz – die Liste bucht sie ebenso und meldet nichts."""
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    parent = helpers.raw_bank(raw_db, '2026-03-05', giro, minor_of('-119.00'))
    child = helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, minor_of('-119.00'), parent=parent, rate=0.19, tax=minor_of('-19.00'))
    alone = helpers.raw_entry(raw_db, '2026-03-06', 6850, 1800, minor_of('-50.00'))
    other = helpers.raw_entry(raw_db, '2026-03-07', 6805, 1800, minor_of('-20.00'))
    for entry_id in (child, alone):
        helpers.raw(raw_db, 'UPDATE Bookings SET BookingType = NULL WHERE ID = ?', entry_id)
    helpers.raw(raw_db, "UPDATE Bookings SET BookingType = 'sonst' WHERE ID = ?", other)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    assert found['unknown_type'] == susa.SusaNotice('unknown_type', 2, minor_of('169.00'))
    accounts = helpers.accounts_of(report)
    assert 6815 not in accounts and 6850 not in accounts and 1406 not in accounts                    # die zwei Zeilen sind nicht gebucht
    assert accounts[6805].month_soll == minor_of('20.00')                                          # die Zeile mit der anderen Art schon
    assert not {'bad_date', 'not_readable', 'unbooked'} & set(found)                                  # und sie steht in keinem anderen Hinweis
    assert {row[0] for row in raw_db.fetch_bookings_range('2026-03-01', '2026-03-31')} == {other}      # der Export liest dieselben Zeilen
    helpers.assert_balanced(report)
    for month in (1, 12):
        assert notices_of(raw_db.get_susa(2026, month))['unknown_type'].count == 2, month


@pytest.mark.parametrize('kind', ['entry', 'bank'])
@pytest.mark.parametrize('date', [date for date, _name in BAD_DATES], ids=[name for _date, name in BAD_DATES])
def test_a_date_that_is_no_calendar_day_is_named_in_every_month_and_booked_in_none(raw_db, date, kind):
    """Eine Zahl als Datum (SQLite speichert ``20260305`` als Zahl), ein Text hinter jedem Monatsende (``'31.12.2026'``, ``'Dezember'``), ein unmögliches
    Datum im Muster ``JJJJ-MM-TT`` (``'2026-02-30'``, ``'2026-12-32'``) und ein leerer Text: Der Satz oder die Bankbewegung stand in keinem Fenster
    oder im aufgelaufenen Block ohne eine Monatsspalte und fiel still weg. Jetzt nennt ``bad_date`` sie in JEDEM Monat, auch im Januar des Folgejahres, und
    sie ist nie gebucht – auch nicht gegen „Nicht kontiert“. (Ein Datum NULL lässt die Tabelle nicht zu: ``DateBooking`` ist ``NOT NULL``.)"""
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    if kind == 'entry':
        helpers.raw_entry(raw_db, date, 6815, 1800, minor_of('-50.00'))
    else:
        helpers.raw_bank(raw_db, date, giro, minor_of('-50.00'))
    helpers.raw_entry(raw_db, '2026-06-15', 6850, 1800, minor_of('-10.00'))                          # ein gewöhnlicher Satz
    for year, month in [*helpers.every_month(2026), (2027, 1)]:
        report = raw_db.get_susa(year, month)
        found, accounts = notices_of(report), helpers.accounts_of(report)
        where = (date, kind, year, month)
        assert (found['bad_date'].count, found['bad_date'].amount) == (1, minor_of('50.00')), where
        assert 6815 not in accounts and 'unbooked' not in found, where
        assert susa.NOT_POSTED not in accounts or not susa.visible(accounts[susa.NOT_POSTED]), where
        assert closing_of(report, 1800) == (-minor_of('10.00') if (year, month) >= (2026, 6) else 0), where     # nur der gewöhnliche Satz
        helpers.assert_balanced(report, where)


def test_an_impossible_december_date_does_not_break_the_carry_into_the_next_year(raw_db):
    """``'2026-12-32'`` sortiert hinter dem 31.12.: Im Dezember fehlte der Satz, im Januar des Folgejahres stand er im Vortrag – der Endsaldo vom Dezember war
    nicht der Anfangssaldo vom Januar. Jetzt steht er in keinem von beiden."""
    helpers.raw_entry(raw_db, '2026-12-20', 6815, 1800, minor_of('-100.00'))
    helpers.raw_entry(raw_db, '2026-12-32', 1810, 1800, minor_of('-50.00'), form='transfer')
    december, january = helpers.accounts_of(raw_db.get_susa(2026, 12)), helpers.accounts_of(raw_db.get_susa(2027, 1))
    carried = [key for key in december if isinstance(key, int) and susa.is_carried(key)]
    assert 1800 in carried and 1810 not in december
    for key in carried:
        assert december[key].closing == (january[key].opening if key in january else 0), key


def test_an_impossible_february_date_is_in_no_accumulated_block_either(raw_db):
    """``'2026-02-30'`` sortiert zwischen dem 28.2. und dem 31.3.: Im Februar fehlte der Satz, ab März stand er im aufgelaufenen Block, aber in keiner
    Monatsspalte – aufgelaufen im März war nicht aufgelaufen im Februar plus der Monat März. Jetzt steht er in keinem."""
    helpers.raw_entry(raw_db, '2026-02-10', 6815, 1800, minor_of('-10.00'))
    helpers.raw_entry(raw_db, '2026-02-30', 6815, 1800, minor_of('-50.00'))
    helpers.raw_entry(raw_db, '2026-03-10', 6815, 1800, minor_of('-20.00'))
    february, march = helpers.accounts_of(raw_db.get_susa(2026, 2)), helpers.accounts_of(raw_db.get_susa(2026, 3))
    assert march[6815].ytd_soll == minor_of('30.00')
    for key, account in march.items():
        earlier = february.get(key)
        assert account.ytd_soll == (earlier.ytd_soll if earlier else 0) + account.month_soll, key
        assert account.ytd_haben == (earlier.ytd_haben if earlier else 0) + account.month_haben, key


@pytest.mark.parametrize('date, year, month', [('2024-02-29', 2024, 2), ('2026-03-01', 2026, 3), ('2026-03-31', 2026, 3), ('2026-12-31', 2026, 12),
                                               ('2026-01-31', 2026, 1), ('2026-04-30', 2026, 4)])
def test_a_valid_date_at_the_edge_of_a_month_stays_booked_in_its_month(raw_db, date, year, month):
    """Der Schalttag, der erste und der letzte Tag eines Monats und der Jahresletzte sind Kalendertage: gebucht im eigenen Monat, nie unter ``bad_date``."""
    helpers.raw_entry(raw_db, date, 6815, 1800, minor_of('-10.00'))
    report = raw_db.get_susa(year, month)
    account = helpers.accounts_of(report)[6815]
    assert (account.month_soll, account.ytd_soll) == (minor_of('10.00'), minor_of('10.00')) and 'bad_date' not in notices_of(report)
    if month > 1:
        assert 6815 not in helpers.accounts_of(raw_db.get_susa(year, month - 1))                    # der Monat davor kennt ihn nicht


def test_a_row_with_amount_zero_and_a_tax_amount_is_named_and_not_booked(raw_db):
    """Der Bank-Editor lässt eine Zeile mit Konto und Steuerbetrag, aber ohne Betrag zu (Bewegung −119 €: „6815 −119, Steuer −19“ und „6815 Betrag 0, Steuer
    −19“): Die Einnahmenüberschussrechnung zählt die Steuer der zweiten, die Liste bucht ohne Betrag keine Seite – und nannte die Zeile nicht. Sie bleibt
    ungebucht (6815 und 1406 stehen wie ohne sie da), ``zero_amount_tax`` nennt sie mit der Summe ihrer Steuer."""
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    parent = helpers.raw_bank(raw_db, '2026-03-05', giro, minor_of('-119.00'))
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, minor_of('-119.00'), parent=parent, rate=0.19, tax=minor_of('-19.00'))
    before = raw_db.get_susa(2026, 3)
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, 0, parent=parent, rate=0.19, tax=minor_of('-19.00'))
    report = raw_db.get_susa(2026, 3)
    assert 'zero_amount_tax' not in notices_of(before)
    assert notices_of(report)['zero_amount_tax'] == susa.SusaNotice('zero_amount_tax', 1, minor_of('19.00'))
    assert helpers.accounts_of(report) == helpers.accounts_of(before) and [c.difference for c in report.bank_checks] == [0]
    assert (helpers.accounts_of(report)[6815].month_soll, helpers.accounts_of(report)[1406].month_soll) == (minor_of('100.00'), minor_of('19.00'))
    helpers.assert_balanced(report)
    euer = {number: total for number, _name, total in raw_db.get_euer_data('2026-03-01', '2026-03-31')}
    assert euer[1406] == pytest.approx(-38.0)                                                       # die Einnahmenüberschussrechnung zählt beide Steuern


def test_a_row_with_amount_zero_and_no_tax_has_nothing_to_book_and_says_nothing(raw_db):
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, 0)
    helpers.raw_entry(raw_db, '2026-03-05', 6850, 1800, 0, tax=0)
    report = raw_db.get_susa(2026, 3)
    assert report.notices == [] and helpers.accounts_of(report).get(6815) is None


@pytest.mark.parametrize('entry_day, bank_day', [('2026-02-25', '2026-03-03'), ('2026-03-03', '2026-02-25')])
def test_a_row_with_another_date_than_its_bank_movement_names_the_month_between_the_two(raw_db, entry_day, bank_day):
    """Ein Buchungssatz unter einer Bankbewegung darf sein eigenes Datum behalten (der Split-Editor lässt es stehen). Liegt das Monatsende zwischen Satz und
    Bewegung, zählt nur einer von beiden bis dahin: Im Februar weicht der Abgleich um 119 € ab (``child_other_period`` nennt den Satz), im März – beide
    bis Monatsende – stimmt er wieder, ohne Hinweis. Gilt für den früheren Satz (25.2. unter der Bewegung vom 3.3.) wie für den späteren."""
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    parent = helpers.raw_bank(raw_db, bank_day, giro, minor_of('-119.00'))
    helpers.raw_entry(raw_db, entry_day, 6815, 1800, minor_of('-119.00'), parent=parent, rate=0.19, tax=minor_of('-19.00'))
    february, march = raw_db.get_susa(2026, 2), raw_db.get_susa(2026, 3)
    assert notices_of(february)['child_other_period'] == susa.SusaNotice('child_other_period', 1, minor_of('119.00'))
    assert [abs(check.difference) for check in february.bank_checks] == [minor_of('119.00')]
    assert 'child_other_period' not in notices_of(march) and [check.difference for check in march.bank_checks] == [0]
    helpers.assert_balanced(february), helpers.assert_balanced(march)


def test_a_row_and_its_bank_movement_on_the_same_side_of_the_month_end_name_no_other_period(raw_db):
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    same = helpers.raw_bank(raw_db, '2026-03-05', giro, minor_of('-10.00'))
    helpers.raw_entry(raw_db, '2026-03-20', 6815, 1800, minor_of('-10.00'), parent=same)
    late = helpers.raw_bank(raw_db, '2026-05-05', giro, minor_of('-10.00'))
    helpers.raw_entry(raw_db, '2026-04-20', 6850, 1800, minor_of('-10.00'), parent=late)
    for month in (1, 3, 6, 12):
        assert 'child_other_period' not in notices_of(raw_db.get_susa(2026, month)), month
    assert notices_of(raw_db.get_susa(2026, 4))['child_other_period'].count == 1                    # 20.4. gegen 5.5.


def test_a_row_booked_on_another_own_account_than_its_bank_movement_is_named_but_a_mirror_and_an_ordinary_row_are_not(raw_db):
    """Das Zuordnen überschreibt die Geldseite nicht: Ein Satz „6815 an 1600“ (Kasse) oder „6815 an 1810“ (zweites Bankkonto) unter einer Bewegung auf 1800
    bucht dorthin – die Liste hat das Konto der Bewegung nicht, der Abgleich weicht um ihn ab. ``child_other_account`` nennt beide (169 €). Ein Spiegel
    (Übertrag 1800 → 1810) berührt das andere Konto absichtlich und ein gewöhnlicher Satz „6815 an 1800“ gar nicht: beide zählen nicht."""
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    for day, number, counter, amount, form in (('2026-03-05', 6815, 1600, '-119.00', 'purpose_first'), ('2026-03-06', 6850, 1810, '-50.00', 'purpose_first'),
                                               ('2026-03-07', 1810, 1800, '-500.00', 'mirror_counter'), ('2026-03-08', 6805, 1800, '-10.00', 'purpose_first')):
        parent = helpers.raw_bank(raw_db, day, giro, minor_of(amount))
        helpers.raw_entry(raw_db, day, number, counter, minor_of(amount), form=form, parent=parent)
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['child_other_account'] == susa.SusaNotice('child_other_account', 2, minor_of('169.00'))
    assert report.bank_checks[0].difference == minor_of('169.00')                                 # genau um die zwei Sätze weicht das Bankkonto ab
    helpers.assert_balanced(report)
    assert 'child_other_account' not in notices_of(raw_db.get_susa(2026, 2))


def test_a_child_row_under_a_movement_without_a_number_that_books_on_an_own_account_is_named(raw_db):
    """Die Bewegung liegt auf einem Konto ohne SKR-Nummer, ihr Satz „6815 an 1810“ bucht auf das zweite Bankkonto: Dessen Abgleich weicht um 30 € ab,
    und kein anderer Hinweis nannte das – ``child_other_account`` zählt den Satz auch dann."""
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    raw_db.insert_account('Ohne Nummer', 'Ich', 'DE04', 'BIC', 'Bank', is_cash=0, skr_account=None)
    parent = helpers.raw_bank(raw_db, '2026-03-05', helpers.account_id(raw_db, 'Ohne Nummer'), minor_of('-30.00'))
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1810, minor_of('-30.00'), parent=parent)
    report = raw_db.get_susa(2026, 3)
    assert notices_of(report)['child_other_account'] == susa.SusaNotice('child_other_account', 1, minor_of('30.00'))
    assert {check.number: check.difference for check in report.bank_checks}[1810] == minor_of('-30.00')
    helpers.assert_balanced(report)


def test_a_child_row_names_another_own_account_only_where_the_row_is_booked_under_a_bank_movement(raw_db):
    """Bei einem Spiegel unbekannter Lesart (bucht nie), bei einem Satz ohne Betrag und bei einem eigenständigen Satz auf einem Bankkonto (keine
    Bankbewegung, die abweichen könnte) schweigt ``child_other_account``."""
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    parent = helpers.raw_bank(raw_db, '2026-03-06', giro, minor_of('-40.00'))
    helpers.raw_entry(raw_db, '2026-03-06', 1810, 1800, minor_of('-40.00'), form='mirror_legacy', parent=parent)    # Lesart unbekannt: bucht nie
    helpers.raw_entry(raw_db, '2026-03-06', 6850, 1810, 0, parent=parent)                                              # ohne Betrag
    helpers.raw_entry(raw_db, '2026-03-07', 6805, 1810, minor_of('-20.00'))                                          # eigenständig
    assert 'child_other_account' not in notices_of(raw_db.get_susa(2026, 3))


def test_the_notice_for_another_own_account_counts_what_book_group_books_and_takes_it_back_with_the_sign():
    context = susa.SusaContext(frozenset(), {1: 6815, 2: 1800, 3: 1810, 4: 1600, 5: 4400}, {10: 1800, 11: 1810, 12: 1600, 13: None}, ())
    assert context.own_numbers == frozenset({1800, 1810, 1600})

    def counted(counter, form='purpose_first', parent=10, signs=(1,), amount=-50_000):
        tally = susa.Tally()
        for sign in signs:
            susa.book_group(tally, context, susa.GroupRow(form, 1, counter, False, None, 'month', parent, 3, amount, 0), sign)
        return {notice.key: (notice.count, notice.amount) for notice in tally.notices()}
    assert counted(3) == {'child_other_account': (3, 50_000)} and counted(4) == {'child_other_account': (3, 50_000)}
    assert counted(2) == {} and counted(5) == {}                                                    # das Konto der Bewegung, ein Konto außerhalb der eigenen
    assert counted(3, form='mirror_counter') == {} and counted(3, form='mirror_coa') == {}
    assert counted(3, form='mirror_legacy') == {'legacy_mirror': (3, 50_000)}                       # bucht nie, meldet nur das eigene
    assert counted(3, parent=13) == {'child_other_account': (3, 50_000)}                            # auch unter einer Bewegung ohne Nummer
    assert counted(3, parent=None) == {}                                                            # ein eigenständiger Satz
    assert counted(3, signs=(1, -1)) == {} and counted(3, amount=0) == {}                           # abgezogen hebt es sich auf; ohne Betrag nichts gebucht


# ═══ Kein Fehler bei einem absurden Bestand: Summen über 64 Bit und Namen, die kein Text sind ═══
BIG = 10 ** 15                                                                  # der größte Betrag, den die Liste liest
OVERFLOW_ROWS = 9300                                                            # 9300 · 10^15 sind mehr als 2^63 ≈ 9,22 · 10^18


def insert_many(db, statement, row, count=OVERFLOW_ROWS):
    with closing(sqlite3.connect(db.db_name)) as con:
        con.executemany(statement, [row] * count)
        con.commit()


ENTRY_INSERT = ("INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxAmount, BookingType, EntryForm, ParentBooking_ID) "
                "VALUES (?, ?, ?, ?, ?, ?, 'purpose_first', ?)")


def overflow_stock(db, case):
    """Der kleinste Bestand, in dem eine Summe der Liste 2^63 übersteigt, je Fall; returns die Spalte, die SQLite dabei nicht mehr summieren kann."""
    giro, coa_cost, coa_bank = helpers.account_id(db, 'Probe-Giro'), helpers.coa(db, 6815), helpers.coa(db, 1800)
    if case == 'bank_movements':
        insert_many(db, "INSERT INTO Bookings (DateBooking, Account_ID, Amount, BookingType) VALUES ('2026-03-05', ?, ?, 'bank')", (giro, BIG))
    elif case == 'child_other_period':
        parent = helpers.raw_bank(db, '2026-04-02', giro, -BIG)
        insert_many(db, ENTRY_INSERT, ('2026-03-05', coa_cost, coa_bank, -BIG, None, 'entry', parent))
    else:
        date, amount, tax, kind = {'booked': ('2026-03-05', -BIG, None, 'entry'), 'bad_date': ('2026-02-30', -BIG, None, 'entry'),
                                   'unknown_type': ('2026-03-05', -BIG, None, None), 'zero_amount_tax': ('2026-03-05', 0, -BIG, 'entry')}[case]
        insert_many(db, ENTRY_INSERT, (date, coa_cost, coa_bank, amount, tax, kind, None))
    return 'TaxAmount' if case == 'zero_amount_tax' else 'Amount'


OVERFLOW_CASES = ['booked', 'bank_movements', 'bad_date', 'unknown_type', 'zero_amount_tax', 'child_other_period']


@pytest.mark.parametrize('case', OVERFLOW_CASES)
def test_a_sum_beyond_64_bits_is_summed_in_python_and_the_list_still_balances(raw_db, case):
    """SQLite summiert in 64 Bit und wirft ab 2^63 ``integer overflow`` – bei 9300 Beträgen von 10^15 Minor Units, die jeder für sich gelesen werden. Die Liste
    fängt das an jeder Summe ab und rechnet in Python weiter (ganze Zahlen ohne Grenze): kein Fehler, dieselben Hinweise, und sie geht auf."""
    column = overflow_stock(raw_db, case)
    with closing(sqlite3.connect(raw_db.db_name)) as con, pytest.raises(sqlite3.OperationalError, match='integer overflow'):
        con.execute(f'SELECT SUM({column}) FROM Bookings').fetchall()                               # der Bestand sprengt SQLite wirklich
    report = raw_db.get_susa(2026, 3)
    total, found, accounts = OVERFLOW_ROWS * BIG, notices_of(report), helpers.accounts_of(report)
    if case == 'booked':
        assert (accounts[6815].month_soll, accounts[1800].month_haben) == (total, total)
    elif case == 'bank_movements':
        assert (found['unbooked'].count, found['unbooked'].incoming, report.bank_checks[0].movements) == (OVERFLOW_ROWS, total, total)
    else:
        assert (found[case].count, found[case].amount) == (OVERFLOW_ROWS, total), case            # die vier übrigen Fälle heißen wie ihr Hinweis
    helpers.assert_balanced(report)
    assert PageSusa(raw_db, SusaQuery(2026, 3)).count('<tr') > 3


def test_the_page_and_the_pdf_answer_with_a_stock_beyond_64_bits(server_port, auto_login):
    auto_login.db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    for case in OVERFLOW_CASES:
        overflow_stock(auto_login.db, case)
    page, pdf = get(server_port, '/susa?from=2026-03-01&to=2026-03-31'), get(server_port, '/susa/pdf?year=2026&month=3')
    assert page.status == 200 and 'Summen- und Saldenliste – März 2026' in page.text
    assert pdf.status == 200 and pdf.body.startswith(b'%PDF-')


def sql_sums_fail(*_args):
    raise sqlite3.OperationalError('integer overflow')


def test_the_python_sums_give_the_report_the_sql_sums_give(world, susa_world, tmp_db):
    """Beide Wege liefern dasselbe: Der Bericht (Konten, Gruppen, Summen, Hinweise, Abgleich) ist gleich, ob SQL summiert oder – erzwungen – Python."""
    random_stock(tmp_db, 3)
    seen = set()
    for db, months in ((world, [(2026, 3), (2026, 12)]), (susa_world.db, [(2025, 12), (2026, 7), (2026, 12)]), (tmp_db, [(2024, 6), (2025, 12), (2026, 12)])):
        for year, month in months:
            expected = db.get_susa(year, month)
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(reporting, '_sum_groups_in_sql', sql_sums_fail)
                assert db.get_susa(year, month) == expected, (year, month)
            seen |= {notice.key for notice in expected.notices}
    assert {'bad_date', 'unknown_type', 'not_readable', 'unbooked'} <= seen                        # die Bestände tragen, was die Summen betrifft


def test_every_other_database_error_of_a_sum_is_passed_on(raw_db, monkeypatch):
    def locked(*_args):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(reporting, '_sum_groups_in_sql', locked)
    with pytest.raises(sqlite3.OperationalError, match='database is locked'):
        raw_db.get_susa(2026, 3)


def test_a_name_that_is_no_text_does_not_stop_the_list_and_stands_escaped_on_the_page(raw_db):
    """Ein Name lässt sich per SQL-Konsole als Blob (in ``ChartOfAccounts`` auch als NULL) schreiben; eine Zahl macht die Spalte zu Text. Die Liste sortiert die
    Bankkonten ohne Nummer nach dem Namen und verbindet sie zu einem Satz, und der Name eines Kontos bekommt einen Zusatz – das scheiterte an einem Blob."""
    for name in ("x'41'", "x'3c623e'"):                                                              # Blob „A“ und Blob „<b>“, beide ohne SKR-Nummer
        helpers.raw(raw_db, f'INSERT INTO Accounts (Name, SKRAccount) VALUES ({name}, NULL)')
    helpers.raw(raw_db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name) VALUES (888881, 4, 6890, x'426c6f62')")      # Blob „Blob“
    helpers.raw(raw_db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name) VALUES (888882, 4, 6891, NULL)")
    blob_bank = helpers.raw(raw_db, "INSERT INTO Accounts (Name, SKRAccount) VALUES (x'426c6f6220426b', 7777)")                          # Blob „Blob Bk“
    helpers.raw_bank(raw_db, '2026-03-04', blob_bank, 50_000)                                        # eine Bewegung ohne Satz: bucht auf 7777
    for number in (6890, 6891):
        helpers.raw_entry(raw_db, '2026-03-05', number, 1800, -50_000)
    report = raw_db.get_susa(2026, 3)
    names = {key: account.name for key, account in helpers.accounts_of(report).items()}
    assert (names[6890], names[6891], names[7777]) == ('Blob', '', 'Blob Bk (nicht im Kontenrahmen)')
    assert report.bank_skipped == ['<b>', 'A']                                                       # nach dem Namen sortiert
    page = PageSusa(raw_db, SusaQuery(2026, 3))
    assert html_lib.escape(susa.BANK_SKIPPED_TEXT.format(names='<b>, A'), quote=True) in page and '<b>, A' not in page
    assert any(row.startswith('6890 | Blob') for row in table_rows(page)) and any(row.startswith('7777 | Blob Bk') for row in table_rows(page))


@pytest.mark.parametrize('value, text', [(None, ''), (b'abc', 'abc'), (b'\xff\xfeA', '��A'), (5, '5'), (1.5, '1.5'), ('Bank', 'Bank'), (b'', '')])
def test_a_name_from_the_database_is_read_as_text(value, text):
    assert reporting._name_text(value) == text


def test_the_sums_of_a_query_are_the_same_in_sql_and_in_python_even_without_a_row(raw_db):
    """Beide Hilfen liefern je Gruppe ``(*Schlüssel, Zahl, *Summen)`` und ohne Schlüssel genau eine Zeile – auch ohne Zeilen (Zahl und Summen 0)."""
    helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, -50_000)
    helpers.raw_entry(raw_db, '2026-03-06', 6850, 1800, -70_000)
    helpers.raw_entry(raw_db, '2026-03-07', 6850, 1800, 30_000)
    with closing(sqlite3.connect(raw_db.db_name)) as con:
        cursor = con.cursor()
        for keys, values, tail, params in (
                (('COA_ID', 'Amount > 0'), ('Amount', 'ABS(Amount)'), 'FROM Bookings WHERE Amount <> ?', [0]),
                ((), ('Amount',), 'FROM Bookings WHERE Amount <> ?', [0]), ((), ('Amount', 'ABS(TaxAmount)'), 'FROM Bookings WHERE Amount = ?', [1]),
                (('COA_ID',), ('Amount',), 'FROM Bookings WHERE Amount = ?', [1])):
            in_sql = reporting._sum_groups_in_sql(cursor, keys, values, tail, params)
            in_python = reporting._sum_groups_in_python(cursor, keys, values, tail, params)
            assert sorted(in_sql, key=repr) == sorted(in_python, key=repr), (keys, values)
        assert reporting._sum_groups_in_sql(cursor, (), ('Amount',), 'FROM Bookings WHERE Amount = ?', [1]) == [(0, 0)]


def test_more_paired_transfers_than_one_chunk_are_all_taken_out_once(raw_db):
    """Über ``_ID_CHUNK`` hinaus: 1100 Überträge, auf beiden Bewegungen kontiert (je ein weggelassener Eingangs-Satz) – die Liste liest die Sätze in
    mehreren Stücken und zieht jeden einmal ab."""
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    giro, zweit = helpers.account_id(raw_db, 'Probe-Giro'), helpers.account_id(raw_db, 'Probe-Zweit')
    coa_a, coa_b = helpers.coa(raw_db, 1800), helpers.coa(raw_db, 1810)
    rows = []
    for index in range(1100):
        day = (datetime.date(2026, 1, 1) + datetime.timedelta(days=index % 200)).isoformat()
        rows.append((day, giro, None, None, -100_000, 'bank', None, None))
        rows.append((day, None, coa_b, coa_a, -100_000, 'entry', index * 4 + 1, 'mirror_counter'))
        rows.append((day, zweit, None, None, 100_000, 'bank', None, None))
        rows.append((day, None, coa_a, coa_b, 100_000, 'entry', index * 4 + 3, 'mirror_counter'))
    with closing(sqlite3.connect(raw_db.db_name)) as con:
        first = con.execute('SELECT COALESCE(MAX(ID), 0) FROM Bookings').fetchone()[0]
        con.executemany("INSERT INTO Bookings (DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, BookingType, ParentBooking_ID, EntryForm, "
                        "Currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'EUR')", [(*row[:6], None if row[6] is None else first + row[6], row[7]) for row in rows])
        con.commit()
    assert len(raw_db.duplicate_transfer_rows()) == 1100 > susa_chunk()
    report = raw_db.get_susa(2026, 12)
    accounts = helpers.accounts_of(report)
    assert accounts[1810].closing == 1100 * 100_000 and accounts[1800].closing == -1100 * 100_000
    assert helpers.check_bank_accounts(raw_db, report) == [] and notices_of(report) == {}
    helpers.assert_balanced(report)


def susa_chunk():
    from db.matching import _ID_CHUNK
    return _ID_CHUNK


def test_doubtful_transfers_of_the_year_up_to_the_month_end_are_counted(raw_db, monkeypatch):
    def entry(entry_id, day):
        return DoubtfulEntry(entry_id, datetime.date.fromisoformat(day), 100_000)
    settlement = TransferSettlement({}, frozenset(), {1: entry(1, '2026-02-10'), 2: entry(2, '2026-03-31'), 3: entry(3, '2026-04-01'),
                                                      4: entry(4, '2025-12-31'), 5: entry(5, '2026-01-01')}, [])
    monkeypatch.setattr(raw_db, '_settle_stock', lambda cursor: settlement)
    assert notices_of(raw_db.get_susa(2026, 3))['doubtful'].count == 3                       # 10.2., 31.3. und 1.1.
    assert notices_of(raw_db.get_susa(2026, 1))['doubtful'].count == 1
    assert 'doubtful' not in notices_of(raw_db.get_susa(2025, 11))


# ═══ Eigenschaft: Bestände aus Zufall ═════════════════════════════════════════
NUMBERS = [None, None, 520, 1050, 1401, 1406, 1460, 1600, 1800, 1810, 2100, 3160, 3806, 4300, 4340, 4345, 4400, 4405, 6815, 6850, 6890, 10000, 8000, 'ohne']
FORMS = [None, 'purpose_first', 'purpose_first', 'liquid_first', 'liquid_first', 'transfer', 'mirror_coa', 'mirror_counter', 'mirror_legacy', 'bogus']
RATES = [None, None, 0, 0.19, 0.19, 0.07, 0.16, 0.05, 0.107, 19.0]


def random_stock(db, seed, size=300):
    """Ein Bestand aus Zufall (fester Seed), direkt per SQL: Bankbewegungen mit und ohne Sätze, Sätze jeder Form mit und ohne Konten, mit Steuer und
    ohne (auch mit Betrag 0), eigenständige Sätze, ein paar Beträge und Daten, die sich nicht lesen lassen (Text, Zahl als Datum, ein Tag, den es nicht
    gibt), Sätze ohne Buchungsart oder mit einer anderen, Sätze mit einem anderen Datum als ihre Bankbewegung. Über 2024 bis 2026 verteilt."""
    rng = random.Random(seed)
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    db.insert_account('Ohne Nummer', 'Ich', 'DE04', 'BIC', 'Bank', is_cash=0, skr_account=None)
    accounts = [helpers.account_id(db, name) for name in ('Probe-Giro', 'Probe-Zweit', 'Kasse', 'Ohne Nummer')] + [None]
    for number in (1050, 6890, 8000):                                               # nicht im Standard-Kontenrahmen
        assert db.insert_chart_of_accounts(4, number, f'Konto {number}', 'Test')
    helpers.raw(db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name) VALUES (777777, 4, NULL, 'Konto ohne Nummer')")
    with closing(sqlite3.connect(db.db_name)) as con:
        coa = {number: con.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?', (number,)).fetchone()[0]
               for number in {n for n in NUMBERS if isinstance(n, int)}}
    coa.update({None: None, 'ohne': 777777})

    def day():
        return (datetime.date(2024, 1, 1) + datetime.timedelta(days=rng.randrange(0, 1095))).isoformat()

    def amount():
        value = rng.choice([0, rng.randint(1, 600_000), rng.randint(1, 60_000_000)]) * rng.choice([1, -1])
        return rng.choice([value] * 24 + ['abc', 12.5, 10 ** 16])

    def own_date(date):
        pick = rng.random()
        if pick < 0.88:
            return date
        if pick < 0.91:
            return '01.02.2026'                                                       # ein Text, der kein Datum der Bücher ist
        if pick < 0.94:
            return 20260305                                                           # eine Zahl als Datum
        if pick < 0.97:
            return '2026-02-30'                                                       # ein Tag, den es nicht gibt
        return (datetime.date.fromisoformat(date) + datetime.timedelta(days=rng.randint(-45, 45))).isoformat()      # ein anderes Datum als die Bewegung

    def entry(parent, date):
        value = amount()
        tax = rng.choice([None, 0, 0, rng.randint(-900_000, 900_000)])
        if tax and isinstance(value, int):
            tax = rng.choice([abs(tax), -abs(tax)]) if rng.random() < 0.5 else tax
        form = rng.choice(FORMS)
        return (own_date(date), None, coa[rng.choice(NUMBERS)], coa[rng.choice(NUMBERS)], value, rng.choice(RATES), tax,
                rng.choice(['entry'] * 40 + [None, 'sonst']), parent, form)

    with closing(sqlite3.connect(db.db_name)) as con:
        sql = ("INSERT INTO Bookings (DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, BookingType, ParentBooking_ID, "
               "EntryForm, Currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EUR')")
        for _ in range(size):
            date = day()
            if rng.random() < 0.55:
                parent = con.execute(sql, (date, rng.choice(accounts), None, None, rng.choice([-1, 1]) * rng.randint(1, 5_000_000), None, None, 'bank',
                                           None, None)).lastrowid
                if rng.random() < 0.8:
                    for _child in range(rng.randint(1, 3)):
                        con.execute(sql, entry(parent, date))
            else:
                con.execute(sql, entry(None, date))
        con.commit()


@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5])
def test_the_books_balance_in_every_month_of_a_random_stock(tmp_db, seed):
    """Eigenschaftsprüfung mit festem Seed über Bestände mit allen Formen, mit und ohne Steuer und Konten, fehlenden Seiten, Spiegeln unbekannter
    Lesart, Bankbewegungen ohne Satz und Nicht-Lesbarem: Summe Soll gleich Summe Haben in jeder Spalte, die Summe der Anfangs- und der Endsalden
    null – auf den Minor-Wert, in jedem Monat 2024 bis 2026; jeder Hinweis hat einen Satz."""
    random_stock(tmp_db, seed)
    seen = set()
    for year, month in helpers.every_month(2024) + helpers.every_month(2025) + helpers.every_month(2026):
        report = tmp_db.get_susa(year, month)
        helpers.assert_balanced(report, (year, month, seed))
        for notice in report.notices:
            assert susa.notice_text(notice)
            seen.add(notice.key)
    assert {'legacy_mirror', 'missing_side', 'no_form', 'tax_not_split', 'unbooked', 'not_readable', 'bad_date', 'unknown_type', 'zero_amount_tax',
            'child_other_period', 'child_other_account'} <= seen


@pytest.mark.parametrize('seed', [11, 12])
def test_the_page_and_the_pdf_are_built_for_a_random_stock(tmp_db, seed):
    from export.pdf_susa import generate_susa_pdf
    random_stock(tmp_db, seed)
    for year, month in ((2024, 6), (2025, 12), (2026, 12)):
        assert PageSusa(tmp_db, SusaQuery(year, month)).count('<tr') > 10
        assert generate_susa_pdf(tmp_db.get_susa(year, month), 'Eigene Firma GmbH', datetime.date(2026, 11, 21)).startswith(b'%PDF-')


# ═══ Die Bilanz der Zeilen: jede Zeile ist gebucht oder genannt ═══════════════
ROW_LIMIT = 10 ** 15


def whole(value):
    """Ein Wert, den die Liste als Betrag liest: eine ganze Zahl (kein Wahrheitswert) bis 10^15."""
    return isinstance(value, int) and not isinstance(value, bool) and -ROW_LIMIT <= value <= ROW_LIMIT


def calendar_day(value):
    """Ist ``value`` ein Kalendertag als Text ``JJJJ-MM-TT``? Mit dem Kalender von Python – unabhängig von dem ``date()`` von SQLite, das die Liste fragt."""
    if not (isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value)):
        return False
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:]))
    except ValueError:
        return False
    return True


def row_classes(db, year, month):
    """Jede Zeile von ``Bookings`` einer Klasse zuordnen, wie die Liste des Monats sie behandeln muss – aus den gespeicherten Werten gerechnet, ohne
    ``get_susa``. Returns ``{Klasse: [(ID, Datum, Betrag, Steuerbetrag)]}``. Die Klassen (jede Zeile steht in genau einer):

    * ``unknown_type`` – ohne Buchungsart (NULL); ``bad_date`` – mit Buchungsart, aber das Datum ist kein Kalendertag als Text;
      ``later`` – gültiges Datum nach dem Monatsende (nicht Sache dieser Liste);
    * Bankbewegung (Datum bis Monatsende): ``not_readable`` (Betrag keine ganze Zahl bis 10^15), ``zero`` (Betrag 0: nichts zu buchen),
      ``bank_with_entries`` (ihre Sätze buchen), ohne Satz: ``covered`` (ein Übertrag deckt sie, die Gegenseite bucht), ``unbooked`` (gebucht gegen
      „Nicht kontiert“) oder ``unbooked_no_number`` (das Konto hat keine Nummer: nur gezählt);
    * jede andere Art (Datum bis Monatsende): ``not_readable`` (Betrag oder Steuerbetrag), ``zero_amount_tax`` (Betrag 0, Steuerbetrag nicht 0),
      ``zero`` (Betrag 0 ohne Steuer), ``omitted`` (Eingangs-Satz eines gepaarten Übertrags: gebucht und wieder abgezogen), ``legacy_mirror``
      (Spiegel unbekannter Lesart: nur gezählt), sonst ``booked``."""
    end = helpers.span_of(year, month)[1]
    with closing(sqlite3.connect(db.db_name)) as con:
        rows = con.execute('SELECT ID, DateBooking, BookingType, Amount, TaxAmount, EntryForm, COA_ID, CounterCOA_ID, Account_ID FROM Bookings').fetchall()
        with_entries = {row[0] for row in con.execute('SELECT DISTINCT ParentBooking_ID FROM Bookings WHERE ParentBooking_ID IS NOT NULL')}
        numbers = {row[0]: datev_keys.account_number(row[1]) for row in con.execute('SELECT ID, SKRAccount FROM Accounts')}
    liquid = db.get_payment_coa_ids()
    with closing(db._get_connection()) as conn:
        settlement = db._settle_stock(conn.cursor())
    classes = {}
    for booking_id, date, booking_type, amount, tax, stored_form, coa_id, counter_id, account_id in rows:
        if booking_type is None:
            kind = 'unknown_type'
        elif not calendar_day(date):
            kind = 'bad_date'
        elif date > end:
            kind = 'later'
        elif booking_type == 'bank':
            if not whole(amount):
                kind = 'not_readable'
            elif amount == 0:
                kind = 'zero'
            elif booking_id in with_entries:
                kind = 'bank_with_entries'
            elif booking_id in settlement.covered:
                kind = 'covered'
            elif numbers.get(account_id) is None:
                kind = 'unbooked_no_number'
            else:
                kind = 'unbooked'
        elif not (whole(amount) and (tax is None or whole(tax))):
            kind = 'not_readable'
        elif amount == 0:
            kind = 'zero_amount_tax' if tax else 'zero'
        elif booking_id in settlement.omitted:
            kind = 'omitted'
        elif ledger_form(stored_form, coa_id, counter_id, liquid)[0] == FORM_MIRROR_LEGACY:
            kind = 'legacy_mirror'
        else:
            kind = 'booked'
        classes.setdefault(kind, []).append((booking_id, date, amount, tax))
    return classes


def assert_rows_balance(db, year, month):
    """Die Bilanz der Zeilen für einen Monat: Die Zähler und Beträge der Hinweise, die ein Fehlen nennen (``unknown_type``, ``bad_date``, ``not_readable``,
    ``zero_amount_tax``, ``legacy_mirror``, ``unbooked_no_number``), und der der gebuchten Bankbewegungen ohne Satz (``unbooked``) sind genau die Zahlen
    der Klassen von ``row_classes`` – jede Zeile steht in einer, keine in zweien –, und die Summe Soll des aufgelaufenen Blocks ist die Summe der
    Beträge der Zeilen, die gebucht sind (``booked`` und ``unbooked`` ab dem 1. Januar): Eine Zeile, die die Liste weder bucht noch nennt, oder eine, die
    sie bucht und nennt, bricht diese Gleichungen. Returns die Klassen, die der Monat enthielt."""
    report = db.get_susa(year, month)
    found, classes, where = notices_of(report), row_classes(db, year, month), (year, month)

    def rows(kind):
        return classes.get(kind, [])

    def gross(kind):
        return sum(abs(row[2]) for row in rows(kind) if whole(row[2]))
    for key, amount in (('unknown_type', gross('unknown_type')), ('bad_date', gross('bad_date')), ('legacy_mirror', gross('legacy_mirror')),
                        ('unbooked_no_number', gross('unbooked_no_number')), ('not_readable', None),
                        ('zero_amount_tax', sum(abs(row[3]) for row in rows('zero_amount_tax')))):
        count = len(rows(key))
        assert (found[key].count if key in found else 0) == count, (where, key)
        if count and amount is not None:
            assert found[key].amount == amount, (where, key)
    assert (found['unbooked'].count if 'unbooked' in found else 0) == len(rows('unbooked')), where
    if rows('unbooked'):
        assert found['unbooked'].incoming == sum(row[2] for row in rows('unbooked') if row[2] > 0), where
        assert found['unbooked'].outgoing == -sum(row[2] for row in rows('unbooked') if row[2] < 0), where
    year_start, month_start = f'{year:04d}-01-01', helpers.span_of(year, month)[0]
    for date_from, total in ((year_start, report.total.ytd_soll), (month_start, report.total.month_soll)):
        assert total == sum(abs(row[2]) for kind in ('booked', 'unbooked') for row in rows(kind) if row[1] >= date_from), (where, date_from)
    assert [notice.key for notice in report.notices] == [key for key in susa.NOTICE_ORDER if key in found], where
    return set(classes)


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_every_row_is_booked_or_counted_by_exactly_one_notice_in_every_month_of_a_random_stock(tmp_db, seed):
    """Die Bilanz der Zeilen (``row_classes``, ``assert_rows_balance``) in jedem Monat 2024 bis 2026 eines Bestands aus Zufall – mit Zeilen ohne Buchungsart,
    mit einer anderen Art, einer Zahl, einem Text und einem unmöglichen Tag als Datum, mit Betrag 0 mit und ohne Steuer, nicht lesbaren Beträgen,
    Spiegeln unbekannter Lesart und Bankbewegungen ohne Satz. Eine Zeile mit Buchungsart und ungültigem Datum steht nur unter ``bad_date`` (auch mit einem
    nicht lesbaren Betrag), eine ohne Buchungsart nur unter ``unknown_type``; ein Satz mit anderer Art als ``entry``/``bank`` zählt wie ein Satz."""
    random_stock(tmp_db, seed)
    seen = set()
    for year, month in helpers.every_month(2024) + helpers.every_month(2025) + helpers.every_month(2026):
        seen |= assert_rows_balance(tmp_db, year, month)
    assert {'unknown_type', 'bad_date', 'later', 'not_readable', 'zero_amount_tax', 'zero', 'legacy_mirror', 'booked', 'bank_with_entries', 'unbooked',
            'unbooked_no_number'} <= seen


def test_every_row_of_the_worlds_is_booked_or_counted_by_exactly_one_notice(world, susa_world, demo_db):
    """Dieselbe Bilanz an der Testwelt, der SuSa-Welt und dem Demobestand: Dort gibt es die gepaarten Überträge (``omitted``) und die von einem Übertrag
    gedeckten Bewegungen (``covered``), die ein Zufallsbestand kaum trifft."""
    seen = set()
    for db, months in ((world, [(2026, 3), (2026, 12)]), (susa_world.db, [(2025, 12), (2026, 7), (2026, 8), (2026, 12)]), (demo_db, [(2025, 12), (2026, 11)])):
        for year, month in months:
            seen |= assert_rows_balance(db, year, month)
    assert {'omitted', 'covered', 'booked', 'bank_with_entries', 'unbooked', 'legacy_mirror'} <= seen


# ═══ Adresse ══════════════════════════════════════════════════════════════════
def _qs(**values):
    return {key: [value] for key, value in values.items()}


@pytest.mark.parametrize('qs, expected', [
    (_qs(year='2026', month='3'), SusaQuery(2026, 3)), (_qs(year='2026', month='03'), SusaQuery(2026, 3)), (_qs(year='1900', month='12'), SusaQuery(1900, 12)),
    (_qs(year='2999', month='1'), SusaQuery(2999, 1)), (_qs(year='2026', month='3', variant='jahr', x='1'), SusaQuery(2026, 3)),
])
def test_a_valid_pdf_address_is_read(qs, expected):
    """Das PDF liest Jahr und Monat aus der Adresse (streng); die Seite nimmt den gemeinsamen Zeitraum und leitet ihren Monat daraus ab
    (``period_month``, ``test_report_period_menu.py``)."""
    assert read_susa_pdf_query(qs) == (expected, None)


INVALID = [_qs(year='2026'), _qs(month='3'), _qs(year='26', month='3'), _qs(year='2026', month='13'), _qs(year='2026', month='0'),
           _qs(year='1899', month='3'), _qs(year='3000', month='3'), _qs(year=' 2026', month='3'), _qs(year='2026', month=' 3'),
           _qs(year='2026', month='003'), _qs(year='２０２６', month='3'), _qs(year='2026', month='+3'), _qs(year='', month=''),
           {'year': ['2026', '2025'], 'month': ['3']}, {'year': ['2026'], 'month': ['3', '4']}, {'year': ['2026', '2026'], 'month': ['3']}]


@pytest.mark.parametrize('qs', INVALID)
def test_an_invalid_pdf_address_is_refused(qs):
    assert read_susa_pdf_query(qs) == (None, 'Ungültiger Zeitraum.') and INVALID_PERIOD_ERROR == 'Ungültiger Zeitraum.'


def test_without_a_period_the_pdf_refuses():
    assert read_susa_pdf_query({}) == (None, 'Ungültiger Zeitraum.')


# ═══ Seite ════════════════════════════════════════════════════════════════════
def page_of(db, year=2026, month=12):
    return PageSusa(db, SusaQuery(year, month))


def table_rows(page, index=0):
    """Der sichtbare Text einer Tabelle der Seite: eine Zeile je ``tr``, die Zellen durch ``|`` getrennt."""
    tables = re.findall(r'<table class="reportTable">(.*?)</table>', page, flags=re.S)
    rows = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', tables[index], flags=re.S):
        found = [html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
        rows.append(' | '.join(found))
    return rows


def test_the_page_shows_the_submenu_the_menu_row_the_title_and_the_two_header_rows(susa_world):
    page = page_of(susa_world.db, 2026, 3)
    assert reports_submenu('susa') in page and '<span id="ActivePage">Auswertungen</span>' in Header1('bwa')
    dom = Dom(page)
    assert not dom.all('form') and not dom.all('select') and not dom.all('option')                    # die Menüzeile ist die der anderen Seiten
    assert dom.one('input', id='dateFrom')['value'] == '2026-03-01' and dom.one('input', id='dateTo')['value'] == '2026-03-31'
    assert re.findall(r"<button class='active' type=\"button\" onclick=\"window\.location\.href='([^']*)'\">(\D[^<]*)</button>", page) == [
        ('/susa?from=2026-03-01&to=2026-03-31', 'Mär')]                                               # der aktive Monat führt auf sich selbst
    assert f'{susa.TITLE} – März 2026</h3>' in page and susa.SUSA_HINT_TEXT in page and susa.SALDO_TEXT in page and 'warnColor' not in page
    assert 'onclick="susaPdf()"' in page
    rows = table_rows(page)
    assert rows[0] == ' | März 2026 | Januar bis März 2026 | '                                   # verbundene Zelle, Monat, aufgelaufen, Endsaldo leer
    assert re.search(r'<th colspan="3"></th><th class="bwaBlock" colspan="2">März 2026</th><th class="bwaBlock" colspan="2">Januar bis März 2026</th>'
                     r'<th></th>', page)
    assert rows[1] == 'Konto | Bezeichnung | Anfangssaldo | Soll | Haben | Soll | Haben | Endsaldo'
    assert rows[-1].startswith('Gesamtsumme | ') and rows[-1].count('|') == 6                     # sechs Werte hinter der verbundenen Zelle


def test_the_page_lists_the_groups_with_their_accounts_and_sums_in_the_order_of_the_list(susa_world):
    page = page_of(susa_world.db, 2026, 12)
    rows = table_rows(page)
    headings = [row for row in rows if '|' not in row]
    assert headings == [group_.label for group_ in susa.GROUPS if group_.key in (
        'fixed_assets', 'current_assets', 'equity', 'liabilities', 'revenue', 'open_items', 'expenses', 'personal', 'not_posted')]
    sums = [row.split(' | ')[0] for row in rows if row.startswith('Summe ')]
    assert sums == ['Summe ' + heading for heading in headings]
    assert any(row.startswith('1800 | ') for row in rows) and any(row.startswith('3801 | Umsatzsteuer 7% | ') for row in rows)      # 3801 führt der Kontenrahmen
    assert len([row for row in rows if row.startswith(' | Nicht kontiert (Gegenseite fehlt) | ')]) == 1          # ohne Nummer, gedämpft
    assert len([row for row in rows if row.startswith(' | Ergebnis der Vorjahre (rechnerisch) | ')]) == 1
    assert '<td></td><td class="hint">Nicht kontiert (Gegenseite fehlt)</td>' in page
    assert '<td></td><td class="hint">Ergebnis der Vorjahre (rechnerisch)</td>' in page
    assert page.count('class="bwaSum bwaResult"') == 1 and page.count('<tr class="bwaSum">') == len(sums)


def test_an_empty_book_shows_only_the_total_and_the_fixed_texts(tmp_db):
    page = page_of(tmp_db, 2026, 3)
    rows = table_rows(page)
    assert rows[2:] == ['Gesamtsumme | 0,00 | 0,00 | 0,00 | 0,00 | 0,00 | 0,00']
    assert '<h4>Hinweise' not in page and susa.OPENING_TEXT in page and susa.EUER_DIFF_TEXT in page


def test_the_fixed_texts_stand_on_the_page_as_hints(susa_world):
    page = page_of(susa_world.db, 2026, 12)
    for text in (susa.OPENING_TEXT, susa.NOT_POSTED_TEXT, susa.TRANSFER_TEXT, susa.TAX_TEXT, susa.EUER_DIFF_TEXT, susa.BANK_CHECK_TEXT):
        assert f'<p class="hint">{html_lib.escape(text, quote=True)}</p>' in page, text[:30]


def test_the_notices_stand_in_a_list_with_the_date_of_the_month_end(susa_world):
    page = page_of(susa_world.db, 2026, 8)
    assert '<h4>Hinweise zu den Buchungen bis zum 31.08.2026</h4><ul>' in page
    assert '<li>1 Bankbewegung ohne Buchungssatz (Eingänge 119,00 €, Ausgänge 0,00 €) steht gegen „Nicht kontiert“.</li>' in page
    assert '<h4>Hinweise' not in page_of(susa_world.db, 2026, 7)


def test_the_bank_check_shows_the_banks_and_a_difference_in_the_warning_colour(susa_world, world):
    page = page_of(susa_world.db, 2026, 12)
    assert '<h4>Abgleich mit den Bankbewegungen</h4>' in page and susa.BANK_DIFF_TEXT not in page and 'warnColor' not in page
    rows = table_rows(page, 1)
    assert rows[0] == 'Konto | Bankkonto | Endsaldo der Liste | Summe der Bankbewegungen bis 31.12.2026 | Abweichung'
    assert [row.split(' | ')[:2] for row in rows[1:]] == [['1800', 'Probe-Giro'], ['1810', 'Probe-Zweit']]
    assert all(row.endswith('| 0,00') for row in rows[1:])
    different = page_of(world, 2026, 12)                                          # die Testwelt weicht ab (Sätze ohne Bankbewegung, Spiegel des Bestands)
    assert susa.BANK_DIFF_TEXT in different and re.search(r'<td class="num warnColor">[\d.,]+ [SH]</td>', different)
    shown = table_rows(different, 1)
    assert shown[1].startswith('1800 | Giro Alpha | ') and shown[1].split(' | ')[-1] != '0,00'


def test_the_page_lists_the_new_notices_in_the_fixed_order_and_explains_the_bank_difference(raw_db):
    """Ein Bestand mit allem, was nicht mehr still wegfällt – eine Zeile ohne Buchungsart, ein Datum, das kein Kalendertag ist, ein Satz mit Betrag 0 und
    Steuer, ein Satz mit anderem Datum und einer auf ein anderes eigenes Konto: Die Seite nennt jeden als Listenpunkt, in der Reihenfolge von
    ``NOTICE_ORDER``, und erklärt die Abweichung des Bankkontos mit dem Text, der die Gründe aufzählt."""
    raw_db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    giro = helpers.account_id(raw_db, 'Probe-Giro')
    no_type = helpers.raw_entry(raw_db, '2026-03-05', 6815, 1800, minor_of('-10.00'))
    helpers.raw(raw_db, 'UPDATE Bookings SET BookingType = NULL WHERE ID = ?', no_type)
    helpers.raw_entry(raw_db, '2026-02-30', 6850, 1800, minor_of('-20.00'))
    helpers.raw_entry(raw_db, '2026-03-06', 6805, 1800, 0, rate=0.19, tax=minor_of('-3.00'))
    later = helpers.raw_bank(raw_db, '2026-04-02', giro, minor_of('-40.00'))
    helpers.raw_entry(raw_db, '2026-03-20', 6815, 1800, minor_of('-40.00'), parent=later)
    other = helpers.raw_bank(raw_db, '2026-03-07', giro, minor_of('-50.00'))
    helpers.raw_entry(raw_db, '2026-03-07', 6850, 1810, minor_of('-50.00'), parent=other)
    report = raw_db.get_susa(2026, 3)
    found = notices_of(report)
    new = ['bad_date', 'unknown_type', 'zero_amount_tax', 'child_other_period', 'child_other_account']                     # in der Reihenfolge der Seite
    assert all(key in found for key in new) and [notice.key for notice in report.notices] == [key for key in susa.NOTICE_ORDER if key in found]
    assert [key for key in susa.NOTICE_ORDER if key in new] == new
    page = PageSusa(raw_db, SusaQuery(2026, 3))
    positions = [page.index(f'<li>{html_lib.escape(susa.notice_text(found[key]), quote=True)}</li>') for key in new]
    assert positions == sorted(positions) and len(set(positions)) == len(new)
    assert any(check.difference for check in report.bank_checks) and html_lib.escape(susa.BANK_DIFF_TEXT, quote=True) in page


def test_banks_without_a_check_are_named(raw_db):
    raw_db.insert_account('Zweites Konto', 'Ich', 'DE05', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    page = page_of(raw_db, 2026, 3)
    assert html_lib.escape(susa.BANK_SKIPPED_TEXT.format(names='Probe-Giro, Zweites Konto'), quote=True) in page
    assert page.count('<table class="reportTable">') == 1                                        # keine Zeile, keine Tabelle des Abgleichs


def test_the_page_has_no_note_about_an_invalid_period(tmp_db):
    """Ein ungültiger Zeitraum der Adresse wird wie auf jeder Seite verworfen (``resolve_period``); die Seite hat dafür keinen Hinweis mehr."""
    page = page_of(tmp_db)
    assert 'warnColor' not in page and INVALID_PERIOD_TEXT not in page and 'ungültig' not in page


def test_the_page_uses_no_inline_style_and_only_var_and_function(susa_world):
    page = page_of(susa_world.db, 2026, 8)
    own = page[page.index("<div class='header2'>"):page.index('<div id="app_confirm"')]
    assert ' style=' not in own and 'innerHTML' not in own
    scripts = Dom(page).scripts
    script = scripts[1]                                                     # hinter dem Skript der Menüzeile (gotoPeriod)
    assert 'gotoPeriod' in scripts[0] and script.startswith('\nvar SUSA_DATA = ')
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden


def test_the_pdf_address_stands_only_in_the_script_block(susa_world):
    page = page_of(susa_world.db, 2026, 3)
    assert '/susa/pdf' not in re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    data = re.search(r'var SUSA_DATA = (.*?);\n', Dom(page).scripts[1], re.S).group(1)
    assert json.loads(data) == {'pdf': '/susa/pdf?year=2026&month=3'}


def test_names_with_markup_are_escaped_on_the_page(tmp_path):
    """Der Name eines Kontos des Kontenrahmens und der eines Bankkontos mit Markup stehen als Text da: Der Aufbau der Seite bleibt der einer Seite mit
    harmlosen Namen."""
    pages = []
    for index, (account_name, bank_name) in enumerate(((HOSTILE, f'Giro {HOSTILE}'), ('Harmloser Name', 'Giro Harmlos'))):
        db = Database(str(tmp_path / f'db{index}.db'))
        db.insert_account(bank_name, 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
        db.insert_chart_of_accounts(4, 6890, account_name, 'Test')
        helpers.mask(db, bank_name, 4400, '119.00', '2026-03-10', '19', '19.00')
        helpers.mask(db, bank_name, 6890, '-12.00', '2026-03-11')
        helpers.raw_bank(db, '2026-03-12', helpers.account_id(db, bank_name), 50_000)
        pages.append(page_of(db, 2026, 3))
    hostile, benign = pages
    assert HOSTILE not in hostile and '<script>alert(1)</script>' not in hostile
    assert html_lib.escape(HOSTILE, quote=True) in hostile
    assert_same_structure(hostile, benign)
    assert any(row.startswith('6890 | ' + HOSTILE) for row in table_rows(hostile))
    assert any(f'Giro {HOSTILE}' in row for row in table_rows(hostile, 1))


def test_no_brand_name_stands_on_the_page(susa_world):
    page = page_of(susa_world.db, 2026, 8)
    visible = html_lib.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<script>.*?</script>', '', page, flags=re.S)))
    for brand in BRANDS:
        assert brand.lower() not in visible.lower(), brand


def test_building_the_page_writes_nothing(susa_world):
    before = dump(susa_world.db.db_name)
    page_of(susa_world.db, 2026, 8)
    assert dump(susa_world.db.db_name) == before


def test_the_page_of_every_month_of_the_demo_is_built(demo_db):
    for year, month in ((2025, 1), (2025, 12), (2026, 6), (2026, 11)):
        page = page_of(demo_db, year, month)
        assert page.count('<tr') > 20 and MONTH_NAMES[month - 1] in page


# ═══ Routen ═══════════════════════════════════════════════════════════════════
PERIOD_COOKIE = 'period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax'


def test_the_page_and_the_pdf_are_get_routes_that_write_nothing_into_the_books(server_port, auto_login):
    """Der Nachweis für die Positivliste des Diagnose-MCP (``/susa``): Datenbank und Datenordner sind nach jedem Aufruf unverändert, es gibt kein
    Steuerdatum. Die Seite setzt – wie jede Seite mit Zeitraum – das Cookie des gemeinsamen Zeitraums, wenn der aus der Adresse kam; ``/susa/pdf``
    setzt nie eines (es steht nicht in der Liste, ein Dokument, schreibt aber auch nichts)."""
    helpers.build_susa_world(auto_login.db)
    before_db, before_tree = dump(auto_login.db.db_name), tree(userctx.DATA_ROOT)
    for path, cookies in (('/susa', []), ('/susa?from=2026-03-01&to=2026-03-31', [PERIOD_COOKIE]), ('/susa?from=2026-03-31&to=2026-03-01', [PERIOD_COOKIE]),
                          ('/susa?from=2026-01-01&to=2026-12-31', ['period=2026-01-01..2026-12-31; Path=/; Max-Age=31536000; SameSite=Lax']),
                          ('/susa?from=x', []), ('/susa?year=2026&month=3', []), ('/susa?year=x&month=3', []), ('/susa?acct=1&variant=jahr', [])):
        response = get(server_port, path)
        assert response.status == 200, path
        assert (response.headers.get_all('Set-Cookie') or []) == cookies, path
    for path in ('/susa/pdf?year=2026&month=3', '/susa/pdf?year=2026&month=12', '/susa/pdf', '/susa/pdf?year=2026', '/susa/pdf?year=bad&month=3',
                 '/susa/pdf?from=2026-03-01&to=2026-03-31'):
        response = get(server_port, path)
        assert response.headers.get('Set-Cookie') is None, path
        assert response.status in (200, 400), path
    assert dump(auto_login.db.db_name) == before_db
    assert tree(userctx.DATA_ROOT) == before_tree


def test_the_page_answers_for_the_month_of_the_period_in_the_address(server_port, auto_login):
    helpers.build_susa_world(auto_login.db)
    response = get(server_port, '/susa?from=2026-08-01&to=2026-08-31')
    assert response.status == 200 and response.headers['Content-Type'].startswith('text/html')
    assert 'Summen- und Saldenliste – August 2026' in response.text and 'warnColor' not in response.text
    assert reports_submenu('susa') in response.text and 'Hinweise zu den Buchungen bis zum 31.08.2026' in response.text
    assert any(row.startswith('Gesamtsumme | ') for row in table_rows(response.text))
    bare = get(server_port, '/susa')
    assert bare.status == 200 and 'warnColor' not in bare.text and 'Summen- und Saldenliste – ' in bare.text
    assert 'Der gewählte Zeitraum' not in get(server_port, '/susa?from=2026-13-01&to=2026-03-31').text       # ungültig: wie überall verworfen, ohne Hinweis


def test_the_pdf_route_answers_with_a_download(server_port, auto_login):
    from urllib.parse import quote
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH')
    helpers.build_susa_world(db)
    response = get(server_port, '/susa/pdf?year=2026&month=8')
    assert response.status == 200 and response.headers['Content-Type'] == 'application/pdf' and response.body.startswith(b'%PDF-')
    assert response.headers['Content-Disposition'].startswith('attachment;') and 'Summen_und_Salden_2026-08.pdf' in response.headers['Content-Disposition']
    assert response.headers['X-Filename'] == quote('Summen_und_Salden_2026-08.pdf', safe='')
    assert response.headers['Cache-Control'] == 'no-cache'


def test_the_pdf_route_refuses_what_it_cannot_read(server_port, auto_login):
    for path in ('/susa/pdf', '/susa/pdf?year=2026', '/susa/pdf?month=3', '/susa/pdf?year=2026&month=13', '/susa/pdf?year=%202026&month=3',
                 '/susa/pdf?year=2026&month=3&month=4', '/susa/pdf?year=26&month=3'):
        response = get(server_port, path)
        assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', path
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'


def test_a_failing_pdf_is_a_fixed_sentence_never_the_text_of_the_error(server_port, auto_login, monkeypatch):
    from export import pdf_susa

    def broken(*_args, **_kwargs):
        raise RuntimeError('Geheimer Pfad /srv/daten')
    monkeypatch.setattr(pdf_susa, 'generate_susa_pdf', broken)
    response = get(server_port, '/susa/pdf?year=2026&month=3')
    assert response.status == 500 and response.text == SUSA_PDF_FAILED_TEXT
    assert 'Geheimer Pfad' not in response.text and response.headers['Content-Type'] == 'text/plain; charset=utf-8'


# ═══ Menü und Diagnose-MCP ════════════════════════════════════════════════════
def test_the_susa_page_is_a_get_route_of_the_diagnosis_tool_and_its_pdf_is_not():
    import tools.mcp_ubuchhaltung as mcp
    assert mcp._PAGE_ALLOWLIST['/susa'] == frozenset({'from', 'to', 'date_from', 'date_to'})          # der gemeinsame Zeitraum, wie bei jeder Seite mit Zeitraum
    assert mcp.check_page_path('/susa?from=2026-10-01&to=2026-10-31') == '/susa?from=2026-10-01&to=2026-10-31'
    with pytest.raises(ValueError, match='Seite nicht freigegeben'):
        mcp.check_page_path('/susa/pdf?year=2026&month=10')
    with pytest.raises(ValueError, match='Query-Parameter nicht freigegeben'):
        mcp.check_page_path('/susa?from=2026-10-01&to=2026-10-31&write=1')
    with pytest.raises(ValueError, match='Query-Parameter nicht freigegeben'):
        mcp.check_page_path('/susa?year=2026&month=10')                                                 # Jahr und Monat liest die Seite nicht mehr
