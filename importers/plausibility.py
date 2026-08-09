# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Plausibilitätsprüfung importierter Kontoauszüge.

PDF-Textextraktion ist keine exakte Wissenschaft: Kopf- und Fußzeilen rutschen
regelmäßig in Empfänger oder Verwendungszweck, Beträge verschwinden, Datumsteile
werden falsch zusammengesetzt. Solche Zeilen landen sonst unbemerkt als
Buchungen in den Büchern.

Die Prüfung läuft zentral für alle Importmodule (siehe
``importers.parse_statement``) und macht Auffälligkeiten sichtbar, ohne den
Import zu blockieren – die Entscheidung trifft der Nutzer in der Vorschau.

Befund-Schlüssel (stabil, die Oberfläche zeigt sie an):

============  ===============================================================
``amount``    Betrag fehlt oder ist 0
``date``      Datum fehlt oder ist unlesbar
``daterange`` Datum liegt weit neben dem Belegdatum (Jahresdreher)
``empty``     weder Empfänger noch Verwendungszweck
``boiler``    Inhalt sieht nach Kopf-/Fußzeile des PDFs aus
``huge``      Betrag unrealistisch groß
============  ===============================================================
"""
import re
from datetime import datetime

#: Beträge oberhalb dieser Grenze sind fast immer ein Parse-Fehler
#: (verrutschtes Tausender-Trennzeichen, zusammengeklebte Spalten).
IMPLAUSIBLE_AMOUNT = 1_000_000.0

#: Wie weit ein Buchungsdatum vom Belegdatum abweichen darf (Tage).
#: Kontoauszüge decken Wochen ab; ein Jahresdreher fällt so auf.
MAX_DATE_DISTANCE_DAYS = 400

# Kopf-/Fußzeilen-Muster. Absichtlich auf das GANZE Feld verankert: "IBAN"
# oder "Auszug" kommen auch in echten Verwendungszwecken vor – als komplettes
# Feld ist es dagegen Layout-Müll.
_BOILERPLATE = [
    re.compile(p, re.IGNORECASE) for p in (
        r'^seite\s+\d+\s*(von|/)\s*\d+$',
        r'^blatt\s*\d*$',
        r'^kontoauszug(\s+nr\.?.*)?$',
        r'^auszug\s*(nr\.?)?\s*\d*(/\d+)?$',
        r'^(alter|neuer|neuer\s+)?kontostand.*$',
        # Umlaute gehen bei der PDF-Extraktion gern verloren – beide Schreibungen
        r'^(übertrag|uebertrag)\b.*$',
        r'^bitte\s+beachten\s+sie\b.*$',
        r'^(blz|bankleitzahl|bic|iban)\s*:?\s*[A-Z0-9\s]*$',
        r'^(tel|telefon|fax|e-?mail|www)\b.*$',
        r'^\d{3,4}$',                       # nackte Formularnummern
        r'^K\d{5,}$',                       # VBR-Fußzeilenschlüssel
        r'^[-–—_=.\s]+$',                   # reine Trennlinien
    )
]


def _looks_like_boilerplate(value: str) -> bool:
    """Ist dieses Feld vollständig Layout-Text statt Buchungsinhalt?"""
    text = ' '.join((value or '').split())
    if not text:
        return False
    return any(p.match(text) for p in _BOILERPLATE)


def _parse_iso(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d')
    except (TypeError, ValueError):
        return None


def check_transaction(transaction, document_date=None):
    """Befunde einer einzelnen Bewegung sammeln.

    :param transaction: :class:`~importers.base.StatementTransaction`
    :param document_date: Belegdatum als ``datetime`` (Bezug für ``daterange``)
    :returns: Liste der Befund-Schlüssel (leer = unauffällig)
    """
    found = []
    amount = transaction.amount
    if amount is None or amount == 0:
        found.append('amount')
    elif abs(float(amount)) > IMPLAUSIBLE_AMOUNT:
        found.append('huge')

    booked = _parse_iso(transaction.date)
    if booked is None:
        found.append('date')
    elif document_date is not None:
        try:
            distance = abs((booked - document_date).days)
        except TypeError:
            distance = 0
        if distance > MAX_DATE_DISTANCE_DAYS:
            found.append('daterange')

    recipient, reference = transaction.recipient, transaction.reference
    if not recipient and not reference:
        found.append('empty')
    elif (_looks_like_boilerplate(recipient)
            and (not reference or _looks_like_boilerplate(reference))):
        # Nur wenn NICHTS an der Zeile nach Inhalt aussieht – ein Beleg mit
        # sinnvollem Verwendungszweck und krudem Empfänger ist brauchbar.
        found.append('boiler')
    return found


def check_statement(statement):
    """Ganzen Beleg prüfen und die Befunde an den Bewegungen vermerken.

    Verändert ``statement`` (setzt ``warnings`` je Bewegung und am Beleg) und
    liefert ihn zurück, damit sich die Prüfung in eine Kette einhängen lässt.
    """
    doc_date = statement.document_date
    if doc_date is not None and not isinstance(doc_date, datetime):
        doc_date = _parse_iso(doc_date)

    for transaction in statement.transactions:
        transaction.warnings = check_transaction(transaction, doc_date)

    if not statement.transactions:
        statement.warnings.append('no_transactions')
    if not statement.iban:
        statement.warnings.append('no_iban')
    return statement
