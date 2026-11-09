# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PDF/A-3b-Aufbau für alle erzeugten PDFs (optional mit Factur-X-Anhang).

Wird von :mod:`export.pdf_core` genutzt. Aufgaben:

* **Schriften** – Liberation Sans (Regular/Bold/Italic, metrisch kompatibel zu
  Helvetica) wird als TrueType-Untermenge eingebettet. Die Content-Streams
  bleiben unverändert (WinAnsi-Bytes), das Layout damit ebenfalls.
* **Metadaten** – Info-Dictionary und XMP (``pdfaid`` Part 3 / Conformance B),
  bei E-Rechnungen zusätzlich das Factur-X-Erweiterungsschema.
* **OutputIntent** mit sRGB-ICC-Profil, Trailer-``/ID``, PDF 1.7.
* **Anhang** ``factur-x.xml`` als EmbeddedFile (Subtype ``text/xml``,
  ``/AFRelationship /Alternative`` – für das Profil EN 16931 vorgesehen; nur
  MINIMUM/BASIC WL verwenden ``/Data``), ``/AF`` im Katalog.

Alle Texte, die in XML (XMP) oder PDF-Strings landen, werden hier maskiert.
"""
import hashlib
import io
import os
import re
import zlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _xml_escape

_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_FILES = {
    '/F1': ('LiberationSans-Regular.ttf', 'LiberationSans', 80, False),
    '/F2': ('LiberationSans-Bold.ttf', 'LiberationSans-Bold', 140, False),
    '/F3': ('LiberationSans-Italic.ttf', 'LiberationSans-Italic', 80, True),
}
ICC_FILE = os.path.join(_DIR, 'icc', 'sRGB-v2.icc')

FACTURX_FILENAME = 'factur-x.xml'
FACTURX_NS = 'urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#'
FACTURX_CONFORMANCE = 'EN 16931'
FACTURX_VERSION = '1.0'

_XML_ILLEGAL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')


# ── Helfer: Maskierung ───────────────────────────────────────────────────────

def xml_text(value) -> str:
    """Text für XML-Elementinhalt/-Attribute: Steuerzeichen raus, Sonderzeichen maskiert."""
    return _xml_escape(_XML_ILLEGAL.sub('', str(value or '')), {'"': '&quot;', "'": '&apos;'})


def pdf_text_string(value) -> bytes:
    """PDF-Text-String (``(…)``) in PDFDocEncoding/UTF-16BE inkl. Maskierung."""
    s = _XML_ILLEGAL.sub('', str(value or ''))
    try:
        raw = s.encode('ascii')
    except UnicodeEncodeError:
        raw = b'\xfe\xff' + s.encode('utf-16-be')
    raw = raw.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')
    raw = raw.replace(b'\r', b'\\r')
    return b'(' + raw + b')'


def pdf_date(dt: datetime) -> str:
    return dt.strftime("D:%Y%m%d%H%M%S+00'00'")


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')


# ── Schriften ────────────────────────────────────────────────────────────────

def _subset_font(path, chars):
    """TrueType-Untermenge für ``chars`` (Unicode-Zeichen) → (Bytes, Font-Metriken)."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    full = TTFont(path)
    upem = full['head'].unitsPerEm
    cmap = full.getBestCmap()
    hmtx = full['hmtx']
    # Breiten aller cp1252-Codes 32–255 (aus der vollständigen Schrift)
    widths = []
    for code in range(32, 256):
        try:
            ch = bytes([code]).decode('cp1252')
        except UnicodeDecodeError:
            widths.append(0)
            continue
        gname = cmap.get(ord(ch))
        widths.append(round(hmtx[gname][0] * 1000 / upem) if gname else 0)

    opts = subset.Options()
    opts.layout_features = []
    opts.notdef_outline = True
    opts.glyph_names = False
    opts.hinting = True
    opts.legacy_kern = False
    opts.recalc_timestamp = False
    opts.name_IDs = [1, 2, 3, 4, 6]
    opts.drop_tables += ['DSIG', 'GSUB', 'GPOS', 'GDEF', 'kern', 'LTSH', 'VDMX',
                         'hdmx', 'gasp', 'FFTM', 'meta', 'STAT', 'BASE', 'JSTF', 'MATH']
    font = subset.load_font(path, opts)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=[ord(c) for c in chars])
    sub.subset(font)
    out = io.BytesIO()
    subset.save_font(font, out, opts)

    head, hhea, post, os2 = full['head'], full['hhea'], full['post'], full['OS/2']

    def scale(v):
        return round(v * 1000 / upem)
    metrics = {
        'widths': widths,
        'bbox': [scale(head.xMin), scale(head.yMin), scale(head.xMax), scale(head.yMax)],
        'ascent': scale(hhea.ascent), 'descent': scale(hhea.descent),
        'cap_height': scale(getattr(os2, 'sCapHeight', 0) or 0.7 * upem),
        'italic_angle': post.italicAngle,
    }
    return out.getvalue(), metrics


def _font_tag(name, chars) -> str:
    digest = hashlib.md5((name + ''.join(sorted(chars))).encode('utf-8')).digest()
    return ''.join(chr(ord('A') + b % 26) for b in digest[:6])


def _decode_chars(content_streams) -> set:
    """Alle in den Content-Streams vorkommenden Zeichen (cp1252-dekodiert)."""
    used = {' '}
    for data in content_streams:
        used.update(data.decode('cp1252', errors='ignore'))
    return {c for c in used if ord(c) >= 32}


# ── XMP ──────────────────────────────────────────────────────────────────────

def build_xmp(title, author, producer, created, modified, facturx=False) -> bytes:
    e = xml_text
    fx_schema = ''
    fx_values = ''
    if facturx:
        def prop(name, desc):
            return ('<rdf:li rdf:parseType="Resource">'
                    f'<pdfaProperty:name>{name}</pdfaProperty:name>'
                    '<pdfaProperty:valueType>Text</pdfaProperty:valueType>'
                    '<pdfaProperty:category>external</pdfaProperty:category>'
                    f'<pdfaProperty:description>{desc}</pdfaProperty:description></rdf:li>')
        fx_schema = (
            '<rdf:Description rdf:about="" '
            'xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/" '
            'xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" '
            'xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">'
            '<pdfaExtension:schemas><rdf:Bag><rdf:li rdf:parseType="Resource">'
            '<pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>'
            f'<pdfaSchema:namespaceURI>{FACTURX_NS}</pdfaSchema:namespaceURI>'
            '<pdfaSchema:prefix>fx</pdfaSchema:prefix>'
            '<pdfaSchema:property><rdf:Seq>'
            + prop('DocumentFileName', 'name of the embedded XML invoice file')
            + prop('DocumentType', 'INVOICE')
            + prop('Version', 'The actual version of the Factur-X XML schema')
            + prop('ConformanceLevel', 'The conformance level of the embedded Factur-X data')
            + '</rdf:Seq></pdfaSchema:property></rdf:li></rdf:Bag></pdfaExtension:schemas>'
            '</rdf:Description>')
        fx_values = (
            f'<rdf:Description rdf:about="" xmlns:fx="{FACTURX_NS}">'
            '<fx:DocumentType>INVOICE</fx:DocumentType>'
            f'<fx:DocumentFileName>{FACTURX_FILENAME}</fx:DocumentFileName>'
            f'<fx:Version>{FACTURX_VERSION}</fx:Version>'
            f'<fx:ConformanceLevel>{FACTURX_CONFORMANCE}</fx:ConformanceLevel>'
            '</rdf:Description>')
    xmp = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">'
        '<pdfaid:part>3</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>'
        '</rdf:Description>'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{e(title)}</rdf:li></rdf:Alt></dc:title>'
        f'<dc:creator><rdf:Seq><rdf:li>{e(author)}</rdf:li></rdf:Seq></dc:creator>'
        '<dc:format>application/pdf</dc:format>'
        '</rdf:Description>'
        '<rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">'
        f'<pdf:Producer>{e(producer)}</pdf:Producer></rdf:Description>'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f'<xmp:CreatorTool>{e(producer)}</xmp:CreatorTool>'
        f'<xmp:CreateDate>{_iso(created)}</xmp:CreateDate>'
        f'<xmp:ModifyDate>{_iso(modified)}</xmp:ModifyDate>'
        f'<xmp:MetadataDate>{_iso(modified)}</xmp:MetadataDate>'
        '</rdf:Description>'
        + fx_schema + fx_values +
        '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
    return xmp.encode('utf-8')


# ── Dokument-Aufbau ──────────────────────────────────────────────────────────

class _Objects:
    """Nummerierte PDF-Objekte; Reihenfolge in der Datei = Nummernfolge."""

    def __init__(self):
        self.bodies = {}
        self.next = 1

    def reserve(self, count=1):
        first = self.next
        self.next += count
        return first if count == 1 else list(range(first, first + count))

    def put(self, num, body: bytes):
        self.bodies[num] = body

    def put_stream(self, num, dict_entries: str, data: bytes):
        self.bodies[num] = (f'<< {dict_entries} /Length {len(data)} >>\nstream\n'.encode('latin-1')
                            + data + b'\nendstream')


def assemble_pdf(page_streams, image=None, page_size=(595, 842), title='Dokument',
                 author='', subject='', producer='uBuchhaltung', facturx_xml=None,
                 created=None) -> bytes:
    """PDF/A-3b-Dokument aus Seiteninhalten (WinAnsi-Bytes) zusammensetzen.

    Args:
        page_streams: Liste unkomprimierter Content-Streams (``bytes``, cp1252).
        image: optionales Logo-dict (siehe ``pdf_core.load_image_xobject``).
        facturx_xml: CII-XML (``bytes``) für den Anhang ``factur-x.xml`` oder ``None``.
        created: Zeitstempel (UTC); Standard: jetzt.
    """
    width, height = page_size
    created = created or datetime.now(timezone.utc).replace(microsecond=0)
    n = max(1, len(page_streams))
    page_streams = list(page_streams) or [b'']
    objs = _Objects()

    catalog = objs.reserve()
    pages_root = objs.reserve()
    page_nums = [objs.reserve() for _ in range(n)]
    content_nums = [objs.reserve() for _ in range(n)]

    # Schriften: nur die tatsächlich benutzten (/F1–/F3) einbetten
    joined = b'\n'.join(page_streams)
    used_fonts = [f for f in ('/F1', '/F2', '/F3')
                  if re.search(re.escape(f.encode()) + rb'\s', joined)] or ['/F1']
    chars = _decode_chars(page_streams)
    font_nums = {}
    font_objs = {}
    for f in used_fonts:
        font_nums[f] = objs.reserve()
        font_objs[f] = (objs.reserve(), objs.reserve())      # Descriptor, FontFile2

    image_num = objs.reserve() if image is not None else None
    xmp_num, icc_num, intent_num, info_num = (objs.reserve() for _ in range(4))
    fx_nums = (objs.reserve(), objs.reserve()) if facturx_xml is not None else None

    # Seiten + Inhalte
    font_res = ' '.join(f'{f} {font_nums[f]} 0 R' for f in used_fonts)
    xobj = f' /XObject << /Logo {image_num} 0 R >>' if image_num else ''
    resources = f'<< /Font << {font_res} >>{xobj} >>'
    objs.put(pages_root, f'<< /Type /Pages /Kids [{" ".join(f"{p} 0 R" for p in page_nums)}] '
                         f'/Count {n} >>'.encode('latin-1'))
    for i in range(n):
        objs.put(page_nums[i], (f'<< /Type /Page /Parent {pages_root} 0 R '
                                f'/MediaBox [0 0 {width} {height}] /Contents {content_nums[i]} 0 R '
                                f'/Resources {resources} >>').encode('latin-1'))
        objs.put_stream(content_nums[i], '/Filter /FlateDecode', zlib.compress(page_streams[i]))

    # Schriftobjekte
    for f in used_fonts:
        file_name, base, stem_v, italic = FONT_FILES[f]
        data, m = _subset_font(os.path.join(_DIR, 'fonts', file_name), chars)
        tag = _font_tag(base, chars)
        psname = f'{tag}+{base}'
        desc_num, file_num = font_objs[f]
        flags = 32 | (64 if italic else 0)
        widths = ' '.join(str(w) for w in m['widths'])
        objs.put(font_nums[f], (
            f'<< /Type /Font /Subtype /TrueType /BaseFont /{psname} /FirstChar 32 /LastChar 255 '
            f'/Widths [{widths}] /FontDescriptor {desc_num} 0 R /Encoding /WinAnsiEncoding >>'
        ).encode('latin-1'))
        objs.put(desc_num, (
            f'<< /Type /FontDescriptor /FontName /{psname} /Flags {flags} '
            f'/FontBBox [{" ".join(str(v) for v in m["bbox"])}] /ItalicAngle {m["italic_angle"]:g} '
            f'/Ascent {m["ascent"]} /Descent {m["descent"]} /CapHeight {m["cap_height"]} '
            f'/StemV {stem_v} /FontFile2 {file_num} 0 R >>').encode('latin-1'))
        objs.put_stream(file_num, f'/Length1 {len(data)} /Filter /FlateDecode', zlib.compress(data))

    if image_num:
        objs.put_stream(
            image_num,
            f'/Type /XObject /Subtype /Image /Width {image["width"]} /Height {image["height"]} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {image["filter"]}', image['data'])

    # Metadaten
    xmp = build_xmp(title, author, producer, created, created, facturx=facturx_xml is not None)
    objs.put_stream(xmp_num, '/Type /Metadata /Subtype /XML', xmp)
    with open(ICC_FILE, 'rb') as fh:
        icc = fh.read()
    objs.put_stream(icc_num, '/N 3 /Filter /FlateDecode', zlib.compress(icc))
    objs.put(intent_num, (
        f'<< /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (sRGB) '
        f'/RegistryName (http://www.color.org) /Info (sRGB IEC 61966-2-1) '
        f'/DestOutputProfile {icc_num} 0 R >>').encode('latin-1'))
    objs.put(info_num, b'<< /Title ' + pdf_text_string(title)
             + b' /Author ' + pdf_text_string(author)
             + b' /Subject ' + pdf_text_string(subject)
             + b' /Creator ' + pdf_text_string(producer)
             + b' /Producer ' + pdf_text_string(producer)
             + b' /CreationDate ' + pdf_text_string(pdf_date(created))
             + b' /ModDate ' + pdf_text_string(pdf_date(created)) + b' >>')

    # Anhang factur-x.xml
    af_entries = ''
    if fx_nums:
        file_num, spec_num = fx_nums
        objs.put_stream(
            file_num,
            '/Type /EmbeddedFile /Subtype /text#2Fxml /Filter /FlateDecode '
            f'/Params << /Size {len(facturx_xml)} /ModDate '
            + pdf_text_string(pdf_date(created)).decode('latin-1') + ' >>',
            zlib.compress(facturx_xml))
        objs.put(spec_num, (
            f'<< /Type /Filespec /F ({FACTURX_FILENAME}) /UF ({FACTURX_FILENAME}) '
            f'/Desc (Factur-X Invoice) /AFRelationship /Alternative '
            f'/EF << /F {file_num} 0 R /UF {file_num} 0 R >> >>').encode('latin-1'))
        af_entries = (f' /AF [{spec_num} 0 R] /Names << /EmbeddedFiles << /Names '
                      f'[({FACTURX_FILENAME}) {spec_num} 0 R] >> >>')

    objs.put(catalog, (
        f'<< /Type /Catalog /Pages {pages_root} 0 R /Metadata {xmp_num} 0 R '
        f'/OutputIntents [{intent_num} 0 R] /Lang (de-DE) '
        f'/MarkInfo << /Marked false >>{af_entries} >>').encode('latin-1'))

    # Schreiben
    header = b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n'
    body = bytearray(header)
    offsets = {}
    for num in range(1, objs.next):
        offsets[num] = len(body)
        body += f'{num} 0 obj\n'.encode('latin-1') + objs.bodies[num] + b'\nendobj\n'
    xref_pos = len(body)
    body += f'xref\n0 {objs.next}\n'.encode('latin-1') + b'0000000000 65535 f \n'
    for num in range(1, objs.next):
        body += f'{offsets[num]:010d} 00000 n \n'.encode('latin-1')
    file_id = hashlib.md5(bytes(body)).hexdigest()
    body += (f'trailer\n<< /Size {objs.next} /Root {catalog} 0 R /Info {info_num} 0 R '
             f'/ID [<{file_id}> <{file_id}>] >>\nstartxref\n{xref_pos}\n%%EOF\n').encode('latin-1')
    return bytes(body)
