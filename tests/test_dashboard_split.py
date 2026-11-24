# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Dashboard zerlegt jede Bewegung in Einnahme, Ausgabe, privat und Übertrag (seit 2.4.1, intern T36).

* Die reinen Funktionen ``split_bank_movement`` und ``split_cash_entry``: jede Zeile der Regeltabelle (``docs/regeln/auswertungen.md``,
  „Dashboard“) und das Gegenbeispiel zur naheliegenden, aber falschen Formel („Betrag − privat − Wirkung der übrigen Sätze“
  zieht einen privaten Umbuchungssatz doppelt ab).
* Überträge zwischen zwei Bankkonten (Giro, Tagesgeld) und zur Kasse – beide Seiten kontiert, nur eine (von der
  Übertragsrechnung des DATEV-Exports gedeckt oder nicht), mit jedem Kontofilter.
* Privatanteil, „Privatentnahme an Bank“, Einlage; Rechnungszahlung und Erstattung bleiben Einnahme bzw. Ausgabe.
* Kaputte Werte (Text als Betrag, ein Datum, das keines ist): kein Fehler, keine Zeile geht verloren.
* Eigenschaftsprüfung über die Testwelt (``entry_form_world``: Kasse 1600, 1460, ohne Nummer; frisch gebaut und
  migriert; jeder Kontofilter): Die vier Töpfe ergeben den Saldo, der Saldo ist eine unabhängig gerechnete Summe der
  Bewegungen und gleich dem von vor der Änderung, Monate und Summen gehen auf.
* Feste Sollwerte der migrierten Welt.
* Die Seite: Zeile „Überträge zwischen eigenen Konten“ nur bei einem Übertrag, alle Werte maskiert.

Alle Namen, Nummern und Beträge sind erfunden.
"""
import json
import os
import random
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from db import Database, _CoreMixin
from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, FORM_PURPOSE_FIRST,
    FORM_TRANSFER,
)
from db.reporting import _minor_or_zero, split_bank_movement, split_cash_entry
from money import from_minor, to_minor
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped
from server.pages_dashboard import PageDashboard
from tests import entry_form_world as world
from tests.entry_form_helpers import D1, D2, bank_account, cash_account_id, coa, execute, query

GOLDEN = json.loads((Path(__file__).parent / 'fixtures' / 'entry_form_golden_2_0_8.json')
                    .read_text(encoding='utf-8'))


def eur(value):
    """Euro-Text → Minor Units."""
    return to_minor(Decimal(value))


# ── die reinen Funktionen ────────────────────────────────────────────────────

# Als COA-ID dient hier die Kontonummer: Die Funktionen kennen nur IDs.
GIRO, TAGES, KASSE, CLEARING = 1800, 1810, 1600, 1460
EXPENSE, REVENUE, WAIT, DEBTOR = 6815, 4400, 4405, 10000
PRIVATE, DEPOSIT = 2100, 2180
OWN = frozenset({GIRO, TAGES, KASSE})                  # eigene Bank- und Kassenkonten
PRIVATE_IDS = frozenset({PRIVATE, DEPOSIT})            # Konten 2100–2199
MASK = FORM_MIRROR_COUNTER                             # so schreibt die Buchungsmaske einen Spiegel


def child(amount, coa_id, counter_id, form):
    return (eur(amount), coa_id, counter_id, form)


def bank(amount, children=(), parent=frozenset({GIRO}), own=OWN, covered=False):
    """``split_bank_movement`` mit den Konten dieses Abschnitts; ``parent``: Konto der Bewegung."""
    return split_bank_movement(eur(amount), list(children), parent, own, own | {CLEARING}, PRIVATE_IDS, covered)


def cash(amount, coa_id, counter_id, form, own=OWN):
    return split_cash_entry(eur(amount), coa_id, counter_id, form, own, PRIVATE_IDS)


# Jede Zeile der Regeltabelle: Bewegung, Sätze, Auswahl, erwartet (Einnahme, Ausgabe, privat, Übertrag)
BANK_CASES = [
    ('ausgang-mit-spiegel', '-100', [child('-100', TAGES, GIRO, MASK)], {}, ('0', '0', '0', '-100')),
    ('eingang-mit-spiegel', '100', [child('100', GIRO, TAGES, MASK)], {'parent': {TAGES}}, ('0', '0', '0', '100')),
    ('eingang-ohne-satz-gedeckt', '100', [], {'covered': True}, ('0', '0', '0', '100')),
    ('eingang-ohne-satz-nicht-gedeckt', '100', [], {}, ('100', '0', '0', '0')),
    ('ausgang-ohne-satz-gedeckt', '-100', [], {'covered': True}, ('0', '0', '0', '-100')),
    ('ausgang-ohne-satz-nicht-gedeckt', '-100', [], {}, ('0', '-100', '0', '0')),
    ('abhebung-bank-an-kasse', '-100', [child('-100', KASSE, GIRO, MASK)], {}, ('0', '0', '0', '-100')),
    # Der Betrag eines Spiegels wird nie gelesen – auch nicht der eines Spiegels des Bestands (Lesart unbekannt)
    ('spiegel-des-bestands-roh-positiv', '-100', [child('100', KASSE, GIRO, FORM_MIRROR_LEGACY)], {},
     ('0', '0', '0', '-100')),
    ('spiegel-des-bestands-roh-negativ', '-100', [child('-100', GIRO, KASSE, FORM_MIRROR_LEGACY)], {},
     ('0', '0', '0', '-100')),
    ('spiegel-ohne-form', '-100', [child('-100', KASSE, GIRO, None)], {}, ('0', '0', '0', '-100')),
    ('privatanteil', '-50', [child('-50', EXPENSE, GIRO, FORM_PURPOSE_FIRST),
                             child('10', PRIVATE, EXPENSE, FORM_TRANSFER)], {}, ('0', '-40', '-10', '0')),
    # Gegenbeispiel zu „Betrag − privat − Wirkung der übrigen Sätze“ (Übertrag −30): Die Umbuchung „2100 an 6815“ hat auf das
    # Konto der Bank keine Wirkung und zählt nicht noch einmal
    ('privatanteil-neben-einem-spiegel', '-50', [child('-40', TAGES, GIRO, MASK),
                                                 child('-10', EXPENSE, GIRO, FORM_PURPOSE_FIRST),
                                                 child('10', PRIVATE, EXPENSE, FORM_TRANSFER)], {},
     ('0', '0', '-10', '-40')),
    # Privat über das Verrechnungskonto (WISO-Paar „X an 1460“ plus „1460 an Bank“): Der Satz „2100 an 1460“ wirkt auf das
    # Konto der Bank nicht (Wirkung 0), privat ist trotzdem sein Betrag – sonst stünde er unter den Ausgaben
    ('privat-ueber-1460-kasse-ist-nicht-1460', '-50', [child('-50', PRIVATE, CLEARING, FORM_PURPOSE_FIRST),
                                                       child('50', CLEARING, GIRO, FORM_MIRROR_COA)], {},
     ('0', '0', '-50', '0')),
    ('privat-und-aufwand-ueber-1460', '-100', [child('-80', EXPENSE, CLEARING, FORM_PURPOSE_FIRST),
                                               child('-20', PRIVATE, CLEARING, FORM_PURPOSE_FIRST),
                                               child('100', CLEARING, GIRO, FORM_MIRROR_COA)], {},
     ('0', '-80', '-20', '0')),
    ('privat-ueber-1460-kasse-ist-1460', '-50', [child('-50', PRIVATE, CLEARING, FORM_PURPOSE_FIRST),
                                                 child('50', CLEARING, GIRO, FORM_MIRROR_COA)],
     {'own': OWN | {CLEARING}}, ('0', '0', '-50', '0')),
    # Zwei Spiegel unter einer Bewegung (ein Ausgang, aufgeteilt auf zwei eigene Konten): zusammen der ganze Übertrag
    ('zwei-spiegel-kinder', '-100', [child('-60', TAGES, GIRO, MASK), child('-40', KASSE, GIRO, MASK)], {},
     ('0', '0', '0', '-100')),
    ('entnahme-an-bank', '-50', [child('-50', PRIVATE, GIRO, FORM_PURPOSE_FIRST)], {}, ('0', '0', '-50', '0')),
    ('einlage-bank-an-2180', '500', [child('500', GIRO, DEPOSIT, FORM_LIQUID_FIRST)], {}, ('0', '0', '500', '0')),
    ('umbuchung-zwischen-privatkonten', '-30', [child('-30', EXPENSE, GIRO, FORM_PURPOSE_FIRST),
                                                child('5', PRIVATE, DEPOSIT, FORM_TRANSFER)], {},
     ('0', '-30', '0', '0')),
    # Eine nicht ganz kontierte Umbuchung mit 2100 im Konto: −Betrag privat, ihre Wirkung auf die Bank zählt als Rest mit
    ('umbuchung-mit-privatkonto-nicht-ganz-kontiert', '-20', [child('-20', PRIVATE, None, FORM_TRANSFER)], {},
     ('0', '-40', '20', '0')),
    ('unkontierter-rest', '-100', [child('-70', EXPENSE, GIRO, FORM_PURPOSE_FIRST)], {}, ('0', '-100', '0', '0')),
    # Der unkontierte Rest neben einem Spiegel steckt im Übertrag (docs/aufgaben.md)
    ('spiegel-mit-unkontiertem-rest', '-150', [child('-100', TAGES, GIRO, MASK),
                                               child('-30', EXPENSE, GIRO, FORM_PURPOSE_FIRST)], {},
     ('0', '-30', '0', '-120')),
    ('aufwand-an-kasse-unter-giro', '-50', [child('-50', EXPENSE, KASSE, FORM_PURPOSE_FIRST)], {},
     ('0', '-50', '0', '0')),
    # 1460 ist nur dann ein eigenes Konto, wenn die Kasse es führt
    ('1460-an-1800-kasse-ist-nicht-1460', '-119', [child('-119', CLEARING, GIRO, MASK)], {}, ('0', '-119', '0', '0')),
    ('1460-an-1800-kasse-ist-1460', '-119', [child('-119', CLEARING, GIRO, MASK)], {'own': OWN | {CLEARING}},
     ('0', '0', '0', '-119')),
    ('rechnungszahlung', '119', [child('119', GIRO, DEBTOR, FORM_LIQUID_FIRST),
                                 child('119', WAIT, REVENUE, FORM_TRANSFER)], {}, ('119', '0', '0', '0')),
    ('erstattung', '-50', [child('-50', GIRO, DEBTOR, FORM_LIQUID_FIRST),
                           child('-50', WAIT, REVENUE, FORM_TRANSFER)], {}, ('0', '-50', '0', '0')),
    ('erloes-an-bank', '105', [child('105', REVENUE, GIRO, FORM_PURPOSE_FIRST)], {}, ('105', '0', '0', '0')),
    # Ein Bankkonto ohne SKR-Nummer kennt kein Konto der Bewegung: Die Wirkung eines Satzes folgt ``bank_effect``
    ('bankkonto-ohne-skr-nummer', '-50', [child('-50', PRIVATE, None, FORM_PURPOSE_FIRST)], {'parent': None},
     ('0', '0', '-50', '0')),
]


@pytest.mark.parametrize('amount, children, options, expected',
                         [pytest.param(*case[1:], id=case[0]) for case in BANK_CASES])
def test_a_bank_movement_is_split_without_a_rest(amount, children, options, expected):
    income, private, expense, transfers = bank(amount, children, **options)

    assert (income, expense, private, transfers) == tuple(eur(part) for part in expected)
    assert income + private + expense + transfers == eur(amount)


CASH_CASES = [
    ('spiegel-kasse-im-soll', '100', KASSE, GIRO, FORM_MIRROR_COA, OWN, ('0', '0', '0', '100')),
    ('spiegel-kasse-im-haben', '-100', GIRO, KASSE, FORM_MIRROR_COA, OWN, ('0', '0', '0', '-100')),
    ('spiegel-des-bestands-roh', '100', GIRO, KASSE, FORM_MIRROR_LEGACY, OWN, ('0', '0', '0', '100')),
    ('spiegel-ohne-form', '100', GIRO, KASSE, None, OWN, ('0', '0', '0', '100')),
    ('privat-auf-der-kasse', '-50', PRIVATE, KASSE, FORM_PURPOSE_FIRST, OWN, ('0', '0', '-50', '0')),
    ('einnahme', '50', KASSE, REVENUE, FORM_LIQUID_FIRST, OWN, ('50', '0', '0', '0')),
    ('ausgabe', '-18.35', EXPENSE, KASSE, FORM_PURPOSE_FIRST, OWN, ('0', '-18.35', '0', '0')),
    ('1460-kein-eigenes-konto', '100', CLEARING, GIRO, FORM_MIRROR_COA, OWN, ('100', '0', '0', '0')),
    ('1460-ist-die-kasse', '100', CLEARING, GIRO, FORM_MIRROR_COA, OWN | {CLEARING}, ('0', '0', '0', '100')),
]


@pytest.mark.parametrize('amount, coa_id, counter_id, form, own, expected',
                         [pytest.param(*case[1:], id=case[0]) for case in CASH_CASES])
def test_a_standalone_entry_on_the_cash_account_lands_in_exactly_one_pot(
        amount, coa_id, counter_id, form, own, expected):
    income, private, expense, transfers = cash(amount, coa_id, counter_id, form, own)

    assert (income, expense, private, transfers) == tuple(eur(part) for part in expected)
    assert sum(1 for part in (income, private, expense, transfers) if part) <= 1


def test_a_float_beyond_64_bits_counts_as_nothing():
    """Eine Kommazahl, die in keine Ganzzahl-Spalte passt (nur per SQL-Konsole schreibbar), zählt 0 – ``from_minor`` könnte
    sie nicht darstellen, und die Seite bekäme einen Fehler."""
    assert _minor_or_zero(1e300) == 0 and _minor_or_zero(-1e300) == 0 and _minor_or_zero(float(2 ** 63)) == 0
    assert _minor_or_zero(1e15) == 10 ** 15
    assert split_bank_movement(1e300, [], frozenset({GIRO}), OWN, OWN | {CLEARING}, PRIVATE_IDS) == (0, 0, 0, 0)


def test_amounts_that_are_not_whole_numbers_count_as_nothing_and_never_raise():
    assert [_minor_or_zero(value) for value in (5, -7, 0, True, False, 5.9, -5.9, float('nan'), float('inf'),
                                               'abc', '12', None, b'7', Decimal('3'))] == \
           [5, -7, 0, 0, 0, 5, -5, 0, 0, 0, 0, 0, 0, 0]
    assert bank('1') == (eur('1'), 0, 0, 0)
    assert split_bank_movement('abc', [], None, OWN, OWN, PRIVATE_IDS) == (0, 0, 0, 0)
    assert split_bank_movement(None, [(None, EXPENSE, GIRO, None), ('x', PRIVATE, None, FORM_TRANSFER)],
                               {GIRO}, OWN, OWN, PRIVATE_IDS) == (0, 0, 0, 0)
    assert split_cash_entry('abc', KASSE, GIRO, FORM_MIRROR_COA, OWN, PRIVATE_IDS) == (0, 0, 0, 0)
    assert split_cash_entry(None, EXPENSE, KASSE, None, OWN, PRIVATE_IDS) == (0, 0, 0, 0)


def test_the_pots_always_add_up_to_the_amount_and_keep_their_signs():
    """Zufällige Bewegungen (fester Anfangswert, keine Uhr) aus jeder Form und jeder Kontenseite: Die vier Teile ergeben
    den Betrag, die Einnahme ist nie negativ, die Ausgabe nie positiv."""
    rng = random.Random(36)
    accounts = [GIRO, TAGES, KASSE, CLEARING, EXPENSE, REVENUE, WAIT, DEBTOR, PRIVATE, DEPOSIT, None]
    forms = [FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER, FORM_MIRROR_COA, FORM_MIRROR_COUNTER,
             FORM_MIRROR_LEGACY, None]
    for _ in range(2000):
        children = [(rng.randint(-20000, 20000), rng.choice(accounts), rng.choice(accounts), rng.choice(forms))
                    for _ in range(rng.randint(0, 4))]
        own = rng.choice([OWN, OWN | {CLEARING}])
        parent = rng.choice([frozenset({GIRO}), frozenset({TAGES}), frozenset({KASSE}), None])
        amount = rng.randint(-50000, 50000)
        parts = split_bank_movement(amount, children, parent, own, own | {CLEARING}, PRIVATE_IDS,
                                    covered=rng.random() < 0.3)
        assert sum(parts) == amount, (amount, children, own, parent)
        assert parts[0] >= 0 and parts[2] <= 0, (amount, children, own, parent)
        entry = split_cash_entry(amount, rng.choice(accounts), rng.choice(accounts), rng.choice(forms), own, PRIVATE_IDS)
        assert sum(entry) == amount and entry[0] >= 0 and entry[2] <= 0


# ── Giro, Tagesgeld und Kasse ────────────────────────────────────────────────

@pytest.fixture
def w(tmp_db):
    giro = bank_account(tmp_db, 'Giro-Eins', 1800)
    tages = bank_account(tmp_db, 'Tages-Zwei', 1810)
    return SimpleNamespace(db=tmp_db, giro=giro, tages=tages, kasse=cash_account_id(tmp_db))


def pots(db, account_ids=None, date_from=D1, date_to=D2):
    """(Einnahme, Ausgabe, privat, Übertrag) der Karten – in der Reihenfolge der Regeltabelle."""
    totals = db.get_dashboard_totals(date_from, date_to, account_ids)
    return totals['income'], totals['expense'], totals['private'], totals['transfers']


def mask(w, account, coa_number, amount, date):
    """Eine Bankbewegung über die Buchungsmaske, das Gegenkonto in „SKR-Konto“ (so kontiert der Nutzer einen Übertrag)."""
    post = {'transaction_id': ['0'], 'date': [date], 'amount': [amount], 'currency': ['EUR'],
            'account': [str(account)], 'coa_id': [str(coa(w.db, coa_number))]}
    assert handle_add_transaction(w.db, post)[0] == 303


def movement(w, account, amount, date):
    """Eine Bankbewegung ohne Buchungssatz."""
    return w.db.insert_booking(date, Decimal(amount), account_id=account, booking_type='bank')


def test_a_transfer_between_two_bank_accounts_booked_on_both_sides_is_neither_income_nor_expense(w):
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    mask(w, w.tages, 1800, '100.00', '2026-03-03')

    assert pots(w.db) == (0, 0, 0, 0)
    totals = w.db.get_dashboard_totals(D1, D2)
    assert (totals['balance'], totals['bank_count'], totals['unlinked_count']) == (0, 2, 0)
    # Ein Übertrag auf ein nicht gewähltes Konto bleibt ein Übertrag und steht dann netto unter „Überträge“
    assert pots(w.db, [w.giro]) == (0, 0, 0, -100.0)
    assert pots(w.db, [w.tages]) == (0, 0, 0, 100.0)
    assert w.db.get_dashboard_totals(D1, D2, [w.giro])['balance'] == -100.0
    march = next(m for m in w.db.get_dashboard_monthly(D1, D2, [w.giro]) if m['year_month'] == '2026-03')
    assert (march['income'], march['expense'], march['transfers']) == (0, 0, -100.0)
    assert pots(w.db, [w.kasse]) == (0, 0, 0, 0)                   # die Kasse hat hier keine Bewegung


def test_a_transfer_booked_on_one_side_only_counts_as_a_transfer_when_the_export_arithmetic_covers_it(w):
    """Giro kontiert („1810“), das Tagesgeld ohne Satz: Die Übertragsrechnung des DATEV-Exports (``settle_transfers``) deckt
    die Bewegung – sie ist ein Übertrag, auch als einziges gewähltes Konto. Als „offen“ zählt sie weiter: Sie ist nicht
    kontiert."""
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.tages, '100.00', '2026-03-03')

    assert pots(w.db) == (0, 0, 0, 0)
    assert pots(w.db, [w.giro]) == (0, 0, 0, -100.0)
    assert pots(w.db, [w.tages]) == (0, 0, 0, 100.0)
    assert w.db.get_dashboard_totals(D1, D2)['unlinked_count'] == 1
    assert w.db.get_dashboard_totals(D1, D2, [w.tages])['unlinked_count'] == 1
    assert w.db.get_dashboard_totals(D1, D2, [w.giro])['unlinked_count'] == 0


def test_a_movement_without_a_record_that_nothing_covers_stays_income(w):
    """Zwei gleich hohe Eingänge ohne Satz neben EINEM kontierten Ausgang: Die Rechnung deckt keinen von beiden."""
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.tages, '100.00', '2026-03-03')
    movement(w, w.tages, '100.00', '2026-03-04')

    assert pots(w.db) == (200.0, 0, 0, -100.0)
    assert pots(w.db, [w.tages]) == (200.0, 0, 0, 0)
    assert w.db.get_dashboard_totals(D1, D2, [w.tages])['unlinked_count'] == 2


def test_a_counter_movement_more_than_14_days_later_is_not_covered(w):
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.tages, '100.00', '2026-04-20')

    assert pots(w.db, [w.tages]) == (100.0, 0, 0, 0)
    assert pots(w.db) == (100.0, 0, 0, -100.0)
    assert w.db.get_dashboard_totals(D1, D2)['unlinked_count'] == 1


def test_the_private_share_of_a_payment_is_split_off(w):
    """−50 gezahlt, davon 10 privat: „6805 an Bank“ −50 und „2100 an 6805“ +10 – Ausgabe −40, privat −10 (bis 2.4.0 stand
    die ganze Bewegung unter „privat“)."""
    db = w.db
    bank_id = movement(w, w.giro, '-50', '2026-04-01')
    db.insert_booking('2026-04-01', Decimal('-50'), coa_id=coa(db, 6805), counter_coa_id=coa(db, 1800),
                      booking_type='entry', parent_booking_id=bank_id)
    db.insert_booking('2026-04-01', Decimal('10'), coa_id=coa(db, 2100), counter_coa_id=coa(db, 6805),
                      booking_type='entry', parent_booking_id=bank_id)

    assert pots(db) == (0, -40.0, -10.0, 0)
    assert db.get_dashboard_totals(D1, D2)['balance'] == -50.0
    assert pots(db, [w.tages]) == (0, 0, 0, 0)


def test_a_private_share_booked_through_the_clearing_account_stays_private(w):
    """Giro −100 als WISO-Paar über 1460: „6815 an 1460“ −80, „2100 an 1460“ −20 und „1460 an 1800“ +100. Die Kasse führt
    1600, 1460 ist also kein eigenes Konto und der private Satz wirkt auf das Giro nicht – privat bleibt er trotzdem (wie
    die EÜR ihn liest), und nur der Aufwand ist Ausgabe."""
    db = w.db
    bank_id = movement(w, w.giro, '-100', '2026-04-01')
    for amount, account, counter in (('-80', 6815, 1460), ('-20', 2100, 1460)):
        db.insert_booking('2026-04-01', Decimal(amount), coa_id=coa(db, account), counter_coa_id=coa(db, counter),
                          booking_type='entry', parent_booking_id=bank_id)
    leg = db.insert_booking('2026-04-01', Decimal('100'), coa_id=coa(db, 1460), counter_coa_id=coa(db, 1800),
                            booking_type='entry', parent_booking_id=bank_id)
    execute(db, 'UPDATE Bookings SET EntryForm = ? WHERE ID = ?', (FORM_MIRROR_COA, leg))   # so schreibt ihn der Import

    assert pots(db) == (0, -80.0, -20.0, 0)
    assert db.get_dashboard_totals(D1, D2)['balance'] == -100.0


def test_a_legacy_mirror_on_the_cash_account_does_not_cover_the_bank_movement_without_a_record(w):
    """Altbestand: die Abhebung steht als eigenständiger Spiegel des Bestands auf der Kasse („1600 an 1800“ +100,
    ``mirror_legacy``), die Giro-Bewegung −100 hat keinen Satz. Der Spiegel wird nie gedeutet, er deckt die Bankseite also
    nicht: Sie bleibt bis zum Kontieren Ausgabe (und „offen“), die Kassenseite ist Übertrag – eine bekannte Grenze
    (docs/aufgaben.md). Der Saldo stimmt."""
    db = w.db
    movement(w, w.giro, '-100', '2026-04-01')
    mirror = db.insert_booking('2026-04-01', Decimal('100'), coa_id=coa(db, 1600), counter_coa_id=coa(db, 1800),
                               booking_type='entry')
    execute(db, 'UPDATE Bookings SET EntryForm = ? WHERE ID = ?', (FORM_MIRROR_LEGACY, mirror))

    assert pots(db) == (0, -100.0, 0, 100.0)
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['balance'], totals['unlinked_count']) == (0, 1)


def test_a_private_withdrawal_and_a_private_deposit_are_private(w):
    db = w.db
    out = movement(w, w.giro, '-50', '2026-04-01')
    db.insert_booking('2026-04-01', Decimal('-50'), coa_id=coa(db, 2100), counter_coa_id=coa(db, 1800),
                      booking_type='entry', parent_booking_id=out)
    inflow = movement(w, w.giro, '500', '2026-04-02')
    db.insert_booking('2026-04-02', Decimal('500'), coa_id=coa(db, 1800), counter_coa_id=coa(db, 2180),
                      booking_type='entry', parent_booking_id=inflow)

    assert pots(db) == (0, 0, 450.0, 0)                           # −50 Entnahme, +500 Einlage
    april = next(m for m in db.get_dashboard_monthly(D1, D2) if m['year_month'] == '2026-04')
    assert (april['private'], april['income'], april['expense']) == (450.0, 0, 0)


def test_a_withdrawal_booked_in_the_mask_is_a_transfer_for_the_bank_and_missing_on_the_cash_side(w):
    """Bank → Kasse über die Maske: Das Kind „1600 an 1800“ hängt an der Bankbewegung. Mit der Bank ist es ein Übertrag; mit
    nur der Kasse fehlt die Bewegung (ihre Kassenseite steht nur als Satz unter der Bankbewegung – docs/aufgaben.md)."""
    mask(w, w.giro, 1600, '-100.00', '2026-03-02')

    assert pots(w.db) == (0, 0, 0, -100.0)
    assert pots(w.db, [w.giro]) == (0, 0, 0, -100.0)
    assert pots(w.db, [w.kasse]) == (0, 0, 0, 0)
    assert w.db.get_dashboard_totals(D1, D2, [w.kasse])['bank_count'] == 0


def _invoice(db, number, gross):
    net = round(gross / 1.19, 2)
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross,
        'amount_due': gross, 'status': 'sent'})


def test_an_invoice_payment_stays_income(w):
    invoice = _invoice(w.db, 'RE-T36', 119.00)
    w.db.ensure_receivable_booking(invoice)
    booking = w.db.insert_booking('2026-02-01', Decimal('119.00'), account_id=w.giro, booking_type='bank',
                                  recipient_client='Käufer AG')
    link_booking_to_invoice_capped(w.db, invoice, booking)

    assert pots(w.db) == (119.0, 0, 0, 0)
    assert w.db.get_dashboard_totals(D1, D2)['bank_count'] == 1


def test_a_refund_stays_an_expense(w):
    """Die Sätze einer Erstattung (``assign_storno_refund``): „Bank an Debitoren“ und „Wartekonto an Erlöskonto“, negativ."""
    db = w.db
    bank_id = movement(w, w.giro, '-50.00', '2026-07-10')
    db.insert_booking('2026-07-10', Decimal('-50.00'), coa_id=coa(db, 1800), counter_coa_id=coa(db, 10000),
                      booking_type='entry', parent_booking_id=bank_id, entry_form=FORM_LIQUID_FIRST)
    db.insert_booking('2026-07-10', Decimal('-50.00'), coa_id=coa(db, 4405), counter_coa_id=coa(db, 4400),
                      booking_type='entry', parent_booking_id=bank_id, entry_form=FORM_TRANSFER)

    assert pots(db) == (0, -50.0, 0, 0)


def test_the_clearing_account_and_the_depreciation_count_as_before(w):
    """Unverändert seit 2.4.0: Das Verrechnungskonto 1460 (bei einer Kasse ohne diese Nummer) und eine AfA-Buchung alter Form
    mit der Kasse als Konto sind keine Kassenbewegung; derselbe Satz ohne Planzeile zählt."""
    db = w.db
    db.insert_booking('2026-03-04', Decimal('-50.00'), coa_id=coa(db, 6805), counter_coa_id=coa(db, 1460),
                      booking_type='entry')
    asset = db.insert_asset('Altlaptop', '2026-01-10', 1800.0, 3, coa_id=coa(db, 520))
    old_form = db.insert_booking('2026-12-31', Decimal('-600'), account_id=w.kasse, coa_id=coa(db, 6220),
                                 booking_type='entry')
    execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, "
                "BookedAt) VALUES (?, 2026, 6000000, 12000000, ?, 'posted', CURRENT_TIMESTAMP)", (asset, old_form))

    assert pots(db) == (0, 0, 0, 0)
    assert db.get_dashboard_totals(D1, D2)['bank_count'] == 0

    db.insert_booking('2026-12-30', Decimal('-600'), account_id=w.kasse, coa_id=coa(db, 6220), booking_type='entry')

    assert pots(db, [w.kasse]) == (0, -600.0, 0, 0)
    assert db.get_dashboard_totals(D1, D2, [w.kasse])['bank_count'] == 1


# ── kaputte Werte ────────────────────────────────────────────────────────────

def test_an_amount_that_is_text_counts_as_a_row_but_lands_in_no_pot(w):
    """Per SQL-Konsole lässt sich jeder Wert schreiben: Die Zeile zählt in ``bank_count``, ihr Betrag 0 – kein Fehler."""
    db = w.db
    broken = movement(w, w.giro, '1.00', '2026-03-02')
    movement(w, w.giro, '10.00', '2026-03-03')
    execute(db, "UPDATE Bookings SET Amount = 'abc' WHERE ID = ?", (broken,))

    totals = db.get_dashboard_totals(D1, D2)

    assert (totals['income'], totals['expense'], totals['balance'], totals['bank_count']) == (10.0, 0, 10.0, 2)
    assert sum(m['income'] for m in db.get_dashboard_monthly(D1, D2)) == 10.0


def test_an_amount_that_is_text_on_a_child_does_not_raise(w):
    db = w.db
    bank_id = movement(w, w.giro, '-50.00', '2026-03-02')
    child_id = db.insert_booking('2026-03-02', Decimal('-50.00'), coa_id=coa(db, 6805),
                                 counter_coa_id=coa(db, 1800), booking_type='entry', parent_booking_id=bank_id)
    execute(db, "UPDATE Bookings SET Amount = 'abc' WHERE ID = ?", (child_id,))

    assert pots(db) == (0, -50.0, 0, 0)                        # die Bewegung bleibt Ausgabe: Der Satz erklärt nichts davon
    assert db.get_dashboard_totals(D1, D2)['bank_count'] == 1


def test_an_amount_with_decimals_is_cut_like_before(w):
    broken = movement(w, w.giro, '1.00', '2026-03-02')
    execute(w.db, 'UPDATE Bookings SET Amount = 1234.5 WHERE ID = ?', (broken,))

    assert w.db.get_dashboard_totals(D1, D2)['income'] == 0.1234     # 1234 Minor Units


def test_a_date_that_is_not_a_date_counts_in_the_totals_and_in_no_month(w):
    db = w.db
    movement(w, w.giro, '10.00', '2026-06-45')               # liegt im Zeitraum, SQLite liest kein Datum daraus
    movement(w, w.giro, '5.00', '2026-06-15')

    totals, monthly = db.get_dashboard(D1, D2)

    assert (totals['income'], totals['bank_count']) == (15.0, 2)
    assert sum(m['income'] for m in monthly) == 5.0
    assert len(monthly) == 12


def test_a_period_that_is_not_a_period_never_raises(w):
    db = w.db
    movement(w, w.giro, '10.00', '2026-13-45')
    movement(w, w.giro, '5.00', '2026-02-10')

    totals, monthly = db.get_dashboard(D1, '2026-13-99')
    assert (totals['income'], totals['bank_count']) == (15.0, 2)
    assert [m['year_month'] for m in monthly] == [f'2026-{n:02d}' for n in range(1, 13)]
    assert sum(m['income'] for m in monthly) == 5.0                # die Zeile mit dem Datum 2026-13-45 steht in keinem Monat

    # Ein Anfang ohne Monat ergibt keine Monatsliste (früher ein Fehler); die Summen rechnen mit dem Text, wie er ist
    for date_from, date_to in (('abc', 'def'), ('', ''), ('2026', D2), ('2026-13-01', D2), (None, None)):
        totals, monthly = db.get_dashboard(date_from, date_to)
        assert monthly == [], (date_from, date_to)
        assert totals['bank_count'] == (1 if date_from == '2026' else 0), (date_from, date_to)


def test_without_an_account_there_is_nothing_to_count(w):
    totals, monthly = w.db.get_dashboard(D1, D2, [9999])

    assert totals == {'income': 0, 'private': 0, 'expense': 0, 'transfers': 0, 'balance': 0,
                      'bank_count': 0, 'unlinked_count': 0}
    assert monthly == []
    assert w.db.get_dashboard_monthly(D1, D2, [9999]) == []


def test_the_totals_and_the_months_are_one_computation(w):
    """``get_dashboard_totals`` und ``get_dashboard_monthly`` sind dünne Hüllen um ``get_dashboard``."""
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.tages, '100.00', '2026-03-03')

    totals, monthly = w.db.get_dashboard(D1, D2, [w.giro])

    assert w.db.get_dashboard_totals(D1, D2, [w.giro]) == totals
    assert w.db.get_dashboard_monthly(D1, D2, [w.giro]) == monthly
    assert set(totals) == {'income', 'private', 'expense', 'transfers', 'balance', 'bank_count', 'unlinked_count'}
    assert set(monthly[0]) == {'year_month', 'label', 'income', 'private', 'expense', 'transfers'}


# ── Kosten der Übertragsrechnung ─────────────────────────────────────────────

def test_the_stock_is_read_only_when_a_movement_without_a_record_is_there(w, monkeypatch):
    """Die Übertragsrechnung liest den ganzen Bestand (``Database._settle_stock``): Das Dashboard ruft sie nur, wenn es eine
    Bewegung ohne Satz gibt, und je Aufruf von ``get_dashboard`` einmal."""
    calls = []
    settle = Database._settle_stock

    def counting(self, cursor):
        calls.append(1)
        return settle(self, cursor)
    monkeypatch.setattr(Database, '_settle_stock', counting)
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')

    w.db.get_dashboard(D1, D2)
    assert calls == []                                              # alles kontiert: nichts zu decken

    movement(w, w.tages, '100.00', '2026-03-03')
    w.db.get_dashboard(D1, D2)
    assert calls == [1]
    PageDashboard(w.db, D1, D2)                                     # die Seite ruft es für alle Karten und Monate einmal
    assert calls == [1, 1]


# ── Eigenschaftsprüfung über die Testwelt ────────────────────────────────────

CASH_NUMBERS = (1600, 1460, None)                 # SKR-Nummer der Kasse der Welt: 1600, Verrechnungskonto, keine
CASH_IDS = ['kasse-1600', 'kasse-1460', 'kasse-ohne-skr']


def build_world(tmp_path, cash_number, migrated):
    """Die Testwelt der Migration: frisch gebaut – oder als Stand 2.0.x zurückversetzt und wieder geöffnet (dann tragen
    ihre Sätze die Form, die 2.0.x ihnen gab: Spiegel des Bestands)."""
    path = str(tmp_path / 'welt.db')
    db = world.build(path, cash_number)
    if migrated:
        world.to_state_2000(path)
        _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
        db = Database(db_name=path)
    return db


@pytest.fixture(params=[(cash_number, migrated) for cash_number in CASH_NUMBERS for migrated in (False, True)],
                ids=[f'{name}-{kind}' for name in CASH_IDS for kind in ('frisch', 'migriert')])
def any_world(request, tmp_path):
    return build_world(tmp_path, *request.param)


@pytest.fixture(params=CASH_NUMBERS, ids=CASH_IDS)
def migrated_world(request, tmp_path):
    """(SKR-Nummer der Kasse, Datenbank der migrierten Welt)."""
    return request.param, build_world(tmp_path, request.param, True)


def world_filters(db):
    """Die Kontofilter: keiner, jedes Konto einzeln, nur Banken, nur Kassen, Bank + Kasse."""
    accounts = db.fetch_accounts()
    cashes = [a[0] for a in accounts if a[6] == 1]
    banks = [a[0] for a in accounts if a[6] != 1]
    filters = {'alle': None, 'nur Banken': banks, 'nur Kassen': cashes, 'Giro Alpha + Kasse': [banks[0], *cashes]}
    for account in accounts:
        filters[f'nur {account[1]}'] = [account[0]]
    return filters


def counted_movements(db, account_ids):
    """Was das Dashboard zählen soll, ohne das Dashboard: ``[(Jahr-Monat, Betrag in Minor Units)]`` aus rohen Abfragen – die
    Bankbewegungen der gewählten Konten und die eigenständigen Sätze, die das Konto einer gewählten Kasse auf einer Seite
    tragen oder deren ``Account_ID`` eine gewählte Kasse ist (dann nicht, wenn sie eine Kassen-Seite tragen oder von der AfA
    referenziert sind). Ein Spiegel zählt aus Sicht der Kasse: Abhebung „1600 an 1800“ +, Einzahlung −."""
    selected = [a for a in db.fetch_accounts() if not account_ids or a[0] in account_ids]
    selected_ids = {a[0] for a in selected}
    cash_ids = {a[0] for a in selected if a[6] == 1}
    numbers = sorted({a[7] for a in selected if a[6] == 1 and a[7]})
    cash_coa = {row[0] for row in query(
        db, f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN ({','.join('?' * len(numbers))})", numbers)}
    moves = [(date[:7], amount) for _id, date, amount, account_id in query(
        db, "SELECT ID, DateBooking, Amount, Account_ID FROM Bookings WHERE BookingType = 'bank' "
            "AND DateBooking BETWEEN ? AND ?", (D1, D2)) if account_id in selected_ids]
    depreciation = {row[0] for row in query(db, 'SELECT Booking_ID FROM AssetDepreciations')}
    for booking_id, date, amount, coa_id, counter_id, form, account_id in query(
            db, "SELECT ID, DateBooking, Amount, COA_ID, CounterCOA_ID, EntryForm, Account_ID FROM Bookings "
                "WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL AND DateBooking BETWEEN ? AND ?", (D1, D2)):
        on_cash_coa = coa_id in cash_coa or counter_id in cash_coa
        on_cash_account = account_id in cash_ids and not on_cash_coa and booking_id not in depreciation
        if not (on_cash_coa or on_cash_account):
            continue
        if form == FORM_MIRROR_COA and coa_id not in cash_coa and counter_id in cash_coa:
            amount = -amount
        elif form == FORM_MIRROR_COUNTER and counter_id not in cash_coa and coa_id in cash_coa:
            amount = -amount
        moves.append((date[:7], amount))
    return moves


def unlinked_movements(db, account_ids):
    """Bankbewegungen der gewählten Konten im Zeitraum ohne einen einzigen Satz."""
    selected = {a[0] for a in db.fetch_accounts() if not account_ids or a[0] in account_ids}
    parents = {row[0] for row in query(db, 'SELECT DISTINCT ParentBooking_ID FROM Bookings')}
    return sum(1 for bank_id, account_id in query(
        db, "SELECT ID, Account_ID FROM Bookings WHERE BookingType = 'bank' AND DateBooking BETWEEN ? AND ?", (D1, D2))
        if account_id in selected and bank_id not in parents)


POTS = ('income', 'private', 'expense', 'transfers')


def test_the_four_pots_add_up_to_the_balance_of_the_counted_movements(any_world):
    db = any_world

    for label, account_ids in world_filters(db).items():
        totals, monthly = db.get_dashboard(D1, D2, account_ids)
        moves = counted_movements(db, account_ids)
        balance = sum(amount for _month, amount in moves)
        minor = {pot: to_minor(totals[pot]) for pot in POTS}

        assert sum(minor.values()) == balance, label                          # die vier Töpfe sind der Saldo …
        assert to_minor(totals['balance']) == balance, label                  # … und der Saldo die Summe der Bewegungen
        assert totals['bank_count'] == len(moves), label
        assert totals['unlinked_count'] == unlinked_movements(db, account_ids), label
        assert totals['income'] >= 0 and totals['expense'] <= 0, label
        assert len(monthly) == 12, label
        for month in monthly:
            assert month['income'] >= 0 and month['expense'] <= 0, (label, month)
            wanted = sum(amount for key, amount in moves if key == month['year_month'])
            assert sum(to_minor(month[pot]) for pot in POTS) == wanted, (label, month['year_month'])
        for pot in POTS:                                                      # die Summen sind die Summen der Monate
            assert sum(to_minor(month[pot]) for month in monthly) == minor[pot], (label, pot)


def test_the_balance_is_the_one_from_before_the_change(migrated_world):
    """Der Saldo, die Zahl der Bewegungen und die der offenen ändern sich mit der Aufteilung nicht: Es sind die Werte, die
    2.0.8 über die migrierte Welt las (``entry_form_golden_2_0_8.json``); für die frische Welt deckt ihn die Summe der
    Bewegungen im Test davor."""
    cash_number, db = migrated_world
    accounts = db.fetch_accounts()
    banks = [a[0] for a in accounts if a[6] != 1]
    golden = GOLDEN[str(cash_number)]

    for section, account_ids in (('dashboard', None), ('dashboard_cash', [a[0] for a in accounts if a[6] == 1]),
                                 ('dashboard_bank', banks[:1])):
        totals = db.get_dashboard_totals(D1, D2, account_ids)
        wanted = golden[section]['totals']
        assert totals['balance'] == float(wanted['balance']), section
        assert totals['bank_count'] == int(wanted['bank_count']), section
        assert totals['unlinked_count'] == int(wanted['unlinked_count']), section


# Feste Sollwerte der migrierten Welt (Einnahme / Ausgabe / privat / Übertrag). Nachgerechnet für „Kasse 1600, alle Konten“
# aus den gezählten Bewegungen (Bank = Giro Alpha; „mirror_l“ = Spiegel des Bestands, sein Betrag wird nie gelesen):
#
#   Bankbewegung (Sätze)                                                 Einn.     Ausg.    privat  Übertr.
#   −59,50   6850 an 1800                                                          −59,50
#   −100,00  1600 an 1800 [mirror_l] (Abhebung über die Maske)                                        −100,00
#   −100,00  6815 an 1800 −70, 6850 an 1800 −30, 6805 an 2100 +7,50                −107,50    +7,50
#   +119,00 / +100,00 / +138,00  Bank an Debitoren + Wartekonto an Erlös         357,00 (zusammen)
#   +105,00  4300 an 1800                                                105,00
#   −70,00   1460 an 1800 [mirror_l] (kein eigenes Konto bei Kasse 1600)           −70,00
#   −33,00   6815 an 1800                                                          −33,00
#   +119,00  ohne Satz, nicht gedeckt                                     119,00
#   −100,00  ohne Satz, nicht gedeckt (der Spiegel des Bestands deckt nichts)     −100,00
#   −55,00   1460 an 1800 [mirror_l]                                               −55,00
#   −77,00   6850 an 1800                                                          −77,00
#   −40,00   6850 an 1800 −30, 6850 an 1050 −10                                    −40,00
#   Summe Bank                                                           581,00   −542,00     7,50   −100,00
#
#   Eigenständige Sätze auf der Kasse                                    Einn.     Ausg.    privat  Übertr.
#   −18,35 6815 an 1600; −21,40 6850 an 1600                                      −39,75
#   +50,00 und +60,00  1600 an 4400                                      110,00
#   Spiegel des Bestands, Rohbetrag: +100 und +40 (April), +90 und +45 (Mai), −25 und +25 (Juni)             +275,00
#   Summe Kasse                                                          110,00    −39,75     0,00    275,00
#
#   Summe alle Konten                                                    691,00   −581,75     7,50    175,00   Saldo 291,75
FIXED = {
    1600: {'alle': (691, -581.75, 7.5, 175), 'Kasse': (110, -39.75, 0, 275), 'Bank': (581, -542, 7.5, -100)},
    1460: {'alle': (581, -596, 7.5, 30), 'Kasse': (0, -9, 0, 85), 'Bank': (581, -587, 7.5, -55)},
    None: {'alle': (581, -642, 7.5, 0), 'Kasse': (0, 0, 0, 0), 'Bank': (581, -642, 7.5, 0)},
}


def test_the_pots_of_the_migrated_world_are_pinned(migrated_world):
    cash_number, db = migrated_world
    accounts = db.fetch_accounts()
    selection = {'alle': None, 'Kasse': [a[0] for a in accounts if a[6] == 1],
                 'Bank': [a[0] for a in accounts if a[6] != 1][:1]}

    for label, account_ids in selection.items():
        assert pots(db, account_ids) == FIXED[cash_number][label], (cash_number, label)


# ── die Seite ────────────────────────────────────────────────────────────────

def test_the_overview_shows_the_transfers_row_only_when_a_transfer_is_left(w):
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    mask(w, w.tages, 1800, '100.00', '2026-03-03')

    both = PageDashboard(w.db, D1, D2)
    giro_only = PageDashboard(w.db, D1, D2, [w.giro])

    assert 'dashTransfers' not in both and 'Überträge' not in both and 'bertr&auml;ge' not in both
    assert 'dashTransfers' in giro_only
    row = giro_only[giro_only.index('<tr class="dashTransfers">'):]
    row = row[:row.index('</tr>')]
    assert '&Uuml;bertr&auml;ge zwischen eigenen Konten' in row
    assert '-100,00 €' in row
    assert giro_only.index('dashTransfers') < giro_only.index('<strong>Saldo</strong>')
    assert 'Überträge: -100,00' in giro_only                       # im Tooltip des Monats


def test_the_values_of_the_transfers_row_are_escaped(w, monkeypatch):
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.giro, '25.00', '2026-03-03')                      # der Saldo (−75) unterscheidet sich vom Übertrag (−100)
    import server.pages_dashboard as page
    hostile = '<img src=x onerror=alert(1)>'
    real = page.fmt_eur
    monkeypatch.setattr(page, 'fmt_eur',
                        lambda value, *args, **kwargs: hostile if value == -100.0 else real(value, *args, **kwargs))

    html = PageDashboard(w.db, D1, D2, [w.giro])

    assert hostile not in html
    assert html.count('&lt;img src=x onerror=alert(1)&gt;') == 2    # in der Zeile und im Tooltip des Monats


def test_the_transfer_note_of_the_export_is_the_same_before_and_after_a_page_view(w):
    mask(w, w.giro, 1810, '-100.00', '2026-03-02')
    movement(w, w.tages, '100.00', '2026-03-03')
    movement(w, w.tages, '100.00', '2026-04-20')
    before = w.db.check_transfers(D1, D2)

    PageDashboard(w.db, D1, D2)
    PageDashboard(w.db, D1, D2, [w.tages])

    assert w.db.check_transfers(D1, D2) == before
    assert before.unbooked                                          # der Hinweis ist nicht leer, das Dashboard ändert ihn nicht
