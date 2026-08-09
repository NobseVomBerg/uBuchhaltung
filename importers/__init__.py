# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Datenimport: Abstraktionsschicht und Registry der Importmodule.

Aufbau:

* ``base``          – gemeinsames Datenmodell (BankStatement, Transaktion)
* ``pdftext``       – Extraktions-Helfer für PDF-Belege
* ``plausibility``  – zentrale Inhaltsprüfung
* ``vbr``, ``dkb``  – je Bank ein eigenständiges Modul

Eine neue Bank anzubinden heißt: ein Modul mit einer
:class:`~importers.base.StatementImporter`-Klasse schreiben und sie unten in
``IMPORTERS`` eintragen. Der Rest der Anwendung ändert sich nicht – sie sieht
nur den fertigen ``BankStatement``.
"""
import os
from typing import List, Optional, Type

from . import pdftext
from .base import (CANONICAL_FIELDS, BankStatement, StatementImporter,
                   StatementTransaction)
from .dkb import DkbImporter
from .plausibility import check_statement, check_transaction
from .vbr import VbrImporter

#: Reihenfolge = Erkennungsreihenfolge; das erste passende Modul gewinnt.
IMPORTERS: List[Type[StatementImporter]] = [VbrImporter, DkbImporter]

__all__ = [
    'CANONICAL_FIELDS', 'BankStatement', 'StatementImporter',
    'StatementTransaction', 'IMPORTERS', 'find_importer', 'parse_statement',
    'check_statement', 'check_transaction',
]


def find_importer(filename: str, text: str) -> Optional[Type[StatementImporter]]:
    """Zuständiges Importmodul für einen Beleg bestimmen (oder None)."""
    for importer in IMPORTERS:
        try:
            if importer.detect(filename or '', text or ''):
                return importer
        except Exception as e:                      # defektes Modul darf den
            print(f"Importer {importer.bank_code} detect failed: {e}")
    return None


def parse_statement(filepath: str, filename: str = '',
                    text: Optional[str] = None) -> Optional[BankStatement]:
    """Beleg einlesen: Modul wählen, parsen, Plausibilität prüfen.

    Returns den geprüften :class:`~importers.base.BankStatement` oder None,
    wenn kein Modul zuständig ist (dann ist es kein Kontoauszug).
    """
    if text is None:
        text = pdftext.extract_text(filepath)
    importer = find_importer(filename or os.path.basename(filepath), text)
    if importer is None:
        return None
    statement = importer().parse(filepath, text)
    return check_statement(statement)
