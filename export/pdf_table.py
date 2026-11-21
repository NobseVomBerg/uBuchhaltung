# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Gemeinsame Bausteine der Tabellen-PDFs (``pdf_bwa``, ``pdf_euer``): Text bereinigen und messen, auf eine Breite kürzen oder umbrechen,
eine Textzelle an absoluter Position, eine Linie und die Verteilung von Blöcken auf Seiten. Reine Funktionen ohne Zustand; die
Seitengeometrie (Ränder, Zeilen je Seite) gibt der Aufrufer mit.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``info_width``, fetter Text mit
denen der Helvetica-Bold-Tabelle) – nur so liegen Beträge rechtsbündig auf einer Kante. Die Bausteine sind aus ``pdf_bwa`` herausgezogen;
dessen Ausgabe ist dieselbe geblieben (``tests/test_pdf_table.py`` hält sie fest).
"""
from .pdf_core import escape_pdf_string
from .pdf_document import text_width
from .pdf_worktime import info_lines, info_width, printed_text


def clean(text) -> str:
    """Ein Text, wie er im PDF steht (``printed_text``), ohne Zeilenumbrüche, Tabulatoren und andere Steuerzeichen – sie ergäben
    mitten im Text eine neue Zeile des Content-Streams."""
    printed = printed_text(text)
    return ' '.join(''.join(' ' if ord(ch) < 32 or ord(ch) == 127 else ch for ch in printed).split())


def width(text: str, size: float, font: str) -> float:
    """Breite eines gedruckten Textes in pt: fett (``/F2``) mit der Helvetica-Bold-Tabelle, sonst mit der eingebetteten Schrift."""
    return text_width(text, size, '/F2') if font == '/F2' else info_width(text, size)


def fit(text, max_width: float, size: float, max_chars: int = 200) -> str:
    """Der Text auf ``max_width`` pt gekürzt (mit „…“), ohne Steuerzeichen. Die Laufzeit hängt nicht von der Länge des Namens ab:
    betrachtet werden höchstens ``max_chars`` Zeichen."""
    raw = str(text if text is not None else '')
    cleaned = clean(raw[:max_chars])
    if len(raw) <= max_chars and info_width(cleaned, size) <= max_width:
        return cleaned
    reserve = info_width('…', size)
    kept, used = 0, 0.0
    for index, char in enumerate(cleaned):
        used += info_width(char, size)
        if used + reserve > max_width:
            break
        kept = index + 1
    return cleaned[:kept].rstrip() + '…'


def wrap(text, max_width: float, size: float, max_lines: int = 200) -> list[str]:
    """Der Text in Zeilen von höchstens ``max_width`` pt: gierig am Wortrand, ein Wort, das allein nicht passt, mitten im Wort
    geteilt; Leerraum und Steuerzeichen zählen als ein Leerzeichen. Nach ``max_lines`` Zeilen ist Schluss, die letzte endet dann mit „…“ –
    nichts fällt weg, ohne dass man es sieht. Ein leerer Text hat keine Zeile. Gemessen wird der Text, wie er gedruckt wird."""
    return info_lines(clean(text), max_width, size, max_lines)


def cell(x: float, y: float, text, font: str, size: float, right: bool = False, gray: bool = False) -> list[str]:
    """Eine Textzelle an absoluter Position; ``right``: ``x`` ist die rechte Kante des Textes, ``gray``: grauer Text."""
    printed = clean(text)
    if right:
        x -= width(printed, size, font)
    ops = ['0.5 g'] if gray else []
    ops.append(f'{font} {size:g} Tf')
    ops.append(f'1 0 0 1 {x:.1f} {y:.1f} Tm ({escape_pdf_string(printed)}) Tj')
    if gray:
        ops.append('0 g')
    return ops


def hline(y: float, left: float, right: float) -> str:
    """Eine dünne Linie von ``left`` bis ``right`` – wird nach dem Textobjekt gezeichnet."""
    return f'q 0.5 w {left:.1f} {y:.1f} m {right:.1f} {y:.1f} l S Q'


def page_layout(block_heights, lines_per_page: int) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen: je Seite die Zeilenzahlen der Blockstücke, die auf ihr stehen
    (mindestens eine Seite). Ein Block wird nicht geteilt, solange er auf eine Seite passt; passt er auf die Restseite nicht
    mehr, beginnt er auf der nächsten. Ein Block, der länger ist als eine Seite, beginnt auf einer neuen Seite und läuft dann
    über so viele, wie er braucht. Keine Seite hat mehr als ``lines_per_page`` Zeilen."""
    pages: list[list[int]] = [[]]
    used = 0
    for height in block_heights:
        height = max(1, int(height))
        if height <= lines_per_page:
            if used + height > lines_per_page:
                pages.append([])
                used = 0
            pages[-1].append(height)
            used += height
            continue
        if used:
            pages.append([])
        while height > lines_per_page:
            pages[-1].append(lines_per_page)
            pages.append([])
            height -= lines_per_page
        pages[-1].append(height)
        used = height
    return pages
