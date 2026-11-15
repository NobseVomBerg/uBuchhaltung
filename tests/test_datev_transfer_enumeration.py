# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""DATEV-Export: Die Zählregel an allen kleinen Beständen (2.3.4).

Einzelne Fälle zeigen nicht, ob die Zählregel (``db.matching.settle_transfers``) irgendwo einen Übertrag verliert oder doppelt stehen
lässt. Diese Datei zählt deshalb auf, was sich in kleinen Beständen überhaupt ergeben kann, und prüft Eigenschaften statt Fälle.

Ein Bestand besteht aus ein bis drei Überträgen zwischen zwei oder drei eigenen Konten, alle über denselben Betrag. Jeder Übertrag
hat eine Ausgangs- und eine Eingangsbewegung (vollständige Kontoauszüge: Der Eingang liegt ``delay`` Tage nach dem Ausgang, negativ
davor); jede Bewegung ist kontiert (Spiegel-Satz), nicht kontiert oder – in den Aufzählungen „anders kontiert“ – mit einem Satz in
anderer Form kontiert (eine Zahlung unter dem Ausgang: ``TransferOther``), und neben dem Übertrag kann ein eigenständiger Spiegel
stehen (WISO-Import). Alle Muster, alle Verzögerungen und alle Abstände der Überträge zueinander, die an den Grenzen des Fensters
(7 Tage) und des Umfelds (14 Tage) etwas ändern, werden durchgespielt. Für jeden Bestand fragt ``judge``, was im Stapel steht:

- **verloren**: ein Übertrag mit Satz oder Spiegel, von dem nichts im Stapel steht (alle seine Zeilen fallen weg)
- **doppelt**: ein Übertrag mit mindestens zwei Zeilen im Stapel; **genannt**, wenn der dritte Hinweis eine davon nennt – verlangt
  wird das nur, wenn eine der Zeilen ein Spiegel-Satz einer Bankbewegung ist: Zwei anders kontierte Seiten (zwei Zahlungssätze) oder
  ein eigenständiger Spiegel neben einem Zahlungssatz hat die Regel keinen Satz zu nennen
- **ungemeldet**: ein Übertrag ganz ohne Satz, dessen beide Bewegungen als gedeckt gelten – der erste Hinweis schweigt

Bewiesen wird, was die Zählregel verspricht – ``Verzögerung ≤ 14 Tage`` (das Umfeld): kein Übertrag geht verloren, auch nicht mit
Hinweis, jede Doppelung wird genannt, und ein ganz unkontierter Übertrag wird gemeldet (zwischen kontierten Überträgen, neben einem
Spiegel, bei Verzögerung über das Fenster). Die Ausnahmen, die es gibt, stehen als eigene Tests mit ihrem Beispiel: eine Verzögerung
über das Umfeld hinaus und – nur mit drei Überträgen – ein ganz unkontierter Übertrag zwischen zwei halb kontierten, deren kontierte
Seite jenseits des Fensters von der satzlosen liegt. Wer die Regel verbessert, ändert diese Tests bewusst.

Alle Werte sind erfundene Testdaten.
"""
import datetime
import itertools
from collections import Counter
from typing import NamedTuple

import pytest

from db.matching import (
    TRANSFER_DAY_WINDOW, TRANSFER_NEIGHBOUR_DAYS, TransferLeg, TransferMirror, TransferOther, settle_transfers)

EUR = 10_000
START = datetime.date(2026, 3, 1)
DAY = datetime.timedelta(days=1)

FREE, BOOKED, OTHER = 0, 1, 2         # Zustand einer Bewegung: ohne Satz, mit Spiegel-Satz, mit einem Satz in anderer Form (False/True gelten wie 0/1)


class Transfer(NamedTuple):
    """Ein Übertrag: von ``src`` nach ``dst``, Ausgang am Tag ``day`` (ab dem 1.3.2026), Eingang ``delay`` Tage danach."""
    src: int
    dst: int
    day: int
    delay: int
    out_booked: int
    in_booked: int
    mirror: bool = False


class Verdict(NamedTuple):
    lost: tuple          # Übertrag mit Satz oder Spiegel, von dem nichts im Stapel steht
    doubled: tuple       # Übertrag mit mindestens zwei Zeilen im Stapel
    unnamed: tuple       # davon die, deren Zeilen der dritte Hinweis nicht nennt
    unreported: tuple    # Übertrag ganz ohne Satz, dessen Bewegungen alle als gedeckt gelten
    reported: tuple      # Bankbewegungen ohne Satz, die der erste Hinweis meldet (IDs)
    doubtful: tuple      # Sätze, die der dritte Hinweis nennt (IDs)


def build(transfers):
    """Aus den Überträgen die Beine, Spiegel und anders kontierten Bewegungen der Regel; dazu je Übertrag seine Zeilen im Stapel, seine
    Bewegungen ohne Satz und die IDs der Sätze, die Spiegel-Sätze einer Bankbewegung sind. Bank-IDs zählen ab 1, Satz-IDs ab 1001."""
    legs, mirrors, others, rows, free, leg_rows = [], [], [], [], [], set()
    bank_id, entry_id = 0, 1000
    for transfer in transfers:
        out_day = START + transfer.day * DAY
        in_day = out_day + transfer.delay * DAY
        own_rows, own_free = [], []
        for state, account, outgoing, day, other in ((transfer.out_booked, transfer.src, True, out_day, transfer.dst),
                                                       (transfer.in_booked, transfer.dst, False, in_day, transfer.src)):
            bank_id += 1
            if state == OTHER:
                entry_id += 1
                others.append(TransferOther(bank_id, account, outgoing, EUR, day))
                own_rows.append(entry_id)
            elif state:
                entry_id += 1
                legs.append(TransferLeg(bank_id, account, outgoing, EUR, day, other, entry_id, day))
                own_rows.append(entry_id)
                leg_rows.add(entry_id)
            else:
                legs.append(TransferLeg(bank_id, account, outgoing, EUR, day))
                own_free.append(bank_id)
        if transfer.mirror:
            entry_id += 1
            mirrors.append(TransferMirror(entry_id, out_day, transfer.src, transfer.dst, EUR))
            own_rows.append(entry_id)
        rows.append(own_rows)
        free.append(own_free)
    return legs, mirrors, others, rows, free, leg_rows


def judge(transfers):
    legs, mirrors, others, rows, free, leg_rows = build(transfers)
    result = settle_transfers(legs, mirrors, others)
    kept = [[row for row in own if row not in result.omitted] for own in rows]
    doubled = tuple(k for k, own in enumerate(kept) if len(own) >= 2)
    return Verdict(
        lost=tuple(k for k, own in enumerate(rows) if own and not kept[k]),
        doubled=doubled,
        unnamed=tuple(k for k in doubled if any(row in leg_rows for row in kept[k])
                      and not any(row in result.doubtful for row in kept[k])),
        unreported=tuple(k for k, own in enumerate(rows) if not own and free[k] and all(b in result.covered for b in free[k])),
        reported=tuple(b for own in free for b in own if b not in result.covered),
        doubtful=tuple(sorted(result.doubtful)))


# ── die Aufzählungen ─────────────────────────────────────────────────────────

PATTERNS = [(True, True), (True, False), (False, True), (False, False)]
WITH_OTHERS = [(out, into) for out in (BOOKED, FREE, OTHER) for into in (BOOKED, FREE, OTHER)]       # alle neun Zustände der beiden Seiten


def inventories(accounts, days, delays, mirrors=(False,), max_transfers=2, first_delays=None, patterns=PATTERNS):
    """Alle Bestände mit höchstens ``max_transfers`` Überträgen. Der erste Übertrag ist immer 1 → 2 am Tag 0 – jeder Bestand lässt
    sich durch Verschieben der Tage und Umbenennen der Konten so schreiben (der früheste Übertrag zuerst); die übrigen sind
    beliebig, ihre Reihenfolge gilt nicht."""
    first_delays = delays if first_delays is None else first_delays
    first = [Transfer(1, 2, 0, delay, out, into, mirror)
             for delay in first_delays for out, into in patterns for mirror in mirrors]
    pool = [Transfer(src, dst, day, delay, out, into, mirror)
            for src in accounts for dst in accounts if src != dst
            for day in days for delay in delays for out, into in patterns for mirror in mirrors]
    for head in first:
        yield [head]
        if max_transfers >= 2:
            for second in pool:
                yield [head, second]
        if max_transfers >= 3:
            for second, third in itertools.combinations_with_replacement(pool, 2):
                yield [head, second, third]


class Tally:
    """Zählt die Befunde einer Aufzählung und merkt sich je Befund den kleinsten Bestand als Beispiel."""

    def __init__(self, stock):
        self.total, self.counts, self.example = 0, Counter(), {}
        for transfers in stock:
            self.total += 1
            verdict = judge(transfers)
            for name in ('lost', 'doubled', 'unnamed', 'unreported'):
                if getattr(verdict, name):
                    self.counts[name] += 1
                    if name not in self.example or len(transfers) < len(self.example[name]):
                        self.example[name] = transfers


ALL_DAYS = list(range(0, 25))
SETS = {
    # (Konten, Tage des zweiten und dritten Übertrags, Verzögerungen, mit eigenständigem Spiegel?, höchstens so viele Überträge)
    'zwei Konten, zwei Überträge, Verzögerung 0–9, Tage 0–24': dict(
        accounts=[1, 2], days=ALL_DAYS, delays=list(range(10)), max_transfers=2),
    'zwei Konten, zwei Überträge, mit Spiegel, Verzögerung 0/2/5/7/8/9': dict(
        accounts=[1, 2], days=[0, 1, 2, 3, 4, 5, 7, 8, 9, 12, 14, 15, 16, 20, 24], delays=[0, 2, 5, 7, 8, 9],
        mirrors=(False, True), max_transfers=2),
    'zwei Konten, drei Überträge, Verzögerung 0/7/9': dict(
        accounts=[1, 2], days=[0, 5, 8, 15], delays=[0, 7, 9], max_transfers=3),
    'drei Konten, drei Überträge, Verzögerung 0/8': dict(
        accounts=[1, 2, 3], days=[0, 8], delays=[0, 8], max_transfers=3),
    'zwei Konten, zwei Überträge, Eingang auch vor dem Ausgang (−9…9)': dict(
        accounts=[1, 2], days=list(range(-16, 17)), delays=[-9, -8, -7, 0, 7, 8, 9], max_transfers=2,
        first_delays=[-9, -8, -7, 0, 7, 8, 9]),
    # jede Seite auch „anders kontiert“ (neun Zustände je Übertrag)
    'zwei Konten, zwei Überträge, auch anders kontiert, Verzögerung 0/3/8/14': dict(
        accounts=[1, 2], days=[0, 1, 3, 5, 7, 8, 9, 14, 15, 20], delays=[0, 3, 8, 14], max_transfers=2, patterns=WITH_OTHERS),
    'zwei Konten, zwei Überträge, auch anders kontiert und mit Spiegel, Verzögerung 0/8': dict(
        accounts=[1, 2], days=[0, 3, 8, 15], delays=[0, 8], mirrors=(False, True), max_transfers=2, patterns=WITH_OTHERS),
    'zwei Konten, drei Überträge, auch anders kontiert, Verzögerung 0/8': dict(
        accounts=[1, 2], days=[0, 8], delays=[0, 8], max_transfers=3, patterns=WITH_OTHERS),
}
INSIDE_THE_WINDOW = [
    dict(accounts=[1, 2], days=[0, 4, 8, 15], delays=[0, 3, 7], max_transfers=3),
    dict(accounts=[1, 2, 3], days=[0, 8], delays=[0, 7], max_transfers=3),
]
BEYOND_THE_NEIGHBOURHOOD = dict(
    accounts=[1, 2], days=[0, 1, 2, 3, 5, 7, 8, 10, 13, 14, 15, 16, 20, 22, 29, 30, 31, 36, 40], delays=[0, 7, 14, 15, 16, 21, 28],
    max_transfers=2)


@pytest.fixture(scope='module')
def tallies():
    return {name: Tally(inventories(**arguments)) for name, arguments in SETS.items()}


def test_the_enumerations_are_as_large_as_stated(tallies):
    """Die Zahl der Bestände steht fest – eine versehentlich leere oder verkürzte Aufzählung bewiese nichts."""
    assert [tally.total for tally in tallies.values()] == [80_040, 69_168, 57_036, 38_024, 51_772, 25_956, 10_404, 48_618]
    assert TRANSFER_DAY_WINDOW == 7 and TRANSFER_NEIGHBOUR_DAYS == 14


# ── was die Zählregel verspricht ─────────────────────────────────────────────

def test_no_transfer_is_ever_lost_silently_or_otherwise_while_the_delay_stays_within_fourteen_days(tallies):
    """Weder ein Übertrag, dessen Satz wegfällt, ohne dass ein anderer Satz ihn trägt, noch einer mit Hinweis: Der Eingangs-Satz fällt
    nur weg, wenn im Umfeld jeder Ausgang seinen Satz trägt – dann steht jeder Übertrag mit seiner Ausgangsseite im Stapel."""
    for name, tally in tallies.items():
        assert tally.counts['lost'] == 0, (name, tally.example.get('lost'))


def test_every_doubling_within_fourteen_days_is_named_by_the_third_hint(tallies):
    """Steht ein Übertrag mit zwei Zeilen im Stapel, nennt der dritte Hinweis eine davon: den Eingangs-Satz, in dessen Umfeld sein
    Ausgang liegt, oder den eigenständigen Spiegel neben der kontierten Bankbewegung."""
    for name, tally in tallies.items():
        assert tally.counts['unnamed'] == 0, (name, tally.example.get('unnamed'))
        assert tally.counts['doubled'] > 0                                                  # die Aufzählung enthält Doppelungen


THREE_ACCOUNTS = 'drei Konten, drei Überträge, Verzögerung 0/8'
OTHERS_WITH_MIRRORS = 'zwei Konten, zwei Überträge, auch anders kontiert und mit Spiegel, Verzögerung 0/8'


def test_an_unbooked_transfer_is_always_reported_with_two_accounts_whatever_the_delay_and_the_mirrors(tallies):
    """Ganz ohne Satz: Mindestens eine seiner Bewegungen meldet der erste Hinweis – auch zwischen kontierten Überträgen, neben einem
    eigenständigen Spiegel, neben anders kontierten Bewegungen und bei Verzögerung über das Fenster (die Deckung zählt die Sätze des
    Umfelds mit). Ausnahmen: ``THREE_ACCOUNTS`` und ``OTHERS_WITH_MIRRORS`` – siehe die benannten Ausnahmen unten."""
    for name, tally in tallies.items():
        if name not in (THREE_ACCOUNTS, OTHERS_WITH_MIRRORS):
            assert tally.counts['unreported'] == 0, (name, tally.example.get('unreported'))


@pytest.mark.parametrize('arguments', INSIDE_THE_WINDOW, ids=['zwei-Konten', 'drei-Konten'])
def test_an_unbooked_transfer_is_always_reported_while_the_delay_stays_inside_the_window(arguments):
    """Mit Verzögerung bis zum Fenster gilt es auch mit drei Konten, und es gibt weder Verlust noch ungenannte Doppelung."""
    tally = Tally(inventories(**arguments))
    assert tally.total > 30_000 and tally.counts['unreported'] == 0, tally.example.get('unreported')
    assert tally.counts['lost'] == 0 and tally.counts['unnamed'] == 0


# ── der einzelne Übertrag: genau das, was man erwartet ───────────────────────

def _single(out, into, delay, mirror=False):
    return judge([Transfer(1, 2, 0, delay, out, into, mirror)])


@pytest.mark.parametrize('delay', range(-14, 15))
def test_an_isolated_transfer_booked_on_both_sides_is_merged_inside_the_window_and_named_beyond_it(delay):
    verdict = _single(True, True, delay)
    assert verdict.lost == () and verdict.reported == ()
    if abs(delay) <= TRANSFER_DAY_WINDOW:
        assert verdict.doubled == () and verdict.doubtful == ()
    else:
        assert verdict.doubled == (0,) and verdict.unnamed == () and verdict.doubtful == (1002,)


@pytest.mark.parametrize('out, into', [(True, False), (False, True)], ids=['nur-der-Ausgang', 'nur-der-Eingang'])
@pytest.mark.parametrize('delay', [-7, -3, 0, 3, 7])
def test_an_isolated_transfer_booked_on_one_side_is_one_row_and_asks_for_nothing_inside_the_window(out, into, delay):
    verdict = _single(out, into, delay)
    assert verdict == Verdict(lost=(), doubled=(), unnamed=(), unreported=(), reported=(), doubtful=())


@pytest.mark.parametrize('out, into, free_id', [(True, False, 2), (False, True, 1)], ids=['nur-der-Ausgang', 'nur-der-Eingang'])
@pytest.mark.parametrize('delay', [-14, -8, 8, 14])
def test_an_isolated_transfer_booked_on_one_side_reports_its_free_movement_beyond_the_window(out, into, free_id, delay):
    """Die beiden Bewegungen liegen in zwei Abschnitten, und die Deckung zählt je Abschnitt: Die Bewegung ohne Satz wird gemeldet,
    obwohl ihr Übertrag im Stapel steht (ein Falschalarm; verloren geht nichts, doppelt steht nichts)."""
    verdict = _single(out, into, delay)
    assert verdict == Verdict(lost=(), doubled=(), unnamed=(), unreported=(), reported=(free_id,), doubtful=())


@pytest.mark.parametrize('delay', [-14, -8, -7, 0, 7, 8, 14])
def test_an_isolated_transfer_without_any_booking_reports_both_movements(delay):
    verdict = _single(False, False, delay)
    assert verdict.reported == (1, 2) and verdict.lost == () and verdict.doubled == () and verdict.unreported == ()


@pytest.mark.parametrize('delay', [-14, -8, -7, 0, 7, 8, 14])
@pytest.mark.parametrize('out, into, named', [
    pytest.param(OTHER, BOOKED, 1002, id='Ausgang-anders-kontiert-Eingang-mit-Spiegel'),
    pytest.param(BOOKED, OTHER, 1001, id='Ausgang-mit-Spiegel-Eingang-anders-kontiert'),
])
def test_a_transfer_with_one_side_booked_otherwise_and_the_other_as_a_mirror_stands_twice_and_the_mirror_is_named(out, into, named, delay):
    """Die eine Seite als Zahlungssatz, die andere als Spiegel: Beide Zeilen stehen im Stapel (die Regel kann sie nicht zusammenlegen –
    der Zahlungssatz ist kein Spiegel), und der Spiegel-Satz steht im dritten Hinweis, solange der Abstand im Umfeld liegt."""
    verdict = _single(out, into, delay)
    assert verdict.lost == () and verdict.doubled == (0,) and verdict.unnamed == () and verdict.doubtful == (named,)


@pytest.mark.parametrize('delay', [-14, 0, 14])
def test_a_transfer_with_both_sides_booked_otherwise_asks_for_nothing_the_rule_has_no_entry_to_name(delay):
    verdict = _single(OTHER, OTHER, delay)
    assert verdict == Verdict(lost=(), doubled=(0,), unnamed=(), unreported=(), reported=(), doubtful=())


@pytest.mark.parametrize('out, into, free_id', [(OTHER, FREE, 2), (FREE, OTHER, 1)], ids=['Ausgang-anders-kontiert', 'Eingang-anders-kontiert'])
@pytest.mark.parametrize('delay', [-3, 0, 3])
def test_the_free_side_of_a_transfer_booked_otherwise_on_the_other_side_is_reported(out, into, free_id, delay):
    """Eine anders kontierte Seite deckt die freie nicht – nur ein Spiegel-Satz der Gegenseite tut das. Die freie Bewegung wird gemeldet
    (ein Falschalarm, wenn die Seiten zusammengehören; verloren geht nichts)."""
    verdict = _single(out, into, delay)
    assert verdict.reported == (free_id,) and verdict.lost == () and verdict.unnamed == ()


@pytest.mark.parametrize('out, into, doubled', [
    pytest.param(False, False, False, id='Spiegel-allein'),
    pytest.param(True, False, True, id='Spiegel-und-kontierter-Ausgang'),
    pytest.param(False, True, True, id='Spiegel-und-kontierter-Eingang'),
    pytest.param(True, True, True, id='Spiegel-und-beide-Seiten-kontiert'),
])
def test_an_isolated_transfer_with_a_standalone_mirror_is_one_row_or_named_as_possibly_doubled(out, into, doubled):
    """Der Spiegel deckt beide Bewegungen ohne Satz; kommt eine kontierte Bankbewegung dazu, steht der Übertrag doppelt im Stapel –
    der dritte Hinweis nennt den Spiegel."""
    verdict = _single(out, into, 2, mirror=True)
    mirror_id = 1001 + out + into                                                          # nach den Sätzen der Bankbewegungen
    assert verdict.lost == () and verdict.reported == () and verdict.unnamed == ()
    assert (verdict.doubled == (0,)) == doubled and (mirror_id in verdict.doubtful) == doubled


# ── die Ausnahmen, ausdrücklich ──────────────────────────────────────────────

def test_known_exception_a_delay_beyond_the_neighbourhood_doubles_a_transfer_booked_otherwise_silently():
    """Der Zahlungssatz unter dem Ausgang am Tag 0, der Eingangs-Satz 15 Tage danach: Der Ausgang liegt außerhalb des Umfelds – beide
    Zeilen stehen im Stapel, und niemand sagt es (wie bei zwei Spiegeln)."""
    verdict = judge([Transfer(1, 2, 0, TRANSFER_NEIGHBOUR_DAYS + 1, OTHER, BOOKED)])
    assert verdict.doubled == (0,) and verdict.unnamed == (0,) and verdict.doubtful == ()


def test_known_exception_a_standalone_mirror_next_to_bank_movements_booked_otherwise_still_covers_a_free_transfer():
    """Ein Übertrag, dessen beide Bankbewegungen als Zahlungssätze kontiert sind, dazu sein eigenständiger Spiegel (drei Erfassungen
    desselben Übertrags), und ein zweiter, ganz unkontierter Übertrag gleicher Höhe am selben Tag: Der Spiegel deckt je Seite eine Bewegung
    ohne Satz – die anders kontierten Bewegungen zehren ihn nicht auf (Absicht der Regel: ``s`` bleibt, wie es war) –, und der erste
    Hinweis schweigt zum zweiten Übertrag."""
    verdict = judge([Transfer(1, 2, 0, 0, False, False), Transfer(1, 2, 0, 0, OTHER, OTHER, True)])
    assert verdict.unreported == (0,) and verdict.reported == () and verdict.lost == ()


def test_the_enumeration_with_booked_otherwise_and_mirrors_finds_these_silent_free_transfers_and_nothing_else(tallies):
    tally = tallies[OTHERS_WITH_MIRRORS]
    assert tally.counts['unreported'] == 13 and tally.counts['lost'] == 0 and tally.counts['unnamed'] == 0
    assert any(transfer.mirror and OTHER in (transfer.out_booked, transfer.in_booked) for transfer in tally.example['unreported'])


def test_known_exception_a_delay_beyond_the_neighbourhood_doubles_silently():
    """Der Eingang 15 Tage nach dem Ausgang: kein Ausgang mit Satz im Umfeld – beide Sätze stehen im Stapel, und niemand sagt es.
    Die Zählregel kennt nur das Umfeld (14 Tage); eine längere Verzögerung ist ihre Grenze."""
    verdict = judge([Transfer(1, 2, 0, TRANSFER_NEIGHBOUR_DAYS + 1, True, True)])
    assert verdict.doubled == (0,) and verdict.unnamed == (0,) and verdict.doubtful == () and verdict.reported == ()


def test_known_exception_a_delay_beyond_the_neighbourhood_can_lose_a_transfer_and_the_free_movement_is_reported():
    """Übertrag 1: Ausgang ohne Satz am Tag 0, Eingang mit Satz am Tag 15. Übertrag 2: Ausgang mit Satz am Tag 15, Eingang mit Satz am
    Tag 29. Der Ausgang ohne Satz liegt 15 Tage vor dem Abschnitt „Tag 15“ – außerhalb des Umfelds: Der Eingangs-Satz von Übertrag 1
    fällt weg, Übertrag 1 steht nirgends im Stapel. Der erste Hinweis meldet den Ausgang ohne Satz (nicht still)."""
    verdict = judge([Transfer(1, 2, 0, 15, False, True), Transfer(1, 2, 15, 14, True, True)])
    assert verdict.lost == (0,) and verdict.reported == (1,)


def test_known_exception_a_delay_beyond_the_neighbourhood_can_lose_a_transfer_silently():
    """Dasselbe mit einem Eingang, der 16 Tage VOR seinem Ausgang liegt, und einem zweiten Übertrag, dessen Eingangs-Satz den Ausgang
    ohne Satz als „gedeckt“ erscheinen lässt: Übertrag 1 geht verloren, kein Hinweis. Außerhalb des Umfelds trägt die Regel nicht."""
    verdict = judge([Transfer(2, 1, 21, -16, False, True), Transfer(2, 1, 5, 16, True, True)])
    assert verdict.lost == (0,) and verdict.reported == ()


def test_known_exception_beyond_the_neighbourhood_the_enumeration_finds_losses_and_doublings():
    """Die Aufzählung mit Verzögerungen bis 28 Tage zeigt, wo die Regel endet: Verluste (hier mit Hinweis) und stille Doppelungen."""
    tally = Tally(inventories(**BEYOND_THE_NEIGHBOURHOOD))
    assert tally.counts['lost'] > 0 and tally.counts['unnamed'] > 0
    for transfers in (tally.example['lost'], tally.example['unnamed']):
        assert max(abs(transfer.delay) for transfer in transfers) > TRANSFER_NEIGHBOUR_DAYS


def test_an_unbooked_transfer_between_booked_ones_is_reported_whatever_their_entries_cover_nearby():
    """Ein ganz unkontierter Übertrag (Ausgang am 8., Eingang am 16.) zwischen zwei kontierten, deren Bewegungen acht Tage auseinanderliegen:
    Die Sätze der Nachbarn decken ihn nicht – die Deckung zählt die Ausgänge und Eingänge mit Satz im Umfeld mit. Beide Bewegungen
    (Bank-IDs 3 und 4) werden gemeldet."""
    verdict = judge([Transfer(1, 2, 0, 8, True, True), Transfer(1, 2, 8, 8, False, False), Transfer(1, 2, 20, 8, True, True)])
    assert verdict.unreported == () and verdict.reported == (3, 4) and verdict.lost == () and verdict.unnamed == ()


def test_a_standalone_mirror_with_both_sides_booked_does_not_cover_an_unbooked_transfer():
    """Spiegel UND beide Seiten kontiert (eine dreifache Erfassung) neben einem ganz unkontierten Übertrag: Der Spiegel neben kontierten
    Bankbewegungen zählt nicht als Überschuss der Deckung – die beiden Bewegungen ohne Satz (Bank-IDs 3 und 4) werden gemeldet."""
    verdict = judge([Transfer(1, 2, 0, 0, True, True, True), Transfer(1, 2, 0, 0, False, False)])
    assert verdict.unreported == () and verdict.reported == (3, 4) and verdict.lost == () and verdict.unnamed == ()


def test_known_exception_an_unbooked_transfer_between_two_half_booked_ones_beyond_the_window_stays_unreported():
    """Übertrag 1: Ausgang ohne Satz am Tag 0, Eingang MIT Satz am Tag 8 (acht Tage – jenseits des Fensters). Übertrag 3: Ausgang MIT
    Satz am Tag 29, Eingang ohne Satz am Tag 37. Dazwischen Übertrag 2 ganz ohne Satz (Tag 14 und 23). Der Eingangs-Satz von Übertrag 1
    liegt im Abschnitt von Übertrag 2 und deckt dort dessen Ausgang, der Ausgangs-Satz von Übertrag 3 deckt dessen Eingang: Die
    Deckung hält die Sätze der Nachbarn für die Gegenseite von Übertrag 2, und dessen Bewegungen werden nicht gemeldet – gemeldet
    werden dafür die satzlosen Seiten der beiden halb kontierten Überträge (Bank-IDs 1 und 6), ein Falschalarm. Es kommt nur mit drei
    Überträgen vor, deren kontierte Seite jenseits des Fensters von der satzlosen liegt; der Export fragt dann trotzdem nach der
    Bestätigung (Hinweis 1)."""
    verdict = judge([Transfer(1, 2, 0, 8, False, True), Transfer(1, 2, 14, 9, False, False), Transfer(1, 2, 29, 8, True, False)])
    assert verdict.unreported == (1,) and verdict.reported == (1, 6) and verdict.lost == () and verdict.unnamed == ()


def test_known_exception_the_same_through_two_groups_with_a_third_account():
    """Dasselbe über zwei Gruppen: Der Eingangs-Satz von Übertrag 1 (1 → 2, Eingang am Tag 8) deckt den Ausgang von Übertrag 2 in der
    Gruppe 1 → 2, der Ausgangs-Satz von Übertrag 3 (3 → 2, Ausgang am Tag 8) dessen Eingang in der Gruppe 3 → 2 – die Bewegung ohne
    Satz gehört zu jeder Gruppe ihres Kontos und Betrags."""
    verdict = judge([Transfer(1, 2, 0, 8, False, True), Transfer(1, 2, 8, 0, False, False), Transfer(3, 2, 8, 8, True, False)])
    assert verdict.unreported == (1,) and verdict.lost == () and verdict.unnamed == ()
