# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Schnappschüsse: Was die App für einen Beleg mit EINEM Steuersatz bucht und ausgibt – genommen am Stand 2.6.6, VOR dem Umbau auf
mehrere Steuersätze je Beleg.

Leitlinie des Umbaus: **Ein Beleg mit nur einem Satz verhält sich danach in jeder Ausgabe genau wie heute.** Dafür steht hier, was die App heute
tut: je Satz – 19 %, 7 %, 0 %, ohne Steuerausweis (§ 19 UStG, Kopf −1) und 5 % (ohne Wartekonto und ohne hinterlegtes Erlöskonto) – ein
eigener, frischer Bestand, in dem derselbe Ablauf läuft (``tests/single_rate_helpers.Scenario``): Versenden, Teilzahlung und schließende
Zahlung, Sammelzahlung, Überzahlung, Zahlung zuordnen und entfernen, Stornieren einer teilbezahlten Rechnung mit Teil- und
Rest-Erstattung, Stornieren und neu ausstellen mit dem Umzug der Zahlung (voll und „50,00 gezahlt, 20,00 erstattet“), Zurückholen,
Berichtigen, der Probelauf von „Zuordnung neu aufbauen“, ein Angebot und seine Umwandlung. Festgehalten sind Buchungssätze, Zahlungszuordnungen,
Rechnungen und Positionen, Protokoll und Fassungen, die Meldungen der Aufrufe (Hinweise, Ablehnungen im Klartext, der Plan des Probelaufs
samt Prüfsumme), die Prüfliste, die offenen Posten des Kunden, EÜR, Summen- und Saldenliste (April und Dezember), Dashboard, der
DATEV-Stapel (Hinweis, Hash und Zeilen, danach das Steuerdatum, das er setzt) und die Dokumente – PDF (Hash der Content-Streams), eingebettete
CII und UBL der Rechnung, der Stornorechnung, der Ersatzrechnung, der berichtigten Fassung (jeweils Hash und lesbare Kernwerte) und des
Angebots. Was die App an einem Satz heute ablehnt, steht als Ablehnung da (das Erlöskonto bei 5 %, dieselbe Bewegung noch einmal, Zurückholen
mit Zahlung) – nichts wird umgangen.

**Die Datei ``single_rate_golden_2_6_6.json`` ist am Stand 2.6.6 erzeugt. Ändert sich ein Wert, ist der Code zu ändern, nie die Datei.**
Der Test nennt bei einer Abweichung den Satz, den Abschnitt und die erste abweichende Stelle. Neu geschrieben wird die Datei nur mit der
Umgebungsvariable ``UBUCHHALTUNG_WRITE_GOLDEN=1`` (``python -m pytest tests/test_single_rate_snapshots.py``): Dann schreibt der Test den Stand
seines Satzes in die Datei und schlägt mit dem Hinweis fehl, dass sie geschrieben wurde – so wird sie nie unbemerkt (und nie in der CI)
überschrieben. Wiederholbar: feste Daten in der Vergangenheit und eine festgesetzte Uhr (Modul-Docstring von ``tests/single_rate_helpers.py``).
"""
import datetime
import json
import os
import time

import pytest

import userctx
from db import Database
from export import einvoice_model, pdfa
from tests import single_rate_helpers as helpers
from tests.single_rate_helpers import CASES, SECTIONS

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'single_rate_golden_2_6_6.json')
WRITE = os.environ.get('UBUCHHALTUNG_WRITE_GOLDEN') == '1'
MAX_GOLDEN_BYTES = 1_000_000


def _golden():
    with open(GOLDEN, encoding='utf-8') as handle:
        return json.load(handle)


def _write_case(key, snapshot):
    """Den Stand eines Satzes in die Golden-Datei schreiben (die anderen Sätze bleiben, wie sie sind)."""
    golden = _golden() if os.path.exists(GOLDEN) else {}
    golden[key] = json.loads(helpers.json_text(snapshot))
    with open(GOLDEN, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(helpers.json_text(golden))


@pytest.mark.parametrize('key', sorted(CASES), ids=lambda key: f'satz_{key}')
def test_what_the_app_books_and_prints_for_one_tax_rate_is_what_it_did_at_2_6_6(key, tmp_db):
    snapshot = json.loads(helpers.json_text(helpers.build_snapshot(tmp_db, key)))    # die Form der Datei: Schlüssel als Text, Tupel als Listen
    if WRITE:
        _write_case(key, snapshot)
        pytest.fail(f'UBUCHHALTUNG_WRITE_GOLDEN=1: Der Stand des Satzes {key} ist in {os.path.basename(GOLDEN)} geschrieben – '
                    'ein Lauf ohne die Variable vergleicht damit.', pytrace=False)
    expected = _golden().get(key)
    assert expected is not None, f'Der Satz {key} fehlt in {os.path.basename(GOLDEN)} (UBUCHHALTUNG_WRITE_GOLDEN=1 schreibt ihn).'
    assert sorted(snapshot) == sorted(SECTIONS)
    for section in SECTIONS:
        found = helpers.first_difference(expected.get(section), snapshot[section])
        assert found is None, (f'Satz {key}, Abschnitt {section}: erste Abweichung bei {section}{found[0]} – '
                               f'erwartet {found[1]} – tatsächlich {found[2]}')
    assert sorted(expected) == sorted(SECTIONS), f'Satz {key}: andere Abschnitte in der Datei: {sorted(expected)}'


def test_a_case_built_twice_in_one_process_gives_the_same_snapshot(tmp_path, monkeypatch):
    """Kein Zustand bleibt zwischen zwei Abläufen im Prozess hängen (Zähler, gelerntes Erlöskonto, Zwischenspeicher, abgelegte Dateien): Zwei
    frische Bestände in je eigenem Datenordner, derselbe Ablauf, derselbe Stand – am Satz 5 %, der am meisten lernt."""
    snapshots = []
    for name in ('eins', 'zwei'):
        monkeypatch.setattr(userctx, 'DATA_ROOT', str(tmp_path / name))
        snapshots.append(json.loads(helpers.json_text(helpers.build_snapshot(Database(db_name=str(tmp_path / f'{name}.db')), '5'))))
    for section in SECTIONS:
        assert helpers.first_difference(snapshots[0][section], snapshots[1][section]) is None, section


@pytest.mark.skipif(WRITE, reason='schreibt die Golden-Datei gerade')
def test_the_golden_file_holds_every_case_and_every_section_in_its_stable_form():
    with open(GOLDEN, encoding='utf-8') as handle:
        text = handle.read()
    golden = json.loads(text)
    assert sorted(golden) == sorted(CASES)
    assert all(sorted(case) == sorted(SECTIONS) for case in golden.values())
    assert helpers.json_text(golden) == text                      # sortierte Schlüssel, Einrückung 1, ``\n`` am Ende, kein ASCII-Zwang
    assert len(text.encode('utf-8')) < MAX_GOLDEN_BYTES


# ── die Hilfen: Vergleich und Uhr ────────────────────────────────────────────

def test_the_first_difference_names_the_place_and_both_values():
    assert helpers.first_difference({'a': [1, 2]}, {'a': [1, 2]}) is None
    assert helpers.first_difference({'a': [1, 2, 3]}, {'a': [1, 5, 3]}, '/x') == ('/x/a[1]', '2', '5')
    assert helpers.first_difference({'a': 1}, {'a': 1, 'b': 2}) == ('/b', '(fehlt in der Datei)', '2')
    assert helpers.first_difference({'a': 1, 'b': 2}, {'a': 1}) == ('/b', '2', '(fehlt im frischen Stand)')
    assert helpers.first_difference(['x'], ['x', 'y']) == ('[1]', '(fehlt in der Datei)', 'y')
    assert helpers.first_difference(1, 1.0) is not None and helpers.first_difference(True, 1) is not None     # Art und Wert zählen
    path, expected, actual = helpers.first_difference(['x' * 1000], ['y'])
    assert path == '[0]' and len(expected) < 450 and actual == 'y'                                         # kein Megabyte-Diff


def test_the_first_difference_shows_a_long_text_around_the_first_deviating_character_with_its_beginning():
    row = 'ID=36 | DateBooking=2025-05-15 | ' + 'Feld=wert | ' * 20 + 'Text=Rechnung 25R001 Malermeister Pinsel | DocumentNumber=25R001'
    other = row.replace('Rechnung 25R001 Malermeister Pinsel', 'Rechnung 25R001')
    path, expected, actual = helpers.first_difference({'bookings': [row]}, {'bookings': [other]})
    assert path == '/bookings[0]'
    assert expected.startswith('ID=36 | DateBooking=2025-05-15 | ') and ' … ' in expected and 'Malermeister Pinsel' in expected
    assert actual.startswith('ID=36 | DateBooking=2025-05-15 | ') and ' … ' in actual and 'Malermeister' not in actual
    assert len(expected) < 220 and len(actual) < 220
    assert helpers.first_difference('abc', 'abd') == ('', 'abc', 'abd') and helpers.first_difference('abc', 'ab') == ('', 'abc', 'ab')


def test_the_first_difference_names_the_readable_deviation_before_the_hash_that_sums_it_up():
    expected = {'cii': {'sha256': 'aaa', 'tax': [{'rate': '19.00'}]}, 'datev': {'bytes': 10, 'rows': ['x']}}
    actual = {'cii': {'sha256': 'bbb', 'tax': [{'rate': '19.0'}]}, 'datev': {'bytes': 11, 'rows': ['y']}}
    assert helpers.first_difference(expected, actual) == ('/cii/tax[0]/rate', '19.00', '19.0')
    assert helpers.first_difference(expected['datev'], actual['datev'], '/datev') == ('/datev/rows[0]', 'x', 'y')
    assert helpers.first_difference({'sha256': 'aaa'}, {'sha256': 'bbb'}) == ('/sha256', 'aaa', 'bbb')       # steht nur der Hash da, ist er die Stelle


def test_the_frozen_clock_fixes_every_reading_of_the_day_and_the_hour_and_gives_the_clock_back():
    classes, zone = (datetime.date, datetime.datetime), os.environ.get('TZ')
    real_day = datetime.date(2020, 1, 1)
    with helpers.frozen_clock():
        assert datetime.date.today() == datetime.date(2025, 7, 15)
        assert datetime.datetime.now() == datetime.datetime(2025, 7, 15, 9, 30)
        assert datetime.datetime.utcnow() == datetime.datetime(2025, 7, 15, 9, 30)
        assert datetime.datetime.now(datetime.timezone.utc).tzinfo is datetime.timezone.utc
        assert isinstance(datetime.date.today(), datetime.date) and isinstance(datetime.datetime.now(), datetime.date)
        assert isinstance(real_day, datetime.date) and isinstance(datetime.datetime(2020, 1, 1), datetime.datetime)   # Echtes bleibt ein Datum
        assert (datetime.date.today() + datetime.timedelta(days=17)).isoformat() == '2025-08-01'
        assert pdfa.datetime.now() == datetime.datetime(2025, 7, 15, 9, 30)          # die Namen, die beim Import gebunden wurden
        assert einvoice_model.date.today() == datetime.date(2025, 7, 15)
        if hasattr(time, 'tzset'):
            assert time.strftime('%Z') == 'UTC'
    assert (datetime.date, datetime.datetime) == classes and os.environ.get('TZ') == zone
    assert pdfa.datetime is classes[1] and einvoice_model.date is classes[0]
