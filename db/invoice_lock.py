# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Sperre versendeter Rechnungen und Statusübergänge.

Es gibt genau EINE Stelle, die entscheidet, was an einer gespeicherten Rechnung noch änderbar
ist und welchen Status sie als Nächstes bekommen darf. Jeder Schreibweg auf eine Rechnung geht
darüber: ``update_invoice_fields`` (Speichern im Formular) und ``set_invoice_status``
(Statuswechsel). Zwischenstand bis zum geführten Berichtigen mit Fassungen und Protokoll und bis
zum Stornobeleg (``docs/aufgaben.md``).

Statusgruppen:

* **bearbeitbar** (``draft``, ``finalized``): alles änderbar.
* **gesperrt** (``sent``, ``overdue``, ``partial_payment``, ``partial``, ``paid``): Finanzangaben –
  Nummer, Rechnungsdatum, Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto,
  Bank und Käufer-USt-IdNr. – sind gesperrt. Weiter änderbar bleiben Begleittext, Anschrift des
  Kunden, Bestellnummer, Leitweg-ID und Leistungsdatum (ohne Fassung und Protokoll).
* **abgeschlossen** (``cancelled``): nichts mehr änderbar.

Verglichen wird mit dem gespeicherten Stand, in der Transaktion des Schreibens (``BEGIN
IMMEDIATE``): Was das Formular unverändert zurückschickt, ist kein Verstoß – Beträge in Minor
Units, Positionen Feld für Feld. Status, ``AmountDue`` und Dokumentpfade kommen nie vom Client.
"""
from collections import namedtuple
from decimal import Decimal, InvalidOperation

from money import to_minor
from .core import fk_explained

EDITABLE_STATUSES = frozenset({'draft', 'finalized'})
LOCKED_STATUSES = frozenset({'sent', 'overdue', 'partial_payment', 'partial', 'paid'})
CLOSED_STATUSES = frozenset({'cancelled'})
FROZEN_STATUSES = LOCKED_STATUSES | CLOSED_STATUSES

STATUS_LABELS = {
    'draft':           'Entwurf',
    'finalized':       'Abgeschlossen',
    'sent':            'Versendet',
    'partial_payment': 'Teilzahlung',
    'overdue':         'Überfällig',
    'paid':            'Bezahlt',
    'cancelled':       'Storniert',
}

EDITABLE, LOCKED, CLOSED = 'editable', 'locked', 'closed'


def canonical_status(status):
    """Status wie die Oberfläche ihn kennt: ohne Angabe ``draft``, der Alt-Wert ``partial`` ist
    ``partial_payment``."""
    if not status:
        return 'draft'
    return 'partial_payment' if status == 'partial' else status


def status_group(status):
    """``'editable'``, ``'locked'`` oder ``'closed'``. Ein unbekannter Status gilt als gesperrt:
    Lieber nichts ändern lassen, als eine Rechnung in einem unklaren Zustand zu überschreiben."""
    status = canonical_status(status)
    if status in EDITABLE_STATUSES:
        return EDITABLE
    if status in CLOSED_STATUSES:
        return CLOSED
    return LOCKED


# ── Felder ───────────────────────────────────────────────────────────────────
# Art eines Feldes:
LOCKED_INPUT = 'locked_input'      # Eingabe des Formulars, bei versendeter Rechnung gesperrt
LOCKED_DERIVED = 'locked_derived'  # aus einer gesperrten Eingabe abgeleitet: bei Sperre nie geschrieben
OPEN_INPUT = 'open_input'          # Eingabe des Formulars, bei versendeter Rechnung änderbar
OPEN_DERIVED = 'open_derived'      # aus den Stammdaten übernommen (Anschrift), bei versendeter änderbar

FIELDS = (
    # (Schlüssel in ``invoice_data`` wie bei insert_invoice, Spalte, Art, Bezeichnung)
    ('invoice_number', 'InvoiceNumber', LOCKED_INPUT, 'Rechnungsnummer'),
    ('invoice_date', 'InvoiceDate', LOCKED_INPUT, 'Rechnungsdatum'),
    ('own_company_id', 'OwnCompanyId', LOCKED_INPUT, 'eigene Firma'),
    ('customer_id', 'CustomerId', LOCKED_INPUT, 'Kunde'),
    ('bank_account_id', 'BankAccountId', LOCKED_INPUT, 'Bankverbindung'),
    ('payment_due_date', 'PaymentDueDate', LOCKED_INPUT, 'Fälligkeit'),
    ('skonto_days', 'SkontoDays', LOCKED_INPUT, 'Skonto'),
    ('skonto_percent', 'SkontoPercent', LOCKED_INPUT, 'Skonto'),
    ('tax_rate', 'TaxRate', LOCKED_INPUT, 'Steuersatz'),
    ('currency', 'Currency', LOCKED_INPUT, 'Währung'),
    ('seller_name', 'SellerName', LOCKED_DERIVED, None),
    ('seller_company', 'SellerCompany', LOCKED_DERIVED, None),
    ('seller_street', 'SellerStreet', LOCKED_DERIVED, None),
    ('seller_postal_code', 'SellerPostalCode', LOCKED_DERIVED, None),
    ('seller_city', 'SellerCity', LOCKED_DERIVED, None),
    ('seller_country', 'SellerCountry', LOCKED_DERIVED, None),
    ('seller_vat_id', 'SellerVATID', LOCKED_DERIVED, None),
    ('seller_email', 'SellerEmail', LOCKED_DERIVED, None),
    ('seller_phone', 'SellerPhone', LOCKED_DERIVED, None),
    ('buyer_vat_id', 'BuyerVATID', LOCKED_DERIVED, None),
    ('bank_name', 'BankName', LOCKED_DERIVED, None),
    ('bank_iban', 'BankIBAN', LOCKED_DERIVED, None),
    ('bank_bic', 'BankBIC', LOCKED_DERIVED, None),
    ('tax_category', 'TaxCategory', LOCKED_DERIVED, None),
    ('sum_net', 'SumNet', LOCKED_DERIVED, None),
    ('tax_amount', 'TaxAmount', LOCKED_DERIVED, None),
    ('sum_gross', 'SumGross', LOCKED_DERIVED, None),
    ('amount_due', 'AmountDue', LOCKED_DERIVED, None),
    ('payment_terms', 'PaymentTerms', OPEN_INPUT, 'Begleittext'),
    ('order_number', 'OrderNumber', OPEN_INPUT, 'Bestellnummer'),
    ('buyer_reference', 'BuyerReference', OPEN_INPUT, 'Leitweg-ID'),
    ('buyer_route_id', 'BuyerRouteID', OPEN_INPUT, 'Leitweg-ID'),
    ('delivery_date', 'DeliveryDate', OPEN_INPUT, 'Leistungsdatum'),
    ('buyer_name', 'BuyerName', OPEN_DERIVED, None),
    ('buyer_company', 'BuyerCompany', OPEN_DERIVED, None),
    ('buyer_street', 'BuyerStreet', OPEN_DERIVED, None),
    ('buyer_postal_code', 'BuyerPostalCode', OPEN_DERIVED, None),
    ('buyer_city', 'BuyerCity', OPEN_DERIVED, None),
    ('buyer_country', 'BuyerCountry', OPEN_DERIVED, None),
)
COLUMN_OF = {key: column for key, column, _kind, _label in FIELDS}
KIND_OF = {key: kind for key, _column, kind, _label in FIELDS}
LABEL_OF = {key: label for key, _column, _kind, label in FIELDS}
MONEY_COLUMNS = frozenset({'SumNet', 'TaxAmount', 'SumGross', 'AmountDue'})
ITEMS_LABEL = 'Positionen'

LOCKED_MESSAGE = ('Versendete Rechnung: Beträge, Positionen, Nummer, Datum und Kunde lassen sich '
                  'nicht mehr ändern.')
LOCKED_UNPAID_HINT = 'Ohne zugeordnete Zahlung lässt sie sich über den Status zurück auf Entwurf holen.'
LOCKED_PAID_HINT = 'Zu dieser Rechnung sind Zahlungen zugeordnet.'
CLOSED_MESSAGE = 'Stornierte Rechnung: Sie lässt sich nicht mehr ändern.'
CHANGED_MESSAGE = 'Die Rechnung wurde inzwischen geändert – bitte die Seite neu laden.'


class DocumentNotFound(LookupError):
    """Das Aktualisieren trifft keinen Beleg des erwarteten Typs (Rechnung bzw. Angebot)."""


class InvoiceLocked(Exception):
    """Das Speichern würde eine gesperrte Angabe ändern. ``str(exc)`` ist ein Klartext für die
    Oberfläche, ``fields`` nennt, was geändert würde."""

    def __init__(self, message, fields=()):
        super().__init__(message)
        self.fields = list(fields)


def lock_message(status, payments):
    """Klartext, warum sich diese Rechnung nicht (mehr) ändern lässt."""
    if status_group(status) == CLOSED:
        return CLOSED_MESSAGE
    return f'{LOCKED_MESSAGE} {LOCKED_PAID_HINT if payments else LOCKED_UNPAID_HINT}'


# ── Vergleich mit dem gespeicherten Stand ────────────────────────────────────

def _text(value):
    return '' if value is None else ' '.join(str(value).split())


def _date(value):
    return str(value)[:10] if value else ''


def _id(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _number(value):
    """Skonto: leer und 0 sind „kein Skonto“ (so schickt es das Formular)."""
    if value in (None, '', 0):
        return None
    try:
        return Decimal(str(value)).normalize()
    except InvalidOperation:
        return str(value)


def _rate(value):
    """Steuersatz als Prozent; 0.19 und 19 sind derselbe Satz, der Sentinel -1 („keine USt
    ausgewiesen“) ein eigener."""
    if value is None or value == '':
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if rate < 0:
        return 'ohne'
    if 0 < rate <= 1:
        rate *= 100
    return rate.quantize(Decimal('0.0001')).normalize()


def _currency(value):
    return _text(value) or 'EUR'


_NORMALIZE = {
    'invoice_number': _text, 'invoice_date': _date, 'own_company_id': _id, 'customer_id': _id,
    'bank_account_id': _id, 'payment_due_date': _date, 'skonto_days': _number,
    'skonto_percent': _number, 'tax_rate': _rate, 'currency': _currency,
    'payment_terms': _text, 'order_number': _text, 'buyer_reference': _text,
    'buyer_route_id': _text, 'delivery_date': _date,
}


def _unit(value):
    # Das Formular zeigt (und schickt zurück), was `unit_display` aus der gespeicherten Einheit macht:
    # „C62“ wird „Stk.“. Gleich ist, was gleich angezeigt wird.
    from export.einvoice_model import unit_display
    return unit_display(value)


def _quantity(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value)).normalize()
    except InvalidOperation:
        return str(value)


def _item_key(description, quantity, unit, price_minor, rate):
    return (_text(description), _quantity(quantity), _unit(unit), price_minor, _rate(rate))


def _items_differ(stored_rows, items):
    """Unterscheiden sich die Positionen (Bezeichnung, Menge, Einheit, Einzelpreis in Minor Units,
    Steuersatz) – in dieser Reihenfolge? ``stored_rows``: ``(Description, Quantity, Unit,
    PricePerUnit, TaxRate)`` aus der Datenbank; ``items``: die Positionen wie bei
    ``insert_invoice_item``. Die Positionssumme ist abgeleitet und zählt nicht."""
    try:
        new = [_item_key(item.get('description'), item.get('quantity'), item.get('unit', 'C62'),
                         to_minor(item.get('price_per_unit') or 0), item.get('tax_rate'))
               for item in items]
    except (TypeError, ValueError):
        return True
    old = [_item_key(row[0], row[1], row[2], row[3], row[4]) for row in stored_rows]
    return old != new


def check_changes(row, changes, stored_items=None, items=None):
    """Welche gesperrten Angaben würde dieser Schreibversuch ändern?

    ``row``: die gespeicherte Rechnung (Spalte → Wert), ``changes``: die beabsichtigten Werte
    (Schlüssel wie bei ``insert_invoice``). Es zählen nur die Schlüssel, die ``changes`` nennt –
    wer nur den Begleittext schickt, ändert sonst nichts. Eine bearbeitbare Rechnung hat nichts
    Gesperrtes; bei einer gesperrten zählen die Finanzangaben, bei einer abgeschlossenen auch die
    übrigen Eingaben. Abgeleitete Werte (Absender, Bank, Summen, Anschrift) werden nicht
    verglichen: Sie ändern sich nur mit der Eingabe, aus der sie stammen.

    ``stored_items``/``items`` (beide angegeben): die Positionen, gespeichert und neu.

    Returns die Bezeichnungen der Angaben, die sich änderten (leer: nichts Gesperrtes)."""
    group = status_group(row.get('Status'))
    if group == EDITABLE:
        return []
    kinds = (LOCKED_INPUT,) if group == LOCKED else (LOCKED_INPUT, OPEN_INPUT)
    problems = []
    for key, value in changes.items():
        if KIND_OF.get(key) not in kinds:
            continue
        normalize = _NORMALIZE[key]
        if normalize(row.get(COLUMN_OF[key])) != normalize(value) and LABEL_OF[key] not in problems:
            problems.append(LABEL_OF[key])
    if stored_items is not None and items is not None and _items_differ(stored_items, items):
        problems.append(ITEMS_LABEL)
    return problems


# ── Statusübergänge ──────────────────────────────────────────────────────────
# Art eines Übergangs; sie sagt der Oberfläche, was nach dem Wechsel zu tun ist:
#   free       Entwurf ↔ abgeschlossen: nichts
#   send       Versenden: Forderung buchen, Dokument erzeugen – der einzige Weg in die Sperre
#   switch     versendet ↔ überfällig: nichts buchen, nichts erzeugen
#   reopen     zurückholen (versendet/überfällig → Entwurf/abgeschlossen): die Forderung wird wieder entfernt
#   mark_paid  „bezahlt“ von Hand (Skonto): nichts
#   cancel     stornieren von Hand: Forderung wie bisher (bleibt, solange eine Zahlung daran hängt)
#   revive     storniert → versendet/überfällig: Forderung wieder buchen, ein fehlendes Dokument nachziehen
#   heal       bezahlt/Teilzahlung → versendet/überfällig, nur ohne Zahlung: heilt einen verwaisten Zustand
#   same       derselbe Status
UNPAID, PAID = 'unpaid', 'paid'      # Bedingung: keine bzw. mindestens eine zugeordnete Zahlung

TRANSITIONS = {
    'draft': {'finalized': ('free', None), 'sent': ('send', None)},
    'finalized': {'draft': ('free', None), 'sent': ('send', None)},
    'sent': {'overdue': ('switch', None), 'draft': ('reopen', UNPAID),
             'finalized': ('reopen', UNPAID), 'paid': ('mark_paid', PAID),
             'cancelled': ('cancel', None)},
    'overdue': {'sent': ('switch', None), 'draft': ('reopen', UNPAID),
                'finalized': ('reopen', UNPAID), 'paid': ('mark_paid', PAID),
                'cancelled': ('cancel', None)},
    'partial_payment': {'paid': ('mark_paid', PAID), 'sent': ('heal', UNPAID),
                        'overdue': ('heal', UNPAID), 'cancelled': ('cancel', None)},
    'paid': {'sent': ('heal', UNPAID), 'overdue': ('heal', UNPAID), 'cancelled': ('cancel', None)},
    'cancelled': {'sent': ('revive', None), 'overdue': ('revive', None)},
}

Verdict = namedtuple('Verdict', 'kind message code')
StatusChange = namedtuple('StatusChange', 'ok message code old new kind')


def _quoted(status):
    return f'„{STATUS_LABELS.get(status, status)}“'


def check_transition(old, new, payments):
    """Darf eine Rechnung vom Status ``old`` auf ``new``, wenn ``payments`` Zahlungen zugeordnet
    sind? Returns ``Verdict(kind, message, code)``: bei Erlaubnis die Art des Übergangs, sonst ein
    Klartext und der Statuscode für die Antwort."""
    old, new = canonical_status(old), canonical_status(new)
    if new == old:
        return Verdict('same', None, 200)
    rule = TRANSITIONS.get(old, {}).get(new)
    if rule is None:
        return Verdict(None, _not_allowed(old, new), 409)
    kind, condition = rule
    if condition == UNPAID and payments:
        if kind == 'reopen':
            return Verdict(None, 'Zu dieser Rechnung sind Zahlungen zugeordnet – sie lässt sich '
                                 'nicht zurück auf Entwurf oder Abgeschlossen setzen. Erst die '
                                 'Zuordnungen entfernen.', 409)
        return Verdict(None, f'Der Status {_quoted(old)} ergibt sich aus den zugeordneten '
                             'Zahlungen und ändert sich mit ihnen.', 409)
    if condition == PAID and not payments:
        return Verdict(None, 'Bezahlt nur möglich, wenn mindestens eine Zahlung verknüpft ist.', 400)
    return Verdict(kind, None, 200)


def _not_allowed(old, new):
    if old not in STATUS_LABELS:
        return f'Der Status „{old}“ ist unbekannt und lässt sich hier nicht ändern.'
    if old == 'cancelled':
        return ('Eine stornierte Rechnung lässt sich nur wieder auf „Versendet“ oder '
                '„Überfällig“ setzen.')
    if old in EDITABLE_STATUSES:
        if new == 'cancelled':
            return 'Ein Entwurf wird nicht storniert – er lässt sich löschen, solange er ein Entwurf ist.'
        return (f'Der Status {_quoted(new)} gilt erst für eine versendete Rechnung – '
                'zuerst auf „Versendet“ setzen.')
    if new in EDITABLE_STATUSES:
        return ('Eine Rechnung mit Zahlungen lässt sich nicht zurück auf Entwurf oder '
                'Abgeschlossen setzen.')
    if new == 'partial_payment':
        return 'Teilzahlung wird automatisch gesetzt, sobald eine Zahlung zugeordnet ist.'
    return f'Der Status {_quoted(old)} lässt sich nicht auf {_quoted(new)} setzen.'


def allowed_targets(status, payments):
    """Die Status, auf die diese Rechnung gesetzt werden darf (ohne den aktuellen) – in der
    Reihenfolge von ``STATUS_LABELS``. Die Statusauswahl der Oberfläche bietet nur diese an."""
    return [new for new in STATUS_LABELS
            if new != canonical_status(status)
            and check_transition(status, new, payments).message is None]


# ── Zugriff auf die gespeicherte Rechnung ────────────────────────────────────

def _invoice_row(conn, invoice_id):
    """Die gespeicherte Rechnung als Spalte → Wert (rohe Werte, Geld in Minor Units); ``None``,
    wenn es sie nicht gibt oder der Beleg ein Angebot ist."""
    cursor = conn.execute('SELECT * FROM Invoices WHERE ID = ?', (invoice_id,))
    values = cursor.fetchone()
    if values is None:
        return None
    row = dict(zip((column[0] for column in cursor.description), values))
    return row if (row.get('DocumentType') or 'invoice') == 'invoice' else None


def _begin_immediate(conn):
    """Schreibsperre holen, falls der Aufrufer noch keine Transaktion begonnen hat: Prüfen und
    Schreiben sehen dann denselben Stand, zwei gleichzeitige Anfragen laufen nacheinander."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


class InvoiceLockMixin:
    def update_invoice_fields(self, tx, invoice_id, invoice_data, items=None):
        """Eine gespeicherte Rechnung in der Transaktion ``tx`` aktualisieren – die einzige Stelle,
        die den Kopf einer Rechnung beim Speichern im Formular überschreibt.

        ``invoice_data``: die Felder (Schlüssel wie bei ``insert_invoice``; Status, Pfade und
        unbekannte Schlüssel gibt es hier nicht), ``items``: die Positionen wie bei
        ``insert_invoice_item`` (ohne ``invoice_id``) oder ``None`` für „Positionen unverändert“.

        * bearbeitbar: alle Felder werden geschrieben, die Positionen ersetzt;
        * gesperrt: ändert ``check_changes`` etwas Gesperrtes, bleibt alles, wie es ist
          (:class:`InvoiceLocked`); sonst werden nur die änderbaren Felder geschrieben;
        * abgeschlossen: nichts wird geschrieben, jede Änderung wird abgelehnt.

        Geprüft wird gegen den Stand in der Transaktion, geschrieben mit der Statusgruppe in der
        ``WHERE``-Bedingung – eine zweite Anfrage, die die Rechnung inzwischen versendet hat, ändert
        sie nicht mehr. Returns ``'edited'``, ``'open'`` (nur änderbare Felder) oder
        ``'unchanged'``; :class:`DocumentNotFound`, wenn es die Rechnung nicht gibt."""
        unknown = sorted(key for key in invoice_data if key not in COLUMN_OF)
        if unknown:
            raise ValueError(f'unbekannte Rechnungsfelder: {", ".join(unknown)}')
        conn = tx.conn
        _begin_immediate(conn)
        row = _invoice_row(conn, invoice_id)
        if row is None:
            raise DocumentNotFound('Rechnung nicht gefunden.')
        group = status_group(row['Status'])
        if group == EDITABLE:
            if invoice_data:
                self._write_fields(conn, invoice_id, invoice_data, EDITABLE)
            if items is not None:
                conn.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (invoice_id,))
                for item in items:
                    self.insert_invoice_item({**item, 'invoice_id': invoice_id}, tx=tx)
            return 'edited'
        stored_items = None
        if items is not None:
            stored_items = conn.execute(
                'SELECT Description, Quantity, Unit, PricePerUnit, TaxRate FROM InvoiceItems '
                'WHERE InvoiceId = ? ORDER BY Position, ID', (invoice_id,)).fetchall()
        problems = check_changes(row, invoice_data, stored_items, items)
        if problems:
            payments = conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?',
                                    (invoice_id,)).fetchone()[0]
            raise InvoiceLocked(lock_message(row['Status'], payments), problems)
        if group == CLOSED:
            return 'unchanged'
        open_data = {key: value for key, value in invoice_data.items()
                     if KIND_OF[key] in (OPEN_INPUT, OPEN_DERIVED)}
        if not open_data:
            return 'unchanged'
        self._write_fields(conn, invoice_id, open_data, LOCKED)
        return 'open'

    @staticmethod
    def _write_fields(conn, invoice_id, invoice_data, group):
        """``UPDATE`` der genannten Felder, nur solange die Rechnung noch in dieser Statusgruppe
        steht (``EDITABLE`` oder ``LOCKED``)."""
        columns = [COLUMN_OF[key] for key in invoice_data]
        values = [to_minor(value or 0) if COLUMN_OF[key] in MONEY_COLUMNS else value
                  for key, value in invoice_data.items()]
        if group == EDITABLE:
            guard, params = "COALESCE(Status, 'draft') IN (?, ?)", sorted(EDITABLE_STATUSES)
        else:
            guard = "COALESCE(Status, 'draft') NOT IN (?, ?, ?)"
            params = sorted(EDITABLE_STATUSES | CLOSED_STATUSES)
        sql = (f"UPDATE Invoices SET {', '.join(f'{column} = ?' for column in columns)} "
               f"WHERE ID = ? AND COALESCE(DocumentType, 'invoice') = 'invoice' AND {guard}")
        refs = {column: value for column, value in zip(columns, values)
                if column in ('OwnCompanyId', 'CustomerId', 'BankAccountId')}
        with fk_explained(conn, 'Invoices', refs):
            cursor = conn.execute(sql, [*values, invoice_id, *params])
        if cursor.rowcount != 1:
            raise InvoiceLocked(CHANGED_MESSAGE)

    def set_invoice_status(self, invoice_id, new_status):
        """Den Status einer Rechnung setzen – nach der Übergangstabelle (``TRANSITIONS``), in einer
        Transaktion: Prüfen und Schreiben sehen denselben Stand.

        Geschrieben werden der Status und, außer beim Wechsel zwischen versendet und überfällig,
        ``AmountDue`` aus den Zahlungen (der gewählte Status bleibt). Was danach zu tun ist –
        Forderung buchen oder zurücknehmen, Dokument erzeugen –, sagt ``StatusChange.kind``; das
        übernimmt der Aufrufer. Returns :class:`StatusChange`, bei einer Ablehnung mit Klartext
        in ``message`` und dem Statuscode für die Antwort in ``code``."""
        new = canonical_status(new_status)
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = _invoice_row(conn, invoice_id)
            if row is None:
                conn.rollback()
                return StatusChange(False, 'Rechnung nicht gefunden', 404, None, new, None)
            old = canonical_status(row['Status'])
            payments = conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?',
                                    (invoice_id,)).fetchone()[0]
            verdict = check_transition(old, new, payments)
            if verdict.message:
                conn.rollback()
                return StatusChange(False, verdict.message, verdict.code, old, new, None)
            if verdict.kind != 'same':
                cursor = conn.execute(
                    "UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP "
                    "WHERE ID = ? AND COALESCE(Status, 'draft') = ?",
                    (new, invoice_id, row['Status'] or 'draft'))
                if cursor.rowcount != 1:
                    conn.rollback()
                    return StatusChange(False, CHANGED_MESSAGE, 409, old, new, None)
                if verdict.kind != 'switch':
                    self._recalc_invoice(conn, invoice_id, adjust_status=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return StatusChange(True, None, 200, old, new, verdict.kind)
