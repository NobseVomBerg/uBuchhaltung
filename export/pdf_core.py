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
  - build_single_page_pdf(ops, ...)   – einseitiges A4-PDF aus Content-Operatoren bauen

Schriften im erzeugten PDF:  /F1 Helvetica · /F2 Helvetica-Bold · /F3 Helvetica-Oblique
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


def build_single_page_pdf(content_ops, image=None, page_size=(595, 842)) -> bytes:
    """Einseitiges PDF aus einer Liste von Content-Stream-Operatoren bauen.

    Args:
        content_ops: Liste von Strings (PDF-Content-Operatoren, inkl. ``BT``/``ET``).
                     Schriften ``/F1``, ``/F2``, ``/F3`` stehen zur Verfügung; ein
                     optionales Bild ist als ``/Logo`` zeichenbar.
        image:       optionales dict aus :func:`load_image_xobject` (oder None).
        page_size:   (Breite, Höhe) in PDF-Punkten – Default A4 hochkant (595×842).

    Returns:
        Vollständige PDF-Datei als ``bytes``.

    Deterministisches Objekt-Layout (Bild nur, falls vorhanden):
        1 Catalog · 2 Pages · 3 Font F1 · 4 Font F2 · 5 Font F3
        6 Page · 7 Content-Stream · [8 Image]
    """
    width, height = page_size

    # Content-Stream komprimieren (FlateDecode); Text als WinAnsi/cp1252 kodieren
    content_bytes = to_winansi_bytes("\n".join(content_ops))
    compressed = zlib.compress(content_bytes)

    has_image = image is not None
    image_obj_num = 8 if has_image else None

    xobject_ref = f" /XObject << /Logo {image_obj_num} 0 R >>" if has_image else ""
    resources = f"<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >>{xobject_ref} >>"

    # Textuelle Objekte (1–6) als (num, content)-Liste
    text_objects = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [6 0 R] /Count 1 >>"),
        (3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
        (4, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"),
        (5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>"),
        (6, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Contents 7 0 R /Resources {resources} >>"),
    ]

    # Objekt-Bytes in Nummern-Reihenfolge aufbauen (1..N)
    parts = []  # list[bytes], Index entspricht (obj_num - 1)

    for num, content in text_objects:
        parts.append(f"{num} 0 obj\n{content}\nendobj\n".encode('latin-1', errors='replace'))

    # Objekt 7: Content-Stream (binär, komprimiert)
    stream_header = f"7 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
    parts.append(stream_header.encode('latin-1') + compressed + b"\nendstream\nendobj\n")

    # Objekt 8: Bild-XObject (optional, binär)
    if has_image:
        img_header = (f"8 0 obj\n<< /Type /XObject /Subtype /Image "
                      f"/Width {image['width']} /Height {image['height']} "
                      f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                      f"/Filter {image['filter']} /Length {len(image['data'])} >>\nstream\n")
        parts.append(img_header.encode('latin-1') + image['data'] + b"\nendstream\nendobj\n")

    # Dokument zusammensetzen + xref-Offsets berechnen
    pdf_header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n".encode('latin-1')
    offsets = []
    offset = len(pdf_header)
    for part in parts:
        offsets.append(offset)
        offset += len(part)

    body = b"".join(parts)
    xref_offset = len(pdf_header) + len(body)

    num_objects = len(parts)
    xref = f"xref\n0 {num_objects + 1}\n"
    xref += "0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"

    trailer = (f"trailer\n<< /Size {num_objects + 1} /Root 1 0 R >>\n"
               f"startxref\n{xref_offset}\n%%EOF")

    return pdf_header + body + xref.encode('latin-1') + trailer.encode('latin-1')


def build_multi_page_pdf(pages, image=None, page_size=(595, 842)) -> bytes:
    """Mehrseitiges PDF aus einer Liste von Content-Operator-Listen bauen.

    Args:
        pages: Liste von Content-Streams (je eine Operator-Liste pro Seite, inkl.
               ``BT``/``ET``). Schriften ``/F1``–``/F3`` und – falls vorhanden –
               das Bild ``/Logo`` stehen auf jeder Seite zur Verfügung (das Logo
               erscheint nur dort, wo die Seite den ``Do``-Operator enthält).
        image: optionales dict aus :func:`load_image_xobject` (oder None).
        page_size: (Breite, Höhe) – Default A4 hochkant.

    Objekt-Layout (lückenlos nummeriert): 1 Catalog · 2 Pages · 3–5 Fonts ·
    je Seite (6+2i) Page + (7+2i) Content · zuletzt das Bild (falls vorhanden).
    """
    width, height = page_size
    n = max(1, len(pages))
    has_image = image is not None

    page_obj_nums    = [6 + 2 * i for i in range(n)]
    content_obj_nums = [7 + 2 * i for i in range(n)]
    image_obj_num    = (6 + 2 * n) if has_image else None

    xobject_ref = f" /XObject << /Logo {image_obj_num} 0 R >>" if has_image else ""
    resources   = f"<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >>{xobject_ref} >>"
    kids        = " ".join(f"{p} 0 R" for p in page_obj_nums)

    obj_bytes = {}

    def _put(num, content):
        obj_bytes[num] = f"{num} 0 obj\n{content}\nendobj\n".encode('latin-1', errors='replace')

    _put(1, "<< /Type /Catalog /Pages 2 0 R >>")
    _put(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    _put(3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    _put(4, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    _put(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>")

    for i in range(n):
        pnum, cnum = page_obj_nums[i], content_obj_nums[i]
        _put(pnum, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                   f"/Contents {cnum} 0 R /Resources {resources} >>")
        compressed = zlib.compress(to_winansi_bytes("\n".join(pages[i])))
        header = f"{cnum} 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
        obj_bytes[cnum] = header.encode('latin-1') + compressed + b"\nendstream\nendobj\n"

    if has_image:
        img_header = (f"{image_obj_num} 0 obj\n<< /Type /XObject /Subtype /Image "
                      f"/Width {image['width']} /Height {image['height']} "
                      f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                      f"/Filter {image['filter']} /Length {len(image['data'])} >>\nstream\n")
        obj_bytes[image_obj_num] = img_header.encode('latin-1') + image['data'] + b"\nendstream\nendobj\n"

    max_num = max(obj_bytes)
    pdf_header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n".encode('latin-1')
    parts = [obj_bytes[k] for k in range(1, max_num + 1)]

    offsets = []
    offset = len(pdf_header)
    for part in parts:
        offsets.append(offset)
        offset += len(part)
    body = b"".join(parts)
    xref_offset = len(pdf_header) + len(body)

    xref = f"xref\n0 {max_num + 1}\n" + "0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"
    trailer = (f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\n"
               f"startxref\n{xref_offset}\n%%EOF")

    return pdf_header + body + xref.encode('latin-1') + trailer.encode('latin-1')
