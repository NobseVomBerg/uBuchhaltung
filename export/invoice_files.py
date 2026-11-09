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
(todo #3).
"""
import os

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


def drop_stale_file(old_path, new_path):
    """Vorgänger-Datei löschen, wenn der Name sich geändert hat.

    Ändern sich Rechnungsnummer oder Kunde, würde die alte Datei sonst als
    Dublette liegen bleiben.
    """
    if not old_path or not new_path:
        return
    try:
        if (os.path.isfile(old_path)
                and os.path.abspath(old_path) != os.path.abspath(new_path)):
            os.remove(old_path)
    except OSError as e:      # Nicht löschbar ist kein Grund, den Export abzubrechen
        print(f"Alte Rechnungsdatei nicht entfernt ({old_path}): {e}")


def wants_xrechnung(invoice) -> bool:
    """True, wenn die Rechnung E-Rechnungsdaten trägt (Leitweg-ID/Bestellnr.)."""
    return bool(_col(invoice, _IDX_ROUTE_ID) or _col(invoice, _IDX_ORDER_NUMBER))


def generate_invoice_documents(db, invoice_id: int, with_xml=None) -> dict:
    """PDF (und bei E-Rechnungsdaten das XML) erzeugen und ablegen.

    Args:
        db: Datenbank-Instanz.
        invoice_id: ID der Rechnung.
        with_xml: ``None`` = automatisch (nur bei E-Rechnungsdaten),
            ``True``/``False`` erzwingt bzw. unterdrückt das XML.

    Returns:
        dict mit ``pdf_path``/``pdf_filename`` (bzw. ``None``),
        ``xml_path``/``xml_filename`` und ``error`` (Text oder ``None``).
    """
    from .pdf_invoice import generate_invoice_pdf

    result = {'pdf_path': None, 'pdf_filename': None,
              'xml_path': None, 'xml_filename': None, 'error': None}

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


def stored_invoice_document(db, invoice_id: int, kind: str = 'pdf'):
    """Abgelegtes Dokument einer Rechnung laden.

    Returns:
        tuple: ``(bytes, dateiname, fehlertext)`` – bei Erfolg ist der
        Fehlertext ``None``, sonst sind Bytes und Dateiname ``None``.
    """
    idx, extension, label = _KINDS[kind]
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        return None, None, 'Rechnung nicht gefunden.'

    path = _col(invoice, idx)
    # Fallback: Datei am Namensschema suchen, falls der DB-Pfad fehlt oder
    # veraltet ist (z. B. nach einem Umzug des Datenverzeichnisses).
    if not path or not os.path.isfile(path):
        candidate = invoice_file_path(invoice, extension)
        path = candidate if os.path.isfile(candidate) else None

    if not path:
        return None, None, f'Kein {label} vorhanden.'
    try:
        with open(path, 'rb') as f:
            return f.read(), os.path.basename(path), None
    except OSError as e:
        print(f"Error reading {kind} for invoice {invoice_id}: {e}")
        return None, None, f'{label} konnte nicht gelesen werden.'
