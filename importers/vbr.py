# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Importmodul: Volksbank Rottweil (VBR), Kontoauszug als PDF.

VBR liefert seine Auszüge textbasiert, nicht als Tabelle: eine Kopfzeile je
Bewegung ("01.12. 01.12. Lastschrift PN:931 1.142,18 S") und darunter
Detailzeilen mit Empfänger, Verwendungszweck und SEPA-Technikfeldern.

Das Modul kennt nur diese Eigenheiten; Datenmodell und Plausibilitätsprüfung
kommen aus der Abstraktionsschicht (siehe ``importers.base``).
"""
import re
from datetime import datetime
from typing import List, Optional

import pdfplumber

from . import pdftext
from .base import BankStatement, StatementImporter, StatementTransaction

# Fußzeile, die VBR unter jede Seite setzt:
#   0128 / 000 / K00009283 / (optional 5M) / "Bitte beachten Sie die Hinweise …"
_PAGE_FOOTER = re.compile(
    r'\n\d{4}\n\d{3}\n'
    r'K\d{5,}\n'
    r'(?:[A-Z0-9]{1,4}\n)?'
    r'Bitte beachten Sie die Hinweise[^\n]*')

_TRANSACTION_HEAD = re.compile(r'^(\d{2}\.\d{2}\.) \d{2}\.\d{2}\. (.+)')
_NEXT_TRANSACTION = re.compile(r'^\d{2}\.\d{2}\. \d{2}\.\d{2}\.')
_AMOUNT = re.compile(r'([\d.,]+)\s+([SH])\s*$')
_IBAN_IN_TEXT = re.compile(
    r'I\s*B\s*A\s*N\s*:?\s*([A-Z]{2}\s*\d{2}[A-Z0-9\s]{15,}?)'
    r'(?:\s*B\s*I\s*C\s*:|\s|$)', re.IGNORECASE)

# SEPA-Technikfelder (EREF/MREF/CRED/DEBT/REF/IBAN/BIC) FELDWEISE entfernen:
# Schlüsselwort + Doppelpunkt + genau EIN Wert-Token. Zeilenumbruch-Artefakte
# ("…28R\n4 CRED") und gruppierte IBANs ("DE87 3003 …") hängen als kurze
# Fragmente (≤4 Zeichen) an, die nur mitgelöscht werden, wenn direkt das
# nächste Feld oder das Textende folgt. Inhalt VOR, ZWISCHEN und NACH den
# Feldern bleibt erhalten (eine frühere Variante löschte ab dem ersten
# Schlüsselwort alles). Wortgrenzen beidseitig + Pflicht-Doppelpunkt
# verhindern False-Positives wie "Arabic", "DB IC 2024" oder den Vornamen
# "Iban".
_SEPA_KEYWORD = (r'(?<![A-Za-z])(?:'
                 r'[EM]\s*R\s*E\s*F'
                 r'|C\s*R\s*E\s*D'
                 r'|D\s*E\s*B\s*T'
                 r'|R\s*E\s*F'
                 r'|I\s*B\s*A\s*N'
                 r'|B\s*I\s*C'
                 r')(?![A-Za-z])\s*:')
_SEPA_FIELD = re.compile(
    _SEPA_KEYWORD + r'\s*\S+'
    r'(?:(?:\s+\S{1,4})+(?=\s*(?:' + _SEPA_KEYWORD + r'|$)))?',
    re.IGNORECASE)


def parse_text(text: str, year: Optional[int] = None) -> List[dict]:
    """Bewegungen aus dem Seitentext eines VBR-Auszugs lesen.

    *year* ist das Auszugsjahr aus der Kopfzeile (z. B. 2024 aus
    ``Kontoauszug Nr. 1/2024``) – die Zeilen selbst tragen nur Tag und Monat.
    """
    if year is None:
        year = datetime.now().year
    transactions: List[dict] = []

    text = _PAGE_FOOTER.sub('', text)
    lines = text.split('\n')

    i = 0
    while i < len(lines):
        match = _TRANSACTION_HEAD.match(lines[i].strip())
        if not match:
            i += 1
            continue

        bu_tag = match.group(1)             # z. B. "01.12."
        rest_of_line = match.group(2)       # alles nach dem zweiten Datum
        amount_match = _AMOUNT.search(rest_of_line)
        if not amount_match:
            i += 1
            continue

        amount = float(amount_match.group(1).replace('.', '').replace(',', '.'))
        if amount_match.group(2) == 'S':    # S = Soll = Belastung
            amount = -amount
        trans_type = rest_of_line[:amount_match.start()].strip()

        # Detailzeilen bis zur nächsten Bewegung einsammeln
        detail_lines = []
        j = i + 1
        while j < len(lines):
            next_line = lines[j].strip()
            if _NEXT_TRANSACTION.match(next_line):
                break
            if (not next_line
                    or 'Kontoauszug' in next_line
                    or 'Blatt' in next_line
                    or re.match(r'^\d{3,4}$', next_line)
                    or re.match(r'^K\d{5,}$', next_line)
                    or next_line.startswith('Bitte beachten Sie')):
                j += 1
                continue
            detail_lines.append(next_line)
            j += 1

        full_text = '\n'.join(detail_lines)

        # IBAN zuerst: sie kann über Zeilen gebrochen sein ("IB AN:")
        foreign_iban = ''
        iban_match = _IBAN_IN_TEXT.search(' '.join(detail_lines))
        if iban_match:
            foreign_iban = re.sub(r'\s+', '', iban_match.group(1).upper())

        cleaned_text = _SEPA_FIELD.sub('', full_text)
        cleaned_lines = [re.sub(r'\s{2,}', ' ', ln).strip()
                         for ln in cleaned_text.split('\n')]
        cleaned_lines = [ln for ln in cleaned_lines if ln]

        # Abschlussbuchungen des Auszugs haben keinen Empfänger
        abschluss_line = next(
            (ln for ln in cleaned_lines
             if re.match(r'^Abschluss\s', ln, re.IGNORECASE)), None)
        if abschluss_line:
            recipient, reference_lines = 'VBR', [abschluss_line]
        else:
            recipient = cleaned_lines[0] if cleaned_lines else ''
            reference_lines = cleaned_lines[1:] if cleaned_lines else []

        try:
            transaction_date = datetime.strptime(bu_tag + str(year), '%d.%m.%Y')
        except ValueError:
            transaction_date = datetime.now()

        transactions.append({
            'date': transaction_date.strftime('%Y-%m-%d'),
            'recipient': recipient if recipient else trans_type,
            'reference': '\n'.join(reference_lines) if reference_lines else trans_type,
            'amount': amount,
            'foreign_iban': foreign_iban,
        })
        i = j
    return transactions


# Vergleich ohne Leerzeichen: neuere Auszüge liefern Kopf- und Fußzeilen
# zusammengezogen ("BittebeachtenSiedieHinweise…"), ältere mit Leerzeichen.
_TABLE_HEAD_KEY = 'bu-tagwertvorgang'
_FOOTNOTE_KEY = 'bittebeachtensiediehinweise'
_FORM_NUMBER = re.compile(r'^(?:\d{3,4}|K\d{5,})$')


def _squash(text: str) -> str:
    """Zum Vergleich: Leerzeichen raus, klein."""
    return re.sub(r'\s+', '', text or '').lower()


def page_body(page_text: str) -> str:
    """Kopf- und Fußzeilen einer Auszugsseite entfernen.

    Jede VBR-Seite trägt oben den Briefkopf bis zur Tabellenüberschrift
    "Bu-Tag Wert Vorgang" und unten Formularnummern plus die Fußnote
    "Bitte beachten Sie die Hinweise …". Beides steht zwischen den Zeilen
    einer Buchung, wenn sie über den Seitenumbruch läuft.

    Ohne diesen Schnitt wurde die Fußnote zum Empfänger und die echten
    Detailzeilen der Folgeseite gingen verloren – sie standen dort ohne
    ihre Kopfzeile.

    Seiten ohne Tabellenüberschrift (Hinweisseite am Ende) liefern ''.
    """
    lines = page_text.split('\n')
    head = next((i for i, l in enumerate(lines)
                 if _squash(l).startswith(_TABLE_HEAD_KEY)), None)
    if head is None:
        return ''
    body = lines[head + 1:]

    cut = next((i for i, l in enumerate(body)
                if _FOOTNOTE_KEY in _squash(l)), len(body))
    while cut > 0 and _FORM_NUMBER.match(body[cut - 1].strip()):
        cut -= 1
    return '\n'.join(body[:cut])


def statement_year(text: str) -> int:
    """Auszugsjahr aus der Kopfzeile; Rückfall auf das laufende Jahr."""
    m = re.search(r'Kontoauszug\s+Nr\.\s*\d+/(\d{4})', text)
    if not m:
        m = re.search(r'erstellt\s+am\s+\d{2}\.\d{2}\.(\d{4})', text)
    return int(m.group(1)) if m else datetime.now().year


class VbrImporter(StatementImporter):
    """Volksbank Rottweil, Kontoauszug als PDF."""

    bank_code = 'VBR'
    name = 'Volksbank Rottweil (PDF)'

    @classmethod
    def detect(cls, filename: str, text: str) -> bool:
        return 'volksbank' in (text or '').lower() or 'vbr' in (filename or '').lower()

    def parse(self, filepath: str, text: Optional[str] = None) -> BankStatement:
        if text is None:
            text = pdftext.extract_text(filepath)

        statement = BankStatement(
            bank_code=self.bank_code,
            iban=pdftext.extract_iban(text),
            document_date=pdftext.extract_date(text),
        )
        year = statement_year(text)
        try:
            with pdfplumber.open(filepath) as pdf:
                bodies = [page_body(page.extract_text() or '')
                          for page in pdf.pages]
            # Die bereinigten Seitenrümpfe werden zu EINEM Text verkettet und
            # dann gelesen: nur so findet eine Buchung, die über den
            # Seitenumbruch läuft, ihre Detailzeilen wieder.
            for raw in parse_text('\n'.join(b for b in bodies if b), year):
                statement.transactions.append(StatementTransaction.from_dict(raw))
        except Exception as e:
            print(f"Error parsing transactions from {filepath}: {e}")
            statement.warnings.append('parse_error')
        return statement
