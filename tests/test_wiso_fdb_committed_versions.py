# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Firebird-Leser liest je Satz den letzten abgeschlossenen Stand (2.3.10).

Firebird legt bei jeder Änderung eine neue Satzversion an; ob sie gilt, sagt der Zustand ihrer Transaktion (TIP-Seiten).
War WISO beim Kopieren der Datei geöffnet oder wurde es nicht sauber beendet, stehen dort Versionen nicht abgeschlossener
Transaktionen: Bis 2.3.9 las der Leser einfach die oberste – geänderte Werte statt der gültigen, ein nur vorläufig
gelöschter Satz fehlte, ein nie abgeschlossen angelegter kam mit. Geprüft wird:

* an zwei **echten** Firebird-3.0-Dateien (``tests/fixtures/firebird/``, mit einer offenen Transaktion kopiert): Der Leser
  liefert genau den abgeschlossenen Stand und zählt, was er übergangen hat (``version_stats``);
* dass der Zustand der Transaktion entscheidet (TIP-Bits in einer Kopie umgestellt), und was bei einer unlesbaren
  TIP-Seite geschieht (gelesen wie in der Datei, gezählt als ``unknown``);
* beschädigte Ketten (Rückzeiger ins Leere, auf einen leeren Slot, auf sich selbst, eine Differenz, die sich nicht anwenden
  lässt) – der Satz fehlt und wird gezählt, nie eine Ausnahme –, und Ketten über mehrere Versionen (auch Differenz auf
  Differenz), die die echten Dateien nicht haben;
* ``apply_diff`` als reine Funktion;
* dass das Mehrfach-Lesen einer Relation nicht doppelt zählt und jede TIP-Seite nur einmal von der Platte kommt;
* die Dateien des Builders (``tests/fdb_builder.py``): kein ``unknown``, ein offen angelegter Satz kommt nicht;
* der Weg bis zur Seite: ``Database.import_wiso_fdb`` (Zähler im Ergebnis, nichts Offenes in den Büchern), der Handler
  (``wiso_fdb_result.json``) und die Hinweise auf *Sonstiges*.

Alle Dateien und Werte sind erfunden.
"""
import gzip
import hashlib
import json
import os
import pathlib
import random
import shutil
import struct

import pytest

import userctx
from importers.wiso_fdb import WisoDatabase
from importers.wiso_fdb import ods as ods_module
from importers.wiso_fdb.catalog import Catalog
from importers.wiso_fdb.ods import (
    OdsFile, P_DATA, P_TIP, R_BLOB, R_CHAIN, R_DELTA, TRA_ACTIVE, TRA_COMMITTED, TRA_DEAD, TRA_LIMBO, apply_diff)
from server import handlers
from server.pages_miscellaneous import PageMiscellaneous
from tests.entry_form_helpers import bank_account, query, wiso_database, wiso_row
from tests.fdb_builder import Column, FdbBuilder, LONG, TEXT, _compress
from tests.wiso_upload_helpers import uploaded

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'firebird'
NAMES = {'4k': 'firebird3_open_tx_4k', '8k': 'firebird3_open_tx_8k'}
KEYS = list(NAMES)
EXPECTED = json.loads((FIXTURES / 'expected.json').read_text(encoding='utf-8'))

#: In beiden Dateien ist es die Transaktion Nr. 9, die nie abgeschlossen wurde; die Sätze davon (``uncommitted``).
OPEN_TRANSACTION = 9
UNCOMMITTED = {'4k': 8, '8k': 3}
NO_FINDINGS = {'uncommitted': 0, 'unreadable': 0, 'unknown': 0}


# ── Hilfen ───────────────────────────────────────────────────────────────────
def unpack(tmp_path, key):
    """Die echte Datei ``key`` entpackt nach ``tmp_path``."""
    target = tmp_path / f'{NAMES[key]}.fdb'
    with gzip.open(FIXTURES / f'{NAMES[key]}.fdb.gz', 'rb') as source, open(target, 'wb') as handle:
        shutil.copyfileobj(source, handle)
    return target


def shaped(row):
    """Eine Zeile von ``T`` in der Form von ``expected.json`` (``WIDE`` als Länge und SHA-256)."""
    wide = row.get('WIDE')
    return {'ID': row['ID'], 'TXT': row['TXT'], 'AMOUNT': str(row['AMOUNT']),
            'WIDE_LEN': len(wide) if wide else 0,
            'WIDE_SHA256': hashlib.sha256(wide.encode('latin-1')).hexdigest() if wide else None,
            'MEMO': row.get('MEMO')}


def read_table(path):
    """``(Zeilen von T nach ID, version_stats)`` – über den Katalog, mit aufgelösten Blobs."""
    with Catalog(str(path)) as catalog:
        rows = sorted((shaped(row) for row in catalog.rows('T', blobs=True)), key=lambda row: row['ID'])
        return rows, catalog.version_stats()


def table_pages(path):
    """Die Datenseiten von ``T``."""
    with Catalog(str(path)) as catalog:
        return catalog.ods.data_pages(catalog.relation_id['T'])


def tip_page_of(path, sequence=0):
    """Nummer der TIP-Seite ``sequence`` – gelesen aus ``RDB$PAGES`` (Relation 0, Seitentyp 3), nicht fest verdrahtet."""
    with OdsFile(str(path)) as ods:
        for page_no in ods.data_pages(0):
            for _line, flags, _fmt, data in ods.raw_records(page_no, 0):
                row = OdsFile.decompress(data)
                if flags & ods_module.SKIP_FLAGS or len(row) < 18:
                    continue
                if struct.unpack_from('<H', row, 16)[0] == P_TIP and struct.unpack_from('<I', row, 12)[0] == sequence:
                    return struct.unpack_from('<I', row, 4)[0]
    raise AssertionError('keine TIP-Seite in RDB$PAGES')


class Patch:
    """Eine Kopie der Datei im Speicher, an der sich einzelne Bytes ändern lassen – so entsteht ein Zustand, den die
    Datei so nicht hat: ein anderer Zustand der Transaktion, eine beschädigte Kette, eine Version mit anderem Rückzeiger."""

    def __init__(self, path):
        self.path = str(path)
        with open(self.path, 'rb') as handle:
            self.data = bytearray(handle.read())
        self.page_size = struct.unpack_from('<H', self.data, 16)[0]

    def save(self):
        with open(self.path, 'wb') as handle:
            handle.write(self.data)
        return self.path

    def slot(self, page_no, line):
        """``(Lage des Slot-Eintrags, Beginn des Satzes, Länge)`` – absolut in der Datei."""
        entry = page_no * self.page_size + 24 + line * 4
        offset, length = struct.unpack_from('<HH', self.data, entry)
        return entry, page_no * self.page_size + offset, length

    def head(self, page_no, line):
        """``(Transaktion, Rückzeiger-Seite, Rückzeiger-Zeile, Flags)`` des Satzes."""
        return struct.unpack_from('<IIHH', self.data, self.slot(page_no, line)[1])

    def set_head(self, page_no, line, transaction=None, back=None, flags=None, add_flags=0):
        _entry, start, _length = self.slot(page_no, line)
        if transaction is not None:
            struct.pack_into('<I', self.data, start, transaction)
        if back is not None:
            struct.pack_into('<IH', self.data, start + 4, *back)
        struct.pack_into('<H', self.data, start + 10,
                         (self.head(page_no, line)[3] if flags is None else flags) | add_flags)

    def set_body(self, page_no, line, body):
        """Die komprimierten Nutzdaten des Satzes ersetzen – höchstens so lang wie die bisherigen."""
        entry, start, length = self.slot(page_no, line)
        assert 13 + len(body) <= length
        self.data[start + 13:start + 13 + len(body)] = body
        struct.pack_into('<H', self.data, entry + 2, 13 + len(body))

    def empty_slot(self, page_no, line):
        struct.pack_into('<H', self.data, self.slot(page_no, line)[0] + 2, 0)

    def set_tip_state(self, tip_page, transaction, state):
        at = tip_page * self.page_size + 20 + transaction // 4
        shift = transaction % 4 * 2
        self.data[at] = self.data[at] & ~(3 << shift) & 0xFF | state << shift


def patched(tmp_path, key, name='kopie.fdb'):
    """Eine entpackte Kopie von ``key`` zum Ändern: ``(Patch, erste Datenseite von T)``."""
    path = tmp_path / name
    shutil.copyfile(unpack(tmp_path, key), path)
    return Patch(path), table_pages(path)[0]


def without(rows, *ids):
    return [row for row in rows if row['ID'] not in ids]


# ── die echten Dateien ───────────────────────────────────────────────────────
@pytest.mark.parametrize('key', KEYS)
def test_the_real_files_read_as_their_committed_state(tmp_path, key):
    rows, stats = read_table(unpack(tmp_path, key))

    assert rows == EXPECTED[NAMES[key]]['committed']
    assert stats == {'uncommitted': UNCOMMITTED[key], 'unreadable': 0, 'unknown': 0}


@pytest.mark.parametrize('key', KEYS)
def test_the_committed_state_differs_from_what_the_top_versions_say(key):
    """Zur Sicherheit: Die beiden Stände in ``expected.json`` unterscheiden sich (geänderte Werte, ein vorläufig gelöschter
    Satz, nie abgeschlossen angelegte) – sonst bewiese der Vergleich oben nichts."""
    committed = EXPECTED[NAMES[key]]['committed']
    stored = EXPECTED[NAMES[key]]['as_stored_today']
    ids = {row['ID'] for row in committed}, {row['ID'] for row in stored}
    assert committed != stored
    assert ids[0] - ids[1], 'ein vorläufig gelöschter Satz fehlt oben'
    assert ids[1] - ids[0], 'ein nie abgeschlossen angelegter kommt oben mit'
    assert any(c['ID'] == s['ID'] and c != s for c in committed for s in stored), 'ein geänderter Wert'


@pytest.mark.parametrize('key', KEYS)
def test_reading_a_relation_again_does_not_count_again(tmp_path, key):
    with Catalog(str(unpack(tmp_path, key))) as catalog:
        first = [shaped(row) for row in catalog.rows('T', blobs=True)]
        once = catalog.version_stats()
        again = [shaped(row) for row in catalog.rows('T', blobs=True)]
        catalog.count('T')
        list(catalog.ods.rows(catalog.relation_id['T']))

        assert again == first
        assert catalog.version_stats() == once == {'uncommitted': UNCOMMITTED[key], 'unreadable': 0, 'unknown': 0}


# ── der Zustand der Transaktion entscheidet ──────────────────────────────────
@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('state', [TRA_DEAD, TRA_LIMBO, TRA_ACTIVE], ids=['tot', 'limbo', 'aktiv'])
def test_a_transaction_that_is_not_committed_leaves_the_committed_state(tmp_path, key, state):
    patch, _page = patched(tmp_path, key)
    patch.set_tip_state(tip_page_of(patch.path), OPEN_TRANSACTION, state)

    rows, stats = read_table(patch.save())

    assert rows == EXPECTED[NAMES[key]]['committed']
    assert stats == {'uncommitted': UNCOMMITTED[key], 'unreadable': 0, 'unknown': 0}


@pytest.mark.parametrize('key', KEYS)
def test_once_the_transaction_is_committed_its_versions_count(tmp_path, key):
    """Dieselben Bytes, ein anderer Zustand in der TIP-Seite: Jetzt ist die oberste Version die gültige."""
    patch, _page = patched(tmp_path, key)
    patch.set_tip_state(tip_page_of(patch.path), OPEN_TRANSACTION, TRA_COMMITTED)

    rows, stats = read_table(patch.save())

    assert rows == EXPECTED[NAMES[key]]['as_stored_today']
    assert stats == NO_FINDINGS


@pytest.mark.parametrize('key', KEYS)
def test_the_tip_state_of_the_real_files_is_what_the_reader_assumes(tmp_path, key):
    """Die Transaktionen der Datei: 1 bis 8 abgeschlossen, 9 aktiv; Nr. 0 gilt dem Leser als abgeschlossen (die TIP-Seite führt sie
    als aktiv, Blobsätze tragen sie)."""
    with OdsFile(str(unpack(tmp_path, key))) as ods:
        assert [ods.tra_state(number) for number in (1, 5, 8)] == [TRA_COMMITTED] * 3
        assert ods.tra_state(OPEN_TRANSACTION) == TRA_ACTIVE
        assert ods.tra_state(0) == TRA_COMMITTED


# ── die TIP-Seite lässt sich nicht lesen ─────────────────────────────────────
@pytest.mark.parametrize('key', KEYS)
def test_an_unreadable_tip_page_reads_the_records_as_stored_and_counts_them(tmp_path, key):
    patch, _page = patched(tmp_path, key)
    patch.data[tip_page_of(patch.path) * patch.page_size] = P_DATA       # falscher Seitentyp
    rows, stats = read_table(patch.save())

    assert rows == EXPECTED[NAMES[key]]['as_stored_today']
    assert stats['uncommitted'] == 0 and stats['unreadable'] == 0
    assert stats['unknown'] >= len(rows)


def test_a_tip_page_that_is_missing_or_too_short_gives_no_state(tmp_path):
    path = unpack(tmp_path, '8k')
    tip_start = tip_page_of(path) * 8192

    with open(path, 'rb') as handle:
        whole = handle.read()
    for name, size, known, unknown in (('kurz.fdb', tip_start + 21, (1, 3), (OPEN_TRANSACTION,)),      # 1 Byte Zustände
                                       ('winzig.fdb', tip_start + 10, (), (1, OPEN_TRANSACTION)),      # nicht einmal der Kopf
                                       ('weg.fdb', tip_start, (), (1, OPEN_TRANSACTION))):             # Seite fehlt ganz
        cut = tmp_path / name
        cut.write_bytes(whole[:size])
        with OdsFile(str(cut)) as ods:
            assert [ods.tra_state(number) for number in known] == [TRA_COMMITTED] * len(known), name
            assert [ods.tra_state(number) for number in unknown] == [None] * len(unknown), name
            assert ods.tra_state(0) == TRA_COMMITTED


def test_a_transaction_beyond_the_tip_pages_of_the_file_is_unknown(tmp_path):
    with OdsFile(str(unpack(tmp_path, '8k'))) as ods:
        beyond = (8192 - 20) * 4 + 5                      # die erste Transaktion der zweiten TIP-Seite, die es nicht gibt
        assert ods.tra_state(beyond) is None


def test_a_transaction_of_a_later_tip_page_is_looked_up_there(tmp_path):
    """Eine TIP-Seite führt ``(Seitengröße − 20) × 4`` Transaktionen; die nächste Nummer steht auf der Seite mit
    Sequenznummer 1. Die echten Dateien haben nur eine – hier hängt eine zweite hinten an die Kopie."""
    path = unpack(tmp_path, '8k')
    per_tip = (8192 - 20) * 4
    data = bytearray(path.read_bytes())
    later = len(data) // 8192
    tip = bytearray(8192)
    tip[0] = P_TIP
    tip[20] = 0b11_00_10_01                                  # die ersten vier: limbo, tot, aktiv, abgeschlossen
    path.write_bytes(bytes(data + tip))
    with OdsFile(str(path)) as ods:
        ods._tip_pages[1] = later                            # RDB$PAGES nennt sie nicht; so erfährt der Leser davon
        assert [ods.tra_state(per_tip + number) for number in range(4)] == [
            TRA_LIMBO, TRA_DEAD, TRA_ACTIVE, TRA_COMMITTED]
        assert ods.tra_state(per_tip - 1) == TRA_ACTIVE      # die letzte der ersten Seite: ungenutzt, also 0 (aktiv)


def test_a_file_without_a_tip_page_is_read_as_stored(tmp_path):
    builder = FdbBuilder(tip=False)
    table = builder.table('T', [Column('ID', LONG, 4)])
    table.add(ID=1).add(ID=2).add_open(ID=3)
    with Catalog(builder.write(str(tmp_path / 'ohne-tip.fdb'))) as catalog:
        assert sorted(row['ID'] for row in catalog.rows('T')) == [1, 2, 3]      # auch der offene: nichts ist nachzuschlagen
        stats = catalog.version_stats()
    assert stats['uncommitted'] == 0 and stats['unreadable'] == 0 and stats['unknown'] >= 3


# ── beschädigte Ketten ───────────────────────────────────────────────────────
@pytest.mark.parametrize('key', KEYS)
def test_a_back_pointer_outside_the_file_loses_the_record_and_counts_it(tmp_path, key):
    patch, page = patched(tmp_path, key)
    assert patch.head(page, 1)[0] == OPEN_TRANSACTION                 # ID 2: in beiden Dateien Slot 1 der ersten Seite
    patch.set_head(page, 1, back=(10 ** 6, 1))

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES[key]]['committed'], 2)
    assert stats == {'uncommitted': UNCOMMITTED[key], 'unreadable': 1, 'unknown': 0}


@pytest.mark.parametrize('key', KEYS)
def test_a_back_pointer_to_an_empty_slot_loses_the_record_and_counts_it(tmp_path, key):
    patch, page = patched(tmp_path, key)
    _tra, back_page, back_line, _flags = patch.head(page, 1)
    patch.empty_slot(back_page, back_line)

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES[key]]['committed'], 2)
    assert stats['unreadable'] == 1 and stats['uncommitted'] == UNCOMMITTED[key]


@pytest.mark.parametrize('key', KEYS)
def test_a_back_pointer_to_a_line_beyond_the_page_loses_the_record_and_counts_it(tmp_path, key):
    patch, page = patched(tmp_path, key)
    patch.set_head(page, 1, back=(page, 500))

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES[key]]['committed'], 2)
    assert stats['unreadable'] == 1


@pytest.mark.parametrize('key', KEYS)
def test_a_version_pointing_to_itself_ends_and_counts(tmp_path, key):
    patch, page = patched(tmp_path, key)
    patch.set_head(page, 1, back=(page, 1))

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES[key]]['committed'], 2)
    assert stats == {'uncommitted': UNCOMMITTED[key], 'unreadable': 1, 'unknown': 0}


@pytest.mark.parametrize('key', KEYS)
def test_a_back_pointer_to_something_that_is_no_version_counts_too(tmp_path, key):
    patch, page = patched(tmp_path, key)
    _tra, back_page, back_line, _flags = patch.head(page, 1)
    patch.set_head(back_page, back_line, add_flags=R_BLOB)

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES[key]]['committed'], 2)
    assert stats['unreadable'] == 1


def test_a_difference_that_cannot_be_applied_loses_the_record_and_counts_it(tmp_path):
    patch, page = patched(tmp_path, '8k')
    tra, back_page, back_line, flags = patch.head(page, 1)
    assert flags & R_DELTA and tra == OPEN_TRANSACTION                 # ID 2 der 8k-Datei: die Vorversion ist eine Differenz
    patch.set_body(back_page, back_line, _compress(bytes([127]) + b'ab'))   # 127 wörtliche Bytes angekündigt, 2 da

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES['8k']]['committed'], 2)
    assert stats == {'uncommitted': UNCOMMITTED['8k'], 'unreadable': 1, 'unknown': 0}


def test_a_damaged_chain_never_costs_another_record(tmp_path):
    patch, page = patched(tmp_path, '4k')
    patch.set_head(page, 1, back=(10 ** 6, 0))
    patch.set_head(page, 3, back=(page, 3))

    rows, stats = read_table(patch.save())

    assert rows == without(EXPECTED[NAMES['4k']]['committed'], 2, 4)
    assert stats == {'uncommitted': 8, 'unreadable': 2, 'unknown': 0}


# ── Ketten über mehrere Versionen (die echten Dateien haben nur Ketten mit einer Vorversion) ─────────────────
def make_diff(newer, older):
    """Ein Differenzstrom, den ``apply_diff`` auf ``newer`` anwendet und daraus ``older`` macht."""
    out, i = bytearray(), 0
    while i < len(older):
        same = i < len(newer) and older[i] == newer[i]
        end = i
        while end < len(older) and end - i < 127 and (end < len(newer) and older[end] == newer[end]) == same:
            end += 1
        if same:
            out.append(256 - (end - i))
        else:
            out.append(end - i)
            out += older[i:end]
        i = end
    return bytes(out)


def three_versions(tmp_path, name='kette.fdb'):
    """Eine Datei mit einem Satz in drei Fassungen, als drei getrennte Sätze einer Seite: Slot 0 'alt' (abgeschlossen),
    Slot 1 'mittel' und Slot 2 'neu' (offene Transaktion) – noch nicht verkettet. Dazu die ganzen Zeilen
    als Bytes (``alt``, ``mittel``, ``neu``)."""
    texts = {'alt': 'C alt', 'mittel': 'B mittel', 'neu': 'A neu'}
    plain = FdbBuilder()
    table = plain.table('T', [Column('ID', LONG, 4), Column('TXT', TEXT, 12)])
    for text in texts.values():
        table.add(ID=1, TXT=text)
    with OdsFile(plain.write(str(tmp_path / 'ganze-zeilen.fdb'))) as ods:
        raws = [raw for _fmt, raw in ods.rows(plain.tables[0].relation_id)]
    chain = FdbBuilder()
    table = chain.table('T', [Column('ID', LONG, 4), Column('TXT', TEXT, 12)])
    table.add(ID=1, TXT=texts['alt'])
    table.add_open(ID=1, TXT=texts['mittel'])
    table.add_open(ID=1, TXT=texts['neu'])
    path = chain.write(str(tmp_path / name))
    raw = dict(zip(('neu', 'mittel', 'alt'), reversed(raws)))
    return path, table_pages(path)[0], raw


def read_ids_and_texts(path):
    with Catalog(str(path)) as catalog:
        return [(row['ID'], row['TXT']) for row in catalog.rows('T')], catalog.version_stats()


def test_three_versions_are_read_back_to_the_first_committed_one(tmp_path):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))                              # neu → mittel
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN)           # mittel → alt
    patch.set_head(page, 0, add_flags=R_CHAIN)

    assert read_ids_and_texts(patch.save()) == ([(1, 'C alt')], {'uncommitted': 1, 'unreadable': 0, 'unknown': 0})


def test_the_search_stops_at_the_first_committed_version(tmp_path):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN, transaction=1)     # mittel ist abgeschlossen
    patch.set_head(page, 0, add_flags=R_CHAIN)

    assert read_ids_and_texts(patch.save())[0] == [(1, 'B mittel')]


def test_a_committed_delete_behind_open_versions_leaves_no_record(tmp_path):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN | 1, transaction=1)        # mittel: abgeschlossen gelöscht
    patch.set_head(page, 0, add_flags=R_CHAIN)
    patch.save()

    assert read_ids_and_texts(path) == ([], {'uncommitted': 1, 'unreadable': 0, 'unknown': 0})
    with Catalog(str(path)) as catalog:
        assert list(catalog.ods.rows(catalog.relation_id['T'])) == []                    # auch kein Satz ohne Daten


def test_a_version_that_was_never_committed_is_not_delivered(tmp_path):
    path, _page, _raw = three_versions(tmp_path)

    rows, stats = read_ids_and_texts(path)

    assert rows == [(1, 'C alt')]                          # nur der abgeschlossene (Slot 0); die zwei offenen haben keine Vorversion
    assert stats == {'uncommitted': 2, 'unreadable': 0, 'unknown': 0}


def test_two_versions_pointing_at_each_other_end_and_count(tmp_path):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))
    patch.set_head(page, 1, back=(page, 2), add_flags=R_CHAIN)                   # mittel → neu → mittel …

    rows, stats = read_ids_and_texts(patch.save())

    assert rows == [(1, 'C alt')]                           # der abgeschlossene Slot 0 bleibt
    assert stats == {'uncommitted': 1, 'unreadable': 1, 'unknown': 0}


def test_a_cycle_is_left_at_the_first_slot_seen_twice(tmp_path, monkeypatch):
    """Nicht erst an der Obergrenze der Kettenlänge: Der Leser merkt sich die Slots, die er schon besucht hat."""
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))
    patch.set_head(page, 1, back=(page, 2), add_flags=R_CHAIN)
    patch.save()
    calls = []
    original = OdsFile.slot
    monkeypatch.setattr(OdsFile, 'slot', lambda self, page_no, line: calls.append((page_no, line)) or original(self, page_no, line))

    read_ids_and_texts(path)

    assert 0 < len(calls) < 20


def test_a_chain_longer_than_the_limit_counts_as_damaged(tmp_path, monkeypatch):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1))
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN)
    patch.set_head(page, 0, add_flags=R_CHAIN)
    patch.save()

    assert read_ids_and_texts(path)[0] == [(1, 'C alt')]                 # zwei Schritte gehen, wie es sein soll
    monkeypatch.setattr(ods_module, 'MAX_VERSIONS', 1)
    rows, stats = read_ids_and_texts(path)

    assert rows == [] and stats == {'uncommitted': 1, 'unreadable': 1, 'unknown': 0}


@pytest.mark.parametrize('first_is_delta', [False, True], ids=['voll', 'diff'])
@pytest.mark.parametrize('second_is_delta', [False, True], ids=['voll', 'diff'])
def test_differences_are_applied_one_version_after_the_other(tmp_path, first_is_delta, second_is_delta):
    """neu (offen) → mittel (offen) → alt (abgeschlossen): Ist ``neu`` ``R_DELTA``, hält ``mittel`` nur die Differenz zu
    ihm; ist ``mittel`` ``R_DELTA``, hält ``alt`` nur die Differenz zu ``mittel`` – Differenz auf Differenz."""
    path, page, raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1), add_flags=R_DELTA if first_is_delta else 0)
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN | (R_DELTA if second_is_delta else 0))
    patch.set_head(page, 0, add_flags=R_CHAIN)
    if first_is_delta:
        patch.set_body(page, 1, _compress(make_diff(raw['neu'], raw['mittel'])))
    if second_is_delta:
        patch.set_body(page, 0, _compress(make_diff(raw['mittel'], raw['alt'])))

    rows, stats = read_ids_and_texts(patch.save())

    assert rows == [(1, 'C alt')]
    assert stats == {'uncommitted': 1, 'unreadable': 0, 'unknown': 0}


def test_a_difference_behind_a_deleted_version_cannot_be_applied(tmp_path):
    path, page, _raw = three_versions(tmp_path)
    patch = Patch(path)
    patch.set_head(page, 2, back=(page, 1), add_flags=R_DELTA)
    patch.set_head(page, 1, back=(page, 0), add_flags=R_CHAIN | 1)                # die Vorversion: ein Löschvermerk
    patch.set_head(page, 0, add_flags=R_CHAIN)

    rows, stats = read_ids_and_texts(patch.save())

    assert rows == [] and stats['unreadable'] == 1


# ── apply_diff ───────────────────────────────────────────────────────────────
def test_apply_diff_copies_what_it_is_told_to_skip_and_replaces_what_it_is_given():
    diff = bytes([256 - 3, 2]) + b'XY' + bytes([256 - 3])
    assert apply_diff(diff, b'abcdefgh') == b'abcXYfgh'


def test_apply_diff_can_make_the_older_version_longer_or_shorter():
    assert apply_diff(bytes([3]) + b'xyz', b'ab') == b'xyz'                    # Wörtliches reicht über das Ende hinaus
    assert apply_diff(bytes([256 - 2, 3]) + b'xyz', b'abcdef') == b'abxyz'     # der Strom endet vor dem Ende der neueren
    assert apply_diff(bytes([256 - 2]), b'abcdef') == b'ab'
    assert apply_diff(b'', b'') == b''


def test_apply_diff_refuses_a_stream_that_cannot_be_applied():
    assert apply_diff(bytes([5]) + b'ab', b'abcdef') is None                   # abgeschnitten: 5 angekündigt, 2 da
    assert apply_diff(bytes([2]), b'abcdef') is None                           # … und gar keines
    assert apply_diff(bytes([256 - 10]), b'abc') is None                       # Unverändertes jenseits der neueren Version


def test_apply_diff_does_not_touch_its_input():
    newer = bytearray(b'abcdef')
    apply_diff(bytes([2]) + b'XY', bytes(newer))
    assert newer == b'abcdef'


def test_make_diff_and_apply_diff_are_inverse_for_any_pair():
    generator = random.Random(2310)
    alphabet = b'ab\x00\xff'                                                    # wenige Zeichen: viele gleiche Stellen
    for _ in range(300):
        newer = bytes(generator.choice(alphabet) for _ in range(generator.randrange(0, 400)))
        older = bytes(newer[i] if generator.random() < 0.8 and i < len(newer) else generator.choice(alphabet)
                      for i in range(generator.randrange(0, 400)))
        assert apply_diff(make_diff(newer, older), newer) == older


# ── Seiten kommen nur einmal von der Platte ──────────────────────────────────
class SeekLog:
    """Merkt sich jede Leseposition der Datei."""

    def __init__(self, handle):
        self._handle = handle
        self.seeks = []

    def seek(self, position, *args):
        self.seeks.append(position)
        return self._handle.seek(position, *args)

    def __getattr__(self, name):
        return getattr(self._handle, name)


@pytest.mark.parametrize('key', KEYS)
def test_the_tip_page_is_read_from_disk_only_once(tmp_path, key):
    path = unpack(tmp_path, key)
    with Catalog(str(path)) as catalog:
        relation_id = catalog.relation_id['T']
    with OdsFile(str(path), cache_pages=2) as ods:                             # winziger Seitenpuffer: leert sich ständig
        tip_at = tip_page_of(path) * ods.page_size
        ods.f = SeekLog(ods.f)
        for _ in range(3):
            assert len(list(ods.rows(relation_id))) == len(EXPECTED[NAMES[key]]['committed'])

        assert len(ods.f.seeks) > len(set(ods.f.seeks)), 'der Seitenpuffer leert sich: andere Seiten kommen mehrfach'
        assert ods.f.seeks.count(tip_at) == 1


# ── die Dateien des Builders ─────────────────────────────────────────────────
def test_a_builder_file_has_no_unknown_state_and_leaves_out_what_only_an_open_transaction_created(tmp_path):
    builder = FdbBuilder()
    table = builder.table('T', [Column('ID', LONG, 4), Column('TXT', TEXT, 10)])
    table.add(ID=1, TXT='eins').add(ID=2, TXT='zwei').add_open(ID=3, TXT='drei offen')
    with Catalog(builder.write(str(tmp_path / 'builder.fdb'))) as catalog:
        assert [(row['ID'], row['TXT']) for row in catalog.rows('T')] == [(1, 'eins'), (2, 'zwei')]
        assert catalog.version_stats() == {'uncommitted': 1, 'unreadable': 0, 'unknown': 0}


def test_a_builder_file_of_the_wiso_tables_is_read_without_any_finding(tmp_path):
    path = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1'), wiso_row(4980, 1200, 23.80, 'R-2')],
                         systems=[{'NAME': 'Datev-Format', 'CONSULTANT_NO': '1001', 'CLIENT_NO': '1',
                                   'USERPASSWORD': 'erfunden'}])
    with WisoDatabase(path) as wiso:
        data = wiso.read(today='2026-06-01')
        assert len(data.bookings) == 2
        assert wiso.version_stats() == NO_FINDINGS


def test_the_findings_of_both_files_are_added(tmp_path):
    client = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1')],
                           open_rows=[wiso_row(4980, 1200, 23.80, 'R-2')])
    standard = FdbBuilder()
    chart = standard.table('BAS_FINACC_PLAN', [Column('ID', LONG, 4), Column('SKR04', LONG, 4)])
    chart.add(ID=1000, SKR04=1600).add_open(ID=1200, SKR04=1800)
    standard_path = standard.write(str(tmp_path / 'DB0.FDB'))

    with WisoDatabase(client, standard_path) as wiso:
        wiso.read(today='2026-06-01')
        assert wiso.version_stats() == {'uncommitted': 2, 'unreadable': 0, 'unknown': 0}
        assert wiso.catalog.version_stats() == {'uncommitted': 1, 'unreadable': 0, 'unknown': 0}


# ── der Weg bis zur Seite ────────────────────────────────────────────────────
NOTICE_UNCOMMITTED = ('Die WISO-Datenbank enthielt {records} (WISO war beim Kopieren geöffnet oder wurde nicht sauber '
                      'beendet). Übernommen wurde jeweils der letzte abgeschlossene Stand.')
NOTICE_UNREADABLE = ('{records} im Import – bitte WISO schließen, die Datei neu kopieren und den Import wiederholen.')
NOTICES = ('Die WISO-Datenbank enthielt', 'nicht auf einen abgeschlossenen Stand zurückführen',
           'nicht feststellen, ob')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Satz-Bank', 1800)
    os.makedirs(userctx.user_data_dir(), exist_ok=True)               # dorthin schreibt der Handler sein Ergebnis
    return tmp_db


def stored_result():
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), encoding='utf-8') as handle:
        return json.load(handle)


def test_the_import_leaves_out_a_booking_only_an_open_transaction_created(db, tmp_path):
    path = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1'), wiso_row(4980, 1200, 23.80, 'R-2')],
                         open_rows=[wiso_row(4980, 1200, 35.70, 'R-OFFEN')])

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert not result['errors'] and result['imported'] == 2
    assert (result['uncommitted_records'], result['unreadable_versions'], result['unknown_tx_state']) == (1, 0, 0)
    assert {row[0] for row in query(db, "SELECT DocumentNumber FROM Bookings WHERE BookingType = 'entry'")} == {'R-1', 'R-2'}


def test_the_import_of_a_file_without_findings_reports_zeros(db, tmp_path):
    path = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1')])

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert (result['uncommitted_records'], result['unreadable_versions'], result['unknown_tx_state']) == (0, 0, 0)


def test_an_import_with_an_unreadable_tip_page_goes_on_and_counts(db, tmp_path):
    path = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1')],
                         open_rows=[wiso_row(4980, 1200, 35.70, 'R-OFFEN')])
    patch = Patch(path)
    patch.data[tip_page_of(path) * patch.page_size] = P_DATA
    patch.save()

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert not result['errors'] and not result['blocker']
    assert result['imported'] == 2                                      # ohne Zustand: gelesen, wie es in der Datei steht
    assert result['unknown_tx_state'] > 0 and result['uncommitted_records'] == 0


def test_the_handler_stores_the_numbers_and_the_page_names_them_only_when_there_are_any(db, tmp_path):
    path = wiso_database(tmp_path / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-1')],
                         open_rows=[wiso_row(4980, 1200, 35.70, 'R-OFFEN')])

    status, location = handlers.handle_wiso_fdb_import(db, uploaded(path))

    assert status == 303 and 'wiso_fdb=ok' in location
    stored = stored_result()
    assert (stored['uncommitted_records'], stored['unreadable_versions'], stored['unknown_tx_state']) == (1, 0, 0)
    page = PageMiscellaneous(db)
    assert NOTICE_UNCOMMITTED.format(records='1 Satz aus einem nicht abgeschlossenen Vorgang') in page
    assert NOTICES[1] not in page and NOTICES[2] not in page

    (tmp_path / 'sauber').mkdir()
    clean = wiso_database(tmp_path / 'sauber' / 'DB1.FDB', [wiso_row(4980, 1200, 11.90, 'R-3')])
    handlers.handle_wiso_fdb_import(db, {**uploaded(clean), 'confirm_existing': ['1']})
    assert stored_result()['uncommitted_records'] == 0
    page = PageMiscellaneous(db)
    assert not [notice for notice in NOTICES if notice in page]


def store(db_result):
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), 'w', encoding='utf-8') as handle:
        json.dump(db_result, handle)


def test_page_names_the_findings_with_their_numbers(db):
    store({'uncommitted_records': 12, 'unreadable_versions': 3, 'unknown_tx_state': 7})

    page = PageMiscellaneous(db)

    assert NOTICE_UNCOMMITTED.format(records='12 Sätze aus nicht abgeschlossenen Vorgängen') in page
    assert NOTICE_UNREADABLE.format(records='3 Sätze ließen sich nicht auf einen abgeschlossenen Stand zurückführen '
                                            'und fehlen') in page
    assert ('Bei 7 Sätzen ließ sich nicht feststellen, ob ihr Vorgang abgeschlossen ist; sie wurden übernommen, wie sie '
            'in der Datei stehen.') in page


def test_page_names_a_single_record_in_the_singular(db):
    store({'uncommitted_records': 1, 'unreadable_versions': 1, 'unknown_tx_state': 1})

    page = PageMiscellaneous(db)

    assert NOTICE_UNCOMMITTED.format(records='1 Satz aus einem nicht abgeschlossenen Vorgang') in page
    assert NOTICE_UNREADABLE.format(records='1 Satz ließ sich nicht auf einen abgeschlossenen Stand zurückführen und fehlt') in page
    assert ('Bei 1 Satz ließ sich nicht feststellen, ob sein Vorgang abgeschlossen ist; er wurde übernommen, wie er in '
            'der Datei steht.') in page


def test_page_warns_when_records_are_lost_and_only_informs_about_the_uncommitted(db):
    store({'uncommitted_records': 2, 'unreadable_versions': 1, 'unknown_tx_state': 1})

    page = PageMiscellaneous(db)

    assert '<p>ℹ️ Die WISO-Datenbank enthielt 2 Sätze' in page
    assert '<p class="warnColor">⚠️ 1 Satz ließ sich nicht auf einen abgeschlossenen Stand' in page
    assert '<p class="warnColor">⚠️ Bei 1 Satz ließ sich nicht feststellen' in page


@pytest.mark.parametrize('value', [0, None, False, True, -3, 'viele', 1.5, [], {}])
def test_page_shows_no_notice_for_a_zero_or_a_value_that_is_no_count(db, value):
    store({'uncommitted_records': value, 'unreadable_versions': value, 'unknown_tx_state': value})
    page = PageMiscellaneous(db)
    assert not [notice for notice in NOTICES if notice in page]


def test_page_shows_no_notice_for_a_result_without_the_keys(db):
    store({'imported': 3, 'skipped': 0})
    assert not [notice for notice in NOTICES if notice in PageMiscellaneous(db)]
