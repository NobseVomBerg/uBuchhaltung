# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Fassungen und Protokoll einer Rechnung (seit 2.2.0, Schema 2002).

Eine versendete Rechnung hat Finanzangaben, die sich nicht mehr ändern (``db/invoice_lock.py``).
Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum bleiben änderbar – als
**Berichtigung** (``correct_invoice``): Das Dokument, das der Kunde bekommen hat (PDF, XML), wandert
unverändert nach ``invoices/<Jahr>/Fassungen/<ID>/`` und wird in ``InvoiceDocumentVersions`` festgehalten
(Fassung, Pfad, SHA-256), jedes geänderte Feld steht mit altem und neuem Wert im Protokoll
``InvoiceCorrections``, und die Rechnung zählt ihre Fassung (``Invoices.DocumentVersion``) um eins hoch. Die
neue Fassung trägt einen Hinweis (``correction_notice``) und muss dem Kunden erneut zugehen.

Das Protokoll führt auch die Statuswechsel (``Action='status'``, geschrieben von ``set_invoice_status``), und
aus ihm und aus ``Invoices.SentAt`` ergibt sich, ob eine Rechnung schon einmal versendet war
(``was_sent_before``).

Zeitpunkte stehen in UTC (``CURRENT_TIMESTAMP``); angezeigt und in Dokumenten genannt werden sie in der
Ortszeit (``local_date``).
"""
import datetime
import hashlib
import os
import re
import sqlite3
from collections import namedtuple

from .invoice_lock import (
    CHANGED_MESSAGE, CLOSED, COLUMN_OF, EDITABLE, FIELDS, FROZEN_STATUSES, LABEL_OF,
    DocumentNotFound, InvoiceLocked, _Refusal, as_document_version, buyer_snapshot, changed_open_fields,
    check_changes, closed_message, document_version, invoice_row, is_storno, lock_message, row_group)

CORRECTION_ARCHIVE_FAILED_MESSAGE = ('Die bisherigen Dokumente (PDF/XML) ließen sich nicht in den Ordner '
                                     '„Fassungen“ verschieben – die Rechnung wurde nicht berichtigt.')
CORRECTION_FAILED_MESSAGE = ('Die Berichtigung konnte nicht gespeichert werden – die Rechnung und ihre '
                             'Dokumente sind unverändert.')
DELIVERY_DATE_MESSAGE = 'Das Leistungsdatum fehlt oder ist kein gültiges Datum – eine Rechnung braucht es.'


class CorrectionInvalid(ValueError):
    """Eine Berichtigung enthält eine ungültige Angabe (``str(exc)`` ist der Klartext): Nichts wurde geändert."""


_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')

#: Ergebnis von ``correct_invoice``: ob berichtigt wurde (sonst war nichts anders als gespeichert), die Nummer
#: der jetzt aktuellen Fassung und wie viele Dokumente dabei nach „Fassungen“ wanderten
Correction = namedtuple('Correction', 'corrected version archived')

#: Der Hinweis in der berichtigten Fassung: ihre Nummer, das Datum der Berichtigung, das Datum der ersetzten
#: Fassung (``None``, wenn unbekannt) und die geänderten Angaben im Klartext
CorrectionNotice = namedtuple('CorrectionNotice', 'version corrected_on replaces_on fields')

#: Spalten der Anschrift (im Hinweis zusammen „Anschrift“)
ADDRESS_COLUMNS = ('BuyerName', 'BuyerCompany', 'BuyerStreet', 'BuyerPostalCode', 'BuyerCity', 'BuyerCountry')

#: Bezeichnung einer Protokollzeile in der Oberfläche
PROTOCOL_LABELS = {
    'PaymentTerms': 'Zahlungshinweis', 'OrderNumber': 'Bestellnummer', 'BuyerRouteID': 'Leitweg-ID',
    'DeliveryDate': 'Leistungsdatum', 'Status': 'Status',
    'BuyerName': 'Anschrift: Name', 'BuyerCompany': 'Anschrift: Firma', 'BuyerStreet': 'Anschrift: Straße',
    'BuyerPostalCode': 'Anschrift: PLZ', 'BuyerCity': 'Anschrift: Ort', 'BuyerCountry': 'Anschrift: Land',
}
_NOTICE_LABELS = {'PaymentTerms': 'Zahlungshinweis', 'OrderNumber': 'Bestellnummer',
                  'BuyerRouteID': 'Leitweg-ID', 'BuyerReference': 'Leitweg-ID',
                  'DeliveryDate': 'Leistungsdatum', **{column: 'Anschrift' for column in ADDRESS_COLUMNS}}
#: Reihenfolge der geänderten Angaben im Hinweis
_NOTICE_ORDER = ('Leistungsdatum', 'Anschrift', 'Zahlungshinweis', 'Bestellnummer', 'Leitweg-ID')

#: Aus diesen Status ist eine Rechnung hervorgegangen, wenn sie versendet war (Altwert ``partial`` eingeschlossen)
_SENT_STATUSES = tuple(sorted(FROZEN_STATUSES))
#: Die bearbeitbaren Status: Ziel des Zurückholens und Ausgangspunkt eines Versands
_EDITABLE = ('draft', 'finalized')


# ── Zeitpunkte ───────────────────────────────────────────────────────────────

def local_datetime(timestamp):
    """Ein gespeicherter Zeitpunkt (``JJJJ-MM-TT HH:MM:SS``, UTC wie ``CURRENT_TIMESTAMP``) in der Ortszeit;
    ``None``, wenn er sich nicht lesen lässt."""
    text = str(timestamp or '').strip()
    try:
        moment = datetime.datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)


def is_iso_date(value):
    """Ist ``value`` ein ISO-Datum ``JJJJ-MM-TT`` – ein Kalendertag, den es gibt, Jahr 1900–2200, ohne
    Leerraum und ohne andere Schreibweise (dieselbe Regel wie ``server.period.parse_iso_date``; die
    Datenbankschicht lädt das Server-Package nicht)? Gilt für das Leistungsdatum einer Berichtigung und das
    Stornodatum (``cancel_invoice``)."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return False
    try:
        return 1900 <= datetime.date.fromisoformat(value).year <= 2200
    except ValueError:
        return False


def local_date(timestamp):
    """Das Datum eines gespeicherten Zeitpunkts in der Ortszeit (reines Datum ohne Uhrzeit bleibt, wie es
    ist); ``None``, wenn er sich nicht lesen lässt."""
    text = str(timestamp or '').strip()
    if len(text) == 10:
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            return None
    moment = local_datetime(text)
    return moment.date() if moment else None


# ── Was ein Protokolleintrag festhält ────────────────────────────────────────

def _as_text(value):
    return None if value is None else str(value)


def _as_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def change_entries(row, open_data):
    """Die Protokollzeilen einer Berichtigung: ``[(Spalte, alter Wert, neuer Wert)]`` zu den geänderten Angaben
    ``open_data`` (Schlüssel wie bei ``insert_invoice``) der gespeicherten Rechnung ``row``, in der Reihenfolge
    von ``FIELDS``. Die Leitweg-ID, die in zwei Spalten steht, ist eine Angabe (``BuyerRouteID``); jede
    Spalte der Anschrift steht für sich."""
    entries, leitweg = [], False
    for key, column, _kind, _label in FIELDS:
        if key not in open_data:
            continue
        if key in ('buyer_reference', 'buyer_route_id'):
            if leitweg:
                continue
            leitweg = True
            source = 'buyer_route_id' if 'buyer_route_id' in open_data else 'buyer_reference'
            entries.append(('BuyerRouteID', _as_text(row.get(COLUMN_OF[source])), _as_text(open_data[source])))
            continue
        entries.append((column, _as_text(row.get(column)), _as_text(open_data[key])))
    return entries


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


class InvoiceVersionsMixin:
    def correct_invoice(self, invoice_id, changes, expected_version, user_name, refresh_address=False,
                        items=None):
        """Eine versendete Rechnung berichtigen: die änderbaren Angaben in ``changes`` (Schlüssel wie bei
        ``insert_invoice``) schreiben und dabei eine neue Fassung anlegen – in EINER Transaktion
        (``BEGIN IMMEDIATE``, die Rechnung wird darin neu gelesen).

        Gibt es keine Abweichung vom Gespeicherten, passiert nichts (keine Fassung, kein Protokoll). Sonst:

        * ``expected_version`` muss die Nummer der aktuellen Fassung sein (``Invoices.DocumentVersion``, NULL
          gilt als 1) – der Schutz davor, eine inzwischen anderweitig berichtigte Rechnung zu überschreiben;
        * die abgelegten Dokumente (PDF/XML) wandern nach ``Fassungen/<ID>/`` und werden in
          ``InvoiceDocumentVersions`` festgehalten (Fassung, Pfad, SHA-256), ihre Pfade in der Rechnung werden
          geleert; gab es keine (Import), bleibt es bei Feldern und Protokoll;
        * jede geänderte Angabe steht im Protokoll (``Action='correct'``: wer, wann, alt, neu, neue Fassung);
        * ``DocumentVersion`` zählt um eins hoch.

        ``refresh_address`` übernimmt die Anschrift neu aus den Stammdaten desselben Kunden (je geänderter
        Spalte eine Protokollzeile); ``items``: die Positionen des kompletten Formulars, nur zum Vergleich –
        sie sind gesperrt. Das neue Dokument erzeugt der Aufrufer nach dem Commit.

        Das Leistungsdatum ist Pflicht (§ 14 UStG): Steht es unter den geänderten Angaben, muss der neue Wert ein
        ISO-Datum sein – eine Berichtigung leert es nicht und füllt es nicht mit Text. Ein unverändertes
        Leistungsdatum, auch ein leeres (Import), ist kein Verstoß. Das wird vor dem Verschieben der Dokumente
        geprüft.

        Scheitert das Verschieben, das Festhalten der Fassung oder das Speichern, liegen die Dokumente wieder dort,
        wo sie waren, und nichts ist geändert. Raises :class:`InvoiceLocked` (mit Klartext) bei einer gesperrten
        Angabe, einer stornierten Rechnung oder Stornorechnung (beide ganz gesperrt), einer veralteten Fassung
        oder einem Fehler beim Speichern,
        :class:`CorrectionInvalid` bei einem ungültigen Leistungsdatum, :class:`DocumentNotFound`, wenn es die
        Rechnung nicht gibt. Returns :class:`Correction`."""
        unknown = sorted(key for key in changes if key not in COLUMN_OF)
        if unknown:
            raise ValueError(f'unbekannte Rechnungsfelder: {", ".join(unknown)}')
        moved = []
        try:
            with self.transaction() as tx:
                conn = tx.conn
                conn.execute('BEGIN IMMEDIATE')
                row = invoice_row(conn, invoice_id)
                if row is None:
                    raise DocumentNotFound('Rechnung nicht gefunden.')
                group = row_group(row)
                if group == EDITABLE:
                    raise InvoiceLocked(CHANGED_MESSAGE)        # inzwischen zurückgeholt: die Maske ist veraltet
                version = document_version(row)
                changes = dict(changes)
                if refresh_address and row['CustomerId']:
                    customer = self.get_contact_by_id(row['CustomerId'])
                    if customer:
                        changes.update(buyer_snapshot(customer))
                stored_items = None
                if items is not None:
                    stored_items = conn.execute(
                        'SELECT Description, Quantity, Unit, PricePerUnit, TaxRate FROM InvoiceItems '
                        'WHERE InvoiceId = ? ORDER BY Position, ID', (invoice_id,)).fetchall()
                problems = check_changes(row, changes, stored_items, items)
                if problems:
                    payments = conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?',
                                            (invoice_id,)).fetchone()[0]
                    raise InvoiceLocked(lock_message(row['Status'], payments, is_storno(row)), problems)
                open_data = changed_open_fields(row, changes)
                if group == CLOSED:
                    if open_data:
                        raise InvoiceLocked(closed_message(row), [LABEL_OF[key] or key for key in open_data])
                    return Correction(False, version, 0)
                if not open_data:
                    return Correction(False, version, 0)
                if 'delivery_date' in open_data and not is_iso_date(open_data['delivery_date']):
                    raise CorrectionInvalid(DELIVERY_DATE_MESSAGE)
                if _as_int(expected_version) != version:
                    raise InvoiceLocked(CHANGED_MESSAGE)
                new_version = version + 1
                now = conn.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
                moved = self._archive_documents(conn, invoice_id, version, 'corrected', now,
                                                CORRECTION_ARCHIVE_FAILED_MESSAGE)
                self.update_invoice_fields(tx, invoice_id, open_data, correcting=True)
                for column, old, new in change_entries(row, open_data):
                    self._log_invoice_change(conn, invoice_id, row['InvoiceNumber'], user_name, 'correct',
                                             column, old, new, new_version, now)
                if conn.execute(
                        'UPDATE Invoices SET DocumentVersion = ?, PDFPath = NULL, XMLPath = NULL, '
                        'UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ? AND COALESCE(DocumentVersion, 1) = ?',
                        (new_version, invoice_id, version)).rowcount != 1:
                    raise InvoiceLocked(CHANGED_MESSAGE)
        except _Refusal as refusal:                              # das Verschieben ist gescheitert (dort zurückgelegt)
            raise InvoiceLocked(str(refusal)) from None
        except (InvoiceLocked, DocumentNotFound, CorrectionInvalid):
            self._restore_archived(moved)
            raise
        except (sqlite3.Error, OSError) as exc:
            print(f"Berichtigung der Rechnung {invoice_id} nicht gespeichert: {exc}")
            self._restore_archived(moved)
            raise InvoiceLocked(CORRECTION_FAILED_MESSAGE) from None
        except BaseException:
            self._restore_archived(moved)
            raise
        return Correction(True, new_version, len(moved))

    def _archive_documents(self, conn, invoice_id, version, reason, now, failure_message):
        """PDF und XML der Rechnung nach ``Fassungen/<ID>/`` verschieben (``export.invoice_files.
        archive_sent_documents``) und je Datei eine Zeile in ``InvoiceDocumentVersions`` schreiben, in der
        Transaktion von ``conn``: ``version`` ist die Fassung, die dabei abgelegt wird, ``reason`` ``'corrected'``
        oder ``'reopened'``, ``now`` der Zeitpunkt. Die Pfade in der Rechnung leert der Aufrufer.

        Entweder gelingt beides, oder die Dateien liegen wieder am alten Platz und es folgt
        :class:`_Refusal` (``failure_message``) – auch wenn das Festhalten an der Datenbank scheitert
        (``sqlite3.Error``). Jede andere Ausnahme legt sie ebenfalls zurück und wird weitergereicht: Der Aufrufer
        kennt die Verschiebungen erst, wenn diese Methode sie liefert, und könnte sie sonst nicht zurücklegen.
        Hängen mehrere Dateien einer Art an der Rechnung, bekommt nur die erste eine Zeile; die übrigen liegen im
        Ordner und erscheinen als Dateien ohne Fassungsnummer.
        Returns die Verschiebungen ``[(alter Pfad, neuer Pfad)]``."""
        from export.invoice_files import archive_sent_documents, documents_root, restore_archived
        try:
            moved = archive_sent_documents(self, invoice_id)
        except OSError as exc:
            print(f"Dokumente der Rechnung {invoice_id} nicht abgelegt: {exc}")
            raise _Refusal(failure_message, 409) from None
        try:
            root, kinds = documents_root(), set()
            for _old, new in moved:
                kind = 'xml' if new.lower().endswith('.xml') else 'pdf'
                if kind in kinds:
                    continue
                kinds.add(kind)
                conn.execute(
                    'INSERT OR IGNORE INTO InvoiceDocumentVersions '
                    '(InvoiceID, Version, Kind, RelPath, Sha256, ArchivedAt, Reason) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (invoice_id, version, kind, os.path.relpath(new, root).replace(os.sep, '/'),
                     _sha256(new), now, reason))
        except (OSError, sqlite3.Error) as exc:
            print(f"Dokumente der Rechnung {invoice_id} nicht festgehalten: {exc}")
            restore_archived(moved)
            raise _Refusal(failure_message, 409) from None
        except BaseException:
            restore_archived(moved)
            raise
        return moved

    @staticmethod
    def _log_invoice_change(conn, invoice_id, number, user_name, action, field, old, new, version, now=None):
        """Eine Zeile ins Protokoll ``InvoiceCorrections`` schreiben (in der Transaktion von ``conn``):
        ``action`` ``'correct'`` oder ``'status'``, ``version`` die Fassung, die durch den Eintrag entstand
        bzw. gilt."""
        conn.execute(
            'INSERT INTO InvoiceCorrections '
            '(InvoiceID, InvoiceNumber, Version, ChangedAt, UserName, Action, Field, OldValue, NewValue) '
            'VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?)',
            (invoice_id, number, version, now, user_name or None, action, field, old, new))

    def was_sent_before(self, invoice_id, conn=None):
        """Wurde diese Rechnung schon einmal versendet? Dann bleibt ihre Nummer vergeben (der Entwurf gibt sie
        beim Löschen nicht zurück und lässt sie nicht ändern).

        Das sagt ``Invoices.SentAt`` (gesetzt beim Versenden), ein Statuswechsel aus einem versendeten Status im
        Protokoll – und bei einem Bestand aus 2.1.2, der beides nicht kennt, eine Datei im Ordner
        ``Fassungen/<ID>`` (``export.invoice_files.was_sent_before``). Mit ``conn`` in dessen Transaktion."""
        own = conn is None
        conn = self._get_connection() if own else conn
        try:
            if conn.execute('SELECT 1 FROM Invoices WHERE ID = ? AND SentAt IS NOT NULL',
                            (invoice_id,)).fetchone():
                return True
            marks = ','.join('?' * len(_SENT_STATUSES))
            if conn.execute(
                    "SELECT 1 FROM InvoiceCorrections WHERE InvoiceID = ? AND Action = 'status' "
                    f"AND Field = 'Status' AND OldValue IN ({marks}) LIMIT 1",
                    (invoice_id, *_SENT_STATUSES)).fetchone():
                return True
        finally:
            if own:
                conn.close()
        from export.invoice_files import was_sent_before
        return was_sent_before(invoice_id)

    def get_invoice_protocol(self, invoice_id):
        """Das Protokoll einer Rechnung, ältester Eintrag zuerst: ``[(ChangedAt, UserName, Action, Field,
        OldValue, NewValue, Version)]``."""
        conn = self._get_connection()
        try:
            return conn.execute(
                'SELECT ChangedAt, UserName, Action, Field, OldValue, NewValue, Version '
                'FROM InvoiceCorrections WHERE InvoiceID = ? ORDER BY ID', (invoice_id,)).fetchall()
        finally:
            conn.close()

    def get_document_versions(self, invoice_id):
        """Die abgelegten Dokumente einer Rechnung: ``[(Version, Kind, RelPath, Sha256, ArchivedAt, Reason)]``,
        die älteste Fassung zuerst."""
        conn = self._get_connection()
        try:
            return conn.execute(
                'SELECT Version, Kind, RelPath, Sha256, ArchivedAt, Reason FROM InvoiceDocumentVersions '
                'WHERE InvoiceID = ? ORDER BY Version, Kind, ID', (invoice_id,)).fetchall()
        finally:
            conn.close()

    def correction_notice(self, invoice_id):
        """Der Hinweis, den jedes Dokument der aktuellen Fassung trägt, wenn sie eine Berichtigung ist –
        sonst ``None``. Er ergibt sich aus dem Stand der Datenbank, nicht aus einem Parameter: Auch ein später
        über „PDF + E-Rechnung“ erzeugtes Dokument trägt ihn.

        Eine berichtigte Fassung ist die, deren Nummer über 1 liegt und zu der das Protokoll Berichtigungen
        kennt (``Action='correct'``, ``Version`` = die aktuelle). Wurde die Rechnung danach auf Entwurf oder
        Abgeschlossen zurückgeholt, ist sie keine mehr: Eine neu versendete Rechnung trägt keinen Hinweis.

        Das Datum der ersetzten Fassung ist das des jüngsten Protokolleintrags vor der ersten Berichtigung der
        aktuellen Fassung, der eine Berichtigung oder ein Versand ist (Entwurf/abgeschlossen → versendet): So
        heißt es nach Zurückholen und erneutem Versenden das des zweiten Versands, nicht des ersten. Gibt es
        keinen (Bestand ohne Protokoll), ist es ``Invoices.SentAt``, sonst unbekannt (``None``)."""
        conn = self._get_connection()
        try:
            head = conn.execute('SELECT DocumentVersion, SentAt FROM Invoices WHERE ID = ?',
                                (invoice_id,)).fetchone()
            if head is None:
                return None
            version = as_document_version(head[0])
            if version <= 1:
                return None
            entries = conn.execute(
                "SELECT ID, Field, ChangedAt FROM InvoiceCorrections "
                "WHERE InvoiceID = ? AND Action = 'correct' AND Version = ? ORDER BY ID",
                (invoice_id, version)).fetchall()
            if not entries:
                return None
            marks = ','.join('?' * len(_EDITABLE))
            if conn.execute(
                    "SELECT 1 FROM InvoiceCorrections WHERE InvoiceID = ? AND Action = 'status' AND ID > ? "
                    f"AND NewValue IN ({marks}) LIMIT 1",
                    (invoice_id, entries[-1][0], *_EDITABLE)).fetchone():
                return None
            corrected_on = local_date(entries[0][2])
            if corrected_on is None:
                return None
            replaced = conn.execute(
                "SELECT ChangedAt FROM InvoiceCorrections WHERE InvoiceID = ? AND ID < ? AND (Action = 'correct' "
                "OR (Action = 'status' AND Field = 'Status' AND NewValue = 'sent' "
                f"AND OldValue IN ({marks}))) ORDER BY ID DESC LIMIT 1",
                (invoice_id, entries[0][0], *_EDITABLE)).fetchone()
            replaces_on = local_date(replaced[0]) if replaced else local_date(head[1])
        finally:
            conn.close()
        labels = {_NOTICE_LABELS[field] for _id, field, _at in entries if field in _NOTICE_LABELS}
        fields = tuple(label for label in _NOTICE_ORDER if label in labels)
        return CorrectionNotice(version, corrected_on, replaces_on, fields)
