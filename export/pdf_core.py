# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Gemeinsame PDF-Primitive (abhängigkeitsarm, ohne externe PDF-Bibliothek).

Wird von den spezifischen Generatoren genutzt:
  - export/pdf_invoice.py   – Rechnungs-PDF
  - export/pdf_worktime.py  – Arbeitszeit-/Monatsstundenzettel

Bereitgestellt werden:
  - escape_pdf_string(s)              – String-Literale für ``(…)``-Syntax escapen
  - load_image_xobject(path, ...)     – Bild (Logo) als komprimiertes RGB-XObject laden
  - build_single_page_pdf(ops, ...)   – einseitiges A4-PDF/A aus Content-Operatoren bauen

Schriften im erzeugten PDF (eingebettet, PDF/A-3b):  /F1 Liberation Sans ·
/F2 Liberation Sans Bold · /F3 Liberation Sans Italic (metrisch = Helvetica)
Ein optionales Bild ist als  /Logo  in den Page-Resources referenzierbar.
"""
import zlib


# Häufige Unicode-Zeichen, die *nicht* in WinAnsi/cp1252 liegen (typische
# Copy-&-Paste-Artefakte aus Office), auf ein darstellbares Pendant abbilden.
# cp1252 selbst kann bereits •, „ " ' ' – — … € usw. – das deckt das Gros ab.
_WINANSI_TRANSLATE = {
    0x25CF: '•', 0x25AA: '•', 0x25A0: '•', 0x2023: '•',
    0x2043: '•', 0x2219: '•', 0x2756: '•',   # diverse Bullet-Glyphen -> •
    0x2192: '->', 0x2190: '<-', 0x2194: '<->', 0x21D2: '=>',
    0x2713: '+', 0x2714: '+', 0x2717: 'x', 0x2718: 'x',     # Haken/Kreuze
    0x00A0: ' ', 0x202F: ' ', 0x2007: ' ', 0x2008: ' ',     # geschützte/schmale Leerzeichen
    0x2009: ' ', 0x200A: ' ', 0x2002: ' ', 0x2003: ' ', 0x200B: '',
    0x2011: '-', 0x2012: '-', 0x2015: '-', 0x2212: '-',     # diverse Striche/Minus -> -
}


def to_winansi_bytes(text: str) -> bytes:
    """Content-Stream-Text als WinAnsi (cp1252) kodieren.

    Die PDF-Fonts nutzen /WinAnsiEncoding; daher muss der Text als cp1252 (nicht
    latin-1!) kodiert werden, sonst werden •, „ " – … € usw. zu '?'. Seltene,
    nicht in cp1252 enthaltene Zeichen werden vorher transliteriert.
    """
    return text.translate(_WINANSI_TRANSLATE).encode('cp1252', errors='replace')


def safe_filename_component(text) -> str:
    """Text als Bestandteil eines Dateinamens bereinigen.

    Entfernt unter Windows/Linux unzulässige Zeichen (``<>:"/\\|?*``) und
    Steuerzeichen, normalisiert Whitespace. Umlaute bleiben erhalten.
    Dient der Namenskonvention "[Nummer] [Kundenname]" (todo #1).
    """
    cleaned = ''.join(c for c in str(text or '')
                      if c not in '<>:"/\\|?*' and ord(c) >= 32)
    return ' '.join(cleaned.split())


def escape_pdf_string(s: str) -> str:
    """Sonderzeichen für ein PDF-String-Literal (``(…)``-Syntax) maskieren.

    Innerhalb eines ``(…)``-Literals müssen Backslash, ``(`` und ``)`` mit einem
    vorangestellten Backslash geschützt werden.
    """
    s = s.replace('\\', '\\\\')
    s = s.replace('(', '\\(')
    s = s.replace(')', '\\)')
    return s


def logo_display_size(image, max_width_pt, max_height_pt):
    """Anzeigegröße (in pt) für ein Logo-XObject berechnen.

    Entkoppelt die *Pixel*-Auflösung des Bildes von seiner *Darstellungsgröße* auf
    der Seite: das Bild wird (seitenverhältnis-erhaltend) in eine Box aus
    ``max_width_pt`` × ``max_height_pt`` Punkten eingepasst. Weil das Bild deutlich
    mehr Pixel als Punkte hat, wird es dadurch gestochen scharf statt verpixelt.
    """
    pw, ph = image.get('width', 0), image.get('height', 0)
    if pw <= 0 or ph <= 0:
        return float(max_width_pt), float(max_height_pt)
    scale = min(max_width_pt / pw, max_height_pt / ph)
    return pw * scale, ph * scale


def resolve_logo_path(logo):
    """Gespeicherten Logo-Wert in einen physischen Dateipfad für die PDF-
    Einbettung auflösen. Liefert ``None``, wenn die Datei nicht lokal ladbar ist.

    Hintergrund: In der DB steht nur der *logische* Pfad ``data/logos/<datei>``
    (bzw. ein ``/seed_data/private/…``-Pfad). Im Mehrbenutzer-Modus liegt die
    Datei physisch unter ``data/users/<user>/logos/<datei>`` – ``os.path.exists``
    auf dem logischen Pfad schlägt dort fehl (Logo fehlte im PDF). Wir lösen den
    Pfad analog zum HTTP-Handler (Basename → Nutzer-logos-Verzeichnis) auf.
    """
    import os
    if not logo:
        return None
    s = str(logo).strip().replace('\\', '/')
    if s.startswith(('http://', 'https://')):
        return None                      # Remote-URLs koennen wir nicht einbetten
    if os.path.isfile(s):
        return s                         # bereits ein gueltiger (z.B. Single-User-)Pfad
    name = os.path.basename(s)
    try:
        import userctx
        cand = os.path.join(userctx.user_subdir('logos', create=False), name)
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    # Geteiltes Privat-Seed-Logo (Eigentuemer-Setup)
    if 'seed_data/private' in s:
        cand = os.path.join('seed_data', 'private', name)
        if os.path.isfile(cand):
            return cand
    return None


def load_image_xobject(path: str, max_width: int = 600, max_height: int = 320):
    """Ein Bild als komprimiertes DeviceRGB-XObject vorbereiten.

    Args:
        path: Pfad zur Bilddatei.
        max_width / max_height: Bild wird (proportional) auf diese *Pixel*-Maße
            verkleinert. Bewusst großzügig, damit das Logo bei kleiner
            Punkt-Darstellung scharf bleibt (siehe :func:`logo_display_size`).

    Returns:
        dict mit Schlüsseln ``data`` (zlib-komprimierte RGB-Bytes), ``width``,
        ``height``, ``filter`` ('/FlateDecode') – oder ``None`` bei Fehler/Datei fehlt.
    """
    import os
    if not path or not os.path.exists(path):
        return None
    try:
        from PIL import Image
        img = Image.open(path)
        if img.width > max_width or img.height > max_height:
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        rgb_data = img.tobytes()
        return {
            'data': zlib.compress(rgb_data),
            'width': img.width,
            'height': img.height,
            'filter': '/FlateDecode',
        }
    except Exception as e:
        print(f"Error loading image '{path}': {e}")
        return None


def build_single_page_pdf(content_ops, image=None, page_size=(595, 842), **doc) -> bytes:
    """Einseitiges PDF/A-3b aus einer Liste von Content-Stream-Operatoren bauen.

    Args:
        content_ops: Liste von Strings (PDF-Content-Operatoren, inkl. ``BT``/``ET``).
                     Schriften ``/F1`` (Regular), ``/F2`` (Bold), ``/F3`` (Italic)
                     stehen zur Verfügung; ein optionales Bild ist als ``/Logo``
                     zeichenbar.
        image:       optionales dict aus :func:`load_image_xobject` (oder None).
        page_size:   (Breite, Höhe) in PDF-Punkten – Default A4 hochkant (595×842).
        **doc:       Dokument-Eigenschaften, siehe :func:`build_multi_page_pdf`.

    Returns:
        Vollständige PDF-Datei als ``bytes``.
    """
    return build_multi_page_pdf([content_ops], image=image, page_size=page_size, **doc)


def build_multi_page_pdf(pages, image=None, page_size=(595, 842), title='Dokument',
                         author='', subject='', facturx_xml=None, created=None) -> bytes:
    """Mehrseitiges PDF/A-3b aus einer Liste von Content-Operator-Listen bauen.

    Args:
        pages: Liste von Content-Streams (je eine Operator-Liste pro Seite, inkl.
               ``BT``/``ET``). Schriften ``/F1``–``/F3`` und – falls vorhanden –
               das Bild ``/Logo`` stehen auf jeder Seite zur Verfügung (das Logo
               erscheint nur dort, wo die Seite den ``Do``-Operator enthält).
        image: optionales dict aus :func:`load_image_xobject` (oder None).
        page_size: (Breite, Höhe) – Default A4 hochkant.
        title / author / subject: Dokument-Metadaten (Info-Dictionary + XMP).
        facturx_xml: CII-XML (``bytes``); wenn gesetzt, wird es als
            ``factur-x.xml`` eingebettet (ZUGFeRD/Factur-X, Profil EN 16931).
        created: Erstellzeitpunkt (UTC); Standard: jetzt.

    Die Schriften (Liberation Sans) werden als Untermenge eingebettet, die
    Content-Streams bleiben WinAnsi-kodiert. Aufbau siehe :mod:`export.pdfa`.
    """
    from .pdfa import assemble_pdf
    streams = [to_winansi_bytes("\n".join(ops)) for ops in (pages or [[]])]
    return assemble_pdf(streams, image=image, page_size=page_size, title=title,
                        author=author, subject=subject, facturx_xml=facturx_xml,
                        created=created)
