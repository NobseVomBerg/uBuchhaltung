# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""DATEV-Export: Ein Übertrag zwischen zwei eigenen Konten steht nur einmal im Stapel – gezählt, nicht gepaart (2.3.4).

Wer beide Bankbewegungen eines Übertrags kontiert (Buchungsmaske oder Bank-Editor), bekommt unter jeder einen Spiegel:
„1810 S an 1800“ und „1800 H an 1810“ wären dieselbe Buchung zweimal. Einzelne Spiegel zu paaren war falsch (zwei gleich hohe
Überträge, je nur auf einer Seite kontiert, sahen wie ein Paar aus; ein Spiegel deckte beliebig viele Bewegungen). Seit der
Neufassung zählt ``db.matching.settle_transfers``: je Richtung und Betrag Gruppen auf einer Zeitachse, getrennt durch eine Lücke
von mehr als ``TRANSFER_DAY_WINDOW`` (7) Tagen in Abschnitte; die Eingangs-Sätze eines Abschnitts fallen nur weg, wenn jede
Ausgangsbewegung ihren Satz trägt, es mindestens so viele Ausgänge wie Eingänge gibt und im Umfeld des Abschnitts
(``TRANSFER_NEIGHBOUR_DAYS`` = 14 Tage davor und danach) kein Ausgang ohne Satz und kein eigenständiger Spiegel liegt. Was trotz
eines Ausgangs-Spiegels im Umfeld im Stapel bleibt, meldet der Export als „möglicherweise doppelt“. Auch die Deckung einer Bewegung
ohne Satz rechnet über das Umfeld: Steht dort der Satz eines anderen Übertrags, gilt sie nicht als dessen Gegenseite. Geprüft werden

- die reine Rechnung (``settle_transfers``, ``transfer_legs``) ohne Datenbank: die Zählregel als Tabelle, Abschnitte, Gruppen,
  eigenständige Spiegel, das Umfeld, die Meldung „möglicherweise doppelt“, Reihenfolge, 20 000 Überträge je Seite
- über die Datenbank und den echten Handler ``handle_datev_export``: die Standardfälle (über ``insert_booking``, die Buchungsmaske
  und den Bank-Editor), Kasse, 1460, gegenläufige Überträge, WISO-Spiegel, ``mirror_legacy``, Teilspiegel im Split, die Fälle, an denen eine
  Paarbildung einzelner Spiegel scheiterte (zwei Überträge, je nur auf einer Seite kontiert; Zahlungen neben einem Übertrag; mehrere Überträge hintereinander;
  Überträge über Zeitraumgrenzen), die Kontrollrechnung (Summe je Bankkonto im Stapel = Summe der Bankbewegungen), die Regression
  (ohne Überträge bleiben die Zeilen, wie sie waren) und die Leistung

Alle Werte sind erfundene Testdaten.
"""
import csv
import random
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from db.matching import (
    TRANSFER_DAY_WINDOW, TRANSFER_NEIGHBOUR_DAYS, MergedSection, StackEntry, TransferLeg, TransferMirror, TransferOther,
    _boundary_entries, own_transfer_accounts, settle_transfers, transfer_legs)
from export import datev
from server import handlers
from server.app import parse_form
from tests.datev_helpers import confirmed_export, datev_post, is_confirm_redirect, message_of

PERIOD = ('2026-03-01', '2026-03-31')
_SPALTE_SH, _SPALTE_KONTO, _SPALTE_GEGENKONTO = 1, 6, 7


# ═════════════════════════════════════════════════════════════════════════════
# A. Die reine Rechnung: settle_transfers – ohne Datenbank
# ═════════════════════════════════════════════════════════════════════════════
# Konten A, B, C (Accounts.ID 1, 2, 3); „d“ ist der März 2026 als Tag des Monats; Beträge in Minor Units (1 € = 10000).

A, B, C = 1, 2, 3
EUR = 10_000


def day(number):
    return date(2026, 3, 1) + timedelta(days=number - 1)


def out_m(bank_id, d, account=A, other=B, amount=100 * EUR, entry_day=None):
    """Ausgang mit Spiegel-Satz (die Satz-ID ist ``bank_id + 1000``)."""
    return TransferLeg(bank_id, account, True, amount, day(d), other, bank_id + 1000, day(entry_day or d))


def in_m(bank_id, d, account=B, other=A, amount=100 * EUR, entry_day=None):
    """Eingang mit Spiegel-Satz (die Satz-ID ist ``bank_id + 1000``)."""
    return TransferLeg(bank_id, account, False, amount, day(d), other, bank_id + 1000, day(entry_day or d))


def out_c(bank_id, d, account=A, amount=100 * EUR):
    """Ausgang ohne Satz."""
    return TransferLeg(bank_id, account, True, amount, day(d))


def in_c(bank_id, d, account=B, amount=100 * EUR):
    """Eingang ohne Satz."""
    return TransferLeg(bank_id, account, False, amount, day(d))


def solo(entry_id, d, out=A, into=B, amount=100 * EUR):
    """Eigenständiger Spiegel."""
    return TransferMirror(entry_id, day(d), out, into, amount)


def solo_unknown(entry_id, d, a=A, b=B, amount=100 * EUR):
    """Eigenständiger Spiegel ohne bekannte Lesart (``mirror_legacy``, keine Form): die Konten in beliebiger Reihenfolge."""
    return TransferMirror(entry_id, day(d), a, b, amount, False)


def other_out(bank_id, d, account=A, amount=100 * EUR):
    """Ausgang, der anders kontiert ist (Kinder, aber kein Spiegel zwischen eigenen Konten)."""
    return TransferOther(bank_id, account, True, amount, day(d))


def other_in(bank_id, d, account=B, amount=100 * EUR):
    """Eingang, der anders kontiert ist."""
    return TransferOther(bank_id, account, False, amount, day(d))


def section(a_m=0, b_m=0, a_c=0, b_c=0, s=0, d=1):
    """Ein Abschnitt aus den fünf Zahlen der Regel, alles am selben Tag, Konten A → B, ein Betrag. IDs: Ausgang mit Spiegel
    10…, Eingang mit Spiegel 20…, Ausgang ohne Satz 30…, Eingang ohne Satz 40…, eigenständiger Spiegel 50…; die Sätze der Beine
    tragen ``ID + 1000``."""
    legs = ([out_m(10 + i, d) for i in range(a_m)] + [in_m(20 + i, d) for i in range(b_m)]
            + [out_c(30 + i, d) for i in range(a_c)] + [in_c(40 + i, d) for i in range(b_c)])
    return legs, [solo(50 + i, d) for i in range(s)]


# (a_m, b_m, a_c, b_c, s) → fallen die Eingangs-Sätze weg? Regel: a_m ≥ 1, b_m ≥ 1, a_m ≥ b_m + b_c und im Umfeld (hier: der Abschnitt
# selbst) kein Ausgang ohne Satz (a_c == 0) und kein eigenständiger Spiegel (s == 0).
MERGE_TABLE = [
    pytest.param((1, 1, 0, 0, 0), True, id='ein-uebertrag-beidseitig'),
    pytest.param((2, 1, 0, 0, 0), True, id='mehr-ausgaenge-als-eingaenge'),
    pytest.param((2, 2, 0, 0, 0), True, id='gleich-viele'),
    pytest.param((3, 2, 0, 1, 0), True, id='ausgaenge-reichen-auch-fuer-satzlose-eingaenge'),
    pytest.param((2, 1, 0, 2, 0), False, id='ausgaenge-reichen-nicht-fuer-satzlose-eingaenge'),
    pytest.param((2, 2, 0, 1, 0), False, id='zwei-zu-drei'),
    pytest.param((1, 1, 1, 0, 0), False, id='satzloser-ausgang-sperrt'),
    pytest.param((3, 2, 1, 0, 0), False, id='ein-satzloser-ausgang-sperrt-auch-bei-vielen-ausgaengen'),
    pytest.param((1, 1, 0, 0, 1), False, id='eigenstaendiger-spiegel-sperrt'),
    pytest.param((1, 2, 0, 0, 0), False, id='mehr-eingaenge-als-ausgaenge'),
    pytest.param((0, 1, 0, 0, 0), False, id='kein-ausgang-mit-satz'),
    pytest.param((0, 2, 1, 0, 0), False, id='ausgang-nur-ohne-satz'),
    pytest.param((1, 0, 0, 0, 0), False, id='kein-eingang-mit-satz'),
]


@pytest.mark.parametrize('numbers, merges', MERGE_TABLE)
def test_the_incoming_entries_of_a_section_fall_away_exactly_when_the_rule_holds(numbers, merges):
    result = settle_transfers(*section(*numbers))
    b_m = numbers[1]
    assert set(result.omitted) == ({1020 + i for i in range(b_m)} if merges else set())


# (a_m, b_m, a_c, b_c, s) → (Ausgänge ohne Satz gedeckt, Eingänge ohne Satz gedeckt). Regel: a_c ≤ max(0, b_m − A) + s und
# b_c ≤ max(0, a_m − B) + s – reicht es nicht, ist KEINE ohne Satz dieser Seite gedeckt. A und B sind die Ausgänge und Eingänge mit
# Spiegel im Umfeld (hier: der Abschnitt selbst, also a_m und b_m); s zählt nur, wenn dort kein Bein mit Spiegel liegt.
COVER_TABLE = [
    pytest.param((0, 1, 1, 0, 0), (True, True), id='ein-eingang-deckt-einen-satzlosen-ausgang'),
    pytest.param((0, 1, 2, 0, 0), (False, True), id='ein-eingang-deckt-nicht-zwei-satzlose-ausgaenge-keiner-gedeckt'),
    pytest.param((0, 2, 2, 0, 0), (True, True), id='zwei-eingaenge-decken-zwei-satzlose-ausgaenge'),
    pytest.param((1, 0, 0, 1, 0), (True, True), id='ein-ausgang-deckt-einen-satzlosen-eingang'),
    pytest.param((1, 0, 0, 2, 0), (True, False), id='ein-ausgang-deckt-nicht-zwei-satzlose-eingaenge-keiner-gedeckt'),
    pytest.param((1, 1, 1, 1, 0), (False, False), id='beidseitig-kontiert-deckt-nichts'),
    pytest.param((2, 1, 1, 0, 0), (False, True), id='ueberschuss-an-ausgaengen-deckt-keinen-satzlosen-ausgang'),
    pytest.param((1, 2, 1, 0, 0), (True, True), id='ueberschuss-an-eingaengen-deckt-einen-satzlosen-ausgang'),
    pytest.param((2, 1, 0, 1, 0), (True, True), id='ueberschuss-an-ausgaengen-deckt-einen-satzlosen-eingang'),
    pytest.param((0, 0, 1, 1, 1), (True, True), id='ein-eigenstaendiger-spiegel-deckt-beide-seiten'),
    pytest.param((0, 0, 2, 1, 1), (False, True), id='ein-eigenstaendiger-spiegel-deckt-nicht-zwei-satzlose-ausgaenge'),
    pytest.param((0, 0, 2, 2, 2), (True, True), id='zwei-eigenstaendige-spiegel-decken-je-zwei'),
    pytest.param((1, 1, 1, 1, 1), (False, False), id='ein-eigenstaendiger-spiegel-neben-kontierten-beinen-deckt-nichts'),
    pytest.param((1, 0, 0, 2, 1), (True, False), id='der-spiegel-neben-einem-kontierten-ausgang-zaehlt-nicht-als-ueberschuss'),
    pytest.param((0, 1, 2, 0, 1), (False, True), id='der-spiegel-neben-einem-kontierten-eingang-zaehlt-nicht-als-ueberschuss'),
]


@pytest.mark.parametrize('numbers, covered', COVER_TABLE)
def test_a_movement_without_entry_is_covered_exactly_when_the_surplus_of_the_other_side_reaches(numbers, covered):
    result = settle_transfers(*section(*numbers))
    a_c, b_c = numbers[2], numbers[3]
    out_free, in_free = {30 + i for i in range(a_c)}, {40 + i for i in range(b_c)}
    expected = (out_free if covered[0] else set()) | (in_free if covered[1] else set())
    assert set(result.covered) == expected


# (a_m, b_m, a_c, b_c, s) → die IDs der Sätze, die als „möglicherweise doppelt“ gemeldet werden (Eingangs-Sätze 1020…, eigenständige
# Spiegel 50…). Regel: Ein Eingangs-Satz, der bleibt, obwohl im Umfeld ein Ausgang mit Spiegel liegt; ein eigenständiger Spiegel mit einem
# Bein mit Spiegel (Ausgang oder Eingang) im Umfeld.
DOUBTFUL_TABLE = [
    pytest.param((1, 1, 0, 0, 0), set(), id='zusammengelegt:-nichts-zu-melden'),
    pytest.param((2, 2, 0, 0, 0), set(), id='zwei-zusammengelegte-Uebertraege'),
    pytest.param((1, 2, 0, 0, 0), {1020, 1021}, id='mehr-Eingaenge-als-Ausgaenge:-beide-Eingaenge'),
    pytest.param((1, 1, 1, 0, 0), {1020}, id='satzloser-Ausgang-sperrt:-der-Eingang-bleibt-und-wird-gemeldet'),
    pytest.param((2, 1, 0, 2, 0), {1020}, id='Ausgaenge-reichen-nicht-fuer-satzlose-Eingaenge'),
    pytest.param((1, 1, 0, 0, 1), {1020, 50}, id='eigenstaendiger-Spiegel-sperrt:-Eingang-und-Spiegel-werden-gemeldet'),
    pytest.param((0, 1, 0, 0, 0), set(), id='nur-ein-Eingang:-der-Uebertrag-steht-einmal-da'),
    pytest.param((0, 2, 2, 0, 0), set(), id='Eingaenge-ohne-Ausgang-mit-Satz'),
    pytest.param((1, 0, 0, 0, 0), set(), id='nur-ein-Ausgang'),
    pytest.param((0, 0, 1, 1, 1), set(), id='ein-Spiegel-allein-mit-den-Bewegungen-ohne-Satz'),
    pytest.param((0, 1, 0, 0, 1), {50}, id='Spiegel-neben-einem-kontierten-Eingang'),
    pytest.param((1, 0, 0, 0, 1), {50}, id='Spiegel-neben-einem-kontierten-Ausgang'),
    pytest.param((0, 0, 0, 0, 2), set(), id='zwei-Spiegel-ohne-Bein-mit-Satz'),
]


@pytest.mark.parametrize('numbers, doubtful', DOUBTFUL_TABLE)
def test_an_entry_is_named_as_possibly_doubled_exactly_when_the_rule_says(numbers, doubtful):
    result = settle_transfers(*section(*numbers))
    assert set(result.doubtful) == doubtful
    assert all(entry.entry_id in result.doubtful for entry in result.doubtful.values())
    assert not set(result.doubtful) & set(result.omitted)                          # was wegfällt, steht nicht doppelt im Stapel


def test_the_coverage_counts_the_entries_of_the_neighbourhood_not_only_those_of_the_section():
    """Ein ganz unkontierter Übertrag (Ausgang am 9., Eingang am 17.) zwischen zwei kontierten (1. → 9. und 21. → 29.): Im Abschnitt
    „9.“ steht nur der Eingangs-Satz des ersten Übertrags – er deckt den Ausgang ohne Satz nur, wenn man den Ausgangs-Satz vom 1. im
    Umfeld übersieht. Die Deckung zählt ihn mit: Beide Bewegungen ohne Satz sind ungedeckt."""
    legs = [out_m(1, 1), in_m(2, 9), out_c(3, 9), in_c(4, 17), out_m(5, 21), in_m(6, 29)]
    result = settle_transfers(legs)
    assert result.covered == frozenset() and result.omitted == {}
    assert settle_transfers([in_m(2, 9), out_c(3, 9)]).covered == frozenset({3})      # ohne Ausgangs-Satz im Umfeld deckt der Eingangs-Satz
    assert settle_transfers([out_m(1, 9 - TRANSFER_NEIGHBOUR_DAYS - 1), in_m(2, 9), out_c(3, 9)]).covered == frozenset({3})


@pytest.mark.parametrize('distance', [0, TRANSFER_DAY_WINDOW + 1, TRANSFER_NEIGHBOUR_DAYS])
def test_an_outgoing_entry_within_the_neighbourhood_takes_the_incoming_entry_away_from_a_free_outgoing_movement(distance):
    legs = [in_m(1, 20), out_c(2, 20), out_m(3, 20 + distance)]
    assert settle_transfers(legs).covered == frozenset()


def test_an_outgoing_entry_beyond_the_neighbourhood_does_not(distance=TRANSFER_NEIGHBOUR_DAYS + 1):
    legs = [in_m(1, 20), out_c(2, 20), out_m(3, 20 + distance)]
    assert settle_transfers(legs).covered == frozenset({2})


@pytest.mark.parametrize('distance', [0, TRANSFER_DAY_WINDOW + 1, TRANSFER_NEIGHBOUR_DAYS])
def test_an_incoming_entry_within_the_neighbourhood_takes_the_outgoing_entry_away_from_a_free_incoming_movement(distance):
    legs = [out_m(1, 20), in_c(2, 20), in_m(3, 20 - distance)]
    assert settle_transfers(legs).covered == frozenset()


def test_a_standalone_mirror_with_a_booked_leg_next_to_it_covers_nothing_but_alone_it_covers_one():
    near = settle_transfers([in_m(1, 1), out_c(2, 2), out_c(3, 3)], [solo(50, 4)])         # Eingangs-Satz deckt einen, der Spiegel keinen
    assert near.covered == frozenset() and set(near.doubtful) == {50}
    assert settle_transfers([out_c(2, 2)], [solo(50, 4)]).covered == frozenset({2})        # ohne Bein im Umfeld deckt er einen


@pytest.mark.parametrize('distance, covered', [(TRANSFER_NEIGHBOUR_DAYS, False), (TRANSFER_NEIGHBOUR_DAYS + 1, True)],
                         ids=['Bein-14-Tage-danach:-im-Umfeld', 'Bein-15-Tage-danach:-jenseits'])
def test_the_booked_leg_must_lie_within_the_neighbourhood_of_the_section_of_the_mirror(distance, covered):
    result = settle_transfers([out_c(2, 2), in_m(1, 4 + distance)], [solo(50, 4)])         # der Spiegel am 4., sein Abschnitt reicht von 2 bis 4
    assert (2 in result.covered) == covered and (50 in result.doubtful) == (not covered)


def test_a_group_needs_a_leg_with_entry_or_a_standalone_mirror_to_exist():
    """Allein aus Bewegungen ohne Satz entsteht keine Gruppe: Sie decken einander nie."""
    result = settle_transfers([out_c(1, 1), in_c(2, 1), out_c(3, 2), in_c(4, 2)])
    assert result.omitted == {} and result.covered == frozenset() and result.doubtful == {}


def test_without_anything_the_result_is_empty():
    result = settle_transfers([], [])
    assert result.omitted == {} and result.covered == frozenset() and result.doubtful == {}


# ── Abschnitte ───────────────────────────────────────────────────────────────

def test_a_gap_of_exactly_the_window_stays_in_one_section_and_one_day_more_separates():
    assert TRANSFER_DAY_WINDOW == 7 and TRANSFER_NEIGHBOUR_DAYS == 14
    together = settle_transfers([out_m(1, 1), in_m(2, 1 + TRANSFER_DAY_WINDOW)])
    apart = settle_transfers([out_m(1, 1), in_m(2, 1 + TRANSFER_DAY_WINDOW + 1)])
    assert set(together.omitted) == {1002} and together.doubtful == {}
    assert apart.omitted == {} and set(apart.doubtful) == {1002}                    # zwei Abschnitte: nichts fällt weg, der Eingang wird gemeldet


def test_elements_a_few_days_apart_chain_into_one_section_however_long_the_chain_is():
    legs = [out_m(1, 1), out_m(2, 7), in_m(3, 13), in_m(4, 19)]              # Lücken 6, 6, 6; erstes und letztes 18 Tage auseinander
    assert set(settle_transfers(legs).omitted) == {1003, 1004}


def test_the_window_is_a_parameter():
    legs = [out_m(1, 1), in_m(2, 4)]
    assert set(settle_transfers(legs, window=3).omitted) == {1002}
    assert settle_transfers(legs, window=2).omitted == {}


def test_a_movement_without_entry_in_another_section_beyond_the_neighbourhood_does_not_block_the_merge():
    legs = [out_m(1, 1), in_m(2, 2), out_c(3, 2 + TRANSFER_NEIGHBOUR_DAYS + 1)]
    result = settle_transfers(legs)
    assert set(result.omitted) == {1002} and 3 not in result.covered


def test_every_section_is_settled_on_its_own():
    """Zwei Abschnitte derselben Gruppe, weit auseinander: im ersten legen die Sätze zusammen, im zweiten nicht (nur ein Eingang)."""
    legs = [out_m(1, 1), in_m(2, 2), in_m(3, 2 + TRANSFER_NEIGHBOUR_DAYS + 20)]
    result = settle_transfers(legs)
    assert set(result.omitted) == {1002} and result.doubtful == {}


# ── Das Umfeld ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('side', ['after', 'before'])
@pytest.mark.parametrize('distance', [0, 1, TRANSFER_DAY_WINDOW, TRANSFER_DAY_WINDOW + 1, TRANSFER_NEIGHBOUR_DAYS],
                         ids=lambda distance: f'{distance}-Tage')
def test_a_movement_without_entry_within_the_neighbourhood_blocks_the_merge(side, distance):
    """Ausgang mit Satz am 20., Eingang mit Satz am 21.; ein Ausgang ohne Satz bis zu 14 Tage nach dem letzten oder vor dem ersten
    Element des Abschnitts – im Abschnitt selbst oder im nächsten – sperrt das Zusammenlegen: Der Eingangs-Satz könnte der einzige
    seines Übertrags sein."""
    free_day = 21 + distance if side == 'after' else 20 - distance
    result = settle_transfers([out_m(1, 20), in_m(2, 21), out_c(3, free_day)])
    assert result.omitted == {} and set(result.doubtful) == {1002}


@pytest.mark.parametrize('side', ['after', 'before'])
def test_a_movement_without_entry_one_day_beyond_the_neighbourhood_does_not_block(side):
    free_day = 21 + TRANSFER_NEIGHBOUR_DAYS + 1 if side == 'after' else 20 - TRANSFER_NEIGHBOUR_DAYS - 1
    result = settle_transfers([out_m(1, 20), in_m(2, 21), out_c(3, free_day)])
    assert set(result.omitted) == {1002} and result.doubtful == {}


def test_the_neighbourhood_reaches_from_the_first_and_the_last_element_of_the_whole_section():
    """Ein Abschnitt von Tag 1 bis 13 (Lücken 5, 6, 1): Der Rand ist sein letztes Element, nicht eins in der Mitte."""
    legs = [out_m(1, 1), out_m(2, 6), in_m(3, 12), in_m(4, 13)]
    assert settle_transfers(legs + [out_c(5, 13 + TRANSFER_NEIGHBOUR_DAYS)]).omitted == {}
    assert set(settle_transfers(legs + [out_c(5, 13 + TRANSFER_NEIGHBOUR_DAYS + 1)]).omitted) == {1003, 1004}
    assert settle_transfers(legs + [out_c(5, 1 - TRANSFER_NEIGHBOUR_DAYS)]).omitted == {}
    assert set(settle_transfers(legs + [out_c(5, 1 - TRANSFER_NEIGHBOUR_DAYS - 1)]).omitted) == {1003, 1004}


def test_a_standalone_mirror_within_the_neighbourhood_blocks_the_merge_and_beyond_it_does_not():
    assert settle_transfers([out_m(1, 1), in_m(2, 1)], [solo(50, 1 + TRANSFER_NEIGHBOUR_DAYS)]).omitted == {}
    assert set(settle_transfers([out_m(1, 1), in_m(2, 1)], [solo(50, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)]).omitted) == {1002}


def test_the_neighbourhood_is_a_parameter():
    legs = [out_m(1, 1), in_m(2, 1), out_c(3, 12)]                                  # der Ausgang ohne Satz: 11 Tage danach, anderer Abschnitt
    assert settle_transfers(legs, neighbour=11).omitted == {}
    assert set(settle_transfers(legs, neighbour=10).omitted) == {1002}


def test_a_free_outgoing_movement_before_the_only_entry_of_its_transfer_keeps_that_entry():
    """Übertrag 1: Ausgang am 1. ohne Satz, Eingang am 10. mit Satz (neun Tage, mehr als das Fenster); Übertrag 2: Ausgang am 10. mit Satz,
    Eingang am 19. ohne Satz. Ohne Umfeld legte der Abschnitt „10.“ den einzigen Satz von Übertrag 1 mit dem Ausgang von Übertrag 2
    zusammen – Übertrag 1 stünde nirgends im Stapel. Mit dem Umfeld sperrt der Ausgang vom 1."""
    result = settle_transfers([out_c(1, 1), in_m(2, 10), out_m(3, 10), in_c(4, 19)])
    assert result.omitted == {} and set(result.doubtful) == {1002}
    same_section = settle_transfers([out_c(1, 1), in_m(2, 7), out_m(3, 7), in_c(4, 13)])           # sechs Tage: ein Abschnitt, der Ausgang sperrt ihn
    assert same_section.omitted == {} and set(same_section.doubtful) == {1002}


def test_an_incoming_entry_is_merged_inside_the_window_named_inside_the_neighbourhood_and_left_alone_beyond_it():
    def after(distance):
        return settle_transfers([out_m(1, 1), in_m(2, 1 + distance)])
    for distance in range(0, TRANSFER_DAY_WINDOW + 1):
        assert set(after(distance).omitted) == {1002} and after(distance).doubtful == {}
    for distance in range(TRANSFER_DAY_WINDOW + 1, TRANSFER_NEIGHBOUR_DAYS + 1):
        assert after(distance).omitted == {} and set(after(distance).doubtful) == {1002}
    for distance in (TRANSFER_NEIGHBOUR_DAYS + 1, 40):
        assert after(distance).omitted == {} and after(distance).doubtful == {}       # die bekannte Grenze: stille Doppelung


def test_one_free_outgoing_movement_in_a_long_chain_blocks_the_chain_but_names_every_incoming_entry():
    """Zehn gleich hohe Überträge an zehn Tagen, einer der Ausgänge ohne Satz: Nichts fällt weg (auch nicht die neun, die es dürften –
    welcher zu welchem gehört, bleibt offen), aber jeder Eingangs-Satz wird als möglicherweise doppelt gemeldet."""
    legs = []
    for number in range(10):
        legs.append(out_c(1 + number, 1 + number) if number == 4 else out_m(1 + number, 1 + number))
        legs.append(in_m(21 + number, 1 + number))
    result = settle_transfers(legs)
    assert result.omitted == {} and set(result.doubtful) == {1021 + number for number in range(10)}
    assert 5 in result.covered                                                   # der Ausgang ohne Satz ist durch einen Eingangs-Satz gedeckt


def test_a_standalone_mirror_is_named_as_possibly_doubled_next_to_a_booked_leg_within_the_neighbourhood_only():
    near = settle_transfers([in_m(1, 1)], [solo(50, 1 + TRANSFER_NEIGHBOUR_DAYS)])
    far = settle_transfers([in_m(1, 1)], [solo(50, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)])
    assert set(near.doubtful) == {50} and far.doubtful == {}
    assert set(settle_transfers([out_m(1, 1)], [solo(50, 1 - TRANSFER_NEIGHBOUR_DAYS)]).doubtful) == {50}


# ── Gruppen: Richtung, Konten und Betrag ─────────────────────────────────────

def test_transfers_in_the_opposite_direction_are_separate_groups():
    legs = [out_m(1, 1, A, B), in_m(2, 1, B, A),           # A → B
            out_m(3, 1, B, A), in_m(4, 1, A, B)]            # B → A, derselbe Betrag am selben Tag
    assert set(settle_transfers(legs).omitted) == {1002, 1004}


def test_another_amount_or_another_pair_of_accounts_is_another_group():
    assert settle_transfers([out_m(1, 1), in_m(2, 1, amount=100 * EUR + 1)]).omitted == {}
    assert settle_transfers([out_m(1, 1, A, B), in_m(2, 1, C, A)]).omitted == {}
    assert settle_transfers([out_m(1, 1, A, B), in_m(2, 1, B, C)]).omitted == {}


def test_an_incoming_movement_of_the_outgoing_account_never_belongs_to_the_group_of_its_outgoing_leg():
    """Ein Ausgang A → B und ein Eingang auf A (von B) sind verschiedene Richtungen: nichts fällt weg, nichts ist gedeckt."""
    result = settle_transfers([out_m(1, 1, A, B), in_c(2, 1, A)])
    assert result.omitted == {} and result.covered == frozenset()


def test_a_movement_without_entry_belongs_to_every_group_of_its_account_and_amount_and_is_covered_in_any_of_them():
    legs = [out_m(1, 1, A, B), in_m(2, 1, B, A),                   # Gruppe A → B: beidseitig kontiert, deckt nichts
            in_m(3, 1, C, A),                                      # Gruppe A → C: nur der Eingang kontiert
            out_c(4, 1, A)]                                        # ein Ausgang auf A ohne Satz: gehört zu beiden Gruppen
    result = settle_transfers(legs)
    assert 4 in result.covered                                     # in A → C gedeckt …
    assert result.omitted == {}                                    # … und in A → B sperrt er das Zusammenlegen (er könnte dazugehören)


def test_the_same_incoming_entry_is_never_left_out_by_two_groups():
    legs = [out_m(1, 1, A, B), in_m(2, 1, B, A)]
    assert list(settle_transfers(legs).omitted) == [1002]


# ── Eigenständige Spiegel ────────────────────────────────────────────────────

def test_a_standalone_mirror_blocks_the_merge_of_its_section_and_of_the_next_within_the_neighbourhood():
    assert settle_transfers([out_m(1, 1), in_m(2, 1)], [solo(50, 1)]).omitted == {}
    assert settle_transfers([out_m(1, 1), in_m(2, 1)], [solo(50, 1 + TRANSFER_DAY_WINDOW + 1)]).omitted == {}


def test_a_standalone_mirror_of_another_direction_or_amount_is_no_member_of_the_group():
    legs = [out_m(1, 1), in_m(2, 1)]
    assert set(settle_transfers(legs, [solo(50, 1, out=B, into=A)]).omitted) == {1002}
    assert set(settle_transfers(legs, [solo(50, 1, amount=99 * EUR)]).omitted) == {1002}


# ── Anders kontierte Bewegungen: das dritte Element ──────────────────────────────────────────────────────────────────

def test_a_movement_booked_otherwise_alone_makes_no_group_and_changes_nothing():
    result = settle_transfers([], [], [other_out(1, 1), other_in(2, 1)])
    assert result.omitted == {} and result.covered == frozenset() and result.doubtful == {} and result.merged == []
    free = settle_transfers([out_c(1, 1), in_c(2, 1)], [], [other_out(3, 1), other_in(4, 1)])
    assert free.covered == frozenset() and free.doubtful == {} and free.omitted == {}


def test_a_movement_booked_otherwise_never_changes_what_merges():
    """Zusammengelegt wird allein nach Ausgängen und Eingängen mit Satz und den sperrenden Bewegungen: ein Zahlungssatz unter einem
    Ausgang gleicher Höhe ändert daran nichts (er kann ein ganz anderer Vorgang sein)."""
    assert set(settle_transfers([out_m(1, 1), in_m(2, 1)], [], [other_out(3, 1)]).omitted) == {1002}
    assert set(settle_transfers([out_m(1, 1), in_m(2, 1)], [], [other_in(3, 1)]).omitted) == {1002}
    assert set(settle_transfers([out_m(1, 1), in_m(2, 1)], [], [other_out(3, 1), other_in(4, 1)]).omitted) == {1002}


# (Legs, andere Bewegungen) → die IDs der Sätze, die als „möglicherweise doppelt“ gemeldet werden.
OTHERS_DOUBTFUL_TABLE = [
    pytest.param([in_m(1, 1)], [other_out(2, 1)], {1001}, id='Eingangs-Satz-neben-einem-anders-kontierten-Ausgang'),
    pytest.param([in_m(1, 1)], [other_out(2, 1 + TRANSFER_NEIGHBOUR_DAYS)], {1001}, id='Ausgang-14-Tage-danach:-im-Umfeld'),
    pytest.param([in_m(1, 1 + TRANSFER_NEIGHBOUR_DAYS)], [other_out(2, 1)], {1001}, id='Ausgang-14-Tage-davor:-im-Umfeld'),
    pytest.param([in_m(1, 1)], [other_out(2, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)], set(), id='Ausgang-15-Tage-danach:-jenseits'),
    pytest.param([in_m(1, 1)], [other_out(2, 1, account=C)], set(), id='anders-kontierter-Ausgang-auf-einem-anderen-Konto'),
    pytest.param([in_m(1, 1)], [other_out(2, 1, amount=99 * EUR)], set(), id='anders-kontierter-Ausgang-mit-anderem-Betrag'),
    pytest.param([in_m(1, 1)], [other_in(2, 1)], set(), id='ein-anders-kontierter-Eingang-betrifft-keinen-Eingangs-Satz'),
    pytest.param([in_m(1, 1)], [other_out(2, 1, account=B)], set(), id='ein-Ausgang-auf-dem-Eingangskonto-ist-eine-andere-Richtung'),
    pytest.param([out_m(1, 1)], [other_in(2, 1)], {1001}, id='Ausgangs-Satz-neben-einem-anders-kontierten-Eingang'),
    pytest.param([out_m(1, 1)], [other_in(2, 1 + TRANSFER_NEIGHBOUR_DAYS)], {1001}, id='Eingang-14-Tage-danach:-im-Umfeld'),
    pytest.param([out_m(1, 1)], [other_in(2, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)], set(), id='Eingang-15-Tage-danach:-jenseits'),
    pytest.param([out_m(1, 1)], [other_out(2, 1)], set(), id='ein-anders-kontierter-Ausgang-betrifft-keinen-Ausgangs-Satz'),
    pytest.param([out_m(1, 1)], [other_in(2, 1, account=C)], set(), id='anders-kontierter-Eingang-auf-einem-anderen-Konto'),
    pytest.param([out_m(1, 1), in_m(2, 1)], [other_out(3, 1)], set(), id='zusammengelegt:-der-Eingangs-Satz-fällt-weg-und-wird-nicht-genannt'),
    pytest.param([out_m(1, 1), in_m(2, 1)], [other_in(3, 1)], {1001}, id='zusammengelegt:-der-Ausgangs-Satz-neben-dem-anders-kontierten-Eingang'),
    pytest.param([out_m(1, 1), in_m(2, 1)], [other_out(3, 1), other_in(4, 1)], {1001}, id='zusammengelegt:-beide-Seiten-anders-kontiert-nebenan'),
    pytest.param([in_m(1, 1), in_m(2, 1)], [other_out(3, 1)], {1001, 1002}, id='mehrere-Eingangs-Sätze-neben-einem-anders-kontierten-Ausgang'),
    pytest.param([in_m(1, 1)], [other_out(2, 1), other_out(3, 3)], {1001}, id='zwei-anders-kontierte-Ausgänge-nennen-den-Satz-einmal'),
]


@pytest.mark.parametrize('legs, others, doubtful', OTHERS_DOUBTFUL_TABLE)
def test_an_entry_next_to_a_movement_booked_otherwise_is_named_as_possibly_doubled(legs, others, doubtful):
    result = settle_transfers(legs, [], others)
    assert set(result.doubtful) == doubtful
    assert not set(result.doubtful) & set(result.omitted)


# (Legs, andere Bewegungen) → (IDs der gedeckten Bewegungen ohne Satz): Eine anders kontierte Bewegung ist verbraucht, wie ein Ausgang
# oder Eingang mit Satz im Umfeld – sie schmälert den Überschuss der Gegenseite.
OTHERS_COVER_TABLE = [
    pytest.param([in_m(1, 1), out_c(2, 1)], [], {2}, id='ohne:-der-Eingangs-Satz-deckt-den-Ausgang-ohne-Satz'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_out(3, 1)], set(), id='ein-anders-kontierter-Ausgang-verbraucht-den-Eingangs-Satz'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_out(3, 1 + TRANSFER_NEIGHBOUR_DAYS)], set(), id='anders-kontierter-Ausgang-14-Tage-danach'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_out(3, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)], {2}, id='anders-kontierter-Ausgang-15-Tage-danach'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_out(3, 1, account=C)], {2}, id='anders-kontierter-Ausgang-auf-anderem-Konto'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_out(3, 1, amount=99 * EUR)], {2}, id='anders-kontierter-Ausgang-mit-anderem-Betrag'),
    pytest.param([in_m(1, 1), out_c(2, 1)], [other_in(3, 1)], {2}, id='ein-anders-kontierter-Eingang-verbraucht-keinen-Ausgang'),
    pytest.param([in_m(1, 1), in_m(2, 1), out_c(3, 1)], [other_out(4, 1)], {3}, id='zwei-Eingangs-Sätze-reichen-für-einen-anders-kontierten-und-einen-freien-Ausgang'),
    pytest.param([in_m(1, 1), in_m(2, 1), out_c(3, 1), out_c(4, 1)], [other_out(5, 1)], set(), id='zwei-Eingangs-Sätze-reichen-nicht-für-drei-Ausgänge'),
    pytest.param([out_m(1, 1), in_c(2, 1)], [], {2}, id='Spiegel:-der-Ausgangs-Satz-deckt-den-Eingang-ohne-Satz'),
    pytest.param([out_m(1, 1), in_c(2, 1)], [other_in(3, 1)], set(), id='ein-anders-kontierter-Eingang-verbraucht-den-Ausgangs-Satz'),
    pytest.param([out_m(1, 1), in_c(2, 1)], [other_out(3, 1)], {2}, id='ein-anders-kontierter-Ausgang-verbraucht-keinen-Eingang'),
    pytest.param([out_m(1, 1), out_m(2, 1), in_c(3, 1)], [other_in(4, 1)], {3}, id='zwei-Ausgangs-Sätze-reichen-für-einen-anders-kontierten-und-einen-freien-Eingang'),
]


@pytest.mark.parametrize('legs, others, covered', OTHERS_COVER_TABLE)
def test_a_movement_booked_otherwise_is_used_up_in_the_coverage(legs, others, covered):
    assert set(settle_transfers(legs, [], others).covered) == covered


def test_a_movement_booked_otherwise_does_not_touch_the_surplus_of_a_standalone_mirror():
    """Der Spiegel deckt je Seite eine Bewegung ohne Satz; ein Zahlungssatz gleicher Höhe nebenan ändert daran nichts."""
    assert settle_transfers([out_c(1, 1)], [solo(50, 1)], [other_out(2, 1)]).covered == frozenset({1})
    assert settle_transfers([in_c(1, 1)], [solo(50, 1)], [other_in(2, 1)]).covered == frozenset({1})


def test_the_other_movements_are_counted_in_the_neighbourhood_of_the_whole_section():
    """Wie bei den Ausgängen mit Satz: Der Rand ist das erste und das letzte Element des ganzen Abschnitts."""
    legs = [in_m(1, 1), in_m(2, 6), out_c(3, 12), out_c(4, 12)]                      # ein Abschnitt von Tag 1 bis 12: zwei Eingänge, zwei Ausgänge
    assert settle_transfers(legs).covered == frozenset({3, 4})                       # ohne die anders kontierte Zahlung reichen die zwei Sätze
    assert settle_transfers(legs, [], [other_out(5, 12 + TRANSFER_NEIGHBOUR_DAYS)]).covered == frozenset()
    assert settle_transfers(legs, [], [other_out(5, 12 + TRANSFER_NEIGHBOUR_DAYS + 1)]).covered == frozenset({3, 4})
    assert settle_transfers(legs, [], [other_out(5, 1 - TRANSFER_NEIGHBOUR_DAYS)]).covered == frozenset()
    assert settle_transfers(legs, [], [other_out(5, 1 - TRANSFER_NEIGHBOUR_DAYS - 1)]).covered == frozenset({3, 4})


def test_the_result_with_other_movements_does_not_depend_on_the_order_and_the_input_is_not_changed():
    legs = [out_m(1, 1), in_m(2, 9), in_m(3, 20), out_c(4, 24), in_c(5, 25)]
    others = [other_out(10, 3), other_in(11, 5), other_out(12, 22)]
    expected = settle_transfers(legs, [solo(50, 30)], others)
    shuffler = random.Random(11)
    for _ in range(20):
        shuffled_legs, shuffled_others = legs[:], others[:]
        shuffler.shuffle(shuffled_legs)
        shuffler.shuffle(shuffled_others)
        assert settle_transfers(shuffled_legs, [solo(50, 30)], shuffled_others) == expected
    before = (list(legs), list(others))
    settle_transfers(legs, [], others)
    assert (legs, others) == before


# ── Der Auslöser: die Bewegung, die das Zusammenlegen sperrt ─────────────────────────────────────────────────────────

def test_the_trigger_is_the_oldest_movement_that_blocks_the_merge_or_starts_the_hint():
    """Ein Ausgang ohne Satz am 5., ein eigenständiger Spiegel am 3.: Der ältere ist der Auslöser. Eine anders kontierte Zahlung am 2.
    ist noch älter."""
    legs = [out_m(1, 1), in_m(2, 1), out_c(3, 5)]
    assert settle_transfers(legs, [solo(50, 3)]).doubtful[1002].trigger == day(3)
    assert settle_transfers(legs).doubtful[1002].trigger == day(5)
    assert settle_transfers(legs, [solo(50, 3)], [other_out(4, 2)]).doubtful[1002].trigger == day(2)
    assert settle_transfers(legs, [solo(50, 3)], [other_out(4, 4)]).doubtful[1002].trigger == day(3)


def test_the_trigger_of_an_entry_named_only_because_of_a_booked_outgoing_leg_is_none():
    """Ein Eingang neun Tage nach dem Ausgang, beide mit Satz: Der Eingangs-Satz bleibt, aber keine Bewegung sperrt – es gibt keinen
    Auslöser, den der Hinweis nennen könnte."""
    result = settle_transfers([out_m(1, 1), in_m(2, 10)])
    assert set(result.doubtful) == {1002} and result.doubtful[1002].trigger is None


def test_the_trigger_of_an_outgoing_entry_is_the_first_other_incoming_movement():
    result = settle_transfers([out_m(1, 1)], [], [other_in(2, 9), other_in(3, 4)])
    assert result.doubtful[1001].trigger == day(4)


def test_a_standalone_mirror_named_as_possibly_doubled_has_no_trigger():
    result = settle_transfers([in_m(1, 1)], [solo(50, 2)])
    assert set(result.doubtful) == {50} and result.doubtful[50].trigger is None


def test_the_trigger_only_counts_movements_within_the_neighbourhood():
    legs = [out_m(1, 1), in_m(2, 10)]                                                # Eingangs-Satz bleibt (zwei Abschnitte)
    far = settle_transfers(legs + [out_c(3, 10 + TRANSFER_NEIGHBOUR_DAYS + 1)])
    near = settle_transfers(legs + [out_c(3, 10 + TRANSFER_NEIGHBOUR_DAYS)])
    assert far.doubtful[1002].trigger is None and near.doubtful[1002].trigger == day(10 + TRANSFER_NEIGHBOUR_DAYS)


# ── Mit der Datenbank oder ohne Bank: eigenständige Spiegel ohne bekannte Lesart ─────────────────────────────────────

def test_a_standalone_mirror_of_unknown_reading_is_named_next_to_a_booked_leg_of_the_same_pair_and_amount():
    """Er legt nichts zusammen und deckt nichts; neben einer kontierten Bankbewegung gleichen Betrags zwischen denselben Konten (in
    beliebiger Richtung) im Umfeld wird er genannt – ohne Auslöser."""
    for leg in (out_m(1, 1), in_m(1, 1)):
        result = settle_transfers([leg], [solo_unknown(50, 1 + TRANSFER_NEIGHBOUR_DAYS)])
        assert set(result.doubtful) == {50} and result.omitted == {} and result.doubtful[50].trigger is None
        assert settle_transfers([leg], [solo_unknown(50, 1 + TRANSFER_NEIGHBOUR_DAYS + 1)]).doubtful == {}
    assert set(settle_transfers([in_m(1, 1)], [solo_unknown(50, 1, a=B, b=A)]).doubtful) == {50}      # Konten in anderer Reihenfolge
    assert settle_transfers([in_m(1, 1)], [solo_unknown(50, 1, amount=99 * EUR)]).doubtful == {}
    assert settle_transfers([in_m(1, 1)], [solo_unknown(50, 1, a=A, b=C)]).doubtful == {}
    assert settle_transfers([], [solo_unknown(50, 1)]).doubtful == {}                                 # ohne kontierte Bankbewegung: nichts


def test_a_standalone_mirror_of_unknown_reading_covers_and_blocks_nothing():
    legs = [out_m(1, 1), in_m(2, 1), out_c(3, 30), in_c(4, 30)]
    result = settle_transfers(legs, [solo_unknown(50, 1), solo_unknown(51, 30)])
    assert set(result.omitted) == {1002} and result.covered == frozenset()


# ── Hinweis 2 je Abschnitt gezählt ───────────────────────────────────────────────────────────────────────────────────

def stack_entries(base, *days):
    return tuple(StackEntry(base + number, day(d), 100 * EUR) for number, d in enumerate(days))


MARCH_FIRST, MARCH_LAST = day(1), day(31)


def boundary(sections, first=MARCH_FIRST, last=MARCH_LAST):
    """``_boundary_entries`` mit den Tagen der Ergebnisse statt der Einträge: ``(Tage der weggelassenen, Tage der Ausgangs-Sätze)``."""
    incoming, outgoing = _boundary_entries(sections, first, last)
    return [entry.day for entry in incoming], [entry.day for entry in outgoing]


def test_a_section_inside_the_period_has_nothing_to_say():
    assert boundary([MergedSection(stack_entries(100, 3, 5, 9), stack_entries(200, 3, 5, 9))]) == ([], [])


def test_equal_numbers_in_the_period_say_nothing_although_the_section_runs_over_the_boundary():
    """Eine lange Kette gleichzeitig gebuchter Überträge: Im Zeitraum stehen genauso viele Ausgangs- wie weggelassene Eingangs-Sätze –
    einer je Übertrag, nichts fehlt –, auch wenn der Abschnitt davor und danach weitergeht."""
    days = (-20, -13, -6, 1, 8, 15, 22, 29, 36, 43)
    section = MergedSection(stack_entries(100, *days), stack_entries(200, *days))
    assert boundary([section]) == ([], [])


def test_more_omitted_entries_than_outgoing_entries_in_the_period_name_the_surplus_at_the_edge_of_the_incoming_transfer():
    """Ausgänge am 27.2. und 2.3., Eingänge am 3.3. und 4.3.: Zwei weggelassene Eingangs-Sätze gegen einen Ausgangs-Satz im Zeitraum –
    einer gehört zum Ausgang im Februar. Der Übertrag läuft über den Anfang: genannt wird der frühere Eingangs-Satz."""
    section = MergedSection(stack_entries(100, 3, 4), stack_entries(200, -1, 2))                         # day(−1) ist der 27.2.
    assert day(-1) == date(2026, 2, 27)
    assert boundary([section]) == ([day(3)], [])


def test_the_surplus_is_named_at_the_end_when_the_outgoing_entries_lie_after_the_period():
    """Eingänge am 30. und 31.3., Ausgänge am 31.3. und 2.4.: der überzählige Eingangs-Satz gehört zum Ausgang im April – genannt
    wird der spätere."""
    section = MergedSection(stack_entries(100, 30, 31), stack_entries(200, 31, 33))
    assert boundary([section]) == ([day(31)], [])


def test_the_surplus_is_split_between_both_edges_when_outgoing_entries_lie_on_both_sides():
    """Im Zeitraum vier weggelassene Eingangs-Sätze und ein Ausgangs-Satz (am 20.) – Überschuss 3; vor dem Zeitraum liegt ein Ausgang,
    danach zwei: der früheste Eingangs-Satz und die zwei spätesten werden genannt."""
    section = MergedSection(stack_entries(100, 3, 4, 20, 25), stack_entries(200, -5, 40, 41, 20))
    assert boundary([section]) == ([day(3), day(20), day(25)], [])


def test_an_outgoing_entry_is_named_when_the_incoming_entry_of_its_section_lies_outside():
    """Ausgang am 31.3., Eingang am 2.4.: Im März-Stapel steht der Ausgangs-Satz, der Eingangs-Satz fiel (mit dem Ausgang im März)
    weg und gehört in den April – war der April schon exportiert, bevor beide Seiten kontiert waren, steht er dort ebenfalls."""
    section = MergedSection(stack_entries(100, 33), stack_entries(200, 31))
    assert boundary([section]) == ([], [day(31)])
    assert boundary([section], day(32), day(61)) == ([day(33)], [])                          # der April-Stapel: das erste Hinweisbild


def test_no_outgoing_entry_is_named_without_an_omitted_entry_outside():
    """Drei Ausgänge im Zeitraum, ein Eingang im Zeitraum, keiner außerhalb: Der Überschuss an Ausgängen ist kein Zeitraumproblem."""
    section = MergedSection(stack_entries(100, 5), stack_entries(200, 3, 4, 5))
    assert boundary([section]) == ([], [])


def test_the_outgoing_entries_named_never_exceed_the_incoming_entries_outside():
    """Drei Ausgänge im Zeitraum, ein weggelassener Eingang im Zeitraum, ein weiterer im April: höchstens ein Ausgangs-Satz hat seine
    Eingangsseite außerhalb."""
    section = MergedSection(stack_entries(100, 5, 33), stack_entries(200, 3, 4, 5))
    _, outgoing = boundary([section])
    assert len(outgoing) == 1 and outgoing == [day(5)]                                        # der Rand zum April hin: der späteste


def test_a_long_balanced_series_before_the_period_does_not_pull_the_naming_to_the_start():
    """Zehn gleichzeitig gebuchte Überträge vor dem Zeitraum (Ausgang und Eingang am selben Tag), im Zeitraum fünf Ausgänge (2., 9., 16.,
    23., 30.) und vier Eingänge (4., 11., 18., 25.), der fünfte Eingang am 2.4.: Der Übertrag, der die Grenze überquert, ist der vom 30. –
    genannt wird der späteste Ausgangs-Satz, nicht der erste (die zehn davor sind ausgeglichen)."""
    earlier = tuple(range(-69, 1, 7))
    section = MergedSection(stack_entries(100, *earlier, 4, 11, 18, 25, 33), stack_entries(200, *earlier, 2, 9, 16, 23, 30))
    assert boundary([section]) == ([], [day(30)])


def test_a_long_balanced_series_before_the_period_does_not_pull_the_naming_of_an_incoming_surplus_to_the_start_either():
    """Zehn gleichzeitig gebuchte Überträge davor; im Zeitraum fünf Eingänge (4., 11., 18., 25., 30.) und vier Ausgänge (4., 11., 18., 25.),
    der fünfte Ausgang erst am 2.4.: Der überzählige Eingangs-Satz ist der vom 30. – nicht der erste des Zeitraums."""
    earlier = tuple(range(-69, 1, 7))
    section = MergedSection(stack_entries(100, *earlier, 4, 11, 18, 25, 30), stack_entries(200, *earlier, 4, 11, 18, 25, 33))
    assert boundary([section]) == ([day(30)], [])


def test_a_transfer_that_runs_into_the_period_from_before_is_named_at_the_start_whatever_comes_after():
    """Der Ausgang des Übertrags am 28.2., sein Eingang am 2.3. – davor und danach ausgeglichene Reihen: Der erste Eingangs-Satz des
    Zeitraums ist der überzählige, nicht der letzte."""
    earlier = tuple(range(-69, 0, 7))                                                     # bis zum 24.2.
    section = MergedSection(stack_entries(100, *earlier, 2, 9, 16, 23, 30), stack_entries(200, *earlier, 0, 7, 14, 21, 28))
    assert boundary([section]) == ([day(2)], [])


def test_every_section_is_counted_on_its_own():
    """Zwei Abschnitte (zwei Beträge): Was in dem einen fehlt, wird nicht mit dem Überschuss des anderen verrechnet."""
    first = MergedSection(stack_entries(100, 3), stack_entries(200, -3))                       # Eingang im Zeitraum, Ausgang davor
    second = MergedSection(stack_entries(300, 20), stack_entries(400, 20, 21))                 # alles im Zeitraum
    assert boundary([first, second]) == ([day(3)], [])
    assert boundary([second, first]) == ([day(3)], [])


def test_the_entries_come_sorted_by_date_and_id_across_sections():
    first = MergedSection(stack_entries(100, 9), stack_entries(200, -9))
    second = MergedSection(stack_entries(300, 4), stack_entries(400, -4))
    incoming, outgoing = _boundary_entries([first, second], MARCH_FIRST, MARCH_LAST)
    assert [entry.entry_id for entry in incoming] == [300, 100] and outgoing == []


# ── Was herauskommt ──────────────────────────────────────────────────────────

def test_an_omitted_entry_carries_its_id_its_date_and_its_amount_and_the_section_names_the_outgoing_entries():
    """Das Datum der Sätze zählt (nicht das der Bankbewegungen): Der Abschnitt hält die weggelassenen Eingangs-Sätze und die
    Ausgangs-Sätze, deren Übertrag sie sind – daraus zählt ``check_transfers`` je Zeitraum (Hinweis 2)."""
    legs = [out_m(1, 3, entry_day=3), out_m(2, 5, entry_day=9), in_m(3, 6, amount=100 * EUR, entry_day=7)]
    result = settle_transfers(legs)
    assert result.omitted == {1003: StackEntry(1003, day(7), 100 * EUR)}
    assert result.merged == [MergedSection((StackEntry(1003, day(7), 100 * EUR),),
                                           (StackEntry(1001, day(3), 100 * EUR), StackEntry(1002, day(9), 100 * EUR)))]


def test_a_section_that_does_not_merge_is_no_merged_section():
    assert settle_transfers([out_m(1, 1), in_m(2, 1), out_c(3, 2)]).merged == []
    assert settle_transfers([out_m(1, 1)]).merged == []


def test_the_result_does_not_depend_on_the_order_of_the_input():
    legs = [out_m(1, 1), out_m(2, 4), in_m(3, 2), in_m(4, 8), out_c(5, 30), in_c(6, 40), out_m(7, 12), in_m(8, 13)]
    mirrors = [solo(50, 20), solo(51, 21)]
    expected = settle_transfers(legs, mirrors)
    shuffler = random.Random(7)
    for _ in range(20):
        shuffled_legs, shuffled_mirrors = legs[:], mirrors[:]
        shuffler.shuffle(shuffled_legs)
        shuffler.shuffle(shuffled_mirrors)
        assert settle_transfers(shuffled_legs, shuffled_mirrors) == expected


def test_the_input_is_not_changed():
    legs, mirrors = [out_m(1, 1), in_m(2, 1)], [solo(50, 1)]
    before = (list(legs), list(mirrors))
    settle_transfers(legs, mirrors)
    assert (legs, mirrors) == before


# ── Leistung der reinen Rechnung ─────────────────────────────────────────────

@pytest.mark.parametrize('spacing', [1, 10], ids=['eine-lange-kette', 'ein-abschnitt-je-uebertrag'])
def test_twenty_thousand_transfers_per_side_of_the_same_amount_are_counted_in_seconds(spacing):
    """Gruppen über Wörterbücher, je Gruppe einmal sortiert – nicht quadratisch: 20 000 gleich hohe Überträge je Seite (eine Gruppe,
    bei Abstand 1 ein einziger Abschnitt mit 20 000 Ausgängen und 20 000 Eingängen) in wenigen Sekunden."""
    count, start = 20_000, date(2026, 1, 1)
    legs = []
    for i in range(count):
        when = start + timedelta(days=i * spacing)
        legs.append(TransferLeg(2 * i + 1, A, True, 100 * EUR, when, B, 100_000 + 2 * i + 1, when))
        legs.append(TransferLeg(2 * i + 2, B, False, 100 * EUR, when, A, 100_000 + 2 * i + 2, when))
    started = time.perf_counter()
    result = settle_transfers(legs)
    elapsed = time.perf_counter() - started
    assert len(result.omitted) == count
    assert elapsed < 10, f'{elapsed:.1f} s für 40 000 Beine'


def test_twenty_thousand_movements_without_entry_next_to_one_booked_side_are_counted_in_seconds():
    count, start = 20_000, date(2026, 1, 1)
    legs = [out_m(1, 1)] + [TransferLeg(10 + i, B, False, 100 * EUR, start + timedelta(days=i % 4)) for i in range(count)]
    started = time.perf_counter()
    result = settle_transfers(legs)
    assert time.perf_counter() - started < 10
    assert result.covered == frozenset() and result.omitted == {}              # 20 000 Eingänge ohne Satz gegen einen Ausgang: keiner gedeckt


def test_twenty_thousand_sections_next_to_twenty_thousand_free_movements_ask_the_neighbourhood_in_seconds():
    """Das Umfeld wird mit Halbierungssuche auf den sortierten Tagen gefragt, nicht je Element durchgesehen: 20 000 Abschnitte (alle
    20 Tage ein Übertrag), jeder mit einem Ausgang ohne Satz zehn Tage danach – alle 20 000 Eingangs-Sätze bleiben und werden genannt."""
    count, start = 20_000, date(2026, 1, 1)
    legs = []
    for i in range(count):
        when = start + timedelta(days=i * 20)
        legs.append(TransferLeg(3 * i + 1, A, True, 100 * EUR, when, B, 100_000 + 3 * i + 1, when))
        legs.append(TransferLeg(3 * i + 2, B, False, 100 * EUR, when, A, 100_000 + 3 * i + 2, when))
        legs.append(TransferLeg(3 * i + 3, A, True, 100 * EUR, when + timedelta(days=10)))
    started = time.perf_counter()
    result = settle_transfers(legs)
    elapsed = time.perf_counter() - started
    assert result.omitted == {} and len(result.doubtful) == count
    assert elapsed < 10, f'{elapsed:.1f} s für 60 000 Beine'


def test_one_free_movement_at_the_end_of_a_chain_of_twenty_thousand_transfers_blocks_it_in_seconds():
    """Ein einziger Abschnitt mit 20 000 Ausgängen und 20 000 Eingängen und ein Ausgang ohne Satz im Umfeld: nichts fällt weg, jeder
    Eingangs-Satz wird genannt – in einem Durchgang, nicht quadratisch."""
    count, start = 20_000, date(2026, 1, 1)
    legs = []
    for i in range(count):
        when = start + timedelta(days=i % 400)
        legs.append(TransferLeg(2 * i + 1, A, True, 100 * EUR, when, B, 100_000 + 2 * i + 1, when))
        legs.append(TransferLeg(2 * i + 2, B, False, 100 * EUR, when, A, 100_000 + 2 * i + 2, when))
    legs.append(TransferLeg(999_999, A, True, 100 * EUR, start + timedelta(days=399 + TRANSFER_NEIGHBOUR_DAYS)))
    started = time.perf_counter()
    result = settle_transfers(legs)
    elapsed = time.perf_counter() - started
    assert result.omitted == {} and len(result.doubtful) == count
    assert elapsed < 10, f'{elapsed:.1f} s für 40 001 Beine'


# ═════════════════════════════════════════════════════════════════════════════
# B. Aus Zeilen werden Beine: transfer_legs – ohne Datenbank
# ═════════════════════════════════════════════════════════════════════════════
# Eigene Konten A, B, C mit den COA-IDs 101, 102, 103; Zeilen so, wie die Datenbank sie liefert.

ACCOUNT_COAS = {A: {101}, B: {102}, C: {103}}
BANK_COAS = {101, 102, 103}
MARCH_1 = date(2026, 3, 1)


def legs_of(banks, entries=(), account_coas=ACCOUNT_COAS, bank_coas=BANK_COAS):
    """``transfer_legs`` für ``banks`` ``[(ID, Datum, Betrag, Konto)]`` und ``entries``
    ``[(ID, Bankbewegung|None, Datum, Betrag, COA, Gegenkonto, Form)]``: ``(Beine, eigenständige Spiegel, anders kontierte)``."""
    with_children = {parent for _id, parent, *_rest in entries if parent is not None}
    return transfer_legs(banks, with_children, entries, account_coas, bank_coas)


@pytest.mark.parametrize('form', ['mirror_counter', 'mirror_coa', 'mirror_legacy', None])
@pytest.mark.parametrize('entry_amount', [100 * EUR, -100 * EUR])
@pytest.mark.parametrize('bank_amount, outgoing', [(-100 * EUR, True), (100 * EUR, False)])
def test_the_direction_comes_from_the_bank_movement_never_from_the_form_or_the_sign_of_the_entry(
        form, entry_amount, bank_amount, outgoing):
    legs, mirrors, others = legs_of([(1, '2026-03-01', bank_amount, A)], [(11, 1, '2026-03-01', entry_amount, 102, 101, form)])
    assert legs == [TransferLeg(1, A, outgoing, 100 * EUR, MARCH_1, B, 11, MARCH_1)] and mirrors == [] and others == []


def test_the_amount_of_a_leg_with_entry_is_the_amount_of_the_mirror_not_of_the_movement():
    """Ein Teilspiegel in einem Split: Die Bankbewegung über 500 trägt einen Spiegel über 100."""
    legs, _, others = legs_of([(1, '2026-03-01', -500 * EUR, A)],
                              [(11, 1, '2026-03-01', -400 * EUR, 6815, 101, 'purpose_first'),      # kein Konto im Spiegel (COA 6815)
                               (12, 1, '2026-03-01', -100 * EUR, 102, 101, 'mirror_counter')])
    assert [(leg.entry_id, leg.amount) for leg in legs] == [(12, 100 * EUR)] and others == []


def test_a_movement_without_any_child_is_a_leg_without_entry_with_its_own_amount():
    legs, _, others = legs_of([(1, '2026-03-01', -75 * EUR, A), (2, '2026-03-02', 40 * EUR, B)])
    assert legs == [TransferLeg(1, A, True, 75 * EUR, MARCH_1), TransferLeg(2, B, False, 40 * EUR, date(2026, 3, 2))]
    assert others == []


def test_a_movement_with_children_but_without_a_mirror_is_no_leg_but_booked_otherwise():
    """Eine Zahlung, eine Umbuchung in anderer Form: Die Bankbewegung ist kein Bein, aber sie ist nicht frei – ``settle_transfers``
    zählt sie als verbraucht (``TransferOther``), wenn sie die Seite eines Übertrags sein kann."""
    legs, mirrors, others = legs_of([(1, '2026-03-01', -50 * EUR, A), (2, '2026-03-02', 70 * EUR, B)],
                                    [(11, 1, '2026-03-01', -50 * EUR, 6815, 101, 'purpose_first'),
                                     (12, 2, '2026-03-02', 70 * EUR, 4400, 102, 'purpose_first')])
    assert legs == [] and mirrors == []
    assert others == [TransferOther(1, A, True, 50 * EUR, MARCH_1), TransferOther(2, B, False, 70 * EUR, date(2026, 3, 2))]


def test_a_movement_with_a_mirror_and_another_child_is_a_leg_and_not_booked_otherwise():
    legs, _, others = legs_of([(1, '2026-03-01', -500 * EUR, A)],
                              [(11, 1, '2026-03-01', -400 * EUR, 6815, 101, 'purpose_first'),
                               (12, 1, '2026-03-01', -100 * EUR, 102, 101, 'mirror_counter')])
    assert len(legs) == 1 and others == []


def test_several_mirrors_under_one_movement_are_several_legs():
    legs, _, others = legs_of([(1, '2026-03-01', -300 * EUR, A)],
                              [(11, 1, '2026-03-01', -100 * EUR, 102, 101, 'mirror_counter'),
                               (12, 1, '2026-03-01', -200 * EUR, 103, 101, 'mirror_counter')])
    assert [(leg.entry_id, leg.other, leg.amount) for leg in legs] == [(11, B, 100 * EUR), (12, C, 200 * EUR)] and others == []


@pytest.mark.parametrize('coa, counter', [
    pytest.param(102, 103, id='beide-Seiten-ohne-das-Konto-der-Bankbewegung'),
    pytest.param(101, 101, id='dasselbe-Konto-auf-beiden-Seiten'),
    pytest.param(102, 1460, id='eine-Seite-ist-kein-eigenes-Konto'),
    pytest.param(1460, 101, id='die-andere-Seite-ist-kein-eigenes-Konto'),
])
def test_a_child_that_is_no_mirror_between_the_account_of_the_movement_and_another_own_account_is_no_leg(coa, counter):
    legs, _, others = legs_of([(1, '2026-03-01', -100 * EUR, A)], [(11, 1, '2026-03-01', -100 * EUR, coa, counter, 'mirror_counter')])
    assert legs == [] and others == [TransferOther(1, A, True, 100 * EUR, MARCH_1)]


@pytest.mark.parametrize('form', ['purpose_first', 'liquid_first', 'transfer'])
def test_a_form_that_is_no_mirror_is_never_a_leg_even_between_two_own_accounts(form):
    legs, _, others = legs_of([(1, '2026-03-01', -100 * EUR, A)], [(11, 1, '2026-03-01', -100 * EUR, 102, 101, form)])
    assert legs == [] and others == [TransferOther(1, A, True, 100 * EUR, MARCH_1)]


def test_two_accounts_with_the_same_number_take_no_part_neither_of_them():
    shared = {A: {101}, B: {101}, C: {103}}                                     # A und B tragen dieselbe SKR-Nummer
    assert own_transfer_accounts(shared) == {C: frozenset({103})}
    legs, mirrors, others = legs_of([(1, '2026-03-01', -100 * EUR, A), (2, '2026-03-01', 100 * EUR, B), (3, '2026-03-01', 5 * EUR, C)],
                                    account_coas=shared)
    assert [leg.bank_id for leg in legs] == [3] and mirrors == [] and others == []
    assert own_transfer_accounts({A: set(), B: {102}}) == {B: frozenset({102})}


def test_a_leg_never_names_an_account_that_takes_no_part_as_the_other_side():
    shared = {A: {101}, B: {102}, C: {102}}                                     # B und C tragen dieselbe Nummer
    legs, _, others = legs_of([(1, '2026-03-01', -100 * EUR, A)], [(11, 1, '2026-03-01', -100 * EUR, 102, 101, 'mirror_counter')],
                              account_coas=shared)
    assert legs == [] and others == [TransferOther(1, A, True, 100 * EUR, MARCH_1)]


@pytest.mark.parametrize('form, amount, out, into', [
    pytest.param('mirror_coa', 100 * EUR, B, A, id='mirror_coa-positiv:-das-Konto-A-im-Soll-bekommt'),
    pytest.param('mirror_coa', -100 * EUR, A, B, id='mirror_coa-negativ:-das-Konto-A-verliert'),
    pytest.param('mirror_counter', 100 * EUR, A, B, id='mirror_counter-positiv:-das-Gegenkonto-B-bekommt'),
    pytest.param('mirror_counter', -100 * EUR, B, A, id='mirror_counter-negativ:-das-Gegenkonto-B-verliert'),
])
def test_a_standalone_mirror_of_known_reading_says_through_bank_effect_which_account_loses(form, amount, out, into):
    _, mirrors, _ = legs_of([], [(11, None, '2026-03-01', amount, 101, 102, form)])          # Konto A (101), Gegenkonto B (102)
    assert mirrors == [TransferMirror(11, MARCH_1, out, into, 100 * EUR)]


@pytest.mark.parametrize('form', ['mirror_legacy', None])
def test_a_standalone_mirror_of_unknown_reading_is_kept_without_a_direction(form):
    """Er legt nichts zusammen und deckt nichts, aber er kann als möglicherweise doppelt genannt werden: ``known`` ist falsch, die
    Konten stehen in der Reihenfolge des Satzes (Betrag und Vorzeichen werden nicht gedeutet)."""
    for amount in (100 * EUR, -100 * EUR):
        _, mirrors, _ = legs_of([], [(11, None, '2026-03-01', amount, 101, 102, form)])
        assert mirrors == [TransferMirror(11, MARCH_1, A, B, 100 * EUR, False)]


def test_zero_amounts_and_unreadable_dates_take_no_part():
    banks = [(1, '2026-03-01', 0, A), (2, 'kein Datum', -100 * EUR, A), (3, '2026-03-01', -100 * EUR, 99)]
    entries = [(11, None, '2026-03-01', 0, 101, 102, 'mirror_coa'), (12, None, 'gestern', 100 * EUR, 101, 102, 'mirror_coa')]
    assert legs_of(banks, entries) == ([], [], [])


# ── A: Nur JJJJ-MM-TT nimmt teil – jedes andere Datum und jeder andere Betrag nicht ──────────────────────────────────

NOT_AN_ISO_DAY = [
    pytest.param('20260305', id='ohne-Bindestriche'),
    pytest.param('2026-W10-4', id='Wochendatum'),
    pytest.param('2026-064', id='Ordinaldatum'),
    pytest.param('2026-03-05 12:30:00', id='mit-Uhrzeit-und-Leerzeichen'),
    pytest.param('2026-03-05T12:30:00', id='mit-Uhrzeit-und-T'),
    pytest.param('2026-03-05Z', id='mit-Zone'),
    pytest.param('2026-3-5', id='ohne-fuehrende-Nullen'),
    pytest.param(' 2026-03-05', id='Leerzeichen-davor'),
    pytest.param('2026-03-05 ', id='Leerzeichen-dahinter'),
    pytest.param('2026-03-05\n', id='Zeilenumbruch-dahinter'),
    pytest.param('2026-02-30', id='kein-Kalendertag'),
    pytest.param('2026-13-01', id='kein-Monat'),
    pytest.param('0001-01-03', id='Jahr-vor-1900'),
    pytest.param('1899-12-31', id='ein-Tag-vor-1900'),
    pytest.param('2201-01-01', id='Jahr-nach-2200'),
    pytest.param('9999-12-30', id='Jahr-9999'),
    pytest.param('\uff12\uff10\uff12\uff16-03-05', id='Ziffern-in-Vollbreite'),
    pytest.param('', id='leer'),
    pytest.param('gestern', id='Text'),
    pytest.param(None, id='NULL'),
    pytest.param(20260305, id='Zahl'),
    pytest.param(2026.0305, id='Kommazahl'),
]


@pytest.mark.parametrize('value', NOT_AN_ISO_DAY)
def test_a_date_that_is_not_exactly_an_iso_day_makes_the_row_take_no_part(value):
    """Bankbewegung, Spiegel-Satz und eigenständiger Spiegel: Die Zeile liefert weder ein Bein noch einen Spiegel – und
    kein Datum, das über das Datumsende hinaus rechnen ließe (``0001-01-03`` minus 14 Tage, ``9999-12-30`` plus 14 Tage)."""
    assert legs_of([(1, value, -100 * EUR, A)]) == ([], [], [])                                       # Bankbewegung ohne Kind
    assert legs_of([(1, value, -100 * EUR, A)], [(11, 1, '2026-03-01', -100 * EUR, 102, 101, 'mirror_counter')]) == ([], [], [])
    legs, mirrors, others = legs_of([(1, '2026-03-01', -100 * EUR, A)], [(11, 1, value, -100 * EUR, 102, 101, 'mirror_counter')])
    assert legs == [] and mirrors == [] and others == [TransferOther(1, A, True, 100 * EUR, MARCH_1)]    # anders kontiert
    assert legs_of([], [(11, None, value, 100 * EUR, 101, 102, 'mirror_coa')]) == ([], [], [])
    assert legs_of([], [(11, None, value, 100 * EUR, 101, 102, 'mirror_legacy')]) == ([], [], [])


@pytest.mark.parametrize('value, expected', [('1900-01-01', date(1900, 1, 1)), ('2200-12-31', date(2200, 12, 31)),
                                             ('2024-02-29', date(2024, 2, 29)), ('2026-03-31', date(2026, 3, 31))])
def test_a_calendar_day_from_1900_to_2200_takes_part(value, expected):
    legs, _, _ = legs_of([(1, value, -100 * EUR, A)])
    assert [leg.day for leg in legs] == [expected]


@pytest.mark.parametrize('value', ['100', 'abc', '', None, 100.0, 1e300, float('nan'), float('inf'), True, False, b'100', [100]])
def test_an_amount_that_is_not_a_whole_number_makes_the_row_take_no_part(value):
    assert legs_of([(1, '2026-03-01', value, A)]) == ([], [], [])
    assert legs_of([(1, '2026-03-01', -100 * EUR, A)], [(11, 1, '2026-03-01', value, 102, 101, 'mirror_counter')]) == (
        [], [], [TransferOther(1, A, True, 100 * EUR, MARCH_1)])
    assert legs_of([], [(11, None, '2026-03-01', value, 101, 102, 'mirror_coa')]) == ([], [], [])


def test_the_border_dates_do_not_make_the_calculation_fail():
    """Die Umfeld-Rechnung läuft nie über das Datumsende: Teilnehmende Tage liegen zwischen 1900 und 2200."""
    banks = [(1, '1900-01-01', -100 * EUR, A), (2, '2200-12-31', 100 * EUR, B), (3, '0001-01-03', -100 * EUR, A), (4, '9999-12-30', 100 * EUR, B)]
    legs, mirrors, others = legs_of(banks)
    assert [leg.bank_id for leg in legs] == [1, 2]
    result = settle_transfers(legs, mirrors, others)
    assert result.omitted == {} and result.doubtful == {}


# ═════════════════════════════════════════════════════════════════════════════
# C. Über die Datenbank und den echten Handler
# ═════════════════════════════════════════════════════════════════════════════

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


@pytest.fixture
def world(tmp_db):
    """Zwei eigene Bankkonten (SKR 1800 und 1810) neben der Standard-Kasse (SKR 1600, ``cash``)."""
    def account(name, skr):
        tmp_db.insert_account(name, 'Owner', 'DE00TRANSFER', 'BIC', 'Bank', is_cash=0, skr_account=skr)
        return next(a[0] for a in tmp_db.fetch_accounts() if a[1] == name)
    cash = next(a[0] for a in tmp_db.fetch_accounts() if a[6] == 1)
    return SimpleNamespace(db=tmp_db, a=account('Transfer-Eins', 1800), b=account('Transfer-Zwei', 1810), cash=cash)


def _bank(world, account, day_text, amount):
    return world.db.insert_booking(day_text, Decimal(amount), account_id=account, booking_type='bank')


def _mirror(world, parent, day_text, amount, coa, counter, form=None, auto_mirror=True):
    """Spiegel-Satz unter einer Bankbewegung, wie ihn die Buchungsmaske schreibt: das andere Konto als Konto, das eigene als
    Gegenkonto, der Betrag in der Richtung auf dem eigenen Konto (ohne ``form`` abgeleitet: ``mirror_counter``)."""
    return world.db.insert_booking(day_text, Decimal(amount), coa_id=_coa(world.db, coa),
                                   counter_coa_id=_coa(world.db, counter), booking_type='entry', parent_booking_id=parent,
                                   auto_mirror=auto_mirror, entry_form=form)


def _book_transfer(world, amount='100.00', day_out='2026-03-10', day_in=None, form=None, out_account=None, in_account=None,
                   out_number=1800, in_number=1810):
    """Beide Bankbewegungen eines Übertrags von ``out_number`` nach ``in_number`` samt Spiegel (standardmäßig 1800 → 1810 auf
    den Konten ``a`` und ``b``). Gibt die IDs (Bank Ausgang, Satz Ausgang, Bank Eingang, Satz Eingang) zurück."""
    day_in = day_in or day_out
    out_bank = _bank(world, out_account or world.a, day_out, f'-{amount}')
    out_row = _mirror(world, out_bank, day_out, f'-{amount}', in_number, out_number, form=form)
    in_bank = _bank(world, in_account or world.b, day_in, amount)
    in_row = _mirror(world, in_bank, day_in, amount, out_number, in_number, form=form)
    return out_bank, out_row, in_bank, in_row


def _handler(world, date_from, date_to, **more):
    return handlers.handle_datev_export(world.db, datev_post(date_from, date_to, **more))


def _rows_of(result):
    assert isinstance(result[0], bytes), result
    lines = [line for line in result[0].decode('cp1252').split('\r\n') if line][2:]
    return [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]


def _export(world, period=PERIOD):
    """Der Stapel über den echten Handler, ohne Bestätigung: die Felder je Zeile, ohne Vorlauf und Überschrift. Gibt es Hinweise,
    schlägt der Test an."""
    return _rows_of(_handler(world, *period))


def _export_confirmed(world, period=PERIOD):
    """Wie ``_export``, nach ausdrücklicher Bestätigung der Hinweise."""
    return _rows_of(confirmed_export(world.db, *period))


def _pairs(rows):
    """(Konto, Gegenkonto, S/H) je Zeile."""
    return [(fields[_SPALTE_KONTO], fields[_SPALTE_GEGENKONTO], fields[_SPALTE_SH]) for fields in rows]


def _stamped(db):
    """IDs der Buchungen mit Steuerdatum."""
    con = sqlite3.connect(db.db_name)
    try:
        return {booking_id for booking_id, tax in con.execute('SELECT ID, DateTax FROM Bookings') if tax}
    finally:
        con.close()


def _execute(db, sql, params=()):
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _dump(db):
    con = sqlite3.connect(db.db_name)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def _through_the_server_parser(post_data):
    """Wie im Server verschicken: urlencode → ``server.app.parse_form``."""
    return parse_form(urlencode([(key, value) for key, values in post_data.items() for value in values]))


# ── Der Standardfall: beide Seiten kontiert ──────────────────────────────────

def test_a_transfer_booked_on_both_sides_has_a_mirror_under_each_bank_movement(world):
    """So steht ein beidseitig kontierter Übertrag in den Büchern: je ein Spiegel unter jeder Bankbewegung, beide „Betrag auf dem
    Gegenkonto“. Ohne die Regel wären es im Stapel „1810 S an 1800“ und „1800 H an 1810“ – Soll 1810 und Haben 1800, einmal
    von jeder Seite."""
    out_bank, out_row, in_bank, in_row = _book_transfer(world)
    forms = {row[0]: row[9] for bank in (out_bank, in_bank) for row in world.db.get_child_bookings_for_bank(bank)}
    assert forms == {out_row: 'mirror_counter', in_row: 'mirror_counter'}
    assert world.db.duplicate_transfer_rows() == {in_row}


def test_both_sides_booked_with_insert_booking_give_one_row_of_the_outgoing_side(world):
    _out_bank, out_row, _in_bank, in_row = _book_transfer(world)

    rows = _export(world)

    assert _pairs(rows) == [('1810', '1800', 'S')]                       # Soll 1810 (Eingang), Haben 1800 (Ausgang)
    assert _stamped(world.db) == {out_row} and in_row not in _stamped(world.db)   # nur die exportierte Zeile bekommt ein Steuerdatum


def test_both_sides_booked_through_the_mask_give_one_row(world):
    """Wie ein Nutzer es tut: Bankbewegung anlegen, in der Maske das Gegenkonto wählen und speichern – für beide Seiten."""
    def mask(bank_id, account, coa, amount):
        return {'transaction_id': [str(bank_id)], 'date': ['2026-03-10'], 'amount': [amount], 'account': [str(account)],
                'currency': ['EUR'], 'coa_id': [str(_coa(world.db, coa))]}

    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    assert handlers.handle_add_transaction(world.db, mask(out_bank, world.a, 1810, '-100.00'))[0] == 303
    assert handlers.handle_add_transaction(world.db, mask(in_bank, world.b, 1800, '100.00'))[0] == 303

    out_children = world.db.get_child_bookings_for_bank(out_bank)
    in_children = world.db.get_child_bookings_for_bank(in_bank)
    assert len(out_children) == len(in_children) == 1
    assert out_children[0][3] == in_children[0][3] == 1                                  # AutoMirror, wie die Maske es schreibt
    assert out_children[0][9] == in_children[0][9] == 'mirror_counter'

    assert world.db.duplicate_transfer_rows() == {in_children[0][0]}
    assert _pairs(_export(world)) == [('1810', '1800', 'S')]
    assert _stamped(world.db) == {out_children[0][0]}


def test_both_sides_booked_with_new_bank_movements_in_the_mask_give_one_row(world):
    """Die Maske legt eine neue Bankbewegung samt Satz in einem Zug an (``coa_id`` gesetzt)."""
    def mask(account, coa, amount):
        return {'transaction_id': ['0'], 'date': ['2026-03-10'], 'amount': [amount], 'account': [str(account)],
                'currency': ['EUR'], 'coa_id': [str(_coa(world.db, coa))]}

    assert handlers.handle_add_transaction(world.db, mask(world.a, 1810, '-100.00'))[0] == 303
    assert handlers.handle_add_transaction(world.db, mask(world.b, 1800, '100.00'))[0] == 303

    assert _pairs(_export(world)) == [('1810', '1800', 'S')]


def test_both_sides_booked_through_the_bank_editor_give_one_row(world):
    """Der Buchungssatz-Editor der Bankmaske schickt die Sätze als Zeilen (``split_*``): Kontiert der Nutzer so beide Bankbewegungen
    als Übertrag, steht unter jeder ein Spiegel – und der Stapel hat die Buchung einmal."""
    def editor(bank_id, account, coa, amount):
        return _through_the_server_parser({
            'transaction_id': [str(bank_id)], 'date': ['2026-03-10'], 'amount': [amount], 'account': [str(account)],
            'currency': ['EUR'], 'split_id': [''], 'split_amount': [amount], 'split_coa': [str(_coa(world.db, coa))],
            'split_tax_rate': [''], 'split_tax_amount': [''], 'split_docnr': [''], 'split_text': ['Übertrag']})

    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    assert handlers.handle_add_transaction(world.db, editor(out_bank, world.a, 1810, '-100.00'))[0] == 303
    assert handlers.handle_add_transaction(world.db, editor(in_bank, world.b, 1800, '100.00'))[0] == 303

    out_children = world.db.get_child_bookings_for_bank(out_bank)
    in_children = world.db.get_child_bookings_for_bank(in_bank)
    assert len(out_children) == len(in_children) == 1
    assert out_children[0][9] == in_children[0][9] == 'mirror_counter'

    assert world.db.duplicate_transfer_rows() == {in_children[0][0]}
    assert _pairs(_export(world)) == [('1810', '1800', 'S')]
    assert _stamped(world.db) == {out_children[0][0]}


def test_the_kept_row_is_the_one_of_the_outgoing_side_whatever_the_order_of_booking(world):
    """Zuerst der Eingang, dann der Ausgang gebucht (höhere ID beim Ausgang): behalten wird trotzdem der Ausgang."""
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    in_row = _mirror(world, in_bank, '2026-03-10', '100.00', 1800, 1810)
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    out_row = _mirror(world, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    assert out_row > in_row

    assert world.db.duplicate_transfer_rows() == {in_row}
    assert _pairs(_export(world)) == [('1810', '1800', 'S')]
    assert _stamped(world.db) == {out_row}


def test_opposite_transfers_of_the_same_amount_are_separate_groups_each_with_one_row(world):
    """A → B und am Tag danach B → A über denselben Betrag, beide beidseitig kontiert: zwei Gruppen, zwei Zeilen – die
    Eingangsseiten fallen weg, nicht die Ausgangsseite des anderen Übertrags."""
    _book_transfer(world, day_out='2026-03-10')
    _out_bank, out_row, _in_bank, in_row = _book_transfer(world, day_out='2026-03-11', out_account=world.b, in_account=world.a,
                                                          out_number=1810, in_number=1800)

    assert len(world.db.duplicate_transfer_rows()) == 2 and in_row in world.db.duplicate_transfer_rows()
    assert sorted(_pairs(_export(world))) == [('1800', '1810', 'S'), ('1810', '1800', 'S')]
    assert out_row in _stamped(world.db) and in_row not in _stamped(world.db)


def test_two_equal_transfers_in_one_week_are_two_rows(world):
    """Zwei gleich hohe Überträge, beide beidseitig kontiert: ein Abschnitt mit zwei Ausgängen und zwei Eingängen – beide
    Eingangs-Sätze fallen weg, zwei Zeilen bleiben."""
    _book_transfer(world, day_out='2026-03-10')
    _book_transfer(world, day_out='2026-03-12')

    assert len(world.db.duplicate_transfer_rows()) == 2
    assert _pairs(_export(world)) == [('1810', '1800', 'S'), ('1810', '1800', 'S')]


# ── Eine Seite kontiert ──────────────────────────────────────────────────────

@pytest.mark.parametrize('booked_side', ['outgoing', 'incoming'])
def test_a_transfer_booked_on_one_side_only_stays_one_row_and_the_other_side_is_not_reported(world, booked_side):
    """Die andere Bankbewegung bleibt ohne Satz – ihr Spiegel deckt sie, der Export fragt nicht nach einer Bestätigung."""
    if booked_side == 'outgoing':
        bank = _bank(world, world.a, '2026-03-10', '-100.00')
        _mirror(world, bank, '2026-03-10', '-100.00', 1810, 1800)
        _bank(world, world.b, '2026-03-10', '100.00')
    else:
        bank = _bank(world, world.b, '2026-03-10', '100.00')
        _mirror(world, bank, '2026-03-10', '100.00', 1800, 1810)
        _bank(world, world.a, '2026-03-10', '-100.00')

    assert world.db.duplicate_transfer_rows() == set()
    assert world.db.find_unbooked_bank_movements(*PERIOD) == []
    assert len(_export(world)) == 1


# ── Der Spiegel eines WISO-Imports ───────────────────────────────────────────

@pytest.mark.parametrize('linked', [False, True], ids=['eigenstaendig', 'verknuepft'])
def test_one_wiso_mirror_per_transfer_is_one_row_and_nothing_is_reported(world, linked):
    """Eine Zeile je Übertrag („1810 an 1800“, ``mirror_coa``, Rohbetrag): eigenständig oder an eine der beiden Bankzeilen gehängt –
    es gibt nur den einen Satz, nichts fällt weg, und die Bankbewegung(en) ohne Satz sind durch ihn gedeckt."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _bank(world, world.b, '2026-03-10', '100.00')
    _mirror(world, out_bank if linked else None, '2026-03-10', '100.00', 1810, 1800, form='mirror_coa', auto_mirror=False)

    assert world.db.duplicate_transfer_rows() == set()
    assert world.db.find_unbooked_bank_movements(*PERIOD) == []
    assert _pairs(_export(world)) == [('1810', '1800', 'S')]


def test_a_standalone_mirror_and_a_booked_other_side_are_not_merged_but_named_as_possibly_doubled(world):
    """Ein eigenständiger Spiegel (WISO oder von Hand) plus die von Hand kontierte Gegenseite ist eine doppelte Erfassung des
    Nutzers, nicht der mechanische Fall: Nichts wird zusammengelegt (s > 0), beide Sätze stehen im Stapel – und der Export sagt, dass
    der Übertrag möglicherweise doppelt darin steht (der Spiegel, in dessen Umfeld eine kontierte Bankbewegung liegt)."""
    mirror_row = _mirror(world, None, '2026-03-10', '100.00', 1810, 1800, form='mirror_coa', auto_mirror=False)
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    _mirror(world, in_bank, '2026-03-10', '100.00', 1800, 1810)
    _bank(world, world.a, '2026-03-10', '-100.00')

    assert world.db.duplicate_transfer_rows() == set()
    assert world.db.find_unbooked_bank_movements(*PERIOD) == []                      # der Ausgang ist durch beide gedeckt
    assert world.db.check_transfers(*PERIOD).doubtful == [(mirror_row, '2026-03-10', Decimal('100.00'))]
    assert is_confirm_redirect(_handler(world, *PERIOD))
    assert sorted(_pairs(_export_confirmed(world))) == [('1800', '1810', 'H'), ('1810', '1800', 'S')]


def test_a_standalone_mirror_of_unknown_reading_covers_nothing_and_merges_nothing(world):
    """``mirror_legacy`` ohne Bankbewegung: Welches Konto verliert, ist unbekannt – der Satz deckt nichts, beide Bewegungen werden
    gemeldet."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    _mirror(world, None, '2026-03-10', '100.00', 1810, 1800, form='mirror_legacy', auto_mirror=False)

    assert world.db.duplicate_transfer_rows() == set()
    assert [row[0] for row in world.db.find_unbooked_bank_movements(*PERIOD)] == [out_bank, in_bank]


# ── mirror_legacy unter den Bankbewegungen ───────────────────────────────────

def test_a_pair_of_legacy_mirrors_gives_one_row_with_the_marker_as_before(world):
    """Altbestand aus der Maske von 2.0.x: Der Satz trägt den Betrag seiner Bankbewegung, ``mirror_legacy`` wird nicht umgedeutet.
    Die Richtung kommt allein aus dem Vorzeichen der Bankbewegungen; die behaltene Zeile wird gelesen wie bisher
    (Gegenkonto-Seite): bei Betrag < 0 steht das Gegenkonto im Haben, das Feld „Konto“ im Soll (S)."""
    _out_bank, out_row, _in_bank, in_row = _book_transfer(world, form='mirror_legacy')

    assert world.db.duplicate_transfer_rows() == {in_row}
    assert _pairs(_export(world)) == [('1810', '1800', 'S')]
    assert _stamped(world.db) == {out_row}


@pytest.mark.parametrize('sign', [1, -1], ids=['Betrag-wie-die-Bankbewegung', 'Betrag-umgekehrt'])
def test_the_count_does_not_read_the_sign_of_a_legacy_mirror(world, sign):
    """Die Lesart eines ``mirror_legacy`` ist unbekannt – für die Beine zählt das Vorzeichen der Bankbewegung."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _mirror(world, out_bank, '2026-03-10', f'{-100 * sign}.00', 1810, 1800, form='mirror_legacy')
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    in_row = _mirror(world, in_bank, '2026-03-10', f'{100 * sign}.00', 1800, 1810, form='mirror_legacy')

    assert world.db.duplicate_transfer_rows() == {in_row}


def test_a_mirror_row_without_a_form_counts_like_any_mirror_between_two_own_accounts(world):
    """Eine per Roh-SQL angelegte Zeile ohne Form: ``is_mirror_row`` gilt für sie, die Beine folgen den Bankbewegungen."""
    _out_bank, out_row, _in_bank, in_row = _book_transfer(world)
    _execute(world.db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID IN (?, ?)', (out_row, in_row))
    assert world.db.duplicate_transfer_rows() == {in_row}


def test_the_count_does_not_touch_the_form_of_a_legacy_mirror(world):
    _out_bank, out_row, _in_bank, _in_row = _book_transfer(world, form='mirror_legacy')
    before = _dump(world.db)

    assert len(world.db.duplicate_transfer_rows()) == 1

    assert _dump(world.db) == before
    assert world.db.get_booking_by_id(out_row)[22] == 'mirror_legacy'


# ── Teilspiegel in einem Split ───────────────────────────────────────────────

def test_a_partial_mirror_in_a_split_is_counted_with_its_own_amount(world):
    """Die Bankbewegung über 500,00 trägt einen Aufwand (400,00) und einen Spiegel über 100,00 an B; B bucht seine +100,00 – der
    Eingangs-Satz fällt weg, weil der Spiegel (nicht die Bankbewegung) den Betrag des Beins bestimmt."""
    out_bank = _bank(world, world.a, '2026-03-10', '-500.00')
    world.db.insert_booking('2026-03-10', Decimal('-400.00'), coa_id=_coa(world.db, 6815), counter_coa_id=_coa(world.db, 1800),
                            booking_type='entry', parent_booking_id=out_bank)
    out_row = _mirror(world, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    in_row = _mirror(world, in_bank, '2026-03-10', '100.00', 1800, 1810)

    assert world.db.duplicate_transfer_rows() == {in_row}
    assert sorted(_pairs(_export(world))) == [('1810', '1800', 'S'), ('6815', '1800', 'S')]
    assert out_row in _stamped(world.db)


# ── Kasse und Verrechnungskonto ──────────────────────────────────────────────

def test_a_transfer_between_a_bank_and_the_cash_account_is_one_row(world):
    """Die Kasse (SKR 1600) ist ein eigenes Konto mit Bankzeilen: Ist ein Übertrag Bank → Kasse auf beiden Seiten kontiert, steht er
    einmal im Stapel."""
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(world, amount='200.00', in_account=world.cash, in_number=1600)

    assert world.db.duplicate_transfer_rows() == {in_row}
    assert _pairs(_export(world)) == [('1600', '1800', 'S')]


def test_a_transfer_through_the_clearing_account_stays_two_rows_and_nothing_is_reported(world):
    """Über 1460 gibt es zwei echte Teilbuchungen (1460 an 1800, 1810 an 1460) – 1460 ist kein eigenes Konto, die Sätze sind
    keine Spiegel im Sinn der Bank-zu-Bank-Regel."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _mirror(world, out_bank, '2026-03-10', '-100.00', 1460, 1800)
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    _mirror(world, in_bank, '2026-03-10', '100.00', 1460, 1810)

    assert world.db.duplicate_transfer_rows() == set()
    assert world.db.find_unbooked_bank_movements(*PERIOD) == []
    assert sorted(_pairs(_export(world))) == [('1460', '1800', 'S'), ('1460', '1810', 'H')]


def test_a_cash_account_that_still_carries_1460_behaves_like_any_own_account(world):
    """Bestand: Eine Kasse mit der Nummer 1460 ist ein eigenes Konto – der Übertrag Bank → Kasse steht einmal im Stapel."""
    _execute(world.db, 'UPDATE Accounts SET SKRAccount = 1460 WHERE ID = ?', (world.cash,))
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(world, amount='50.00', in_account=world.cash, in_number=1460)

    assert world.db.duplicate_transfer_rows() == {in_row}
    assert _pairs(_export(world)) == [('1460', '1800', 'S')]


def test_two_accounts_with_the_same_number_take_no_part_and_their_movements_are_reported(world):
    """Zwei eigene Konten mit der Nummer 1800: Ein Satz auf 1800 ließe sich keinem zuordnen – nichts wird zusammengelegt, die
    Bewegung auf dem zweiten Konto ohne Satz wird gemeldet."""
    world.db.insert_account('Transfer-Drei', 'Owner', 'DE00TRANSFER', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    third = next(a[0] for a in world.db.fetch_accounts() if a[1] == 'Transfer-Drei')
    _book_transfer(world)
    lonely = _bank(world, third, '2026-03-11', '100.00')

    assert world.db.duplicate_transfer_rows() == set()
    assert [row[0] for row in world.db.find_unbooked_bank_movements(*PERIOD)] == [lonely]
    assert len(_export_confirmed(world)) == 2                                      # beide Sätze des Übertrags bleiben


# ── Die Zählregel dort, wo eine Paarbildung einzelner Spiegel scheiterte ─────

@pytest.mark.parametrize('offset', [0, TRANSFER_DAY_WINDOW], ids=['am-selben-Tag', 'am-Rand-des-Fensters'])
def test_two_equal_transfers_each_booked_on_one_side_only_keep_both_entries_and_report_the_free_movements(
        world, offset):
    """Zwei echte, gleich hohe Überträge A → B; der erste nur auf A kontiert (die Gegenseite b1 ohne Satz), der zweite nur auf B
    (der Ausgang a2 ohne Satz). Eine Paarbildung einzelner Spiegel läse die beiden Sätze als ein Paar und ließe einen weg – die Stapelsumme
    stimmte nicht. Jetzt: ein Abschnitt mit a_m = a_c = b_m = b_c = 1, nichts fällt weg, und – so ergibt es die Regel, die
    Gegenseiten sind nicht zu unterscheiden – beide Bewegungen ohne Satz werden gemeldet."""
    a1 = _bank(world, world.a, '2026-03-02', '-500.00')
    k1 = _mirror(world, a1, '2026-03-02', '-500.00', 1810, 1800)
    b1 = _bank(world, world.b, '2026-03-02', '500.00')
    day_two = (date(2026, 3, 2) + timedelta(days=offset)).isoformat()
    a2 = _bank(world, world.a, day_two, '-500.00')
    b2 = _bank(world, world.b, day_two, '500.00')
    d2 = _mirror(world, b2, day_two, '500.00', 1800, 1810)

    assert world.db.duplicate_transfer_rows() == set()
    assert sorted(row[0] for row in world.db.find_unbooked_bank_movements(*PERIOD)) == sorted([a2, b1])
    rows = _export_confirmed(world)
    assert len(rows) == 2 and {k1, d2} <= _stamped(world.db)
    assert _balances(rows) == {1800: -100_000, 1810: 100_000}                       # stimmt mit den Bankbewegungen überein


def test_two_equal_transfers_booked_on_one_side_only_beyond_the_window_name_the_second_entry_and_report_both_free_movements(world):
    """Acht Tage auseinander (Lücke 8 > 7 Tage): zwei Abschnitte, beide Sätze bleiben, der Stapel stimmt. Weil im Umfeld des zweiten
    Abschnitts ein Ausgang mit Satz liegt, nennt der Export den Eingangs-Satz als möglicherweise doppelt (ohne Paarbildung lässt sich
    das nicht ausschließen); aus demselben Grund deckt keiner der beiden Sätze die Bewegung ohne Satz seines Abschnitts – beide werden
    gemeldet."""
    a1 = _bank(world, world.a, '2026-03-02', '-500.00')
    _mirror(world, a1, '2026-03-02', '-500.00', 1810, 1800)
    b1 = _bank(world, world.b, '2026-03-02', '500.00')
    a2 = _bank(world, world.a, '2026-03-10', '-500.00')
    b2 = _bank(world, world.b, '2026-03-10', '500.00')
    d2 = _mirror(world, b2, '2026-03-10', '500.00', 1800, 1810)

    assert world.db.duplicate_transfer_rows() == set()
    assert [row[0] for row in world.db.find_unbooked_bank_movements(*PERIOD)] == [b1, a2]
    assert world.db.check_transfers(*PERIOD).doubtful == [(d2, '2026-03-10', Decimal('500.00'))]
    rows = _export_confirmed(world)
    assert len(rows) == 2 and _balances(rows) == {1800: -100_000, 1810: 100_000}


def test_two_equal_transfers_booked_on_one_side_only_beyond_the_neighbourhood_are_independent_and_nothing_is_said(world):
    """Fünfzehn Tage auseinander: Kein Ausgang mit Satz im Umfeld des zweiten Abschnitts – nichts wird gemeldet, der Export läuft."""
    a1 = _bank(world, world.a, '2026-03-02', '-500.00')
    _mirror(world, a1, '2026-03-02', '-500.00', 1810, 1800)
    _bank(world, world.b, '2026-03-02', '500.00')
    _bank(world, world.a, '2026-03-17', '-500.00')
    b2 = _bank(world, world.b, '2026-03-17', '500.00')
    _mirror(world, b2, '2026-03-17', '500.00', 1800, 1810)

    assert world.db.check_transfers(*PERIOD).doubtful == [] and world.db.find_unbooked_bank_movements(*PERIOD) == []
    rows = _export(world)
    assert len(rows) == 2 and _balances(rows) == {1800: -100_000, 1810: 100_000}


def test_a_free_payment_next_to_a_transfer_booked_on_both_sides_blocks_the_merge_and_is_reported(world):
    """Beidseitig kontierter Übertrag über 500,00 und zwei Tage später eine Lieferantenzahlung −500,00 auf A ohne Satz: Ein Spiegel,
    der sie „deckte“, ließe sie ungemeldet – und beim Zusammenlegen wüsste niemand, welcher Ausgang die Zahlung ist. Hier sperrt
    a_c = 1 das Zusammenlegen (nichts fällt weg), und die Zahlung wird gemeldet."""
    _out_bank, _out_row, _in_bank, _in_row = _book_transfer(world, amount='500.00', day_out='2026-03-02')
    payment = _bank(world, world.a, '2026-03-04', '-500.00')

    assert world.db.duplicate_transfer_rows() == set()
    assert [row[0] for row in world.db.find_unbooked_bank_movements(*PERIOD)] == [payment]
    assert len(_export_confirmed(world)) == 2                                       # beide Spiegel bleiben (eine Doppelung, die nicht verloren geht)


def test_a_free_payment_ten_days_after_a_transfer_booked_on_both_sides_blocks_the_merge_through_the_neighbourhood(world):
    """Beidseitig kontierter Übertrag über 500,00 am 2.3., eine Lieferantenzahlung −500,00 auf A ohne Satz am 12.3. (zehn Tage danach,
    ein anderer Abschnitt): Sie sperrt das Zusammenlegen im Umfeld – nichts fällt weg, beide Sätze stehen im Stapel. Der Export meldet
    die Zahlung (erster Hinweis) und den Eingangs-Satz als möglicherweise doppelt (dritter Hinweis)."""
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(world, amount='500.00', day_out='2026-03-02')
    payment = _bank(world, world.a, '2026-03-12', '-500.00')

    check = world.db.check_transfers(*PERIOD)
    assert check.omitted == set() and [row[0] for row in check.unbooked] == [payment]
    assert check.doubtful == [(in_row, '2026-03-02', Decimal('500.00'))]
    message = message_of(_handler(world, *PERIOD))
    assert '1 Bankbewegung im Zeitraum hat keinen Buchungssatz' in message and 'steht möglicherweise doppelt im Stapel' in message
    assert len(_export_confirmed(world)) == 2


@pytest.mark.parametrize('payment_day, blocks', [('2026-03-16', True), ('2026-03-17', False)],
                         ids=['14-Tage-danach:-im-Umfeld', '15-Tage-danach:-jenseits-des-Umfelds'])
def test_the_same_payment_blocks_the_merge_only_within_fourteen_days_of_the_transfer(world, payment_day, blocks):
    """Übertrag über 500,00 am 2.3., beidseitig kontiert; die Zahlung gleicher Höhe ohne Satz am 16.3. (14 Tage danach) sperrt noch,
    die am 17.3. nicht mehr – dann fällt der Eingangs-Satz weg."""
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(world, amount='500.00', day_out='2026-03-02')
    _bank(world, world.a, payment_day, '-500.00')
    assert world.db.duplicate_transfer_rows() == (set() if blocks else {in_row})


def test_outgoing_1_and_4_march_incoming_3_and_8_march_are_one_section_and_both_incoming_entries_fall_away(world):
    """Zwei Überträge über je 100,00, beidseitig kontiert: Ausgänge am 1.3. und 4.3., Eingänge am 3.3. und 8.3. Lücken 2, 1, 4:
    ein Abschnitt mit a_m = b_m = 2 – beide Eingangs-Sätze fallen weg, zwei Zeilen bleiben, die Summen stimmen."""
    for out_day, in_day in (('2026-03-01', '2026-03-03'), ('2026-03-04', '2026-03-08')):
        _book_transfer(world, day_out=out_day, day_in=in_day)

    assert len(world.db.duplicate_transfer_rows()) == 2
    rows = _export(world)
    assert _pairs(rows) == [('1810', '1800', 'S'), ('1810', '1800', 'S')]
    assert _balances(rows) == {1800: -20_000, 1810: 20_000}


def test_outgoing_1_and_4_march_incoming_3_and_11_march_are_still_one_section(world):
    """Der zweite Eingang am Rand des Fensters (11.3. = 4.3. + 7 Tage): ein Abschnitt, beide Eingangs-Sätze fallen weg."""
    for out_day, in_day in (('2026-03-01', '2026-03-03'), ('2026-03-04', '2026-03-11')):
        _book_transfer(world, day_out=out_day, day_in=in_day)

    assert len(world.db.duplicate_transfer_rows()) == 2
    assert _balances(_export(world)) == {1800: -20_000, 1810: 20_000}


def test_an_incoming_entry_more_than_the_window_after_the_last_outgoing_one_stays_in_the_stack_and_is_named(world):
    """Ausgänge am 1.3. und 4.3., Eingänge am 3.3. und 12.3.: Der 12.3. liegt 8 Tage (mehr als 7) nach dem 4.3. und bildet einen
    eigenen Abschnitt ohne Ausgang – dort fällt nichts weg. Im ersten Abschnitt (a_m = 2, b_m = 1) fällt der Eingang vom 3.3. weg,
    der vom 12.3. bleibt. Ohne Paarbildung lässt sich nicht sagen, dass der 12.3. zum Ausgang vom 4.3. gehört – im Zweifel bleibt der
    Satz stehen, und diese eine Buchung steht dann doppelt (Konto 1800 im Stapel −300,00 statt −200,00). Verloren geht nichts, und der
    Export nennt den Satz: Im Umfeld (14 Tage) liegt ein Ausgang mit Satz."""
    _book_transfer(world, day_out='2026-03-01', day_in='2026-03-03')
    _out_bank, _out_row, _in_bank, late_in_row = _book_transfer(world, day_out='2026-03-04', day_in='2026-03-12')

    omitted = world.db.duplicate_transfer_rows()
    assert len(omitted) == 1 and late_in_row not in omitted
    assert world.db.check_transfers(*PERIOD).doubtful == [(late_in_row, '2026-03-12', Decimal('100.00'))]
    rows = _export_confirmed(world)
    assert len(rows) == 3 and _balances(rows) == {1800: -30_000, 1810: 30_000}


def test_an_incoming_entry_beyond_the_neighbourhood_stays_in_the_stack_without_a_word_the_known_limit(world):
    """Der zweite Eingang 16 Tage nach dem zweiten Ausgang (4.3. → 20.3.): kein Ausgang mit Satz im Umfeld – der Satz bleibt, steht
    doppelt im Stapel (−300,00 statt −200,00) und wird nicht genannt. Das ist die Grenze: Eine Verzögerung über das Umfeld hinaus
    erkennt die Zählregel nicht."""
    _book_transfer(world, day_out='2026-03-01', day_in='2026-03-03')
    _out_bank, _out_row, _in_bank, late_in_row = _book_transfer(world, day_out='2026-03-04', day_in='2026-03-20')

    assert late_in_row not in world.db.duplicate_transfer_rows()
    assert world.db.check_transfers(*PERIOD).doubtful == []
    assert _balances(_export(world)) == {1800: -30_000, 1810: 30_000}


# ── Das Fenster ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('offset', [0, 1, 3, TRANSFER_DAY_WINDOW, -TRANSFER_DAY_WINDOW])
def test_dates_inside_the_window_form_one_section(world, offset):
    day_in = (date(2026, 3, 10) + timedelta(days=offset)).isoformat()
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(world, day_in=day_in)
    assert world.db.duplicate_transfer_rows() == {in_row}


@pytest.mark.parametrize('offset', [TRANSFER_DAY_WINDOW + 1, -TRANSFER_DAY_WINDOW - 1, 20])
def test_dates_outside_the_window_leave_both_rows(world, offset):
    day_in = (date(2026, 3, 10) + timedelta(days=offset)).isoformat()
    _book_transfer(world, day_in=day_in)
    assert world.db.duplicate_transfer_rows() == set()


# ── Nichts fällt weg, was nicht zusammengehört ───────────────────────────────

def test_another_amount_is_no_group(world):
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _mirror(world, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    in_bank = _bank(world, world.b, '2026-03-10', '100.01')
    _mirror(world, in_bank, '2026-03-10', '100.01', 1800, 1810)
    assert world.db.duplicate_transfer_rows() == set()


@pytest.mark.parametrize('sign', ['-', ''], ids=['zwei-Ausgaenge', 'zwei-Eingaenge'])
def test_two_movements_in_the_same_direction_form_no_group(world, sign):
    first = _bank(world, world.a, '2026-03-10', f'{sign}100.00')
    _mirror(world, first, '2026-03-10', f'{sign}100.00', 1810, 1800)
    second = _bank(world, world.b, '2026-03-10', f'{sign}100.00')
    _mirror(world, second, '2026-03-10', f'{sign}100.00', 1800, 1810)
    assert world.db.duplicate_transfer_rows() == set()


def test_two_movements_of_the_same_account_form_no_group(world):
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _mirror(world, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    in_bank = _bank(world, world.a, '2026-03-10', '100.00')
    _mirror(world, in_bank, '2026-03-10', '100.00', 1800, 1810)
    assert world.db.duplicate_transfer_rows() == set()


def test_the_accounts_of_the_rows_must_be_the_accounts_of_the_movements(world):
    """Der Satz unter der Bank 1810 nennt 1600 statt 1800: kein Übertrag zwischen den beiden Konten der Bankbewegungen."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _mirror(world, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    in_bank = _bank(world, world.b, '2026-03-10', '100.00')
    _mirror(world, in_bank, '2026-03-10', '100.00', 1600, 1810)
    assert world.db.duplicate_transfer_rows() == set()


@pytest.mark.parametrize('form', ['purpose_first', 'liquid_first', 'transfer'])
def test_a_row_that_is_no_mirror_never_counts(world, form):
    _book_transfer(world, form=form)
    assert world.db.duplicate_transfer_rows() == set()


def test_a_bank_account_without_skr_number_takes_no_part(world):
    world.db.insert_account('Ohne-SKR', 'Owner', 'DE00TRANSFER', 'BIC', 'Bank', is_cash=0, skr_account=None)
    account = next(a[0] for a in world.db.fetch_accounts() if a[1] == 'Ohne-SKR')
    _book_transfer(world, out_account=account)
    assert world.db.duplicate_transfer_rows() == set()


def test_a_movement_of_zero_never_counts(world):
    _book_transfer(world, amount='0.00')
    assert world.db.duplicate_transfer_rows() == set()


def test_the_count_only_reads_and_gives_the_same_result_every_time(world):
    _book_transfer(world)
    before = _dump(world.db)
    first = world.db.duplicate_transfer_rows()
    assert len(first) == 1 and world.db.duplicate_transfer_rows() == first
    assert _dump(world.db) == before


def test_without_transfers_the_set_is_empty_and_the_export_is_as_before(world):
    bank = _bank(world, world.a, '2026-03-10', '-50.00')
    _mirror(world, bank, '2026-03-10', '-50.00', 6815, 1800)
    assert world.db.duplicate_transfer_rows() == set()
    assert _pairs(_export(world)) == [('6815', '1800', 'S')]


# ── Der ganze Bestand, nicht der Zeitraum: Überträge über Zeitraumgrenzen ────

def _expense(world, day_text, amount='-10.00'):
    return world.db.insert_booking(day_text, Decimal(amount), coa_id=_coa(world.db, 6815), counter_coa_id=_coa(world.db, 1800),
                                   booking_type='entry')


def test_an_incoming_entry_whose_outgoing_side_is_in_the_previous_period_is_left_out_after_a_confirmation(world):
    """Ausgang 31.3., Eingang 1.4., beide kontiert: Der Eingangs-Satz fällt weg (er steht mit dem Ausgang im März-Stapel). Der
    April-Stapel nennt das und verlangt die Bestätigung; danach fehlt der Eingang dort. Auch der März-Stapel fragt, in anderen Worten:
    Seine Eingangsseite liegt außerhalb – wurde der April schon exportiert, bevor beide Seiten kontiert waren, steht der Übertrag dort
    ebenfalls."""
    _out_bank, out_row, _in_bank, in_row = _book_transfer(world, amount='700.00', day_out='2026-03-31', day_in='2026-04-01')
    _expense(world, '2026-04-15')

    first = _handler(world, '2026-04-01', '2026-04-30')
    assert is_confirm_redirect(first)
    message = message_of(first)
    assert message.startswith('Nicht exportiert: 1 Übertrag zwischen eigenen Konten steht mit seiner Ausgangsseite außerhalb '
                              'des Zeitraums und deshalb nicht in diesem Stapel – 2026-04-01 700,00 EUR. Er gehört in den Stapel '
                              'des Zeitraums seiner Ausgangsseite.')
    assert _stamped(world.db) == set()                                              # nichts exportiert, nichts gestempelt

    april = _export_confirmed(world, ('2026-04-01', '2026-04-30'))
    assert _pairs(april) == [('6815', '1800', 'S')] and in_row not in _stamped(world.db)

    march_first = _handler(world, '2026-03-01', '2026-03-31')                        # der Ausgang steht hier, die Eingangsseite außerhalb
    assert is_confirm_redirect(march_first)
    assert message_of(march_first).startswith(
        'Nicht exportiert: 1 Übertrag steht mit seiner Ausgangsseite in diesem Stapel, seine Eingangsseite liegt außerhalb des '
        'Zeitraums – wurde der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, steht er dort ebenfalls: '
        '2026-03-31 700,00 EUR.')
    march = _export_confirmed(world, ('2026-03-01', '2026-03-31'))
    assert _pairs(march) == [('1810', '1800', 'S')] and out_row in _stamped(world.db)


def test_across_the_year_boundary_both_exports_ask_for_a_confirmation_each_in_its_own_words(world):
    """Eingang 30.12., Ausgang 02.01., beide kontiert: Der Eingangs-Satz fällt weg – der Stapel 2025 sagt, dass der Übertrag mit
    seiner Ausgangsseite in 2026 steht; der Stapel 2026 hat den Ausgang und sagt, dass die Eingangsseite in 2025 liegt."""
    _out_bank, _out_row, _in_bank, in_row = _book_transfer(
        world, amount='800.00', day_out='2026-01-02', day_in='2025-12-30', out_account=world.a, in_account=world.b)
    _expense(world, '2025-12-15')

    first = _handler(world, '2025-01-01', '2025-12-31')
    assert is_confirm_redirect(first) and '2025-12-30 800,00 EUR' in message_of(first)

    second = _handler(world, '2026-01-01', '2026-12-31')
    assert is_confirm_redirect(second) and '2026-01-02 800,00 EUR' in message_of(second)
    assert 'seine Eingangsseite liegt außerhalb des Zeitraums' in message_of(second)

    assert _pairs(_export_confirmed(world, ('2025-01-01', '2025-12-31'))) == [('6815', '1800', 'S')]
    assert _pairs(_export_confirmed(world, ('2026-01-01', '2026-12-31'))) == [('1810', '1800', 'S')]
    assert in_row not in _stamped(world.db)


def test_the_incoming_side_before_the_period_is_named_in_the_period_of_the_outgoing_side_in_the_words_of_that_side(world):
    """Der Stapel, aus dem ein Satz wegfällt (März), sagt, dass der Übertrag mit seiner Ausgangsseite im anderen steht; der Stapel
    mit dem Ausgang (April) sagt, dass die Eingangsseite außerhalb liegt und der Übertrag dort schon stehen kann."""
    _book_transfer(world, amount='700.00', day_out='2026-04-02', day_in='2026-03-31')
    first = _handler(world, '2026-03-01', '2026-03-31')
    assert is_confirm_redirect(first) and 'steht mit seiner Ausgangsseite außerhalb des Zeitraums' in message_of(first)
    second = _handler(world, '2026-04-01', '2026-04-30')
    assert is_confirm_redirect(second) and 'seine Eingangsseite liegt außerhalb des Zeitraums' in message_of(second)
    assert '2026-04-02 700,00 EUR' in message_of(second)
    assert _pairs(_export_confirmed(world, ('2026-04-01', '2026-04-30'))) == [('1810', '1800', 'S')]


def test_booking_the_outgoing_side_late_moves_the_transfer_into_the_stack_of_its_outgoing_side(world):
    """Ausgang 31.3. noch ohne Satz, Eingang 1.4. kontiert: Der Eingangs-Satz steht im April (die satzlose Bewegung ist dort
    gedeckt, der März-Stapel hat den Übertrag nicht). Kontiert der Nutzer den Ausgang später, wandert der Übertrag in den
    März-Stapel – und der April-Stapel meldet, dass sein Eingangs-Satz weggefallen ist."""
    out_bank = _bank(world, world.a, '2026-03-31', '-700.00')
    in_bank = _bank(world, world.b, '2026-04-01', '700.00')
    _mirror(world, in_bank, '2026-04-01', '700.00', 1800, 1810)
    _expense(world, '2026-03-15')

    assert world.db.find_unbooked_bank_movements('2026-03-01', '2026-03-31') == []   # gedeckt
    assert _pairs(_export(world, ('2026-03-01', '2026-03-31'))) == [('6815', '1800', 'S')]
    assert _pairs(_export(world, ('2026-04-01', '2026-04-30'))) == [('1800', '1810', 'H')]

    _mirror(world, out_bank, '2026-03-31', '-700.00', 1810, 1800)
    assert is_confirm_redirect(_handler(world, '2026-03-01', '2026-03-31'))              # die Eingangsseite liegt im April: schon exportiert?
    assert sorted(_pairs(_export_confirmed(world, ('2026-03-01', '2026-03-31')))) == [('1810', '1800', 'S'), ('6815', '1800', 'S')]
    assert is_confirm_redirect(_handler(world, '2026-04-01', '2026-04-30'))


# ── Kontrollrechnung ─────────────────────────────────────────────────────────

def _balances(rows):
    """Saldo je Konto im Stapel in Cent (Soll +): Konto und Gegenkonto, S/H wie in der Datei."""
    totals = {}
    for fields in rows:
        cents = int(Decimal(fields[0].replace(',', '.')) * 100)
        sign = 1 if fields[_SPALTE_SH] == 'S' else -1
        if fields[_SPALTE_KONTO]:
            totals[int(fields[_SPALTE_KONTO])] = totals.get(int(fields[_SPALTE_KONTO]), 0) + sign * cents
        if fields[_SPALTE_GEGENKONTO]:
            totals[int(fields[_SPALTE_GEGENKONTO])] = totals.get(int(fields[_SPALTE_GEGENKONTO]), 0) - sign * cents
    return {number: total for number, total in totals.items() if number in (1800, 1810, 1600)}


def test_control_the_sum_per_bank_account_in_the_stack_is_the_sum_of_its_bank_movements(world):
    """In einem Bestand ohne Bewegung ohne Satz stimmt der Saldo jedes Bankkontos im Stapel mit der Summe seiner Bankbewegungen
    überein – bei Überträgen in beide Richtungen, zur Kasse, mit Teilspiegel im Split, mit WISO-Spiegel und mit mehreren
    Überträgen am selben Tag."""
    movements = {1800: 0, 1810: 0, 1600: 0}

    def bank(account, number, day_text, amount):
        movements[number] += int(Decimal(amount) * 100)
        return _bank(world, account, day_text, amount)

    o = bank(world.a, 1800, '2026-02-03', '-300.00'); _mirror(world, o, '2026-02-03', '-300.00', 1810, 1800)        # A → B
    i = bank(world.b, 1810, '2026-02-03', '300.00'); _mirror(world, i, '2026-02-03', '300.00', 1800, 1810)
    o = bank(world.b, 1810, '2026-02-05', '-300.00'); _mirror(world, o, '2026-02-05', '-300.00', 1800, 1810)       # B → A, gleicher Betrag
    i = bank(world.a, 1800, '2026-02-05', '300.00'); _mirror(world, i, '2026-02-05', '300.00', 1810, 1800)
    o = bank(world.a, 1800, '2026-02-10', '-200.00'); _mirror(world, o, '2026-02-10', '-200.00', 1600, 1800)       # Abhebung
    i = bank(world.cash, 1600, '2026-02-10', '200.00'); _mirror(world, i, '2026-02-10', '200.00', 1800, 1600)
    o = bank(world.a, 1800, '2026-02-12', '-50.00')                                                                # Split: Aufwand + Übertrag
    world.db.insert_booking('2026-02-12', Decimal('-30.00'), coa_id=_coa(world.db, 6815), counter_coa_id=_coa(world.db, 1800),
                            booking_type='entry', parent_booking_id=o)
    _mirror(world, o, '2026-02-12', '-20.00', 1810, 1800)
    i = bank(world.b, 1810, '2026-02-13', '20.00'); _mirror(world, i, '2026-02-13', '20.00', 1800, 1810)
    o = bank(world.a, 1800, '2026-02-20', '-40.00'); _mirror(world, o, '2026-02-20', '-40.00', 1810, 1800)         # nur einseitig … beidseitig
    i = bank(world.b, 1810, '2026-02-21', '40.00'); _mirror(world, i, '2026-02-21', '40.00', 1800, 1810)
    o = bank(world.a, 1800, '2026-02-24', '-60.00'); _mirror(world, o, '2026-02-24', '60.00', 1810, 1800, form='mirror_coa')   # WISO-Lesart
    i = bank(world.b, 1810, '2026-02-25', '60.00'); _mirror(world, i, '2026-02-25', '60.00', 1810, 1800, form='mirror_coa')
    for _ in range(3):                                                                                              # drei gleich hohe am selben Tag
        o = bank(world.a, 1800, '2026-02-26', '-25.00'); _mirror(world, o, '2026-02-26', '-25.00', 1810, 1800)
        i = bank(world.b, 1810, '2026-02-26', '25.00'); _mirror(world, i, '2026-02-26', '25.00', 1800, 1810)

    assert world.db.find_unbalanced_splits('2026-02-01', '2026-02-28') == []
    assert world.db.find_unbooked_bank_movements('2026-02-01', '2026-02-28') == []
    rows = _export(world, ('2026-02-01', '2026-02-28'))
    assert _balances(rows) == movements


# ── Regression: ohne Überträge und ohne Bewegungen ohne Satz bleibt alles, wie es war ──

def test_a_period_without_transfers_and_without_free_movements_exports_the_rows_the_export_function_writes(world):
    """Der Handler legt nichts zusammen und lässt nichts weg, wenn es keine Überträge gibt: Die Datenzeilen sind die, die
    ``export_to_datev`` für alle Sätze des Zeitraums schreibt – und dieselben Sätze bekommen ihr Steuerdatum."""
    db, c = world.db, (lambda number: _coa(world.db, number))
    bank = _bank(world, world.a, '2026-03-02', '-119.00')
    db.insert_booking('2026-03-02', Decimal('-119.00'), coa_id=c(6815), counter_coa_id=c(1800), booking_type='entry',
                      parent_booking_id=bank, tax_rate=0.19, tax_amount=Decimal('-19.00'), text='Büro;"x"\nZeile',
                      document_number='RE 1')
    bank = _bank(world, world.b, '2026-03-05', '238.00')
    db.insert_booking('2026-03-05', Decimal('238.00'), coa_id=c(4400), counter_coa_id=c(1810), booking_type='entry',
                      parent_booking_id=bank, tax_rate=0.19, tax_amount=Decimal('38.00'))
    db.insert_booking('2026-03-07', Decimal('-50.00'), coa_id=c(6815), counter_coa_id=c(1600), booking_type='entry',
                      tax_rate=0.07, tax_amount=Decimal('-3.27'))
    db.insert_booking('2026-03-08', Decimal('100.00'), coa_id=c(1810), counter_coa_id=c(1800), booking_type='entry')   # eigenständiger Spiegel
    db.insert_booking('2026-03-09', Decimal('30.00'), coa_id=c(6220), counter_coa_id=c(800), booking_type='entry',
                      entry_form='transfer')
    expected_bookings = db.fetch_bookings_range(*PERIOD)
    expected, expected_ids = datev.export_to_datev(
        expected_bookings, db.get_coa_id_to_number_map(), *PERIOD, liquid_coa_ids=db.get_payment_coa_ids(),
        waiting_numbers=db.get_waiting_account_numbers(), invoices=db.get_datev_invoice_data(),
        consultant_no=None, client_no=None)

    csv_bytes, _name = _handler(world, *PERIOD)

    def data_lines(raw):
        return [line for line in raw.decode('cp1252').split('\r\n') if line][2:]
    assert data_lines(csv_bytes) == data_lines(expected) and len(data_lines(expected)) == 5
    assert _stamped(db) == set(expected_ids)


# ── Leistung über die Datenbank ──────────────────────────────────────────────

def _bulk_transfers(world, per_side, year=2026):
    """``per_side`` gleich hohe Überträge A → B, beidseitig kontiert, über ein Jahr verteilt – direkt per SQL (geprüft wird die
    Rechnung, nicht der Schreibweg)."""
    db = world.db
    coa_a, coa_b = _coa(db, 1800), _coa(db, 1810)
    con = sqlite3.connect(db.db_name)
    try:
        next_id = con.execute('SELECT COALESCE(MAX(ID), 0) FROM Bookings').fetchone()[0] + 1
        rows = []
        for i in range(per_side):
            when = (date(year, 1, 1) + timedelta(days=i % 365)).isoformat()
            first = next_id + 4 * i
            rows += [(first, when, world.a, None, None, -1_000_000, 'bank', None, None),
                     (first + 1, when, None, coa_b, coa_a, -1_000_000, 'entry', first, 'mirror_counter'),
                     (first + 2, when, world.b, None, None, 1_000_000, 'bank', None, None),
                     (first + 3, when, None, coa_a, coa_b, 1_000_000, 'entry', first + 2, 'mirror_counter')]
        con.executemany('INSERT INTO Bookings (ID, DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, BookingType, '
                        'ParentBooking_ID, EntryForm, Currency) VALUES (?,?,?,?,?,?,?,?,?,"EUR")', rows)
        con.commit()
    finally:
        con.close()


def test_twenty_thousand_transfers_per_side_are_counted_by_the_database_methods_in_seconds(world):
    """Eine Gruppe mit 20 000 Ausgängen und 20 000 Eingängen (ein Jahr, ein einziger Abschnitt): die Rechnung bleibt linear –
    Laden, Zählen und Hinweise in wenigen Sekunden."""
    _bulk_transfers(world, 20_000)
    started = time.perf_counter()
    check = world.db.check_transfers('2026-01-01', '2026-12-31')
    elapsed = time.perf_counter() - started
    assert len(check.omitted) == 20_000 and check.unbooked == [] and check.elsewhere == []
    assert elapsed < 15, f'{elapsed:.1f} s für 80 000 Zeilen'
    assert len(world.db.duplicate_transfer_rows()) == 20_000
    assert world.db.find_unbooked_bank_movements('2026-01-01', '2026-12-31') == []


def test_twenty_thousand_possibly_doubled_transfers_are_named_by_the_database_method_in_seconds(world):
    """Dieselbe Gruppe, dazu ein Ausgang ohne Satz im Umfeld: Nichts fällt weg, 20 000 Eingangs-Sätze stehen im dritten Hinweis."""
    _bulk_transfers(world, 20_000)
    _bank(world, world.a, '2026-06-01', '-100.00')                                       # derselbe Betrag wie die Überträge, ohne Satz
    started = time.perf_counter()
    check = world.db.check_transfers('2026-01-01', '2026-12-31')
    elapsed = time.perf_counter() - started
    assert check.omitted == set() and len(check.doubtful) == 20_000 and len(check.unbooked) == 1
    assert elapsed < 15, f'{elapsed:.1f} s für 80 000 Zeilen'
    started = time.perf_counter()
    result = _handler(world, '2026-01-01', '2026-12-31')
    assert is_confirm_redirect(result) and '20000 Überträge zwischen eigenen Konten stehen möglicherweise doppelt' in message_of(result)
    assert time.perf_counter() - started < 30


def test_the_export_of_twenty_thousand_transfers_per_side_leaves_out_exactly_the_incoming_side_in_seconds(world):
    """Über den Handler: Prüfung der Splits, Hinweise, Zählen, Export von 20 000 Zeilen – die Eingangsseite fehlt, nichts sonst."""
    _bulk_transfers(world, 20_000)
    started = time.perf_counter()
    rows = _rows_of(_handler(world, '2026-01-01', '2026-12-31'))
    elapsed = time.perf_counter() - started
    assert len(rows) == 20_000 and {_pairs([fields])[0] for fields in rows} == {('1810', '1800', 'S')}
    assert elapsed < 30, f'{elapsed:.1f} s'
