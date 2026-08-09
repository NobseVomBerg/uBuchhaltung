# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Systemkatalog einer Firebird-Datei: Tabellennamen, Spalten, Werte.

Aufsetzend auf :mod:`.ods` liefert dieses Modul aus rohen Sätzen benannte
Python-Werte. Dafür braucht es drei Systemtabellen:

======================  ====================================================
``RDB$RELATIONS`` (6)   Tabellennamen, Relations-Id, aktuelle Formatnummer
``RDB$RELATION_FIELDS`` Spaltennamen samt Feld-Id
``RDB$FORMATS`` (8)     je (Relation, Format) ein Blob mit den Deskriptoren
======================  ====================================================

Der Deskriptorblob ist die eigentliche Quelle der Wahrheit: er nennt für jede
Spalte Typ, Länge, Nachkommastellen und **Byte-Offset im Satz**. Eine Tabelle
kann mehrere Formate haben (jedes ``ALTER TABLE`` erzeugt eines); welches gilt,
steht im Satzkopf. Genau deshalb wird pro Satz nachgeschlagen und nicht einmal
pro Tabelle.

Für Systemtabellen selbst gibt es keinen Deskriptorblob – ihre Offsets sind
hier fest hinterlegt und in den Konstanten dokumentiert.
"""
import datetime
import struct

from .ods import OdsFile

#: Firebird zählt Tage ab dem 17.11.1858 (Modified Julian Date).
EPOCH = datetime.date(1858, 11, 17)

# dsc_dtype aus Firebirds ``dsc.h``
D_TEXT, D_CSTRING, D_VARYING = 1, 2, 3
D_BYTE, D_SHORT, D_LONG, D_QUAD = 7, 8, 9, 10
D_REAL, D_DOUBLE, D_DFLOAT = 11, 12, 13
D_DATE, D_TIME, D_TIMESTAMP = 14, 15, 16
D_BLOB, D_ARRAY, D_INT64, D_DBKEY, D_BOOLEAN = 17, 18, 19, 20, 21

# Relations-Ids der benötigten Systemtabellen
REL_FIELDS, REL_RELATION_FIELDS, REL_RELATIONS, REL_FORMATS = 2, 5, 6, 8

#: ``RDB$RELATIONS``: drei Blobs (je 8 Byte, ab 8) vor den Smallints.
REL_ID_OFF, REL_FORMAT_OFF, REL_NAME_OFF = 32, 38, 42
#: ``RDB$RELATION_FIELDS``: Nullvektor 4 Byte, dann char(31)-Felder;
#: FIELD_POSITION liegt bei 290, FIELD_ID bei 306.
RF_NAME_OFF, RF_RELATION_OFF, RF_FIELD_ID_OFF = 4, 35, 306
#: Metadatennamen sind ``CHAR(31)``.
NAME_LEN = 31

#: Firebird kennt keine Relations-Ids < 128 für Anwendertabellen.
FIRST_USER_RELATION = 128


class Column:
    """Eine Spalte: Name, Typ und Lage im Satz."""

    __slots__ = ('index', 'name', 'dtype', 'scale', 'length', 'sub_type',
                 'flags', 'offset')

    def __init__(self, index, descriptor):
        (self.dtype, self.scale, self.length, self.sub_type,
         self.flags, self.offset) = descriptor
        self.index = index
        self.name = f'F{index}'

    def __repr__(self):
        return f'<Column {self.name} dtype={self.dtype} @{self.offset}>'


def _text(raw, offset, length=NAME_LEN):
    return raw[offset:offset + length].decode('latin-1').rstrip('\x00 ')


class Catalog:
    """Benannter Zugriff auf die Tabellen einer ``.FDB``-Datei."""

    def __init__(self, path):
        self.ods = OdsFile(path)
        self._descriptor_blobs = {}
        self._columns = {}
        self._field_names = {}
        self.relation_id = {}
        self.relation_name = {}
        self.relation_format = {}
        self._load_formats()
        self._load_relations()
        self._load_field_names()

    def close(self):
        self.ods.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    def _load_formats(self):
        """``RDB$FORMATS``: Nullvektor 4, Relation 4, Format 6, Blob-Id 8."""
        for _fmt, raw in self.ods.rows(REL_FORMATS):
            if len(raw) < 16:
                continue
            relation = struct.unpack_from('<h', raw, 4)[0]
            number = struct.unpack_from('<h', raw, 6)[0]
            self._descriptor_blobs[(relation, number)] = \
                struct.unpack_from('<II', raw, 8)

    def _load_relations(self):
        for _fmt, raw in self.ods.rows(REL_RELATIONS):
            if len(raw) < REL_NAME_OFF + NAME_LEN:
                continue
            name = _text(raw, REL_NAME_OFF)
            if not name or not name[0].isalpha():
                continue
            rid = struct.unpack_from('<H', raw, REL_ID_OFF)[0]
            self.relation_id[name] = rid
            self.relation_name[rid] = name
            self.relation_format[rid] = struct.unpack_from(
                '<H', raw, REL_FORMAT_OFF)[0]

    def _load_field_names(self):
        for _fmt, raw in self.ods.rows(REL_RELATION_FIELDS):
            if len(raw) < RF_FIELD_ID_OFF + 2:
                continue
            field = _text(raw, RF_NAME_OFF)
            relation = _text(raw, RF_RELATION_OFF)
            rid = self.relation_id.get(relation)
            if rid is None or not field:
                continue
            field_id = struct.unpack_from('<h', raw, RF_FIELD_ID_OFF)[0]
            if field_id >= 0:
                self._field_names[(rid, field_id)] = field
        self._columns.clear()

    # ------------------------------------------------------------------
    def columns(self, relation_id, format_number):
        """Spalten eines Satzformats; leer, wenn kein Deskriptor vorliegt.

        Der Blob beginnt mit der Spaltenanzahl (2 Byte), danach folgen
        Deskriptoren zu je 12 Byte.
        """
        key = (relation_id, format_number)
        if key in self._columns:
            return self._columns[key]
        location = self._descriptor_blobs.get(key)
        columns = []
        if location:
            blob = self.ods.blob(*location)
            if len(blob) >= 2:
                count = struct.unpack_from('<H', blob, 0)[0]
                for index in range(count):
                    at = 2 + index * 12
                    if at + 12 > len(blob):
                        break
                    column = Column(index, struct.unpack_from('<bbHhHI', blob, at))
                    column.name = self._field_names.get(
                        (relation_id, index), column.name)
                    columns.append(column)
        self._columns[key] = columns
        return columns

    def table_names(self, user_only=True):
        """Tabellennamen; ohne Systemtabellen, wenn ``user_only``."""
        names = sorted(self.relation_id)
        if not user_only:
            return names
        return [n for n in names
                if self.relation_id[n] >= FIRST_USER_RELATION
                and not n.startswith(('RDB$', 'MON$', 'SEC$'))]

    # ------------------------------------------------------------------
    @staticmethod
    def value(raw, column):
        """Einen Spaltenwert aus den Satzbytes lesen."""
        offset, length, dtype = column.offset, column.length, column.dtype
        if offset + length > len(raw):
            return None
        if dtype in (D_TEXT, D_CSTRING):
            return raw[offset:offset + length].decode('latin-1').rstrip('\x00 ')
        if dtype == D_VARYING:
            used = struct.unpack_from('<H', raw, offset)[0]
            used = min(used, length - 2)
            return raw[offset + 2:offset + 2 + used].decode('latin-1')
        if dtype == D_BOOLEAN:
            return bool(raw[offset])
        if dtype == D_BYTE:
            return struct.unpack_from('<b', raw, offset)[0]
        if dtype in (D_SHORT, D_LONG, D_INT64):
            code = {D_SHORT: '<h', D_LONG: '<i', D_INT64: '<q'}[dtype]
            number = struct.unpack_from(code, raw, offset)[0]
            return number * (10 ** column.scale) if column.scale else number
        if dtype == D_REAL:
            return struct.unpack_from('<f', raw, offset)[0]
        if dtype in (D_DOUBLE, D_DFLOAT):
            return struct.unpack_from('<d', raw, offset)[0]
        if dtype == D_DATE:
            days = struct.unpack_from('<i', raw, offset)[0]
            return (EPOCH + datetime.timedelta(days=days)).isoformat()
        if dtype == D_TIME:
            return _clock(struct.unpack_from('<I', raw, offset)[0])
        if dtype == D_TIMESTAMP:
            days, ticks = struct.unpack_from('<iI', raw, offset)
            date = (EPOCH + datetime.timedelta(days=days)).isoformat()
            return f'{date} {_clock(ticks)}'
        if dtype in (D_BLOB, D_ARRAY):
            return struct.unpack_from('<II', raw, offset)   # (Relation, Satz)
        return raw[offset:offset + length]

    def rows(self, table, blobs=False):
        """Sätze einer Tabelle als ``dict`` (Spaltenname → Wert).

        Mit ``blobs=True`` werden Blob-Verweise gleich aufgelöst – Textblobs
        (``sub_type == 1``) zu ``str``, alles andere zu ``bytes``.
        """
        relation_id = self.relation_id[table]
        for format_number, raw in self.ods.rows(relation_id):
            columns = self.columns(relation_id, format_number)
            if not columns or not raw:
                continue
            null_bytes = raw[:(len(columns) + 7) // 8]
            row = {}
            for column in columns:
                index = column.index
                if (index // 8 < len(null_bytes)
                        and null_bytes[index // 8] >> (index % 8) & 1):
                    row[column.name] = None
                    continue
                value = self.value(raw, column)
                if blobs and column.dtype == D_BLOB and isinstance(value, tuple):
                    content = self.ods.blob(*value)
                    value = None if not content else (
                        content.decode('latin-1') if column.sub_type == 1
                        else content)
                row[column.name] = value
            yield row

    def count(self, table):
        return sum(1 for _ in self.rows(table))


def _clock(ticks):
    """Firebird speichert Uhrzeiten in Zehntausendstelsekunden."""
    seconds = ticks // 10000
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
