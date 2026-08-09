# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Baut synthetische Firebird-Dateien (ODS 12) für die Tests.

Damit lässt sich :mod:`importers.wiso_fdb` vollständig prüfen, ohne eine
echte WISO-Datenbank zu benötigen – und ohne echte Daten in den Tests.

Erzeugt wird genau so viel Dateiformat, wie der Leser anfasst: Header-Page,
Pointer-Pages, Data-Pages, RLE-komprimierte Sätze, Blobs und die drei
Systemtabellen ``RDB$PAGES``, ``RDB$RELATIONS``, ``RDB$RELATION_FIELDS`` sowie
``RDB$FORMATS``.
"""
import datetime
import struct

EPOCH = datetime.date(1858, 11, 17)

# dsc_dtype
TEXT, VARYING, SHORT, LONG, DOUBLE = 1, 3, 8, 9, 12
DATE, TIMESTAMP, BLOB, INT64, BOOLEAN = 14, 16, 17, 19, 21

_ALIGN = {TEXT: 1, VARYING: 2, SHORT: 2, LONG: 4, DOUBLE: 8,
          DATE: 4, TIMESTAMP: 8, BLOB: 8, INT64: 8, BOOLEAN: 1}

# Relations-Ids der Systemtabellen
R_RELATION_FIELDS, R_RELATIONS, R_FORMATS = 5, 6, 8
FIRST_USER_RELATION = 128

# Feste Offsets, die der Leser für die Systemtabellen annimmt
REL_ID_OFF, REL_FORMAT_OFF, REL_NAME_OFF = 32, 38, 42
RF_NAME_OFF, RF_RELATION_OFF, RF_FIELD_ID_OFF = 4, 35, 306
NAME_LEN = 31
REL_RECORD_LEN = REL_NAME_OFF + NAME_LEN            # 73
RF_RECORD_LEN = RF_FIELD_ID_OFF + 2                 # 308


class Column:
    def __init__(self, name, dtype, length, scale=0, sub_type=0):
        self.name, self.dtype = name, dtype
        self.length, self.scale, self.sub_type = length, scale, sub_type
        self.offset = 0


class Table:
    def __init__(self, relation_id, name, columns):
        self.relation_id, self.name = relation_id, name
        self.columns = columns
        self.rows = []
        offset = _round_up((len(columns) + 7) // 8, 4)   # Nullvektor
        for column in columns:
            offset = _round_up(offset, _ALIGN[column.dtype])
            column.offset = offset
            offset += column.length
        self.record_length = _round_up(offset, 4)

    def add(self, **values):
        self.rows.append(values)
        return self


def _round_up(value, to):
    return (value + to - 1) // to * to


def _compress(data):
    """RLE, wie Firebird sie erwartet – hier nur wörtliche Läufe."""
    out = bytearray()
    for start in range(0, len(data), 127):
        chunk = data[start:start + 127]
        out.append(len(chunk))
        out += chunk
    out.append(0)
    return bytes(out)


def _record(data, flags=0, fmt=0):
    """Satzkopf ``rhd`` (13 Byte) plus komprimierte Nutzdaten."""
    return struct.pack('<IIHHB', 1, 0, 0, flags, fmt) + _compress(data)


class FdbBuilder:
    """Sammelt Tabellen und schreibt daraus eine gültige ``.FDB``."""

    def __init__(self, page_size=2048):
        self.page_size = page_size
        self.tables = []
        self._next_relation = FIRST_USER_RELATION

    def table(self, name, columns):
        table = Table(self._next_relation, name, columns)
        self._next_relation += 1
        self.tables.append(table)
        return table

    # ------------------------------------------------------------------
    def _encode(self, table, values, blobs):
        """Eine Zeile in die Satzbytes des Formats gießen."""
        raw = bytearray(table.record_length)
        for index, column in enumerate(table.columns):
            value = values.get(column.name)
            if value is None:
                raw[index // 8] |= 1 << (index % 8)
                continue
            at = column.offset
            if column.dtype == TEXT:
                encoded = str(value).encode('latin-1')[:column.length]
                raw[at:at + len(encoded)] = encoded
                raw[at + len(encoded):at + column.length] = \
                    b' ' * (column.length - len(encoded))
            elif column.dtype == VARYING:
                encoded = str(value).encode('latin-1')[:column.length - 2]
                struct.pack_into('<H', raw, at, len(encoded))
                raw[at + 2:at + 2 + len(encoded)] = encoded
            elif column.dtype == SHORT:
                struct.pack_into('<h', raw, at, int(value))
            elif column.dtype == LONG:
                struct.pack_into('<i', raw, at, int(value))
            elif column.dtype == INT64:
                struct.pack_into('<q', raw, at,
                                 int(round(value / (10 ** column.scale))))
            elif column.dtype == DOUBLE:
                struct.pack_into('<d', raw, at, float(value))
            elif column.dtype == BOOLEAN:
                raw[at] = 1 if value else 0
            elif column.dtype == DATE:
                struct.pack_into('<i', raw, at, _days(value))
            elif column.dtype == TIMESTAMP:
                struct.pack_into('<iI', raw, at, _days(value[:10]),
                                 _ticks(value[11:] or '00:00:00'))
            elif column.dtype == BLOB:
                blobs.append(str(value))
                struct.pack_into('<II', raw, at, table.relation_id,
                                 len(blobs) - 1)
            else:
                raise ValueError(f'unbekannter dtype {column.dtype}')
        return bytes(raw)

    def _descriptor_blob(self, table):
        out = struct.pack('<H', len(table.columns))
        for column in table.columns:
            out += struct.pack('<bbHhHI', column.dtype, column.scale,
                               column.length, column.sub_type, 0, column.offset)
        return out

    # ------------------------------------------------------------------
    def build(self):
        """Alle Seiten erzeugen und als ``bytes`` zurückgeben."""
        pages = [b'']                       # Seite 0 = Header, später gefüllt
        pages_rows = []                     # RDB$PAGES
        relations_rows, fields_rows, formats_rows = [], [], []
        blob_records = {}                   # relation -> Liste von Blobinhalten

        system = [
            (R_RELATION_FIELDS, 'RDB$RELATION_FIELDS'),
            (R_RELATIONS, 'RDB$RELATIONS'),
            (R_FORMATS, 'RDB$FORMATS'),
        ]
        for relation_id, name in system:
            relations_rows.append((relation_id, 0, name))

        for table in self.tables:
            relations_rows.append((table.relation_id, 0, table.name))
            for index, column in enumerate(table.columns):
                fields_rows.append((table.name, column.name, index))

        # Nutztabellen: Sätze und Blobs
        table_records = {}
        for table in self.tables:
            blobs = []
            table_records[table.relation_id] = [
                _record(self._encode(table, row, blobs)) for row in table.rows]
            blob_records[table.relation_id] = blobs

        # RDB$FORMATS: je Tabelle ein Deskriptorblob
        format_blobs = []
        for table in self.tables:
            formats_rows.append((table.relation_id, 0, len(format_blobs)))
            format_blobs.append(self._descriptor_blob(table))
        blob_records[R_FORMATS] = format_blobs

        def emit(relation_id, records, blobs=()):
            """Pointer-Page und so viele Data-Pages wie nötig anlegen.

            Blobs stehen auf der ersten Seite, damit ihre Satznummer gleich der
            Slotnummer ist (Satznummer = Sequenz × max_records + Slot).
            """
            built = _data_pages(self.page_size, relation_id, records, blobs)
            pointer_page = len(pages)
            first_data = pointer_page + 1
            numbers = list(range(first_data, first_data + len(built)))
            pages.append(_pointer_page(self.page_size, relation_id, numbers))
            pages.extend(built)
            pages_rows.append((pointer_page, relation_id))
            return pointer_page

        # Reihenfolge egal – RDB$PAGES kommt zum Schluss und kennt alle.
        layout = []
        for relation_id, records, blobs in (
                [(R_RELATIONS, [_relations_record(r) for r in relations_rows], [])] +
                [(R_RELATION_FIELDS, [_fields_record(f) for f in fields_rows], [])] +
                [(R_FORMATS, [_formats_record(f) for f in formats_rows],
                  blob_records.get(R_FORMATS, []))] +
                [(t.relation_id, table_records[t.relation_id],
                  blob_records.get(t.relation_id, [])) for t in self.tables]):
            layout.append(emit(relation_id, records, blobs))

        # RDB$PAGES selbst (Relation 0)
        pages_pointer = len(pages)
        pages.append(_pointer_page(self.page_size, 0, [len(pages) + 1]))
        pages_rows.append((pages_pointer, 0))
        pages.extend(_data_pages(
            self.page_size, 0, [_pages_record(p, r) for p, r in pages_rows]))

        pages[0] = _header_page(self.page_size, pages_pointer)
        return b''.join(pages)

    def write(self, path):
        with open(path, 'wb') as handle:
            handle.write(self.build())
        return path


# ----------------------------------------------------------------------
# Sätze der Systemtabellen
# ----------------------------------------------------------------------
def _pages_record(page, relation):
    raw = bytearray(18)
    raw[0] = 0xF0                                   # Nullvektor: nichts NULL
    struct.pack_into('<I', raw, 4, page)
    struct.pack_into('<H', raw, 8, relation)
    struct.pack_into('<I', raw, 12, 0)              # Sequenz 0
    struct.pack_into('<H', raw, 16, 4)              # Pointer-Page
    return _record(bytes(raw))


def _relations_record(row):
    relation_id, format_number, name = row
    raw = bytearray(REL_RECORD_LEN)
    struct.pack_into('<H', raw, REL_ID_OFF, relation_id)
    struct.pack_into('<H', raw, REL_FORMAT_OFF, format_number)
    encoded = name.encode('latin-1')[:NAME_LEN]
    raw[REL_NAME_OFF:REL_NAME_OFF + NAME_LEN] = \
        encoded + b' ' * (NAME_LEN - len(encoded))
    return _record(bytes(raw))


def _fields_record(row):
    table, field, field_id = row
    raw = bytearray(RF_RECORD_LEN)
    for offset, value in ((RF_NAME_OFF, field), (RF_RELATION_OFF, table)):
        encoded = value.encode('latin-1')[:NAME_LEN]
        raw[offset:offset + NAME_LEN] = encoded + b' ' * (NAME_LEN - len(encoded))
    struct.pack_into('<h', raw, RF_FIELD_ID_OFF, field_id)
    return _record(bytes(raw))


def _formats_record(row):
    relation_id, format_number, blob_number = row
    raw = bytearray(16)
    raw[0] = 0xF8
    struct.pack_into('<h', raw, 4, relation_id)
    struct.pack_into('<h', raw, 6, format_number)
    struct.pack_into('<II', raw, 8, R_FORMATS, blob_number)
    return _record(bytes(raw))


def _blob_record(content):
    """``blh`` mit Level 0: Kopf 28 Byte, dann ein Segment mit Längenpräfix."""
    payload = content.encode('latin-1') if isinstance(content, str) else content
    head = bytearray(28)
    struct.pack_into('<H', head, 8, len(payload))       # max_segment
    struct.pack_into('<H', head, 10, 16)                # flags: rhd_blob
    head[12] = 0                                        # level 0
    struct.pack_into('<I', head, 16, 1)                 # ein Segment
    struct.pack_into('<I', head, 20, len(payload))      # Gesamtlänge
    struct.pack_into('<H', head, 24, 0)                 # sub_type
    return bytes(head) + struct.pack('<H', len(payload)) + payload


# ----------------------------------------------------------------------
# Seiten
# ----------------------------------------------------------------------
def _header_page(page_size, pages_pointer):
    page = bytearray(page_size)
    page[0] = 1                                   # pag_header
    struct.pack_into('<HH', page, 16, page_size, 0x800C)   # ODS 12
    struct.pack_into('<I', page, 20, pages_pointer)
    struct.pack_into('<H', page, 42, 0x12)        # Dialekt 3, nicht verschlüsselt
    return bytes(page)


def _pointer_page(page_size, relation_id, data_pages):
    page = bytearray(page_size)
    page[0] = 4
    struct.pack_into('<IIHH', page, 16, 0, 0, len(data_pages), relation_id)
    struct.pack_into(f'<{len(data_pages)}I', page, 32, *data_pages)
    return bytes(page)


def _data_pages(page_size, relation_id, records, blobs=()):
    """Sätze auf so viele Datenseiten verteilen, wie nötig.

    Auf einer Seite liegen die Slots (je 4 Byte) vorn und die Sätze hinten;
    beides wächst aufeinander zu.
    """
    # Blobs zuerst: ihre Satznummer ist die Slotnummer.
    entries = [_blob_record(b) for b in blobs] + list(records)
    max_records = (page_size - 24) // 17          # wie im Leser
    built, batch, used = [], [], 0

    def flush(sequence):
        page = bytearray(page_size)
        page[0] = 5
        cursor = page_size
        slots = []
        for entry in batch:
            cursor -= len(entry)
            page[cursor:cursor + len(entry)] = entry
            slots.append((cursor, len(entry)))
        struct.pack_into('<IHH', page, 16, sequence, relation_id, len(slots))
        for index, (offset, length) in enumerate(slots):
            struct.pack_into('<HH', page, 24 + index * 4, offset, length)
        built.append(bytes(page))

    for entry in entries:
        needed = used + len(entry) + (len(batch) + 1) * 4 + 24
        if batch and (needed > page_size or len(batch) >= max_records):
            flush(len(built))
            batch, used = [], 0
        if len(entry) + 28 > page_size:
            raise ValueError('Satz größer als eine Seite – page_size erhöhen')
        batch.append(entry)
        used += len(entry)
    flush(len(built))
    return built


def _days(iso_date):
    return (datetime.date.fromisoformat(iso_date) - EPOCH).days


def _ticks(clock):
    hours, minutes, seconds = (int(p) for p in clock.split(':'))
    return ((hours * 3600) + (minutes * 60) + seconds) * 10000
