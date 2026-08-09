# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Gemeinsame Extraktions-Helfer für PDF-Belege.

Text, IBAN und Belegdatum braucht jedes Importmodul – deshalb liegen sie hier
und nicht in einem der bankspezifischen Module. Reine Funktionen ohne Zustand.
"""
import re
from datetime import datetime
from typing import Optional

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

#: Standardmuster für das Belegdatum ("erstellt am 31.01.2024")
DOCUMENT_DATE_PATTERN = r'erstellt am (\d{2}\.\d{2}\.\d{4})'


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
