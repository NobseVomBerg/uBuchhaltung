# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ablage der Rechnungsdokumente (PDF + XRechnung-XML).

Eine Rechnung liegt im Datenbereich des Nutzers unter
``invoices/<Jahr>/<Rechnungs-Nr.> <Kundenname>.<Endung>`` – derselbe Name, unter
dem sie auch zum Download angeboten wird (todo #2). Dieses Modul bündelt

* die Namenskonvention (:func:`invoice_file_stem`, :func:`invoice_dir`),
* das Erzeugen beider Dokumente (:func:`generate_invoice_documents`) und
* das Ausliefern der *gespeicherten* Dateien (:func:`stored_invoice_document`).

Heruntergeladen wird stets die abgelegte Datei, nie eine frisch erzeugte: ab
Status „versendet“ muss der Kunde exakt das Dokument bekommen, das im Haus liegt
(todo #3). Dasselbe gilt für Angebote (``quotes/<Jahr>/…``, geschrieben von
``export/pdf_quote.py``).

Ein in der Datenbank gespeicherter Dokumentpfad wird nur benutzt, wenn er im
Dokumentordner des Nutzers liegt (:func:`path_in_documents`): Die Datenbank lässt
sich per SQL-Konsole oder zurückgespielte Sicherung verändern, und sonst ließe sich
darüber jede lesbare Datei (``auth.db``, die Datenbank eines anderen Benutzers)
ausliefern oder löschen.
"""
import os

from db.invoices import document_type
from .pdf_core import safe_filename_component

# Spaltenindizes der Invoices-Zeile (``SELECT *``), siehe db/schema.py
_IDX_NUMBER = 1
_IDX_DATE = 2
_IDX_BUYER_NAME = 14
_IDX_BUYER_COMPANY = 15
_IDX_ROUTE_ID = 22
_IDX_ORDER_NUMBER = 23
_IDX_PDF_PATH = 41
_IDX_XML_PATH = 42


def _col(invoice, idx):
    return invoice[idx] if invoice and len(invoice) > idx else None


def invoice_file_stem(invoice) -> str:
    """Dateiname ohne Endung: ``"<Rechnungs-Nr.> <Kundenname>"``.

    Beispiel: ``R26003 Malermeister Pinsel``. Fehlt der Kunde, bleibt die
    Rechnungsnummer allein stehen.
    """
    number = safe_filename_component(_col(invoice, _IDX_NUMBER) or 'DRAFT')
    customer = safe_filename_component(_col(invoice, _IDX_BUYER_COMPANY)
                                       or _col(invoice, _IDX_BUYER_NAME))
    return ' '.join(p for p in (number, customer) if p) or 'Rechnung'


def invoice_dir(invoice=None) -> str:
    """Ablageordner der Rechnungsdokumente (Jahr aus dem Rechnungsdatum)."""
    import datetime
    import userctx
    date = str(_col(invoice, _IDX_DATE) or '')
    year = date[:4] if len(date) >= 4 and date[:4].isdigit() else None
    if not year:
        year = str(datetime.datetime.now().year)
    return os.path.join(userctx.user_data_dir(), 'invoices', year)


def invoice_file_path(invoice, extension: str) -> str:
    """Vollständiger Ablagepfad für ``extension`` ("pdf" / "xml")."""
    return os.path.join(invoice_dir(invoice),
                        f"{invoice_file_stem(invoice)}.{extension}")


def is_quote(invoice) -> bool:
    """True, wenn die Zeile ein Angebot ist (``DocumentType = 'quote'``)."""
    return document_type(invoice) == 'quote'


def documents_root(invoice=None) -> str:
    """Ordner, in dem die Dokumente des aktuellen Nutzers liegen dürfen: ``invoices``,
    bei einem Angebot ``quotes`` (dorthin schreibt ``generate_quote_pdf``)."""
    import userctx
    return os.path.join(userctx.user_data_dir(), 'quotes' if is_quote(invoice) else 'invoices')


def path_in_documents(path, invoice=None) -> bool:
    """Liegt ``path`` – nach Auflösen von Links und ``..`` – im Dokumentordner des Nutzers?

    Gilt für jeden gespeicherten Dokumentpfad, bevor die Datei gelesen oder gelöscht
    wird. ``invoice`` bestimmt den Ordner (Rechnung oder Angebot, siehe
    :func:`documents_root`). Alles, was kein Pfad ist (z. B. eine Zahl, die ``open`` als
    Dateinummer läse) oder sich nicht auflösen lässt, gilt als außerhalb.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(documents_root(invoice))
        return os.path.normcase(os.path.commonpath([real, root])) == os.path.normcase(root)
    except (OSError, ValueError):     # NUL im Pfad, anderes Laufwerk unter Windows …
        return False


def drop_stale_file(old_path, new_path):
    """Vorgänger-Datei löschen, wenn der Name sich geändert hat.

    Ändern sich Rechnungsnummer oder Kunde, würde die alte Datei sonst als
    Dublette liegen bleiben. Gelöscht wird nur im Dokumentordner des Nutzers: Der
    alte Pfad stammt aus der Datenbank (siehe :func:`path_in_documents`).
    """
    if not old_path or not new_path:
        return
    try:
        if not (isinstance(old_path, str) and os.path.isfile(old_path)):
            return
        if os.path.abspath(old_path) == os.path.abspath(new_path):
            return
        if not path_in_documents(old_path):
            print(f"Alte Rechnungsdatei liegt außerhalb des Dokumentordners, "
                  f"nicht entfernt: {old_path!r}")
            return
        os.remove(old_path)
    except OSError as e:      # Nicht löschbar ist kein Grund, den Export abzubrechen
        print(f"Alte Rechnungsdatei nicht entfernt ({old_path}): {e}")


def wants_xrechnung(invoice) -> bool:
    """True, wenn die Rechnung E-Rechnungsdaten trägt (Leitweg-ID/Bestellnr.)."""
    return bool(_col(invoice, _IDX_ROUTE_ID) or _col(invoice, _IDX_ORDER_NUMBER))


# Ab diesen Stati ist die Rechnung versendet und damit eingefroren
FROZEN_STATUSES = frozenset({'sent', 'partial_payment', 'overdue', 'paid'})
_IDX_STATUS = 40


def generate_invoice_documents(db, invoice_id: int, with_xml=None,
                               overwrite_sent=False) -> dict:
    """PDF (hybride ZUGFeRD-E-Rechnung) und ggf. UBL-XML erzeugen und ablegen.

    Das PDF ist PDF/A-3b mit eingebetteter ``factur-x.xml`` (Profil EN 16931).
    Eine bereits versendete Rechnung (siehe :data:`FROZEN_STATUSES`) wird nicht
    neu erzeugt, solange ihr PDF vorliegt – ``overwrite_sent=True`` erzwingt es
    (nur beim Eintritt in den Status „versendet“).

    Args:
        db: Datenbank-Instanz.
        invoice_id: ID der Rechnung.
        with_xml: ``None`` = automatisch (UBL-XML nur bei E-Rechnungsdaten),
            ``True``/``False`` erzwingt bzw. unterdrückt das XML.
        overwrite_sent: auch eine eingefrorene Rechnung neu erzeugen.

    Returns:
        dict mit ``pdf_path``/``pdf_filename`` (bzw. ``None``),
        ``xml_path``/``xml_filename`` und ``error`` (Text oder ``None``).
    """
    from .pdf_invoice import generate_invoice_pdf

    result = {'pdf_path': None, 'pdf_filename': None,
              'xml_path': None, 'xml_filename': None, 'error': None}

    # Nur Rechnungen: für ein Angebot (oder eine fehlende ID) wird nichts erzeugt und kein
    # abgelegter Pfad überschrieben.
    current = db.get_document(invoice_id, 'invoice')
    if current is None:
        result['error'] = 'Rechnung nicht gefunden.'
        return result
    if (not overwrite_sent
            and _col(current, _IDX_STATUS) in FROZEN_STATUSES):
        existing, existing_name, _ = stored_invoice_document(db, invoice_id, 'pdf')
        if existing:
            result['pdf_path'] = stored_invoice_path(db, invoice_id, 'pdf')[0]
            result['pdf_filename'] = existing_name
            return result

    pdf_bytes, pdf_path = generate_invoice_pdf(db, invoice_id)
    if not pdf_bytes or not pdf_path:
        result['error'] = 'PDF konnte nicht erstellt werden.'
        return result
    result['pdf_path'] = pdf_path
    result['pdf_filename'] = os.path.basename(pdf_path)

    invoice = db.get_invoice_by_id(invoice_id)
    if with_xml is None:
        with_xml = wants_xrechnung(invoice)
    if with_xml:
        xml_path = _write_xrechnung(db, invoice, invoice_id)
        if xml_path:
            result['xml_path'] = xml_path
            result['xml_filename'] = os.path.basename(xml_path)
        else:
            result['error'] = 'XRechnung (XML) konnte nicht erstellt werden.'
    return result


def _write_xrechnung(db, invoice, invoice_id):
    """XRechnung-XML neben das PDF legen; Pfad zurückgeben (oder ``None``)."""
    from .xrechnung_invoice import XRechnungGenerator
    try:
        items = db.get_invoice_items(invoice_id)
        xml_content = XRechnungGenerator().generate_xml(invoice, items, db=db)
        xml_path = invoice_file_path(invoice, 'xml')
        os.makedirs(os.path.dirname(xml_path), exist_ok=True)
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        drop_stale_file(_col(invoice, _IDX_XML_PATH), xml_path)
        db.update_invoice_xml_path(invoice_id, xml_path)
        return xml_path
    except Exception as e:
        print(f"Error saving XRechnung XML for invoice {invoice_id}: {e}")
        return None


# Endung, DB-Spalte und Klartext je Dokumentart
_KINDS = {
    'pdf': (_IDX_PDF_PATH, 'pdf', 'PDF'),
    'xml': (_IDX_XML_PATH, 'xml', 'XRechnung (XML)'),
}


def _stored_by_name(invoice, extension):
    """Datei am Namensschema suchen (``None``, wenn es sie nicht gibt).

    Rechnung: ``invoices/<Jahr des Rechnungsdatums>/…``. Beim Angebot steht im Ordnernamen
    das Jahr der Erzeugung, das der Datensatz nicht kennt – dort zählt der neueste Treffer.
    """
    if not is_quote(invoice):
        candidate = invoice_file_path(invoice, extension)
        return candidate if os.path.isfile(candidate) else None
    root = documents_root(invoice)
    name = f"{invoice_file_stem(invoice)}.{extension}"
    try:
        years = sorted(os.listdir(root), reverse=True)
    except OSError:
        return None
    for year in years:
        candidate = os.path.join(root, year, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def stored_invoice_path(db, invoice_id: int, kind: str = 'pdf'):
    """Pfad des abgelegten Dokuments einer Rechnung (bzw. eines Angebots).

    Der in der Datenbank gespeicherte Pfad zählt nur im Dokumentordner des Nutzers
    (:func:`path_in_documents`). Liegt er außerhalb, gilt er als nicht vorhanden – dann
    greift der Fallback über das Namensschema –; die Antwort verrät davon nichts, nur das
    Server-Log. Wer ein gespeichertes Dokument öffnet (Download, E-Mail-Anhang), holt den
    Pfad hier.

    Returns:
        tuple: ``(pfad, fehlertext)`` – bei Erfolg ist der Fehlertext ``None``, sonst der Pfad.
    """
    idx, extension, label = _KINDS[kind]
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        return None, 'Rechnung nicht gefunden.'

    path = _col(invoice, idx)
    if path and not path_in_documents(path, invoice):
        print(f"Dokumentpfad außerhalb des Dokumentordners ignoriert "
              f"(Dokument {invoice_id}, {kind}): {path!r}")
        path = None
    # Fallback: Datei am Namensschema suchen, falls der DB-Pfad fehlt oder
    # veraltet ist (z. B. nach einem Umzug des Datenverzeichnisses).
    if not path or not os.path.isfile(path):
        path = _stored_by_name(invoice, extension)

    if not path:
        hint = ' – bitte zuerst erzeugen' if is_quote(invoice) else ''
        return None, f'Kein {label} vorhanden{hint}.'
    return path, None


def stored_invoice_document(db, invoice_id: int, kind: str = 'pdf'):
    """Abgelegtes Dokument einer Rechnung (bzw. eines Angebots) laden.

    Returns:
        tuple: ``(bytes, dateiname, fehlertext)`` – bei Erfolg ist der
        Fehlertext ``None``, sonst sind Bytes und Dateiname ``None``.
    """
    path, error = stored_invoice_path(db, invoice_id, kind)
    if error:
        return None, None, error
    try:
        with open(path, 'rb') as f:
            return f.read(), os.path.basename(path), None
    except OSError as e:
        print(f"Error reading {kind} for invoice {invoice_id}: {e}")
        return None, None, f'{_KINDS[kind][2]} konnte nicht gelesen werden.'
