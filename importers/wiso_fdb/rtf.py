# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""RTF auf reinen Text zurückführen.

WISO legt Einleitungs- und Schlusstexte einer Rechnung als RTF ab. Für die
Übernahme zählt nur der Wortlaut – Schriftart und Farbe nicht.

Bewusst kein regulärer Ausdruck: RTF ist geklammert und verschachtelt. Ein
Muster über ``{\\fonttbl{\\f0\\fnil Segoe UI;}}`` lässt genau die Reste stehen,
die man loswerden will (``Segoe UI;;;;;;``). Deshalb wird hier geklammert
gezählt und ganze Gruppen werden verworfen, die keinen Fließtext enthalten.
"""
import re

#: Gruppen, deren Inhalt nie Fließtext ist.
SKIP_GROUPS = {
    'fonttbl', 'colortbl', 'stylesheet', 'info', 'generator', 'pict',
    'themedata', 'colorschememapping', 'latentstyles', 'datastore',
    'listtable', 'listoverridetable', 'rsidtbl', 'xmlnstbl', 'filetbl',
    'header', 'footer', 'footnote', 'nonshppict', 'shppict', 'object',
}

#: Steuerworte, die als Zeichen zählen.
BREAKS = {'par': '\n', 'line': '\n', 'tab': '\t', 'page': '\n\n',
          'sect': '\n\n', 'cell': '\t', 'row': '\n'}
#: Steuerworte, die für ein einzelnes Zeichen stehen.
SYMBOLS = {'ldblquote': '„', 'rdblquote': '“', 'lquote': '‚', 'rquote': '‘',
           'emdash': '—', 'endash': '–', 'bullet': '•', 'nbsp': ' ', '~': ' '}

#: Hinter einem Steuerwort steht ein Leerzeichen als Trenner – es gehört nicht
#: zum Text und wird mitverbraucht. Bei ``\'fc`` und ``\{`` gibt es keinen.
_CONTROL = re.compile(r"\\(?:([a-zA-Z]+)(-?\d+)? ?|'([0-9a-fA-F]{2})|(.))")
_BLANK_LINES = re.compile(r'\n{3,}')
_TRAILING_SPACE = re.compile(r'[ \t]+\n')


def rtf_to_text(source):
    """RTF-Zeichenkette zu lesbarem Text; alles andere bleibt unverändert."""
    if not source:
        return ''
    if not source.lstrip().startswith('{\\rtf'):
        return source.strip()

    out = []
    depth = 0
    skip_until = None          # Klammerebene, bis zu der verworfen wird
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        if char == '{':
            depth += 1
            index += 1
            continue
        if char == '}':
            if skip_until is not None and depth <= skip_until:
                skip_until = None
            depth -= 1
            index += 1
            continue
        if char == '\\':
            match = _CONTROL.match(source, index)
            if not match:
                index += 1
                continue
            index = match.end()
            word, param, hexcode, literal = match.groups()
            if skip_until is not None:
                continue
            if hexcode is not None:
                out.append(bytes([int(hexcode, 16)]).decode('cp1252', 'replace'))
            elif literal is not None:
                # \\ \{ \} sind echte Zeichen, \* leitet eine Sondergruppe ein
                if literal in '\\{}':
                    out.append(literal)
                elif literal == '*':
                    skip_until = depth
                elif literal in SYMBOLS:
                    out.append(SYMBOLS[literal])
            elif word in SKIP_GROUPS:
                skip_until = depth
            elif word in BREAKS:
                out.append(BREAKS[word])
            elif word in SYMBOLS:
                out.append(SYMBOLS[word])
            elif word == 'u' and param is not None:
                out.append(chr(int(param) % 65536))
                if index < length and source[index] == '?':
                    index += 1          # Ersatzzeichen für alte Leser
            continue
        if skip_until is None:
            out.append(char)
        index += 1

    text = ''.join(out).replace('\r\n', '\n').replace('\r', '\n')
    text = _TRAILING_SPACE.sub('\n', text)
    return _BLANK_LINES.sub('\n\n', text).strip()
