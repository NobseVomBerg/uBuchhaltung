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

Dazu kommen fünf Zusatzfelder (``DETAIL_FIELDS``), die nur CAMT füllt und die ein Umsatz nur trägt, wo sie einen Wert haben: ``value_date``
(Valuta), ``end_to_end_id``, ``mandate_ref``, ``creditor_id`` (Gläubiger-ID) und ``bank_ref`` (Bankreferenz des Umsatzes). Sie gehen nicht in
``Bookings``, sondern beim Import in ``BankTransactionDetails``; die Bankreferenz verfeinert außerdem den Hinweis der Duplikatprüfung.

Ein Importmodul erbt von :class:`StatementImporter`, erkennt seine Belege über
``detect()`` und liefert in ``parse()`` den fertigen ``BankStatement``. Mehr
schuldet es der Anwendung nicht.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

#: Spalten einer importierten Bankbewegung (Reihenfolge = Anzeigereihenfolge)
CANONICAL_FIELDS = ('date', 'amount', 'recipient', 'reference', 'foreign_iban')

#: Zusatzfelder einer Bankbewegung aus CAMT: Valuta, End-to-End-Referenz, Mandatsreferenz, Gläubiger-ID, Bankreferenz (``''`` = keine)
DETAIL_FIELDS = ('value_date', 'end_to_end_id', 'mandate_ref', 'creditor_id', 'bank_ref')

#: Länge, bis zu der ein Referenzfeld der Bank gilt (ISO 20022: Max35Text); ein längerer Wert ist kein Referenzfeld
MAX_BANK_TEXT = 35

#: Platzhalter, mit denen eine Bank „keine Referenz“ sagt (kleingeschrieben verglichen); sie sind keine Referenz
BANK_REF_PLACEHOLDERS = frozenset({'notprovided', 'nonref', 'notavailable'})

_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def clean_bank_text(value) -> str:
    """Ein Referenzfeld der Bank (Bankreferenz, End-to-End-Referenz, Mandatsreferenz, Gläubiger-ID) als Text, wie die App es speichert und anzeigt – oder ``''``.

    Leer bleibt, was kein Text ist, was nach dem Kürzen des Leerraums leer ist, länger als ``MAX_BANK_TEXT`` Zeichen ist, ein Steuerzeichen enthält (nicht druckbar) oder einer der
    Platzhalter der Banken ist (``BANK_REF_PLACEHOLDERS``, egal in welcher Schreibweise). Die EINE Regel für Leser, Speichern und Anzeigen: was sie nicht sicher lesen kann, bleibt leer."""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > MAX_BANK_TEXT or not value.isprintable() or value.casefold() in BANK_REF_PLACEHOLDERS:
        return ''
    return value


def clean_value_date(value) -> str:
    """Die Valuta einer Bankbewegung als ISO-Datum ``JJJJ-MM-TT`` – ein Kalendertag, der existiert, ohne Leerraum und ohne andere Schreibweise –, sonst ``''``."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return ''
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        return ''
    return value


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
    #: Zusatzfelder (``DETAIL_FIELDS``); nur CAMT füllt sie, immer über ``clean_value_date``/``clean_bank_text``
    value_date: str = ''
    end_to_end_id: str = ''
    mandate_ref: str = ''
    creditor_id: str = ''
    bank_ref: str = ''

    @classmethod
    def from_dict(cls, data: dict) -> 'StatementTransaction':
        """Aus dem Roh-Dict eines Parsers bauen; unbekannte Schlüssel entfallen, die Zusatzfelder gehen durch dieselbe Reinigung wie beim Lesen."""
        return cls(
            date=data.get('date'),
            amount=data.get('amount'),
            recipient=data.get('recipient') or '',
            reference=data.get('reference') or '',
            foreign_iban=data.get('foreign_iban') or '',
            warnings=list(data.get('warnings') or []),
            value_date=clean_value_date(data.get('value_date')),
            end_to_end_id=clean_bank_text(data.get('end_to_end_id')),
            mandate_ref=clean_bank_text(data.get('mandate_ref')),
            creditor_id=clean_bank_text(data.get('creditor_id')),
            bank_ref=clean_bank_text(data.get('bank_ref')),
        )

    def as_dict(self) -> dict:
        """Serialisieren – so, wie Vorschau und Import-Handler es lesen. Die Zusatzfelder stehen nur darin, wo sie einen Wert haben: Eine Datei ohne sie
        (PDF, CSV, ein älterer Import) behält genau die Schlüssel von früher."""
        data = {
            'date': self.date,
            'amount': self.amount,
            'recipient': self.recipient,
            'reference': self.reference,
            'foreign_iban': self.foreign_iban,
            'warnings': list(self.warnings),
        }
        data.update({name: getattr(self, name) for name in DETAIL_FIELDS if getattr(self, name)})
        return data


@dataclass
class BankStatement:
    """Ein Kontoauszug: Kopfdaten plus seine Bewegungen."""

    bank_code: str
    iban: Optional[str] = None
    document_date: Optional[datetime] = None
    transactions: List[StatementTransaction] = field(default_factory=list)
    #: Befunde, die den ganzen Beleg betreffen (keine Bewegungen, IBAN fehlt …)
    warnings: List[str] = field(default_factory=list)
    #: ISO-4217-Code, den der Beleg selbst nennt (CAMT); ``None`` = keine Angabe oder keine lesbare
    currency: Optional[str] = None

    def as_dict(self) -> dict:
        """Serialisieren im Format, das die Import-Vorschau erwartet."""
        return {
            'bank_code': self.bank_code,
            'iban': self.iban,
            'document_date': self.document_date,
            'currency': self.currency,
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
