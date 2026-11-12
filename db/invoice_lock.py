# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Sperre versendeter Rechnungen und Statusübergänge.

Es gibt genau EINE Stelle, die entscheidet, was an einer gespeicherten Rechnung noch änderbar
ist und welchen Status sie als Nächstes bekommen darf. Jeder Schreibweg auf eine Rechnung geht
darüber: ``update_invoice_fields`` (Speichern im Formular) und ``set_invoice_status``
(Statuswechsel). Das Berichtigen einer versendeten Rechnung (``correct_invoice``, db/invoice_versions.py)
ruft ``update_invoice_fields`` auf und legt dazu Fassung und Protokoll an. Der Stornobeleg steht in
``docs/aufgaben.md``.

Statusgruppen:

* **bearbeitbar** (``draft``, ``finalized``): alles änderbar – außer der Rechnungsnummer einer Rechnung, die
  schon einmal versendet war (``was_sent_before``).
* **gesperrt** (``sent``, ``overdue``, ``partial_payment``, ``partial``, ``paid``): Finanzangaben –
  Nummer, Rechnungsdatum, Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto,
  Bank und Käufer-USt-IdNr. – sind gesperrt. Weiter änderbar bleiben Zahlungshinweis, Anschrift des
  Kunden, Bestellnummer, Leitweg-ID und Leistungsdatum – aber nur als Berichtigung, mit neuer Fassung
  und Protokoll.
* **abgeschlossen** (``cancelled``): nichts mehr änderbar.

Verglichen wird mit dem gespeicherten Stand, in der Transaktion des Schreibens (``BEGIN
IMMEDIATE``): Was das Formular unverändert zurückschickt, ist kein Verstoß – Beträge in Minor
Units, Positionen Feld für Feld. Status, ``AmountDue`` und Dokumentpfade kommen nie vom Client.
"""
from collections import namedtuple
from decimal import Decimal, InvalidOperation

from money import to_minor
from .core import fk_explained
from .invoice_payments import PAYMENT_MARK

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
    ('payment_terms', 'PaymentTerms', OPEN_INPUT, 'Zahlungshinweis'),
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
NUMBER_KEPT_MESSAGE = 'Die Rechnung war bereits versendet – ihre Nummer bleibt.'
# Zurückholen (versendet/überfällig → Entwurf/abgeschlossen) nimmt die Forderungsbuchung mit: nur die, die die
# App selbst gebucht hat und die an nichts hängt. Alles andere bleibt stehen – dann bleibt auch die Rechnung
REOPEN_IMPORTED_MESSAGE = ('Die Buchungen dieser Rechnung stammen aus einem Import – sie lässt sich nicht '
                           'auf Entwurf zurückholen.')
REOPEN_ATTACHED_MESSAGE = ('Die Forderung dieser Rechnung ist einer Bankbewegung zugeordnet – sie lässt sich '
                           'nicht auf Entwurf zurückholen. Erst die Zuordnung lösen.')
REOPEN_FOREIGN_MESSAGE = ('Die Forderungsbuchung dieser Rechnung stammt nicht aus der App – sie lässt sich '
                          'nicht auf Entwurf zurückholen.')
REOPEN_PAYMENT_MESSAGE = ('Zu dieser Rechnung gibt es Zahlungsbuchungen – sie lässt sich nicht auf Entwurf '
                          'zurückholen. Erst die Zuordnung entfernen.')
ARCHIVE_FAILED_MESSAGE = ('Die versendeten Dokumente (PDF/XML) ließen sich nicht in den Ordner „Fassungen“ '
                          'verschieben – der Status wurde nicht geändert.')


def document_version(row):
    """Die Nummer der aktuellen Fassung einer Rechnung (``Invoices.DocumentVersion``; NULL gilt als 1)."""
    return row.get('DocumentVersion') or 1


class DocumentNotFound(LookupError):
    """Das Aktualisieren trifft keinen Beleg des erwarteten Typs (Rechnung bzw. Angebot)."""


class _Refusal(Exception):
    """Der Statuswechsel wird abgelehnt: ``str(exc)`` ist der Klartext, ``code`` der Statuscode."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


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


def changed_open_fields(row, changes):
    """Von den Angaben in ``changes`` die änderbaren (Zahlungshinweis, Bestellnummer, Leitweg-ID,
    Leistungsdatum, Anschrift), deren Wert vom gespeicherten abweicht. Verglichen wird genau
    (leer und ``None`` sind dasselbe): Ein unverändertes Speichern schreibt keine Spalte – auch
    nicht den Leerwert an die Stelle von ``NULL`` –, eine Änderung nur am Zeilenumbruch aber schon."""
    def text(value):
        return '' if value is None else str(value)
    return {key: value for key, value in changes.items()
            if KIND_OF[key] in (OPEN_INPUT, OPEN_DERIVED)
            and text(row.get(COLUMN_OF[key])) != text(value)}


def buyer_snapshot(customer):
    """Anschrift eines Kunden (Zeile aus ``get_contact_by_id``) als Momentaufnahme für die Rechnung:
    die Felder ``buyer_*`` wie bei ``insert_invoice``."""
    return {
        'buyer_name': customer[4] or customer[3] or '',
        'buyer_company': customer[4] or '',
        'buyer_street': customer[5] or '',
        'buyer_postal_code': customer[6] or '',
        'buyer_city': customer[7] or '',
        'buyer_country': customer[8] or 'DE',
    }


def check_changes(row, changes, stored_items=None, items=None):
    """Welche gesperrten Angaben würde dieser Schreibversuch ändern?

    ``row``: die gespeicherte Rechnung (Spalte → Wert), ``changes``: die beabsichtigten Werte
    (Schlüssel wie bei ``insert_invoice``). Es zählen nur die Schlüssel, die ``changes`` nennt –
    wer nur den Zahlungshinweis schickt, ändert sonst nichts. Eine bearbeitbare Rechnung hat nichts
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
#   reopen     zurückholen (versendet/überfällig → Entwurf/abgeschlossen): die Forderung wird wieder entfernt,
#              die versendeten Dokumente wandern nach „Fassungen“ (nichts davon halb: scheitert eines, bleibt alles)
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
# ``archived``: wie viele versendete Dokumente (PDF/XML) das Zurückholen in „Fassungen“ abgelegt hat
StatusChange = namedtuple('StatusChange', 'ok message code old new kind archived', defaults=(0,))


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

def invoice_row(conn, invoice_id):
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
    def update_invoice_fields(self, tx, invoice_id, invoice_data, items=None, correcting=False):
        """Eine gespeicherte Rechnung in der Transaktion ``tx`` aktualisieren – die einzige Stelle,
        die den Kopf einer Rechnung beim Speichern im Formular überschreibt.

        ``invoice_data``: die Felder (Schlüssel wie bei ``insert_invoice``; Status, Pfade und
        unbekannte Schlüssel gibt es hier nicht), ``items``: die Positionen wie bei
        ``insert_invoice_item`` (ohne ``invoice_id``) oder ``None`` für „Positionen unverändert“.

        * bearbeitbar: alle Felder werden geschrieben, die Positionen ersetzt – die Rechnungsnummer
          nicht mehr, sobald die Rechnung schon einmal versendet war (:class:`InvoiceLocked`);
        * gesperrt: ändert ``check_changes`` etwas Gesperrtes, bleibt alles, wie es ist
          (:class:`InvoiceLocked`); sonst werden die änderbaren Felder geschrieben – aber nur mit
          ``correcting=True``, dem Aufruf aus ``correct_invoice``, das Fassung und Protokoll anlegt; ohne
          ihn ist eine Änderung daran eine Berichtigung, die hier nicht stattfinden darf;
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
        row = invoice_row(conn, invoice_id)
        if row is None:
            raise DocumentNotFound('Rechnung nicht gefunden.')
        group = status_group(row['Status'])
        if group == EDITABLE:
            if ('invoice_number' in invoice_data
                    and _text(row['InvoiceNumber']) != _text(invoice_data['invoice_number'])
                    and self.was_sent_before(invoice_id, conn)):
                raise InvoiceLocked(NUMBER_KEPT_MESSAGE, [LABEL_OF['invoice_number']])
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
        open_data = changed_open_fields(row, invoice_data)
        if group == CLOSED:
            if open_data:
                raise InvoiceLocked(CLOSED_MESSAGE, [LABEL_OF[key] or key for key in open_data])
            return 'unchanged'
        if not open_data:
            return 'unchanged'
        if not correcting:
            raise InvoiceLocked(CHANGED_MESSAGE)
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

    def set_invoice_status(self, invoice_id, new_status, user_name=None):
        """Den Status einer Rechnung setzen – nach der Übergangstabelle (``TRANSITIONS``), in EINER
        Transaktion: Prüfen, Status, Forderungsbuchung und (beim Zurückholen) das Ablegen der
        versendeten Dokumente gelingen gemeinsam oder gar nicht.

        Geschrieben werden der Status und, außer beim Wechsel zwischen versendet und überfällig,
        ``AmountDue`` aus den Zahlungen (der gewählte Status bleibt). Dazu je nach Art des Übergangs
        (``StatusChange.kind``):

        * ``send`` / ``revive``: die Forderung wird gebucht („Debitoren an Wartekonto“, nur zu einem
          Steuersatz mit Wartekonto); ``send`` setzt außerdem ``SentAt``, den Zeitpunkt des ersten
          Versands (ein späteres Versenden ändert ihn nicht);
        * ``reopen``: die Forderung wird entfernt – aber nur eine, die die App selbst gebucht hat und
          die an nichts hängt, sonst wird das Zurückholen mit Klartext abgelehnt (nichts geändert).
          PDF und XML wandern nach ``invoices/<Jahr>/Fassungen/<ID>/`` (``archive_sent_documents``) und
          werden in ``InvoiceDocumentVersions`` festgehalten, ihre Pfade werden geleert, die Fassung
          zählt um eins hoch (nur, wenn eine Datei abgelegt wurde); scheitert das Verschieben, bleibt
          alles, wie es war;
        * ``cancel``: die Forderung geht, solange nichts an ihr hängt (keine Zahlung, nicht von der
          App gebucht: bleibt stehen).

        Jeder gelungene Wechsel (außer demselben Status) steht im Protokoll ``InvoiceCorrections``
        (``Action='status'``) – wer ihn ausgelöst hat, sagt ``user_name``. Was die Zahlungslogik am Status
        ändert (``_recalc_invoice``), protokolliert sie nicht.

        Das Erzeugen des Dokuments beim Versenden bleibt dem Aufrufer, danach. Returns
        :class:`StatusChange`, bei einer Ablehnung mit Klartext in ``message`` und dem Statuscode
        für die Antwort in ``code``."""
        new = canonical_status(new_status)
        # Konten, die der Wechsel braucht, werden bei Bedarf angelegt – in eigenen Verbindungen, nie
        # innerhalb der Transaktion (ein zweiter Schreiber sperrte sich selbst)
        accounts, accounts_key = self._receivable_accounts_for(invoice_id, new)
        old = kind = None
        archived = []
        try:
            with self.transaction() as tx:
                conn = tx.conn
                conn.execute('BEGIN IMMEDIATE')
                row = invoice_row(conn, invoice_id)
                if row is None:
                    raise _Refusal('Rechnung nicht gefunden', 404)
                old = canonical_status(row['Status'])
                payments = conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?',
                                        (invoice_id,)).fetchone()[0]
                verdict = check_transition(old, new, payments)
                if verdict.message:
                    raise _Refusal(verdict.message, verdict.code)
                kind = verdict.kind
                if kind == 'same':
                    return StatusChange(True, None, 200, old, new, kind)
                if kind == 'reopen':
                    blocker = self._reopen_blocker(conn, row)
                    if blocker:
                        raise _Refusal(blocker, 409)
                if kind in ('send', 'revive') and accounts_key != self._tax_rate_key(row['TaxRate']):
                    raise _Refusal(CHANGED_MESSAGE, 409)      # der Steuersatz hat sich inzwischen geändert
                cursor = conn.execute(
                    "UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP "
                    "WHERE ID = ? AND COALESCE(Status, 'draft') = ?",
                    (new, invoice_id, row['Status'] or 'draft'))
                if cursor.rowcount != 1:
                    raise _Refusal(CHANGED_MESSAGE, 409)
                version = document_version(row)
                if kind != 'switch':
                    self._recalc_invoice(conn, invoice_id, adjust_status=False)
                if kind == 'send':
                    conn.execute('UPDATE Invoices SET SentAt = COALESCE(SentAt, CURRENT_TIMESTAMP) '
                                 'WHERE ID = ?', (invoice_id,))
                if kind in ('send', 'revive'):
                    self._book_receivable(tx, row, accounts)
                elif kind == 'cancel':
                    self._drop_receivable(conn, invoice_id, row['InvoiceNumber'])
                elif kind == 'reopen':
                    self._drop_receivable(conn, invoice_id, row['InvoiceNumber'])
                    now = conn.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
                    archived = self._archive_documents(conn, invoice_id, version, 'reopened', now,
                                                       ARCHIVE_FAILED_MESSAGE)
                    conn.execute('UPDATE Invoices SET PDFPath = NULL, XMLPath = NULL WHERE ID = ?',
                                 (invoice_id,))
                    if archived:
                        version += 1
                        conn.execute('UPDATE Invoices SET DocumentVersion = ? WHERE ID = ?',
                                     (version, invoice_id))
                self._log_invoice_change(conn, invoice_id, row['InvoiceNumber'], user_name, 'status',
                                         'Status', old, new, version)
        except _Refusal as refusal:
            return StatusChange(False, str(refusal), refusal.code, old, new, None)
        except BaseException:
            # Auch ein scheiternder Commit: die Dokumente liegen wieder dort, wo die Rechnung sie sucht
            self._restore_archived(archived)
            raise
        return StatusChange(True, None, 200, old, new, kind, len(archived))

    def _receivable_accounts_for(self, invoice_id, new):
        """``(Konten, Schlüssel des Steuersatzes)`` für einen Wechsel, der die Forderung bucht (Entwurf,
        abgeschlossen oder storniert → versendet/überfällig), sonst ``(None, None)``. Der Schlüssel
        hält fest, für welchen Steuersatz die Konten gelten."""
        if new not in ('sent', 'overdue'):
            return None, None
        invoice = self.get_document(invoice_id, 'invoice')
        if invoice is None or check_transition(invoice[40], new, 0).kind not in ('send', 'revive'):
            return None, None
        return self._receivable_accounts(invoice[35]), self._tax_rate_key(invoice[35])

    def _reopen_blocker(self, conn, row):
        """Klartext, warum eine versendete Rechnung nicht zurück auf Entwurf kann, weil ihre Buchungen
        nicht mit ihr verschwinden dürfen, sonst ``None``."""
        removable, blocker = self._receivable_removal(conn, row['InvoiceNumber'])
        if blocker:
            return blocker
        if conn.execute('SELECT 1 FROM Bookings WHERE DocumentNumber = ? AND Status = ?',
                        (row['InvoiceNumber'], PAYMENT_MARK)).fetchone():
            return REOPEN_PAYMENT_MESSAGE
        return None

    @staticmethod
    def _restore_archived(moved):
        if moved:
            from export.invoice_files import restore_archived
            restore_archived(moved)
