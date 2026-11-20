# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Belegdateien (seit 2.5.11): Ablage, Datum, Pfadprüfung und Anzeige – ohne HTTP.

Jede Datei, die auf der Belege-Seite hochgeladen wird, bekommt sofort einen Eintrag (``Database.insert_receipt_with_file``) und
liegt unter ``<Datenordner des Benutzers>/Belege/<Jahr>/<Nummer>_<Name>``; ``Documents.Path`` steht relativ zum Datenordner (mit
``/``). Den Pfad setzt der Server, nie der Client. Ein Kontoauszug ist dort ein Beleg wie jede andere Datei: Er wird nur gelesen,
um das Belegdatum zu finden und auf den Import der Seite „Buchungen“ hinzuweisen (``receipt_date``), nie importiert und nie in
``pending_imports/`` abgelegt.

Der Inhalt wird VOR der Transaktion der Datenbank als Teildatei (``write_part``) in den Jahresordner geschrieben (``year_folder``
legt ihn an und prüft, dass er kein Link ist); in der Transaktion belegt ``reserve_name`` nur den Namen, und ``os.replace`` setzt
die Teildatei an ihren Platz – das Schreiben von bis zu 200 MB hält so keine Schreibsperre. Nie wird eine vorhandene Datei
überschrieben.

``stored_receipt_path`` ist die EINE Stelle, über die ein gespeicherter Pfad geöffnet oder gelöscht wird: Der Wert in der
Datenbank gilt nur innerhalb von ``Belege/`` und – für Bestand aus dem früheren Upload – ``Documents/`` des angemeldeten
Benutzers (Muster ``export/invoice_files.path_in_documents``). Alles andere gilt als nicht vorhanden; der Pfad steht dann nur im
Server-Log, nie in einer Antwort. Gelöscht wird zusätzlich nur, was der Upload selbst abgelegt hat (``_uploaded_shape``:
``Belege/<JJJJ>/<Name>``, kein Link) – nie der Bestand des früheren Uploads und nie, was der Kontoauszug-Import unter
``Belege/<Jahr>/Konten/`` und ``Belege/<Jahr>/general/`` ablegt.
"""
import collections
import datetime
import os
import re
import secrets
import stat
import sys
import traceback
import unicodedata

import userctx
from export.pdf_core import LOGO_CONTENT_TYPES, logo_content_matches, logo_extension
from .period import parse_iso_date

BELEGE_DIR = 'Belege'                  # hier legt der Upload ab
LEGACY_DIR = 'Documents'               # hier legte der frühere Upload ab: Bestand bleibt les- und herunterladbar
STAGING_DIR = ('upload_tmp', 'receipts')     # Zwischenablage zum Lesen eines PDF (``upload_tmp`` sichert die Datensicherung nicht)

NAME_LIMIT = 120                       # so lang ist der bereinigte Name einer hochgeladenen Datei höchstens (Zeichen, ohne die Nummer davor)
EXTENSION_LIMIT = 20                   # davon die Endung mit Punkt höchstens
NAME_BYTES = 200                       # so lang ist der ganze abgelegte Name (Nummer, ``_``, Name) höchstens (Bytes in UTF-8) – bleibt mit ``_1000`` unter den 255 eines Dateinamens
NUMBER_BYTES = 60                      # davon die Nummer höchstens (Bytes in UTF-8)
DISPLAY_NAME_LIMIT = 255               # so lang ist ``Documents.Filename`` höchstens
STORED_PATH_LIMIT = 1024               # so lang ist ein gespeicherter Pfad höchstens (Zeichen): ein längerer Wert gilt als kein Pfad
HEAD_BYTES = 16                        # so viele Anfangsbytes genügen, um PDF und die Bilder der Positivliste zu erkennen
MAX_NAME_TRIES = 1000                  # so viele Namen probiert ``reserve_name`` (``_2`` … ``_1000``)
PART_SUFFIX = '.part'                  # so enden die Teildateien, die ``write_part`` im Jahresordner anlegt

NOT_FOUND = 'Diesen Beleg gibt es nicht.'
NO_FILE = 'Zu diesem Beleg liegt keine Datei vor.'
FOREIGN = 'Belege lassen sich nur über die Seite „Belege“ öffnen.'
OPEN_FAILED = 'Die Datei konnte nicht geöffnet werden – Einzelheiten stehen im Server-Log.'

# Erlaubt im Dateinamen einer abgelegten Datei – alles andere wird ``_``
_OUTSIDE = re.compile(r'[^A-Za-z0-9ÄÖÜäöüß ._()-]')
# Namen, unter denen Windows ein Gerät öffnet, auch mit Endung („NUL.pdf“): Als Dateiname bekämen sie nie eine Datei
_DEVICE_NAMES = frozenset({'CON', 'PRN', 'AUX', 'NUL', *(f'COM{n}' for n in range(1, 10)), *(f'LPT{n}' for n in range(1, 10))})
# So heißen die Ordner des Kontoauszug-Imports im Jahresordner (``Belege/<Jahr>/Konten/<Bank>/``, ``Belege/<Jahr>/general/``): Eine Datei,
# die ohne Rücksicht auf Groß-/Kleinschreibung genauso heißt, blockierte den Ordner
_IMPORT_FOLDERS = frozenset({'konten', 'general'})

OpenedReceipt = collections.namedtuple('OpenedReceipt', 'source size name inline removable')


class UnsafeFolder(OSError):
    """Der Jahresordner unter ``Belege/`` ist ein Link (oder liegt sonst nicht dort, wo er liegen soll): Dort wird nichts abgelegt."""


def _today():
    return datetime.date.today().isoformat()


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ── Namen ────────────────────────────────────────────────────────────────────
def display_name(original):
    """Der Name einer hochgeladenen Datei, wie er in ``Documents.Filename`` steht und angezeigt wird: nur der Name (``\\`` zählt
    als Trenner), ohne Steuerzeichen und unsichtbare Formatzeichen (Unicode-Kategorien ``Cc`` und ``Cf``: auch die C1-Steuerzeichen
    und die Richtungsumkehr U+202E, die „gnp.pdf“ als „pdf.png“ erscheinen ließe) und ohne Leerraum am Rand, höchstens
    ``DISPLAY_NAME_LIMIT`` Zeichen; leer ergibt ``Beleg``."""
    name = os.path.basename(str(original or '').replace('\\', '/'))
    return ''.join(char for char in name if unicodedata.category(char) not in ('Cc', 'Cf')).strip()[:DISPLAY_NAME_LIMIT] or 'Beleg'


def safe_file_part(text):
    """``text`` als Teil eines Dateinamens: Unicode in NFC, jedes Zeichen außerhalb von ``[A-Za-z0-9ÄÖÜäöüß ._()-]`` (auch
    ``/`` und ``\\``) wird ``_``, Punkte und Leerzeichen am Rand fallen weg."""
    return _OUTSIDE.sub('_', unicodedata.normalize('NFC', str(text or ''))).strip('. ')


def safe_receipt_name(original):
    """Der Dateiname, unter dem eine hochgeladene Datei abgelegt wird – nie ein Pfad, nie ein Name, der aus dem Ordner führt:
    ``\\`` gilt als Trenner, nur der Name bleibt (``basename``), Unicode in NFC, jedes Zeichen außerhalb von
    ``[A-Za-z0-9ÄÖÜäöüß ._()-]`` wird ``_``, Punkte und Leerzeichen am Rand fallen weg, höchstens ``NAME_LIMIT`` Zeichen (die
    Endung bleibt), leer ergibt ``Beleg``. Der Name eines Windows-Geräts („NUL“, „COM1“ …) bekommt einen Unterstrich davor. Der
    Name, unter dem die Datei dann liegt, ist ``stored_file_name`` (mit der Nummer davor und der Grenze in Bytes)."""
    name = safe_file_part(os.path.basename(str(original or '').replace('\\', '/')))
    stem, extension = os.path.splitext(name)
    extension = extension[:EXTENSION_LIMIT]
    name = (stem[:max(1, NAME_LIMIT - len(extension))] + extension).strip('. ') or 'Beleg'
    if name.split('.', 1)[0].rstrip().upper() in _DEVICE_NAMES:
        name = '_' + name
    return name


def _cut_bytes(text, limit):
    """``text`` auf höchstens ``limit`` Bytes in UTF-8 gekürzt – nie mitten in einem Zeichen; ein gekürzter Rest, der mit Punkt oder
    Leerzeichen endet, verliert sie. Was ohnehin passt, bleibt, wie es ist."""
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode('utf-8', 'ignore').rstrip('. ')


def stored_file_name(number, original):
    """Der Name, unter dem eine hochgeladene Datei abgelegt wird: ``<Nummer>_<Name>`` (ohne Nummer nur ``<Name>``).

    Die Nummer geht durch ``safe_file_part`` und ist höchstens ``NUMBER_BYTES`` Bytes lang, der Name ist ``safe_receipt_name``; der
    ganze Name hat höchstens ``NAME_BYTES`` Bytes in UTF-8 – gekürzt wird der Stamm des Namens (zeichenweise, nie mitten in einem
    Zeichen), die Endung bleibt. Ein Name, der ohne Rücksicht auf Groß-/Kleinschreibung ``Konten`` oder ``general`` heißt (so
    heißen die Ordner des Kontoauszug-Imports im Jahresordner), bekommt wie ein Windows-Gerätename einen Unterstrich davor; nur
    der ganze Name ist reserviert, ``Konten.pdf`` ist frei."""
    prefix = _cut_bytes(safe_file_part(number), NUMBER_BYTES)
    lead = f'{prefix}_' if prefix else ''
    stem, extension = os.path.splitext(safe_receipt_name(original))
    room = NAME_BYTES - len(lead.encode('utf-8')) - len(extension.encode('utf-8'))
    name = lead + _cut_bytes(stem, room) + extension
    return '_' + name if name.casefold() in _IMPORT_FOLDERS else name


def year_folder(year):
    """Der Ordner ``<Datenordner des Benutzers>/Belege/<year>`` für neue Dateien – angelegt, wenn er fehlt. ``Belege/`` selbst darf
    ein Link sein (ein Ordner auf einem anderen Laufwerk, ``DEPLOYMENT.md``), der Jahresordner nicht: Sein aufgelöster Pfad muss
    genau ``<Belege, aufgelöst>/<year>`` sein, sonst ``UnsafeFolder`` – eine Datei läge sonst dort, wohin der Link zeigt."""
    belege = os.path.join(userctx.user_data_dir(), BELEGE_DIR)
    folder = os.path.join(belege, str(year))
    os.makedirs(folder, exist_ok=True)
    if os.path.normcase(os.path.realpath(folder)) != os.path.normcase(os.path.join(os.path.realpath(belege), str(year))):
        raise UnsafeFolder('Der Jahresordner der Belege ist ein Link.')
    return folder


def write_part(folder, content):
    """Den Inhalt in eine neue Teildatei (``<32 Hex-Zeichen>.part``) in ``folder`` schreiben; liefert ihren Pfad. Angelegt wird mit
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` (nie eine vorhandene Datei); scheitert das Schreiben, bleibt keine Teildatei liegen."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    path = os.path.join(folder, secrets.token_hex(16) + PART_SUFFIX)
    descriptor = os.open(path, flags, 0o640)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
    except BaseException:
        remove_quietly(path)
        raise
    return path


def reserve_name(folder, name):
    """Den Namen ``name`` in ``folder`` belegen: eine leere Datei anlegen (``O_CREAT|O_EXCL|O_NOFOLLOW``) – nie eine vorhandene
    überschreiben: Gibt es den Namen schon (auch als Link oder Ordner), kommt ``_2``, ``_3`` … vor die Endung. Die leere Datei
    gehört dem Aufrufer; er ersetzt sie (``os.replace``) durch die Teildatei oder entfernt sie wieder. Returns ``(Pfad,
    Dateiname)``; sind ``MAX_NAME_TRIES`` Namen belegt, ``FileExistsError``."""
    stem, extension = os.path.splitext(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    for attempt in range(1, MAX_NAME_TRIES + 1):
        candidate = name if attempt == 1 else f'{stem}_{attempt}{extension}'
        path = os.path.join(folder, candidate)
        try:
            os.close(os.open(path, flags, 0o640))
        except FileExistsError:
            continue
        return path, candidate
    raise FileExistsError(f'Kein freier Dateiname nach {MAX_NAME_TRIES} Versuchen.')


# ── Datum und Art ────────────────────────────────────────────────────────────
def _iso_date(value):
    """``value`` (ein Datum oder ein Text) als ISO-Datum, wenn es ein gültiges ist (Jahr 1900–2200), sonst ``None``."""
    if isinstance(value, datetime.datetime):
        value = value.date()
    if isinstance(value, datetime.date):
        value = value.isoformat()
    return parse_iso_date(value)


def _read_pdf(content):
    """``(als Kontoauszug erkannt, Datum oder None)``. Das PDF liegt nur für diesen Aufruf unter ``upload_tmp/receipts/``."""
    from document_parser import DocumentParser
    folder = os.path.join(userctx.user_data_dir(), *STAGING_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, secrets.token_hex(16) + '.pdf')
    try:
        with open(path, 'xb') as handle:
            handle.write(content)
        parsed = DocumentParser().parse_document(path) or {}
    finally:
        remove_quietly(path)
    return 'transactions' in parsed, parsed.get('document_date') or parsed.get('date')


def _read_camt(name, content):
    from importers.camt import parse_upload
    statements = parse_upload(str(name), content)
    dates = (_iso_date(statement.document_date) for _label, statement in statements)
    return bool(statements), next((day for day in dates if day), None)


def _read_dkb_csv(content):
    from importers.dkb_csv import parse_csv
    return True, parse_csv(content).document_date


def receipt_date(name, content):
    """Datum und Art eines hochgeladenen Belegs: ``(JJJJ-MM-TT, ist_Kontoauszug)``.

    Ein erkannter Kontoauszug (PDF der Volksbank oder DKB, CAMT als XML oder ZIP, Umsatzliste der DKB als CSV) liefert sein
    Belegdatum, wenn er eines nennt; sonst das Datum, das ein PDF nennt, sonst gilt der Tag des Uploads. Die Datei wird dafür nur
    gelesen – nie importiert, nie abgelegt. Jede Ausnahme beim Lesen heißt nur „kein Datum, kein Auszug“ (im Log steht eine Zeile
    mit dem Namen der Ausnahme, nie ein Dateiinhalt): Der Beleg entsteht trotzdem."""
    lower = str(name or '').lower()
    recognised, found = False, None
    try:
        if lower.endswith('.pdf'):
            recognised, found = _read_pdf(content)
        elif lower.endswith(('.xml', '.zip')):
            recognised, found = _read_camt(name, content)
        elif lower.endswith('.csv'):
            recognised, found = _read_dkb_csv(content)
    except Exception as exc:
        recognised, found = False, None
        print(f'Beleg-Upload: nicht als Kontoauszug gelesen ({type(exc).__name__})', file=sys.stderr)
    return _iso_date(found) or _today(), recognised


# ── Gespeicherter Pfad ───────────────────────────────────────────────────────
def _candidate(path):
    """Der absolute Pfad zu einem gespeicherten Wert (ein relativer hängt am Datenordner des Benutzers), Links nicht aufgelöst –
    oder ``None``, wenn der Wert kein brauchbarer Pfad ist (kein Text, leer, NUL, über ``STORED_PATH_LIMIT`` Zeichen: ein sehr
    langer, tief verschachtelter Wert bremste sonst jede Liste, die ihn auflöst)."""
    if not isinstance(path, str) or not path.strip() or '\0' in path or len(path) > STORED_PATH_LIMIT:
        return None
    return path if os.path.isabs(path) else os.path.join(userctx.user_data_dir(), path)


def _resolve(path):
    candidate = _candidate(path)
    if candidate is None:
        return None
    try:
        return os.path.realpath(candidate)
    except (OSError, ValueError):
        return None


def _inside(real, root):
    try:
        return os.path.normcase(os.path.commonpath([real, root])) == os.path.normcase(root)
    except ValueError:                 # anderes Laufwerk unter Windows …
        return False


def _uploaded_shape(path):
    """Der aufgelöste Pfad, wenn der gespeicherte Wert genau die Form hat, in der der Upload ablegt – sonst ``None``. Nur eine solche
    Datei löscht ``remove_receipt_file`` (und nur für sie fragt die Seite „und seine Datei“).

    Die Form: ein relativer Wert, mit ``/`` getrennt genau ``Belege/<JJJJ>/<Name>`` (``<JJJJ>`` vier ASCII-Ziffern, ``<Name>`` nicht
    leer, ohne ``\\``, nicht ``.`` oder ``..``) UND der aufgelöste Pfad ist genau ``<Belege, aufgelöst>/<JJJJ>/<Name>``: weder die Datei
    noch der Jahresordner ist ein Link (``Belege/`` selbst darf einer sein). Was der Kontoauszug-Import unter ``Belege/<Jahr>/Konten/`` und
    ``…/general/`` ablegt, hat diese Form nicht, ebenso wenig ein absoluter Pfad, ``..`` im Wert oder der Bestand in ``Documents/``."""
    if not isinstance(path, str):
        return None
    parts = path.split('/')
    if len(parts) != 3 or parts[0] != BELEGE_DIR or not re.fullmatch('[0-9]{4}', parts[1]):
        return None
    name = parts[2]
    if not name or '\\' in name or name in ('.', '..'):
        return None
    real = _resolve(path)
    if real is None:
        return None
    expected = os.path.join(os.path.realpath(os.path.join(userctx.user_data_dir(), BELEGE_DIR)), parts[1], name)
    return real if os.path.normcase(real) == os.path.normcase(expected) else None


def stored_receipt_path(path, log=True):
    """Der aufgelöste Pfad (Links und ``..`` aufgelöst) zu dem Wert, der in ``Documents.Path`` steht – nur, wenn er in ``Belege/``
    oder ``Documents/`` des angemeldeten Benutzers liegt; sonst ``None``. Ein relativer Wert hängt am Datenordner des Benutzers.
    Ein Wert, der keine Datei nennt (``None``, leer oder nur Leerraum), ergibt ``None`` ohne Meldung; jeder andere abgelehnte
    Wert steht mit einer Zeile im Server-Log (``log=False`` für Listen: eine Zeile je Beleg füllte das Log)."""
    real = _resolve(path)
    if real is not None:
        base = userctx.user_data_dir()
        for folder in (BELEGE_DIR, LEGACY_DIR):
            if _inside(real, os.path.realpath(os.path.join(base, folder))):
                return real
    if log and not (path is None or (isinstance(path, str) and not path.strip())):
        print(f'Belegpfad außerhalb der Beleg-Ordner ignoriert: {path!r}', file=sys.stderr)
    return None


def inline_type(name, head):
    """Der Inhaltstyp, mit dem eine Datei im Browser ANGEZEIGT werden darf – sonst ``None``: nur wenn Endung UND Anfangsbytes
    passen. Ein PDF (``%PDF-``) und die Rasterbilder der Positivliste der Logos (``export/pdf_core.LOGO_CONTENT_TYPES``: PNG,
    JPG, GIF, WEBP). Alles andere – auch SVG, HTML und XML – wird nur heruntergeladen: Es liefe als Skript im Ursprung der App."""
    if os.path.splitext(str(name or ''))[1].lower() == '.pdf':
        return 'application/pdf' if head.startswith(b'%PDF-') else None
    extension = logo_extension(name)
    if extension and logo_content_matches(extension, head):
        return LOGO_CONTENT_TYPES[extension]
    return None


def open_receipt(path, log=True):
    """Die Datei eines Belegs zum Lesen öffnen: ``OpenedReceipt(source, size, name, inline, removable)`` – das Dateiobjekt (der
    Aufrufer schließt es), die Größe der geöffneten Datei, ihr Name auf der Platte, der Inhaltstyp, mit dem sie angezeigt werden
    darf (``inline_type``, sonst ``None``), und ob ``remove_receipt_file`` sie mit dem Beleg löscht (``_uploaded_shape``: die Form,
    in der der Upload ablegt, ``Belege/<Jahr>/<Name>``). ``None``, wenn es keine Datei gibt: kein gültiger Pfad
    (``stored_receipt_path``), nichts da oder keine reguläre Datei (ein Link, ein Ordner, eine Pipe).

    Geöffnet wird mit ``O_NOFOLLOW`` (ein Link, der erst nach der Prüfung an die Stelle der Datei tritt, scheitert) und ohne zu
    warten (eine Pipe hielte das Öffnen sonst auf); gelesen wird danach wartend."""
    real = stored_receipt_path(path, log)
    if real is None:
        return None
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    try:
        descriptor = os.open(real, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        regular = stat.S_ISREG(info.st_mode)
        if regular and hasattr(os, 'O_NONBLOCK'):
            os.set_blocking(descriptor, True)
    except BaseException:
        os.close(descriptor)
        raise
    if not regular:
        os.close(descriptor)
        return None
    source = os.fdopen(descriptor, 'rb', buffering=0)
    try:
        head = source.read(HEAD_BYTES)
        source.seek(0)
    except BaseException:
        source.close()
        raise
    name = os.path.basename(real)
    return OpenedReceipt(source, info.st_size, name, inline_type(name, head), _uploaded_shape(path) is not None)


def file_info(stored):
    """Was die Seite zur Datei eines Belegs weiß: ``None`` (kein Pfad gespeichert), ``False`` (ein Pfad ist gespeichert, gilt aber
    nicht oder die Datei fehlt) oder ``OpenedReceipt`` (schon geschlossen: es zählen Name, ``inline`` und ``removable``).
    Der Pfad selbst erscheint nie auf der Seite."""
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return None
    try:
        opened = open_receipt(stored, log=False)
    except OSError:
        return False
    if opened is None:
        return False
    opened.source.close()
    return opened


# ── Löschen ──────────────────────────────────────────────────────────────────
def _shared(db, real):
    """Zeigt ein (anderer) Beleg auf dieselbe aufgelöste Datei?"""
    conn = db._get_connection()
    try:
        rows = conn.execute("SELECT Path FROM Documents WHERE Path IS NOT NULL AND Path != ''").fetchall()
    finally:
        conn.close()
    same = os.path.normcase(real)
    return any(resolved is not None and os.path.normcase(resolved) == same
               for resolved in (_resolve(value) for (value,) in rows))


def remove_receipt_file(db, path):
    """Die Datei eines gelöschten Belegs entfernen – nur, wenn der gespeicherte Pfad genau die Form hat, in der der Upload ablegt
    (``_uploaded_shape``: ``Belege/<JJJJ>/<Name>``; nicht der Bestand des früheren Uploads in ``Documents/`` und nicht, was der
    Kontoauszug-Import unter ``Belege/<Jahr>/Konten/`` oder ``…/general/`` ablegt), er selbst eine reguläre Datei ist (kein Link)
    und kein anderer Beleg auf dieselbe aufgelöste Datei zeigt (die Zeile des gelöschten Belegs ist beim Aufruf schon weg). Ein
    Fehlschlag wird nur protokolliert. Returns ``True``, wenn die Datei gelöscht wurde."""
    try:
        real = stored_receipt_path(path)
        if real is None:
            return False
        if _uploaded_shape(path) is None:
            print(f'Belegdatei hat nicht die Form {BELEGE_DIR}/<Jahr>/<Name> einer hochgeladenen Datei, nicht entfernt: {path!r}',
                  file=sys.stderr)
            return False
        if not stat.S_ISREG(os.lstat(_candidate(path)).st_mode):
            print(f'Belegdatei ist keine reguläre Datei, nicht entfernt: {path!r}', file=sys.stderr)
            return False
        if _shared(db, real):
            print(f'Belegdatei gehört auch zu einem anderen Beleg, nicht entfernt: {path!r}', file=sys.stderr)
            return False
        os.remove(real)
        return True
    except FileNotFoundError:
        return False                   # sie ist schon weg
    except Exception:
        traceback.print_exc()
        return False
