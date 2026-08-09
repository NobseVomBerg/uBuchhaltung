# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Abstraktionsschicht für den Datenimport – das gemeinsame Datenmodell.

Jede Bank und jedes Fremdsystem liefert seine Kontoauszüge anders. Damit der
Rest der Anwendung davon nichts wissen muss, liefern alle Importmodule
dasselbe Ergebnis: einen ``BankStatement`` mit ``StatementTransaction``-Zeilen.

Die Felder einer Zeile sind bewusst knapp gehalten – es sind genau die Spalten,
die eine Bankbewegung ausmachen und die ``Bookings`` braucht:

===============  ====================================================
``date``         Buchungsdatum, ISO ``YYYY-MM-DD``
``amount``       Betrag in Euro; negativ = Belastung, positiv = Gutschrift
``recipient``    Empfänger/Auftraggeber
``reference``    Verwendungszweck
``foreign_iban`` IBAN der Gegenseite, falls im Beleg enthalten
===============  ====================================================

Ein Importmodul erbt von :class:`StatementImporter`, erkennt seine Belege über
``detect()`` und liefert in ``parse()`` den fertigen ``BankStatement``. Mehr
schuldet es der Anwendung nicht.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

#: Spalten einer importierten Bankbewegung (Reihenfolge = Anzeigereihenfolge)
CANONICAL_FIELDS = ('date', 'amount', 'recipient', 'reference', 'foreign_iban')


@dataclass
class StatementTransaction:
    """Eine Bankbewegung aus einem Kontoauszug."""

    date: Optional[str] = None
    amount: Optional[float] = None
    recipient: str = ''
    reference: str = ''
    foreign_iban: str = ''
    #: Plausibilitäts-Befunde (siehe importers.plausibility)
    warnings: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'StatementTransaction':
        """Aus dem Roh-Dict eines Parsers bauen; unbekannte Schlüssel entfallen."""
        return cls(
            date=data.get('date'),
            amount=data.get('amount'),
            recipient=data.get('recipient') or '',
            reference=data.get('reference') or '',
            foreign_iban=data.get('foreign_iban') or '',
            warnings=list(data.get('warnings') or []),
        )

    def as_dict(self) -> dict:
        """Serialisieren – so, wie Vorschau und Import-Handler es lesen."""
        return {
            'date': self.date,
            'amount': self.amount,
            'recipient': self.recipient,
            'reference': self.reference,
            'foreign_iban': self.foreign_iban,
            'warnings': list(self.warnings),
        }


@dataclass
class BankStatement:
    """Ein Kontoauszug: Kopfdaten plus seine Bewegungen."""

    bank_code: str
    iban: Optional[str] = None
    document_date: Optional[datetime] = None
    transactions: List[StatementTransaction] = field(default_factory=list)
    #: Befunde, die den ganzen Beleg betreffen (keine Bewegungen, IBAN fehlt …)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Serialisieren im Format, das die Import-Vorschau erwartet."""
        return {
            'bank_code': self.bank_code,
            'iban': self.iban,
            'document_date': self.document_date,
            'transactions': [t.as_dict() for t in self.transactions],
            'warnings': list(self.warnings),
        }


class StatementImporter:
    """Basisklasse eines bank- bzw. werkzeugspezifischen Importmoduls.

    Zwei Pflichten, mehr nicht:

    * ``detect(filename, text)`` – ist dieser Beleg meiner?
    * ``parse(filepath, text)``  – liefere ihn als :class:`BankStatement`.

    Die Plausibilitätsprüfung läuft zentral (siehe ``importers.parse_statement``)
    und muss von den Modulen nicht wiederholt werden.
    """

    #: Kurzkennung, landet als ``bank_code`` am Beleg und im Ablagepfad
    bank_code: str = ''
    #: Klartextname für Oberfläche und Protokoll
    name: str = ''

    @classmethod
    def detect(cls, filename: str, text: str) -> bool:
        raise NotImplementedError

    def parse(self, filepath: str, text: Optional[str] = None) -> BankStatement:
        raise NotImplementedError
