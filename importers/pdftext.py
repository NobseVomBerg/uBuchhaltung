# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Gemeinsame Extraktions-Helfer für PDF-Belege.

Text, IBAN und Belegdatum braucht jedes Importmodul – deshalb liegen sie hier
und nicht in einem der bankspezifischen Module. Reine Funktionen ohne Zustand.
"""
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional, Tuple

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

#: Standardmuster für das Belegdatum ("erstellt am 31.01.2024")
DOCUMENT_DATE_PATTERN = r'erstellt am (\d{2}\.\d{2}\.\d{4})'

#: Salden ab dieser Größe gelten als nicht lesbar (verrutschtes Trennzeichen, zusammengeklebte Spalten)
MAX_BALANCE = Decimal('1e12')

# "alter Kontostand [vom TT.MM.JJJJ] 1.234,56 [S|H|-|+|EUR]": ohne Rücksicht auf Groß-/Kleinschreibung und
# tolerant gegen fehlende Leerzeichen (die PDF-Extraktion zieht Wörter zusammen); der Betrag steht in
# derselben Zeile und hat genau zwei Nachkommastellen. ``rest`` ist der Zeilenrest hinter dem Betrag.
# Jede Leerraum-Folge hängt am Wort davor und steht nie neben einer zweiten: Sonst ließe sich eine lange Folge
# (der Text kommt aus hochgeladenen PDFs) beim Zurücklaufen auf viele Weisen aufteilen – die Laufzeit würde quadratisch.
_BALANCE = re.compile(r'''
    (?P<kind>alter|neuer)  [^\S\n]*  kontostand  [^\S\n]*
    (?: : [^\S\n]* )?
    (?: vom [^\S\n]* \d{2}\.\d{2}\.\d{4} [^\S\n]* )?
    (?P<amount> \d{1,3}(?:\.\d{3})*,\d{2} | \d+,\d{2} )  (?!\d)
    (?P<rest> [^\n]* )
''', re.IGNORECASE | re.VERBOSE)

# Hinter dem Betrag: ``EUR`` und/oder das Vorzeichen (S/-: Soll = negativ, H/+: Haben = positiv) als ganzes Wort
_BALANCE_TAIL = re.compile(r'[^\S\n]*(?:EUR(?![A-Za-z]))?[^\S\n]*(?:(?P<mark>[SH+-])(?![A-Za-z0-9]))?',
                           re.IGNORECASE)


def extract_text(filepath: str) -> str:
    """Gesamten Text eines PDFs lesen; bei Fehlern leerer String."""
    if not PDFPLUMBER_AVAILABLE:
        print("pdfplumber not installed. Run: pip install pdfplumber")
        return ""
    text = ""
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
    except Exception as e:
        print(f"Error extracting text from {filepath}: {e}")
    return text


def extract_iban(text: str) -> Optional[str]:
    """IBAN des Kontoinhabers aus dem Belegkopf lesen.

    Zuerst die eindeutige Form "IBAN: … BIC: …" in den ersten Zeilen, danach
    als Rückfall irgendein IBAN-artiges Muster im Kopfbereich. Weiter unten
    stehen die IBANs der Gegenseiten – die sind hier nicht gemeint.
    """
    lines = (text or '').split('\n')
    for line in lines[:20]:
        if 'IBAN:' in line and 'BIC:' in line:
            iban_match = re.search(r'IBAN:\s*([A-Z]{2}[\s\d]+)(?:\s*BIC:)', line)
            if iban_match:
                iban = iban_match.group(1).replace(' ', '')
                if len(iban) >= 15:
                    return iban

    first_part = '\n'.join(lines[:30])
    matches = re.findall(r'\b([A-Z]{2}\d{20,22})\b', first_part.replace(" ", ""))
    return matches[0] if matches else None


def extract_date(text: str, pattern: str = DOCUMENT_DATE_PATTERN) -> Optional[datetime]:
    """Datum per Muster aus dem Text lesen (Format TT.MM.JJJJ)."""
    match = re.search(pattern, text or '')
    if match:
        try:
            return datetime.strptime(match.group(1), '%d.%m.%Y')
        except ValueError:
            pass
    return None


def _balance_hits(text: str):
    """Alle Salden des Textes als (``'alter'``/``'neuer'``, Betrag mit Vorzeichen oder None bei ≥ 10¹²).

    Steht hinter dem Betrag statt S/H/+/-/EUR ein anderes Wort („Soll“, „Haben“ …), bleibt das Vorzeichen offen:
    Die Stelle zählt nicht – nichts wird geraten."""
    for match in _BALANCE.finditer(text or ''):
        rest = match.group('rest')
        tail = _BALANCE_TAIL.match(rest)
        mark = (tail.group('mark') or '').upper()
        if not mark and re.match(r'\s*[^\W\d_]', rest[tail.end():]):
            continue
        value = Decimal(match.group('amount').replace('.', '').replace(',', '.'))
        if mark in ('S', '-'):
            value = -value
        yield match.group('kind').lower(), (None if abs(value) >= MAX_BALANCE else value)


def find_balances(text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Anfangs- und Schlusssaldo eines Auszugs aus seinem Text: (alter Kontostand, neuer Kontostand).

    Anfangssaldo = der erste Treffer „alter Kontostand“, Schlusssaldo = der letzte Treffer „neuer Kontostand“
    (ein mehrseitiger Auszug nennt sie nicht nur einmal); ein Treffer braucht den Betrag in derselben Zeile.
    Gibt es keinen Treffer oder ist der Betrag ab 10¹², ist dieser Saldo ``None``. Gerechnet wird nur in
    ``Decimal``."""
    hits = list(_balance_hits(text))
    opening = next((value for kind, value in hits if kind == 'alter'), None)
    closing = next((value for kind, value in reversed(hits) if kind == 'neuer'), None)
    return opening, closing
