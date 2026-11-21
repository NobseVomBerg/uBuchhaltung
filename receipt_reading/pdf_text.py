# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Den Text einer gewöhnlichen PDF lesen – nur, was die Textregeln brauchen, und nie mehr, als erlaubt ist.

Gelesen werden die ersten ``HEAD_PAGES`` und die letzten ``TAIL_PAGES`` Seiten (vorn der Briefkopf, hinten die Summen; eine
Seite nie doppelt). Nach jeder Seite gelten die Zeit- und die Zeichengrenze: Ist eine überschritten, endet das Lesen mit dem
Hinweis ``text_truncated`` – die bis dahin gelesenen Seiten bleiben.

Das Ergebnis ist ``(Seitentexte, Hinweis)``. Der Hinweis ist ``None`` (alles gelesen), ``text_truncated`` (gekürzt, die Seiten
gelten) oder der Grund, warum es keinen Text gibt – dann ist die Liste leer: ``no_text`` (weniger als ``MIN_VISIBLE_CHARS``
sichtbare Zeichen: ein Scan), ``encrypted`` (es braucht ein Passwort) und ``pdf_invalid`` (jede andere Ausnahme beim Lesen).
Ein Aufrufer macht daraus Status und Befund (:func:`receipt_reading._read_pdf_text`).
"""
import io
import time
import unicodedata
from typing import Optional

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError

HEAD_PAGES = 5
TAIL_PAGES = 3
MIN_VISIBLE_CHARS = 20

_INVISIBLE = frozenset({'Cc', 'Cf', 'Cs'})


class PageTexts(list):
    """Die Seitentexte in Reihenfolge – eine Liste von Texten. ``numbers`` nennt je Text die Seite der Datei (1-basiert): Bei
    mehr als ``HEAD_PAGES + TAIL_PAGES`` Seiten fehlen die mittleren, und das Zitat eines Feldes soll die richtige Seite nennen."""

    def __init__(self, texts=(), numbers=()):
        super().__init__(texts)
        self.numbers = list(numbers)


def wanted_pages(count: int) -> list:
    """Die Stellen (0-basiert) der Seiten, die bei ``count`` Seiten gelesen werden: der Anfang, dann das Ende, ohne Doppelte."""
    head = range(min(HEAD_PAGES, count))
    tail = range(max(count - TAIL_PAGES, HEAD_PAGES), count)
    return [*head, *tail]


def _visible(texts) -> int:
    """Wie viele sichtbare Zeichen die Texte haben, gezählt nur bis ``MIN_VISIBLE_CHARS``."""
    count = 0
    for text in texts:
        for char in text:
            if not char.isspace() and unicodedata.category(char) not in _INVISIBLE:
                count += 1
                if count >= MIN_VISIBLE_CHARS:
                    return count
    return count


def _is_encryption(error: BaseException) -> bool:
    """Braucht die PDF ein Passwort? pdfplumber verpackt die Ausnahmen von pdfminer in eine eigene (``PdfminerException``)."""
    return isinstance(error, PDFEncryptionError) or any(isinstance(arg, PDFEncryptionError) for arg in error.args)


def extract_pdf_text(data: bytes, max_chars: int, deadline: Optional[float]) -> tuple[PageTexts, Optional[str]]:
    """Die Seitentexte einer PDF (siehe Kopf des Moduls). ``max_chars``: so viele Zeichen höchstens; ``deadline``: Zeitpunkt
    (``time.monotonic()``), nach dem keine weitere Seite begonnen wird, oder ``None``. Wirft nie."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages
            wanted = wanted_pages(len(pages))
            texts, numbers, total, truncated = [], [], 0, False
            for position, index in enumerate(wanted):
                text = pages[index].extract_text() or ''
                texts.append(text)
                numbers.append(index + 1)
                total += len(text)
                if position + 1 < len(wanted) and (total >= max_chars or (deadline is not None and time.monotonic() > deadline)):
                    truncated = True
                    break
            if total > max_chars:                       # eine einzige Seite kann die Grenze allein überschreiten
                texts[-1] = texts[-1][:max(0, len(texts[-1]) - (total - max_chars))]
                truncated = True
    except Exception as error:
        return PageTexts(), 'encrypted' if _is_encryption(error) else 'pdf_invalid'
    if _visible(texts) < MIN_VISIBLE_CHARS:
        return PageTexts(), 'no_text'
    return PageTexts(texts, numbers), 'text_truncated' if truncated else None
