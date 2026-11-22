# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Gemeinsame Bausteine der CSV-Downloads der Auswertungen (``csv_bwa``, ``csv_susa``): reine Funktionen, nur Standardbibliothek, ohne Datenbank und
ohne Datei – die CSV entsteht im Speicher und geht als Antwort hinaus.

Das Format ist das, was eine Tabellenkalkulation mit deutscher Einstellung direkt öffnet: UTF-8 mit Byte-Order-Mark (ohne sie liest sie Umlaute
falsch), Semikolon als Trenner (das Komma ist das Dezimalzeichen), Zeilenende CRLF (``csv_bytes``). Beträge und Prozentwerte sind der Anzeige-Text,
den die Seite schon gerundet hat, nur ohne Tausenderpunkte und ohne Prozentzeichen (``amount_cell``, ``percent_cell``): Es gibt keine zweite Stelle,
die rundet, und eine Zelle ist eine Zahl, mit der sich rechnen lässt. Zahlen, die die App selbst erzeugt, dürfen mit ``-`` beginnen (ein negativer
Betrag) – der Schutz vor Formeln gilt nur für Text.

Der Schutz vor Formeln ist der Grund für ``text_cell``: Eine Tabellenkalkulation führt eine Zelle, die mit ``=``, ``+``, ``-`` oder ``@`` beginnt, als
Formel aus. Ein Kontoname steht in der Datenbank, wie ihn jemand eingegeben oder aus einem Import übernommen hat – ein Name wie
``=HYPERLINK(…)`` würde beim Öffnen der Datei ausgeführt. Jeder Text aus der Datenbank geht deshalb durch ``text_cell`` und nur dadurch: Steuerzeichen
und Zeilenumbrüche weg, höchstens ``TEXT_LIMIT`` Zeichen, ein ``'`` vor einem gefährlichen Anfang – auch hinter jedem Komma (für den Fall, dass
jemand die Datei mit dem Komma als Trenner öffnet) und auch für die breiten Schreibweisen der vier Zeichen. Nicht aufweichen.
"""
import codecs
import csv
import io
import re
import unicodedata

TEXT_LIMIT = 200                       # so viele Zeichen hat ein Text höchstens; ein längerer endet mit „…“
BOM = codecs.BOM_UTF8                  # Byte-Order-Mark: So erkennt eine Tabellenkalkulation die Datei als UTF-8
FORMULA_STARTS = ('=', '+', '-', '@')  # So beginnt eine Zelle, die eine Tabellenkalkulation als Formel liest
_BLANKED = frozenset({'Cc', 'Cf'})     # Steuerzeichen, Zeilenumbrüche, Tabulator und unsichtbare Formatzeichen (auch U+202E, das die Schreibrichtung kehrt)
_AMOUNT = re.compile(r'-?[0-9]+,[0-9]{2}')
_PERCENT = re.compile(r'-?[0-9]+,[0-9]+')


def csv_bytes(rows) -> bytes:
    """Zeilen (Listen aus ``str``) als CSV-Bytes: UTF-8 mit Byte-Order-Mark (einmal, vorn), ``;`` als Trenner, ``\\r\\n`` nach jeder Zeile – auch
    der letzten. Eine Zelle steht nur dann in Anführungszeichen, wenn sie ``;``, ``"`` oder einen Zeilenumbruch enthält (``csv.QUOTE_MINIMAL``)."""
    buffer = io.StringIO(newline='')
    csv.writer(buffer, delimiter=';', quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n').writerows(rows)
    return BOM + buffer.getvalue().encode('utf-8')


def text_cell(value, limit: int = TEXT_LIMIT) -> str:
    """Ein Text aus der Datenbank (oder ein fester Text der App) als Zelle – ``None`` ist leer.

    Reihenfolge: Zeichen der Unicode-Kategorien ``Cc`` und ``Cf`` (Steuerzeichen, Zeilenumbrüche, Tabulator, unsichtbare Formatzeichen) werden zu einem
    Leerzeichen, mehrfacher Leerraum zu einem, der Rand fällt weg; ein Text über ``limit`` Zeichen wird auf die ersten ``limit - 1`` mit ``…`` gekürzt;
    **dann erst** der Schutz vor Formeln – beginnt der Text jetzt mit ``=``, ``+``, ``-`` oder ``@``, steht ein ``'`` davor. Erst nach dem Bereinigen,
    weil ein Tabulator, ein Zeilenumbruch oder ein unsichtbares Zeichen vor dem ``=`` den gefährlichen Anfang sonst vor der Prüfung verbärge.

    Der Schutz gilt auch **hinter jedem Komma** des Textes: Wer die Datei mit dem Komma als Trenner öffnet (eine Tabellenkalkulation mit englischer
    Einstellung), bekommt das Stück hinter einem Komma als eigene Zelle – ``x,=1+1`` wird deshalb zu ``x,'=1+1``. Die Zelle kann dadurch um die
    eingefügten Zeichen länger sein als ``limit``."""
    text = '' if value is None else str(value)
    blanked = ''.join(' ' if unicodedata.category(char) in _BLANKED else char for char in text)
    cleaned = ' '.join(blanked.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit - 1] + '…'
    return ','.join(_guarded(part) for part in cleaned.split(','))


def _guarded(part: str) -> str:
    """Ein Teil eines Textes (der ganze Text oder ein Stück zwischen zwei Kommas): Beginnt er – hinter Leerzeichen – wie eine Formel, steht ein ``'``
    vor seinem ersten Zeichen. Verglichen wird die Grundform des Zeichens (NFKC): Auch ``＝`` (U+FF1D) und die anderen breiten und kleinen
    Schreibweisen von ``=``, ``+``, ``-`` und ``@`` zählen."""
    body = part.lstrip(' ')
    if body and unicodedata.normalize('NFKC', body[0])[:1] in FORMULA_STARTS:
        return part[:len(part) - len(body)] + "'" + body
    return part


def amount_cell(text: str) -> str:
    """Der Anzeige-Text eines Betrags (``1.234,56``, ``-1.234,56``, leer) ohne Tausenderpunkte (``1234,56``); leer bleibt leer. Die Rundung ist die der
    Seite: Hier wird nichts gerechnet. Was danach keine Zahl mit Komma und genau zwei Stellen ist, ist ein Fehler des Aufrufers (``ValueError``)."""
    if text == '':
        return ''
    plain = text.replace('.', '')
    if not _AMOUNT.fullmatch(plain):
        raise ValueError(f'Kein Betrag im Anzeigeformat: {text!r}')
    return plain


def percent_cell(text: str) -> str:
    """Der Anzeige-Text eines Prozentwerts (``db.bwa.format_percent``: ``-12,34`` oder ``1.234,56``, ohne Prozentzeichen) als Zahl: ohne Tausenderpunkte, ohne
    ein Prozentzeichen und ohne Leerraum (auch das geschützte Leerzeichen), falls der Text so eines trägt; leer bleibt leer. Was danach keine Zahl mit
    Komma und Nachkommastellen ist, ist ein Fehler des Aufrufers (``ValueError``)."""
    if not text.strip():
        return ''
    plain = ''.join(text.replace('%', '').replace('.', '').split())
    if not _PERCENT.fullmatch(plain):
        raise ValueError(f'Kein Prozentwert im Anzeigeformat: {text!r}')
    return plain
