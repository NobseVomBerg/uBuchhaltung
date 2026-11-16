# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Lesender Zugriff auf Firebird-Datenbankdateien (ODS 12) in reinem Python.

WISO Mein Büro legt seine Daten in Firebird-Dateien ab (``DB1.FDB`` =
Mandant 1). Diese Dateien sind **unverschlüsselt**; für das Lesen braucht es
weder einen Firebird-Server noch ``fbclient.dll`` noch einen Python-Treiber.

Dieses Modul deckt genau den Teil des Dateiformats ab, den ein Export braucht:

* **Seitenverwaltung** – Header-Page, Pointer-Pages, Data-Pages
* **Satzversionen** – je Satz der letzte *abgeschlossene* Stand (siehe unten)
* **Transaktionszustand** – die TIP-Seiten (Transaction Inventory Pages)
* **Fragmente** – Sätze, die über mehrere Seiten verteilt sind
* **RLE** – Firebirds Satzkompression
* **Blobs** – Level 0 bis 2

Schreiben ist ausdrücklich **nicht** vorgesehen: die Datei gehört WISO.

Die Strukturen sind natürlich ausgerichtet (``rhdf`` hat deshalb 22 statt 19
Byte, ``blh`` beginnt seine Nutzdaten bei 28) – das ist der Punkt, an dem
naive Umsetzungen scheitern.

**Satzversionen und Transaktionen.** Firebird überschreibt einen Satz nie, sondern legt bei jeder
Änderung eine neue Version an; die ältere hängt über einen Rückzeiger
(``rhd_b_page``/``rhd_b_line``) dahinter. Ob eine Version gilt, entscheidet der
Zustand ihrer Transaktion, den die TIP-Seiten führen (2 Bit je Transaktion:
aktiv, limbo, tot, abgeschlossen). War WISO beim Kopieren der Datei geöffnet
oder wurde es nicht sauber beendet, stehen dort Versionen nicht abgeschlossener
Transaktionen: Wer einfach die oberste liest, übernimmt geänderte Werte statt der
gültigen, vermisst einen nur vorläufig gelöschten Satz und bekommt einen nie
abgeschlossen angelegten dazu. Der Leser geht deshalb von der obersten Version
über die Rückzeiger zurück bis zur ersten, deren Transaktion abgeschlossen ist. Eine
ältere Version speichert Firebird als Differenz zur nächstneueren (``R_DELTA`` an
der neueren, siehe :func:`apply_diff`). Was dabei auffällt, zählt
:meth:`OdsFile.version_stats` – nichts davon wird still hingenommen.

Ungeprüft (die vorhandenen Dateien haben keine so hohen Transaktionsnummern; nach ``ods.h``
umgesetzt): das Flag ``rhd_long_tranum``. Die oberen 16 Bit der Transaktionsnummer stehen dann
bei Offset 14 des Satzkopfs, und die Nutzdaten beginnen bei 16 (bei einem fragmentierten Satz
wie sonst bei 22).
"""
import os
import struct

# Seitentypen
P_HEADER, P_PIP, P_TIP, P_POINTER, P_DATA, P_ROOT, P_INDEX, P_BLOB = 1, 2, 3, 4, 5, 6, 7, 8

# Satz-Flags (Ods::rhd_*)
R_DELETED, R_CHAIN, R_FRAGMENT, R_INCOMPLETE = 1, 2, 4, 8
R_BLOB, R_DELTA, R_LARGE, R_DAMAGED = 16, 32, 64, 128
R_LONG_TRANUM = 1024

#: Zustand einer Transaktion in der TIP-Seite (``tra_*`` in ``ods.h``).
TRA_ACTIVE, TRA_LIMBO, TRA_DEAD, TRA_COMMITTED = 0, 1, 2, 3

#: Satzkopf ``rhd`` = 13 Byte. Bei fragmentierten Sätzen (``rhdf``) folgen
#: hinter ``rhd_format`` drei Füllbytes, dann ``f_page`` (4) und ``f_line`` (2);
#: die Nutzdaten beginnen deshalb bei 22, nicht bei 19. Mit ``R_LONG_TRANUM`` (``rhde``) bei 16.
HEAD, HEAD_F, HEAD_LONG = 13, 22, 16
F_PAGE_OFF, F_LINE_OFF = 16, 20
TRA_HIGH_OFF = 14

#: Die ersten 20 Byte einer TIP-Seite sind Seitenkopf (16) und ``tip_next`` (4); danach 2 Bit je Transaktion.
TIP_HEAD = 20

#: ``rhd_transaction``, ``rhd_b_page``, ``rhd_b_line``, ``rhd_flags``, ``rhd_format``.
_RECORD_HEAD = struct.Struct('<IIHHB')

#: Flags eines Slots, der nicht der Anfang eines Satzes ist: eine ältere Version, ein Fragment, ein Blob.
NOT_A_RECORD = R_CHAIN | R_FRAGMENT | R_BLOB | R_DAMAGED

#: Sätze, die kein aktueller Datensatz sind (so liest ``RDB$PAGES``, ohne den Zustand einer Transaktion zu kennen).
SKIP_FLAGS = R_DELETED | NOT_A_RECORD

#: Längste Kette von Vorversionen, der der Leser folgt – eine längere gilt als beschädigt.
MAX_VERSIONS = 1024

#: Header-Flag für verschlüsselte Datenbanken (Firebird 3+).
HDR_ENCRYPTED = 0x40


class OdsError(Exception):
    """Datei ist keine lesbare Firebird-Datenbank."""


def apply_diff(diff, newer):
    """Differenzstrom auf die Daten der neueren Version anwenden – ergibt die Daten der älteren.

    Firebird speichert eine ältere Version als Differenz zur nächstneueren. Steuerbyte ``c``: ``0..127``
    ersetzt ab der laufenden Stelle ``c`` Bytes durch die ``c`` folgenden des Stroms (wörtlich),
    ``128..255`` (``c − 256``, negativ) lässt ``256 − c`` Bytes der neueren Version unverändert. Das Ergebnis endet
    dort, wo der Strom endet: kürzer als die neuere Version, wenn die ältere kürzer war, länger, wenn
    wörtliche Bytes über das Ende der neueren hinausgehen.

    ``None``, wenn der Strom sich nicht anwenden lässt: ein wörtlicher Lauf, der über das Ende des
    Stroms hinausgeht (abgeschnitten), oder Unverändertes jenseits der Daten der neueren Version.
    """
    out = bytearray(newer)
    pos = i = 0
    while i < len(diff):
        control = diff[i]
        i += 1
        if control < 128:
            chunk = diff[i:i + control]
            if len(chunk) < control:
                return None
            if pos + control > len(out):
                out.extend(b'\0' * (pos + control - len(out)))
            out[pos:pos + control] = chunk
            pos += control
            i += control
        else:
            pos += 256 - control
    return bytes(out[:pos]) if pos <= len(out) else None


class OdsFile:
    """Eine geöffnete ``.FDB``-Datei.

    >>> db = OdsFile('DB1.FDB')          # doctest: +SKIP
    >>> for fmt, raw in db.rows(176):    # doctest: +SKIP
    ...     ...
    """

    def __init__(self, path, cache_pages=512):
        self.path = path
        self.size = os.path.getsize(path)
        self.f = open(path, 'rb')
        header = self.f.read(64)
        if len(header) < 64 or header[0] != P_HEADER:
            raise OdsError(f'{path}: keine Firebird-Datei')
        self.page_size, ods = struct.unpack_from('<HH', header, 16)
        self.ods = ods & 0x7FFF
        if self.ods != 12:
            raise OdsError(f'{path}: ODS {self.ods}, unterstützt wird nur 12')
        self.pages_pointer = struct.unpack_from('<I', header, 20)[0]
        self.flags = struct.unpack_from('<H', header, 42)[0]
        if self.flags & HDR_ENCRYPTED:
            raise OdsError(f'{path}: Datenbank ist verschlüsselt')
        # Satznummern zählen seitenweise: so viele Sätze passen maximal auf
        # eine Datenseite (Slot 4 Byte + kleinster Satzkopf 13 Byte).
        self.max_records = (self.page_size - 24) // 17
        self._cache_limit = cache_pages
        self._cache = {}
        self._pages_of = {}
        # Was bei den Satzversionen auffiel, je Menge von Sätzen (Datenseite << 16 | Zeile): Wer eine Relation mehrfach
        # liest, zählt nicht doppelt.
        self._uncommitted = set()
        self._unreadable = set()
        self._unknown = set()
        # Je TIP-Seite (Sequenznummer) ihre Zustandsbytes, einmal gelesen – nicht über den Seitenpuffer, der sich leert
        self._tips = {}
        self._per_tip = (self.page_size - TIP_HEAD) * 4
        self._heads, self._tip_pages = self._read_pages_relation()

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Seiten
    # ------------------------------------------------------------------
    def page(self, number):
        """Rohe Seite; leeres bytes bei Seiten außerhalb der Datei."""
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        if number < 0 or number * self.page_size >= self.size:
            return b''
        self.f.seek(number * self.page_size)
        data = self.f.read(self.page_size)
        if len(self._cache) >= self._cache_limit:
            self._cache.clear()
        self._cache[number] = data
        return data

    def _pointer_chain(self, first):
        """``ppg_next``-Kette abgehen; Ergebnis ist die Datenseitenfolge.

        Nullen bleiben als Lücken erhalten – der Index in der Liste ist die
        Sequenznummer, aus der sich Satznummern errechnen.
        """
        pages, seen, number = [], set(), first
        while number and number not in seen:
            seen.add(number)
            page = self.page(number)
            if len(page) < 32 or page[0] != P_POINTER:
                break
            _seq, nxt, count, _rel = struct.unpack_from('<IIHH', page, 16)
            count = min(count, (len(page) - 32) // 4)
            pages += list(struct.unpack_from(f'<{count}I', page, 32))
            number = nxt
        return pages

    def _read_pages_relation(self):
        """``RDB$PAGES`` lesen → ``(Relation → ihre erste Pointer-Page, TIP-Seiten)``.

        ``RDB$PAGES`` ist Relation 0; ihre eigene Pointer-Page steht im
        Dateikopf. Damit hängt sich der Rest des Katalogs auf. Ihre Sätze gelten, wie sie
        oben stehen: Den Zustand einer Transaktion kennt erst, wer die TIP-Seiten gefunden hat,
        und die nennt diese Tabelle (Seitentyp 3, je Sequenznummer eine Seite).
        """
        heads, tips = {}, {}
        for page_no in self._pointer_chain(self.pages_pointer):
            if not page_no:
                continue
            for _line, flags, _fmt, data in self.raw_records(page_no, 0):
                if flags & SKIP_FLAGS:
                    continue
                row = self.decompress(data)
                if len(row) < 18:
                    continue
                page = struct.unpack_from('<I', row, 4)[0]
                relation = struct.unpack_from('<H', row, 8)[0]
                sequence = struct.unpack_from('<I', row, 12)[0]
                page_type = struct.unpack_from('<H', row, 16)[0]
                if page_type == P_POINTER and sequence == 0:
                    heads[relation] = page
                elif page_type == P_TIP:
                    tips[sequence] = page
        return heads, tips

    @property
    def relations(self):
        """Relation-Ids, für die überhaupt Seiten angelegt sind."""
        return set(self._heads)

    def data_pages(self, relation_id):
        """Datenseiten einer Relation; Listenindex = Sequenznummer."""
        if relation_id not in self._pages_of:
            head = self._heads.get(relation_id)
            self._pages_of[relation_id] = self._pointer_chain(head) if head else []
        return self._pages_of[relation_id]

    # ------------------------------------------------------------------
    # Transaktionen
    # ------------------------------------------------------------------
    def _tip(self, sequence):
        """Zustandsbytes (ab Byte 20) der TIP-Seite Nr. ``sequence``; ``None``, wenn sie sich nicht lesen lässt:
        keine Seite in ``RDB$PAGES``, außerhalb der Datei, nicht vom Typ TIP oder zu kurz."""
        if sequence not in self._tips:
            page_no = self._tip_pages.get(sequence)
            page = self.page(page_no) if page_no else b''
            self._tips[sequence] = page[TIP_HEAD:] if len(page) > TIP_HEAD and page[0] == P_TIP else None
        return self._tips[sequence]

    def tra_state(self, tra):
        """Zustand der Transaktion ``tra`` aus den TIP-Seiten (``TRA_ACTIVE`` … ``TRA_COMMITTED``).

        Transaktion 0 gilt als abgeschlossen. ``None``, wenn sich der Zustand nicht lesen lässt (TIP-Seite fehlt,
        ist keine, oder ist zu kurz für diese Transaktion).
        """
        if not tra:
            return TRA_COMMITTED
        sequence, index = divmod(tra, self._per_tip)
        tip = self._tip(sequence)
        if tip is None or index >> 2 >= len(tip):
            return None
        return tip[index >> 2] >> ((index & 3) << 1) & 3

    def version_stats(self):
        """Was beim Lesen der Sätze an ihren Versionen auffiel – ``{'uncommitted', 'unreadable', 'unknown'}``, je die
        Zahl der Sätze (jeder höchstens einmal je Menge, auch wenn die Relation mehrfach gelesen wird).

        * ``uncommitted`` – die oberste Version war nicht abgeschlossen (WISO war geöffnet oder wurde nicht sauber beendet):
          geliefert wurde der letzte abgeschlossene Stand oder, wenn es keinen gibt, nichts.
        * ``unreadable`` – die Kette der Vorversionen ließ sich nicht bis zu einem abgeschlossenen Stand zurückverfolgen
          (Rückzeiger ins Leere, Differenz nicht anwendbar, Schleife): der Satz fehlt.
        * ``unknown`` – der Zustand einer Transaktion ließ sich nicht lesen: der Satz wurde gelesen, wie er in der Datei steht.

        Die Zahlen entstehen beim Lesen – wer sie braucht, fragt nach dem Lesen aller Tabellen.
        """
        return {'uncommitted': len(self._uncommitted), 'unreadable': len(self._unreadable),
                'unknown': len(self._unknown)}

    # ------------------------------------------------------------------
    # Sätze
    # ------------------------------------------------------------------
    def _slots(self, page_no, relation_id=None):
        """Belegte Slots einer Datenseite als ``(line, rohbytes)`` – samt Satzkopf."""
        page = self.page(page_no)
        if len(page) < 24 or page[0] != P_DATA:
            return
        _seq, relation, count = struct.unpack_from('<IHH', page, 16)
        if relation_id is not None and relation != relation_id:
            return
        count = min(count, (len(page) - 24) // 4)
        for line in range(count):
            offset, length = struct.unpack_from('<HH', page, 24 + line * 4)
            if not length or offset + length > len(page):
                continue
            record = page[offset:offset + length]
            if len(record) >= HEAD:
                yield line, record

    def raw_records(self, page_no, relation_id=None):
        """Slots einer Datenseite als ``(line, flags, format, daten)``."""
        for line, record in self._slots(page_no, relation_id):
            flags = struct.unpack_from('<H', record, 10)[0]
            head = HEAD_F if flags & R_INCOMPLETE else HEAD
            yield line, flags, record[12], record[head:]

    def slot(self, page_no, line):
        """Rohbytes eines Slots einschließlich Satzkopf."""
        page = self.page(page_no)
        if len(page) < 24 or page[0] != P_DATA:
            return None
        _seq, _relation, count = struct.unpack_from('<IHH', page, 16)
        if line >= count:
            return None
        offset, length = struct.unpack_from('<HH', page, 24 + line * 4)
        if not length or offset + length > len(page):
            return None
        return page[offset:offset + length]

    @staticmethod
    def decompress(data):
        """Firebirds RLE.

        Steuerbyte c: ``0`` beendet, ``1..127`` kopiert c Bytes wörtlich,
        ``128..255`` wiederholt das nächste Byte ``256-c`` mal.
        """
        out, i, n = bytearray(), 0, len(data)
        while i < n:
            control = data[i]
            i += 1
            if control == 0:
                break
            if control < 128:
                out += data[i:i + control]
                i += control
            else:
                if i >= n:
                    break
                out += bytes(data[i:i + 1]) * (256 - control)
                i += 1
        return bytes(out)

    def _assemble(self, page_no, line, flags, data):
        """Fragmentkette eines übergroßen Satzes zusammensetzen."""
        out = bytearray(self.decompress(data))
        guard = 0
        while flags & R_INCOMPLETE and guard < 4096:
            guard += 1
            record = self.slot(page_no, line)
            if record is None or len(record) < HEAD_F:
                break
            page_no = struct.unpack_from('<I', record, F_PAGE_OFF)[0]
            line = struct.unpack_from('<H', record, F_LINE_OFF)[0]
            if not page_no:
                break
            fragment = self.slot(page_no, line)
            if fragment is None or len(fragment) < HEAD:
                break
            flags = struct.unpack_from('<H', fragment, 10)[0]
            head = HEAD_F if flags & R_INCOMPLETE else HEAD
            out += self.decompress(fragment[head:])
        return bytes(out)

    @staticmethod
    def _head(record):
        """Satzkopf: ``(Transaktion, Rückzeiger-Seite, Rückzeiger-Zeile, Flags, Format, Beginn der Nutzdaten)``."""
        tra, b_page, b_line, flags, fmt = _RECORD_HEAD.unpack_from(record)
        if flags & R_INCOMPLETE:
            start = HEAD_F
        elif flags & R_LONG_TRANUM:
            start = HEAD_LONG
        else:
            start = HEAD
        if flags & R_LONG_TRANUM and len(record) >= HEAD_LONG:
            tra |= struct.unpack_from('<H', record, TRA_HIGH_OFF)[0] << 32
        return tra, b_page, b_line, flags, fmt, start

    def rows(self, relation_id):
        """Sätze einer Relation als ``(format, rohbytes)`` – je Satz sein letzter abgeschlossener Stand.

        Ist die oberste Version eines Satzes nicht abgeschlossen (aktiv, limbo, tot), gilt die Vorversion, die es ist; einen Satz,
        den es nie abgeschlossen gab, liefert der Leser nicht, einen abgeschlossen gelöschten auch nicht. Was dabei auffällt, zählt
        :meth:`version_stats`.
        """
        last_tra = last_state = None
        for page_no in self.data_pages(relation_id):
            if not page_no:
                continue
            for line, record in self._slots(page_no, relation_id):
                tra, _b_page, _b_line, flags, fmt, head = self._head(record)
                if flags & NOT_A_RECORD:
                    continue
                if tra != last_tra:
                    last_tra, last_state = tra, self.tra_state(tra)
                if last_state is None:
                    self._unknown.add(page_no << 16 | line)
                elif last_state != TRA_COMMITTED:
                    found = self._older_version(page_no, line, record)
                    if found is not None:
                        yield found
                    continue
                if not flags & R_DELETED:
                    yield fmt, self._assemble(page_no, line, flags, record[head:])

    def _older_version(self, page_no, line, record):
        """Der letzte abgeschlossene Stand eines Satzes, dessen oberste Version es nicht ist, als ``(format, rohbytes)``.

        Von der obersten Version geht es über die Rückzeiger zurück, bis eine Version abgeschlossen ist. Ist die
        neuere Version ``R_DELTA``, hält die Vorversion nur eine Differenz zu ihr (:func:`apply_diff`), sonst einen
        ganzen Satz. Eine abgeschlossen gelöschte Version, ein fehlender Rückzeiger (der Satz wurde nie abgeschlossen
        angelegt): ``None``. ``None`` auch, wenn die Kette beschädigt ist (``unreadable``): ein Rückzeiger ins Leere
        oder auf etwas, das keine Version ist, eine Differenz, die sich nicht anwenden lässt, eine Schleife oder eine
        Kette über ``MAX_VERSIONS``.
        """
        key = page_no << 16 | line
        self._uncommitted.add(key)
        seen = {key}
        _tra, b_page, b_line, flags, _fmt, head = self._head(record)
        # die Daten der Version, bei der die Suche steht (None: ein Löschvermerk hat keine)
        data = None if flags & R_DELETED else self._assemble(page_no, line, flags, record[head:])
        for _ in range(MAX_VERSIONS):
            if not b_page:
                return None
            prior = self.slot(b_page, b_line)
            if prior is None or len(prior) < HEAD or b_page << 16 | b_line in seen:
                break
            seen.add(b_page << 16 | b_line)
            p_tra, p_b_page, p_b_line, p_flags, p_fmt, p_head = self._head(prior)
            if p_flags & (R_FRAGMENT | R_BLOB | R_DAMAGED):
                break
            if flags & R_DELTA:
                if data is None or p_flags & R_DELETED:
                    break
                data = apply_diff(self._assemble(b_page, b_line, p_flags, prior[p_head:]), data)
                if data is None:
                    break
            else:
                data = None if p_flags & R_DELETED else self._assemble(b_page, b_line, p_flags, prior[p_head:])
            state = self.tra_state(p_tra)
            if state is None:
                self._unknown.add(key)
                state = TRA_COMMITTED
            if state == TRA_COMMITTED:
                return None if p_flags & R_DELETED else (p_fmt, data)
            b_page, b_line, flags = p_b_page, p_b_line, p_flags
        self._unreadable.add(key)
        return None

    # ------------------------------------------------------------------
    # Blobs
    # ------------------------------------------------------------------
    def blob(self, relation_id, record_number):
        """Blobinhalt über ``(Relation, Satznummer)``; Segmentköpfe entfernt.

        ``blh`` ist natürlich ausgerichtet: ``level`` bei 12, ``count`` bei 16,
        ``length`` bei 20, ``sub_type`` bei 24 – Nutzdaten bzw. Seitenvektor
        ab 28.
        """
        pages = self.data_pages(relation_id)
        sequence, line = divmod(record_number, self.max_records)
        if sequence >= len(pages) or not pages[sequence]:
            return b''
        record = self.slot(pages[sequence], line)
        if record is None or len(record) < 28:
            return b''
        if not struct.unpack_from('<H', record, 10)[0] & R_BLOB:
            return b''
        level = record[12]
        length = struct.unpack_from('<I', record, 20)[0]
        stream = record[28:] if level == 0 else self._blob_pages(record, level)
        return self._unsegment(stream, length)

    def _blob_pages(self, record, level):
        """Blobseiten einsammeln (Level 1 direkt, Level 2 über Zeigerseiten)."""
        out = bytearray()
        count = (len(record) - 28) // 4
        for page_no in struct.unpack_from(f'<{count}I', record, 28):
            if not page_no:
                continue
            page = self.page(page_no)
            if len(page) < 28 or page[0] != P_BLOB:
                continue
            used = struct.unpack_from('<H', page, 24)[0]
            if level == 1:
                out += page[28:28 + used]
                continue
            for inner in struct.unpack_from(f'<{used // 4}I', page, 28):
                if not inner:
                    continue
                sub = self.page(inner)
                if len(sub) < 28 or sub[0] != P_BLOB:
                    continue
                out += sub[28:28 + struct.unpack_from('<H', sub, 24)[0]]
        return bytes(out)

    @staticmethod
    def _unsegment(stream, length):
        """Blobs speichern Segmente mit 2-Byte-Längenpräfix."""
        out, i = bytearray(), 0
        while i + 2 <= len(stream) and len(out) < length:
            size = struct.unpack_from('<H', stream, i)[0]
            i += 2
            out += stream[i:i + size]
            i += size
        return bytes(out[:length])
