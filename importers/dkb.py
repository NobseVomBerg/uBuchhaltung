# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Importmodul: Deutsche Kreditbank (DKB), Kontoauszug als PDF.

Die eigentliche Seitenauswertung liegt weiterhin in ``document_parser`` und
wird hier nur eingehängt: Sie ist umfangreich, arbeitet mit
pdfplumber-Wortkoordinaten und hat – anders als der VBR-Parser – keine
Testabdeckung. Ein Verschieben wäre ein Umbau ohne Netz; die Abstraktion
gewinnt dadurch nichts, was sie nicht schon durch dieses Modul hat.

Sobald der DKB-Parser Tests hat (oder für den CSV-Import ohnehin neu
geschrieben wird), gehört er hierher.
"""
from typing import Optional

from . import pdftext
from .base import BankStatement, StatementImporter, StatementTransaction


class DkbImporter(StatementImporter):
    """Deutsche Kreditbank, Kontoauszug als PDF."""

    bank_code = 'DKB'
    name = 'DKB (PDF)'

    @classmethod
    def detect(cls, filename: str, text: str) -> bool:
        lowered = (text or '').lower()
        return ('deutsche kreditbank' in lowered or 'dkb' in lowered
                or 'kontoauszug_' in (filename or '').lower())

    def parse(self, filepath: str, text: Optional[str] = None) -> BankStatement:
        from document_parser import DocumentParser
        raw = DocumentParser.__new__(DocumentParser).parse_bank_statement_dkb(filepath)
        statement = BankStatement(
            bank_code=raw.get('bank_code') or self.bank_code,
            iban=raw.get('iban'),
            document_date=raw.get('document_date'),
        )
        for row in raw.get('transactions') or []:
            statement.transactions.append(StatementTransaction.from_dict(row))
        return statement
