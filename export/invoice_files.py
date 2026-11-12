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

Geht eine versendete Rechnung zurück auf Entwurf, wandern ihre Dokumente in
``invoices/<Jahr>/Fassungen/<ID>/`` (:func:`archive_sent_documents`): Der Kunde hat sie bekommen, sie
verschwinden nicht und werden nie überschrieben. Der Ordner ist zugleich das Merkmal „war schon
versendet“ (:func:`was_sent_before`): Der Entwurf gibt seine Rechnungsnummer dann nie zurück.

Ein in der Datenbank gespeicherter Dokumentpfad wird nur benutzt, wenn er im
Dokumentordner des Nutzers liegt (:func:`path_in_documents`): Die Datenbank lässt
sich per SQL-Konsole oder zurückgespielte Sicherung verändern, und sonst ließe sich
darüber jede lesbare Datei (``auth.db``, die Datenbank eines anderen Benutzers)
ausliefern oder löschen.
"""
import datetime
import os
import shutil

from db.invoice_lock import FROZEN_STATUSES
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


def invoice_file_path(invoice, extension: str, db=None) -> str:
    """Vollständiger Ablagepfad für ``extension`` ("pdf" / "xml").

    Mit ``db``: Gehört der Name schon zur abgelegten Datei eines ANDEREN Belegs (zwei Nummern, die
    zum selben Dateinamen werden, etwa durch ersetzte Sonderzeichen), bekommt der Dateiname die ID
    der Rechnung als Zusatz – die Datei eines anderen Belegs wird nie überschrieben."""
    path = os.path.join(invoice_dir(invoice), f"{invoice_file_stem(invoice)}.{extension}")
    invoice_id = _col(invoice, 0)
    if db is not None and invoice_id is not None and db.document_path_in_use(path, invoice_id):
        path = os.path.join(invoice_dir(invoice),
                            f"{invoice_file_stem(invoice)} (ID {invoice_id}).{extension}")
    return path


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


def drop_stale_file(old_path, new_path, db=None, invoice_id=None):
    """Vorgänger-Datei löschen, wenn der Name sich geändert hat.

    Ändern sich Rechnungsnummer oder Kunde, würde die alte Datei sonst als
    Dublette liegen bleiben. Gelöscht wird nur im Dokumentordner des Nutzers: Der
    alte Pfad stammt aus der Datenbank (siehe :func:`path_in_documents`). Mit ``db``
    bleibt die Datei außerdem liegen, wenn ein anderer Beleg auf sie verweist – sonst
    ließe sich über einen umgebogenen Pfad die archivierte PDF einer fremden Rechnung löschen.
    """
    if not old_path or not new_path:
        return
    try:
        if os.path.abspath(old_path) == os.path.abspath(new_path):
            return
    except (OSError, ValueError, TypeError):
        return
    remove_document_file(old_path, db, invoice_id)


def remove_document_file(path, db=None, invoice_id=None):
    """Eine abgelegte Rechnungsdatei löschen – nur im Dokumentordner des Nutzers
    (:func:`path_in_documents`) und, mit ``db``, nur wenn kein anderer Beleg auf sie verweist.
    Returns ``True``, wenn die Datei gelöscht wurde."""
    try:
        if not (isinstance(path, str) and os.path.isfile(path)):
            return False
        if not path_in_documents(path):
            print(f"Rechnungsdatei liegt außerhalb des Dokumentordners, nicht entfernt: {path!r}")
            return False
        if _in_versions(path):
            print(f"Rechnungsdatei gehört zu einer versendeten Fassung, nicht entfernt: {path!r}")
            return False
        if db is not None and db.document_path_in_use(path, invoice_id):
            print(f"Rechnungsdatei gehört zu einem anderen Beleg, nicht entfernt: {path!r}")
            return False
        os.remove(path)
        return True
    except OSError as e:      # Nicht löschbar ist kein Grund, den Vorgang abzubrechen
        print(f"Rechnungsdatei nicht entfernt ({path}): {e}")
        return False


# ── Fassungen: was versendet war, bleibt liegen ─────────────────────────────────
FASSUNGEN = 'Fassungen'


def versions_dir(invoice) -> str:
    """Ablageordner der versendeten Fassungen einer Rechnung: ``invoices/<Jahr>/Fassungen/<ID>``."""
    return os.path.join(invoice_dir(invoice), FASSUNGEN, str(_col(invoice, 0)))


def _in_versions(path) -> bool:
    """Liegt ``path`` in einem Ordner „Fassungen“ unter dem Dokumentordner? Dort wird nie gelöscht."""
    try:
        relative = os.path.relpath(os.path.realpath(path), os.path.realpath(documents_root()))
    except (OSError, ValueError):
        return False
    return FASSUNGEN in relative.split(os.sep)[:-1]


def was_sent_before(invoice_id) -> bool:
    """Wurde diese Rechnung schon versendet und danach auf Entwurf zurückgeholt? Das Merkmal ist der
    Ordner ``Fassungen/<ID>`` mit einer Datei darin, in irgendeinem Jahresordner (das Rechnungsdatum
    kann sich im Entwurf ändern). Eine Rechnung ohne abgelegte Dokumente hinterlässt keine Spur."""
    import userctx
    root = os.path.join(userctx.user_data_dir(), 'invoices')
    try:
        years = os.listdir(root)
    except OSError:
        return False
    for year in years:
        folder = os.path.join(root, year, FASSUNGEN, str(invoice_id))
        try:
            if any(os.path.isfile(os.path.join(folder, name)) for name in os.listdir(folder)):
                return True
        except OSError:
            continue
    return False


def _version_name(folder, stem, extension, day):
    """Ein Dateiname im Ordner der Fassungen, der noch nicht vergeben ist:
    ``<Name> – versendet bis <JJJJ-MM-TT>.<Endung>``, bei Kollision mit Zähler (``… (2)``)."""
    name = f"{stem} – versendet bis {day}{extension}"
    counter = 1
    while os.path.exists(os.path.join(folder, name)):
        counter += 1
        name = f"{stem} – versendet bis {day} ({counter}){extension}"
    return os.path.join(folder, name)


def archive_sent_documents(db, invoice_id: int, today=None):
    """PDF und XML der versendeten Rechnung nach ``Fassungen/<ID>/`` **verschieben** – beim
    Zurückholen auf Entwurf. Was dort hinkommt, ist die Datei, die der Kunde bekommen hat: Sie wird
    nie überschrieben (ein vergebener Name bekommt einen Zähler) und nie gelöscht.

    Verschoben wird nur, was der Nutzer als Dokument der Rechnung sieht (``stored_invoice_path``:
    gespeicherter Pfad im Dokumentordner, sonst die Datei am Namensschema) und kein anderer Beleg als
    seine Datei führt. Ein gespeicherter Pfad außerhalb des Dokumentordners bleibt, wo er ist. Das
    Eintragen der geleerten Pfade ist Sache des Aufrufers.

    Scheitert ein Verschieben, geht das schon Verschobene zurück und es folgt ein ``OSError``.

    Returns: ``[(alter Pfad, neuer Pfad)]``."""
    invoice = db.get_document(invoice_id, 'invoice')
    if invoice is None:
        return []
    day = today or datetime.date.today().isoformat()
    folder = versions_dir(invoice)
    moved = []
    try:
        for kind in ('pdf', 'xml'):
            path = stored_invoice_path(db, invoice_id, kind, invoice)[0]
            if not path or db.document_path_in_use(path, invoice_id):
                continue
            stem, extension = os.path.splitext(os.path.basename(path))
            os.makedirs(folder, exist_ok=True)
            target = _version_name(folder, stem, extension, day)
            shutil.move(path, target)
            moved.append((path, target))
    except OSError:
        restore_archived(moved)
        raise
    return moved


def restore_archived(moved):
    """Verschobene Dokumente zurücklegen (``[(alter Pfad, neuer Pfad)]`` aus
    :func:`archive_sent_documents`) und einen dabei leer gewordenen Ordner entfernen."""
    for old, new in reversed(moved):
        try:
            if os.path.isfile(new) and not os.path.exists(old):
                shutil.move(new, old)
        except OSError as e:
            print(f"Fassung konnte nicht zurückgelegt werden ({new} → {old}): {e}")
    for _old, new in moved:
        try:
            os.rmdir(os.path.dirname(new))
        except OSError:
            pass


def wants_xrechnung(invoice) -> bool:
    """True, wenn die Rechnung E-Rechnungsdaten trägt (Leitweg-ID/Bestellnr.)."""
    return bool(_col(invoice, _IDX_ROUTE_ID) or _col(invoice, _IDX_ORDER_NUMBER))


# Ab diesen Stati (``db.invoice_lock.FROZEN_STATUSES``: versendet … bezahlt, storniert) ist die
# Rechnung eingefroren
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
        xml_path = invoice_file_path(invoice, 'xml', db)
        os.makedirs(os.path.dirname(xml_path), exist_ok=True)
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        drop_stale_file(_col(invoice, _IDX_XML_PATH), xml_path, db, invoice_id)
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


def _is_document_file(path, invoice):
    """Datei vorhanden UND – nach Auflösen von Links – im Dokumentordner (ein Symlink am
    Namensschema, der hinausführt, zählt nicht)."""
    return os.path.isfile(path) and path_in_documents(path, invoice)


def _stored_by_name(invoice, extension, db=None):
    """Datei am Namensschema suchen (``None``, wenn es sie nicht gibt).

    Rechnung: ``invoices/<Jahr des Rechnungsdatums>/…``. Beim Angebot steht im Ordnernamen
    das Jahr der Erzeugung, das der Datensatz nicht kennt – dort zählt der neueste Treffer.

    Mit ``db`` wird nie die abgelegte Datei eines ANDEREN Belegs genommen (zwei Nummern, die zum
    selben Dateinamen werden): Eine Rechnung findet dann ihre Datei mit dem Zusatz aus
    :func:`invoice_file_path`, sonst keine.
    """
    own_id = _col(invoice, 0)

    def belongs_to_another(path):
        return db is not None and own_id is not None and db.document_path_in_use(path, own_id)

    if not is_quote(invoice):
        candidate = invoice_file_path(invoice, extension)
        if not _is_document_file(candidate, invoice):
            return None
        if not belongs_to_another(candidate):
            return candidate
        candidate = invoice_file_path(invoice, extension, db)       # mit der ID im Namen
        return candidate if _is_document_file(candidate, invoice) else None
    root = documents_root(invoice)
    name = f"{invoice_file_stem(invoice)}.{extension}"
    try:
        years = sorted(os.listdir(root), reverse=True)
    except OSError:
        return None
    for year in years:
        candidate = os.path.join(root, year, name)
        if _is_document_file(candidate, invoice) and not belongs_to_another(candidate):
            return candidate
    return None


def stored_invoice_path(db, invoice_id: int, kind: str = 'pdf', invoice=None):
    """Pfad des abgelegten Dokuments einer Rechnung (bzw. eines Angebots).

    Der in der Datenbank gespeicherte Pfad zählt nur im Dokumentordner des Nutzers
    (:func:`path_in_documents`). Liegt er außerhalb, gilt er als nicht vorhanden – dann
    greift der Fallback über das Namensschema –; die Antwort verrät davon nichts, nur das
    Server-Log. Wer ein gespeichertes Dokument öffnet (Download, E-Mail-Anhang) oder anzeigt,
    ob es eines gibt (Listen, Formulare), holt den Pfad hier.

    ``invoice`` ist die schon geladene Zeile (``SELECT *``); damit entfällt das erneute Lesen,
    etwa je Zeile einer Liste.

    Returns:
        tuple: ``(pfad, fehlertext)`` – bei Erfolg ist der Fehlertext ``None``, sonst der Pfad.
    """
    idx, extension, label = _KINDS[kind]
    if invoice is None:
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
        path = _stored_by_name(invoice, extension, db)

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
