# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Lesender Zugriff auf Firebird-Datenbankdateien (ODS 12) in reinem Python.

WISO Mein Büro legt seine Daten in Firebird-Dateien ab (``DB1.FDB`` =
Mandant 1). Diese Dateien sind **unverschlüsselt**; für das Lesen braucht es
weder einen Firebird-Server noch ``fbclient.dll`` noch einen Python-Treiber.

Dieses Modul deckt genau den Teil des Dateiformats ab, den ein Export braucht:

* **Seitenverwaltung** – Header-Page, Pointer-Pages, Data-Pages
* **Satzversionen** – nur die aktuelle Version, keine alten Stände
* **Fragmente** – Sätze, die über mehrere Seiten verteilt sind
* **RLE** – Firebirds Satzkompression
* **Blobs** – Level 0 bis 2

Schreiben ist ausdrücklich **nicht** vorgesehen: die Datei gehört WISO.

Die Strukturen sind natürlich ausgerichtet (``rhdf`` hat deshalb 22 statt 19
Byte, ``blh`` beginnt seine Nutzdaten bei 28) – das ist der Punkt, an dem
naive Umsetzungen scheitern.
"""
import os
import struct

# Seitentypen
P_HEADER, P_PIP, P_POINTER, P_DATA, P_ROOT, P_INDEX, P_BLOB = 1, 2, 4, 5, 6, 7, 8

# Satz-Flags (Ods::rhd_*)
R_DELETED, R_CHAIN, R_FRAGMENT, R_INCOMPLETE = 1, 2, 4, 8
R_BLOB, R_DELTA, R_LARGE, R_DAMAGED = 16, 32, 64, 128

#: Satzkopf ``rhd`` = 13 Byte. Bei fragmentierten Sätzen (``rhdf``) folgen
#: hinter ``rhd_format`` drei Füllbytes, dann ``f_page`` (4) und ``f_line`` (2);
#: die Nutzdaten beginnen deshalb bei 22, nicht bei 19.
HEAD, HEAD_F = 13, 22
F_PAGE_OFF, F_LINE_OFF = 16, 20

#: Sätze, die kein aktueller Datensatz sind.
SKIP_FLAGS = R_DELETED | R_CHAIN | R_FRAGMENT | R_BLOB | R_DAMAGED

#: Header-Flag für verschlüsselte Datenbanken (Firebird 3+).
HDR_ENCRYPTED = 0x40


class OdsError(Exception):
    """Datei ist keine lesbare Firebird-Datenbank."""


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
        self._heads = self._read_pages_relation()

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
        """``RDB$PAGES`` lesen → Relation auf ihre erste Pointer-Page.

        ``RDB$PAGES`` ist Relation 0; ihre eigene Pointer-Page steht im
        Dateikopf. Damit hängt sich der Rest des Katalogs auf.
        """
        heads = {}
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
        return heads

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
    # Sätze
    # ------------------------------------------------------------------
    def raw_records(self, page_no, relation_id=None):
        """Slots einer Datenseite als ``(line, flags, format, daten)``."""
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
            if len(record) < HEAD:
                continue
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

    def rows(self, relation_id):
        """Aktuelle Sätze einer Relation als ``(format, rohbytes)``."""
        for page_no in self.data_pages(relation_id):
            if not page_no:
                continue
            for line, flags, fmt, data in self.raw_records(page_no, relation_id):
                if flags & SKIP_FLAGS:
                    continue
                yield fmt, self._assemble(page_no, line, flags, data)

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
