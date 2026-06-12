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

    # Content-Stream komprimieren (FlateDecode)
    content_bytes = "\n".join(content_ops).encode('latin-1', errors='replace')
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
