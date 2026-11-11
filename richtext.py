# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Bereinigung des Fließtexts im Angebot (Editor-HTML) per Positivliste.

Das HTML kommt aus einem ``contenteditable``-Feld – auch mit Text, der aus einer Webseite
eingefügt wurde – und liegt als Altbestand in der Datenbank. Deshalb läuft ``sanitize_richtext``
beim Speichern UND bei jeder Ausgabe; roh kommt das Feld nie in eine Seite.

Die Ausgabe wird aus den Tokens von ``html.parser`` neu aufgebaut: nur Elemente der Positivliste,
ohne jedes Attribut, Text maskiert. Liest der Parser etwas anders als ein Browser, ändert das nichts
an der Sicherheit – heraus kommt ausschließlich Markup, das hier erzeugt wurde.
"""
import html
from collections import Counter
from html.parser import HTMLParser

# Was der Editor erzeugt (fett, kursiv, unterstrichen, Absätze, Listen) und was der PDF-Satz
# (export/pdf_document._richtext_paragraphs) versteht: b/strong, i/em, p, div, br, ul, ol, li
ALLOWED_TAGS = frozenset({'b', 'strong', 'i', 'em', 'u', 'p', 'br', 'ul', 'ol', 'li', 'div', 'span'})
_VOID_TAGS = frozenset({'br'})
# Diese Elemente fallen samt Inhalt weg; alle übrigen nicht erlaubten verlieren nur ihr Tag
# (<embed> ist ein leeres Element ohne Ende-Tag: nur das Tag fällt weg, der Text danach bleibt)
_DROP_WITH_CONTENT = frozenset({'script', 'style', 'iframe', 'object', 'svg', 'math'})
# Tiefer verschachtelt als das schreibt kein Editor; weitere Start-Tags fallen weg, ihr Text bleibt
MAX_DEPTH = 50


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []              # geöffnete erlaubte Elemente, zum Schließen
        self._open_count = Counter()  # dieselben je Tag gezählt – End-Tags ohne Suche in der Liste
        self._skip = None           # Element, dessen Inhalt gerade wegfällt
        self._skip_depth = 0        # Verschachtelung gleichnamiger Elemente darin

    def handle_starttag(self, tag, attrs):
        if self._skip:
            if tag == self._skip:
                self._skip_depth += 1
        elif tag in _DROP_WITH_CONTENT:
            self._skip, self._skip_depth = tag, 1
        elif tag in ALLOWED_TAGS:           # attrs fallen weg: kein style, href, on…
            if tag in _VOID_TAGS:
                self.out.append(f'<{tag}>')
            elif len(self.open) < MAX_DEPTH:
                self.out.append(f'<{tag}>')
                self.open.append(tag)
                self._open_count[tag] += 1

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip = None
        elif self._open_count[tag]:         # ein überzähliges </p> oder </br> bleibt ohne Wirkung
            while True:
                top = self.open.pop()
                self._open_count[top] -= 1
                self.out.append(f'</{top}>')
                if top == tag:
                    break

    def handle_data(self, data):
        if not self._skip:
            # Anführungszeichen brauchen im Textknoten keine Maskierung; &nbsp; wie im Editor
            self.out.append(html.escape(data, quote=False).replace('\xa0', '&nbsp;'))

    # Kommentare, <!DOCTYPE>, <?…?> und CDATA-Abschnitte übernimmt niemand (die Voreinstellung
    # von HTMLParser ist, sie zu ignorieren).


def sanitize_richtext(html_text):
    """Editor-HTML auf die Positivliste beschränken (``None`` bei leerem Ergebnis).

    Erlaubt sind ``b strong i em u p br ul ol li div span`` ohne Attribute. Skripte, Rahmen und
    eingebettete Objekte (``script style iframe object svg math``) fallen samt Inhalt weg,
    alle anderen Elemente behalten nur ihren Text, offene Elemente werden geschlossen.
    Idempotent: ein zweiter Durchgang ändert nichts mehr."""
    if not html_text:
        return None
    cleaner = _Cleaner()
    cleaner.feed(html_text if isinstance(html_text, str) else str(html_text))
    cleaner.close()
    cleaner.out.extend(f'</{tag}>' for tag in reversed(cleaner.open))
    return ''.join(cleaner.out).strip() or None
