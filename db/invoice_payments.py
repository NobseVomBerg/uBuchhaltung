# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Zahlungszuordnung Rechnung ↔ Bankbewegung.

Eine Zuordnung besteht aus genau einer InvoicePayments-Zeile und den Buchungssätzen, die die
App für sie erzeugt hat. Diese Sätze sind eindeutig bestimmt durch

* ``ParentBooking_ID`` = die Bankbewegung der Zuordnung,
* ``DocumentNumber``   = die Rechnungsnummer,
* ``Status``           = ``PAYMENT_MARK`` (``'invoice_payment'``).

Es gibt genau EINE Stelle, die Zahlungen zuordnet (``assign_invoice_payment``: Zahlungseingang auf eine
Rechnung), EINE, die eine Stornorechnung erstattet (``assign_storno_refund``: Zahlungsausgang auf eine
Stornorechnung, seit 2.2.3), und EINE, die Zuordnungen entfernt (``unassign_invoice_payment``);
``delete_transaction`` und der WISO-Import gehen über dieselben Bausteine. Anlegen und Entfernen laufen in
einer Transaktion mit ``BEGIN IMMEDIATE``: Prüfung, Schreiben und Neuberechnung der Rechnung sehen denselben
Stand – zwei gleichzeitige Anfragen legen das Paar (Rechnung, Bankbewegung) nie zweimal an.
"""
import datetime
import re
import sqlite3
from collections import defaultdict, namedtuple
from decimal import Decimal

from money import to_minor, from_minor, prorate, round_minor, fmt_eur, is_covered
from tax_groups import (MAX_RATE_BP, MIN_RATE_BP, RateGroupsMismatch, booked_fault, booking_rate, bp_pct, group_label, item_rate_of, mixed_groups,
                        rate_pct, refund_pool, split_payment)
from .matching import FORM_LIQUID_FIRST, FORM_PURPOSE_FIRST, FORM_TRANSFER, REVENUE_ACCOUNT_RANGE, bank_effect, receivables_covered
from .reporting import is_income_account

PAYMENT_MARK = 'invoice_payment'

# Wert von ``Invoices.InvoiceKind`` einer Stornorechnung (``db/invoice_storno.py``). Hier definiert, weil
# dieses Modul das unterste der Rechnungsmodule ist; ``db.invoice_lock`` reicht den Namen weiter.
STORNO_KIND = 'storno'
STORNO_PAYMENT_MESSAGE = 'Einer Stornorechnung lässt sich keine Zahlung zuordnen.'

# Ein Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1, seit 2.7.0): Sein Satz im Kopf ist nur ein Platzhalter (der höchste Satz der
# Positionen), und jede Buchung, die ihn nähme, buchte falsch – maßgeblich sind die Satzgruppen (``tax_groups.mixed_groups``). Versenden, Wiederbeleben,
# Zahlung zuordnen, Stornieren (die Gegenbuchung je Satz) und die Erstattung buchen je Satz (Forderung je Satz, ``tax_groups.split_payment`` verteilt eine
# Zahlung und eine Erstattung). Zwei Wege rühren ihn nicht je Satz an: Der Neuaufbau der Zuordnung rechnet nur den offenen Betrag neu, und der Umzug der
# Zahlungen beim Versenden einer Ersatzrechnung unterbleibt (``MOVE_MIXED_HINT``). Allein ``link_invoice_to_transaction`` (der WISO-Import) lehnt so einen
# Beleg ab – VOR dem ersten Schreiben, mit diesem Satz: Der Weg schreibt eine Zuordnung ohne Buchungssätze, deren Anteil je Satz keine spätere Buchung
# kennen könnte. Wie ``STORNO_KIND`` hier definiert, weil dieses Modul das unterste der Rechnungsmodule ist; ``db.invoices`` reicht die Namen weiter.
MIXED_LINK_NOT_BOOKABLE = 'Einer Rechnung mit mehreren Steuersätzen lässt sich eine Zahlung nur mit ihren Buchungssätzen zuordnen.'

# Die Ablehnungen der Buchungen eines Belegs mit Steuersatz je Position. ``{rate} %``: der Satz (``rate_sentence``: „19 %“, „5,5 %“, bei einer steuerfreien
# Gruppe „steuerfreie Leistungen“), ``{amount}``: ein Betrag (``fmt_eur``)
MIXED_GROUPS_MISMATCH = ('Die Positionen dieser Rechnung passen nicht zu ihren Summen – eine Rechnung mit mehreren Steuersätzen '
                         'lässt sich so nicht buchen. Bitte die Rechnung prüfen.')
MIXED_GROUP_NOT_POSITIVE = ('Eine Rechnung mit mehreren Steuersätzen lässt sich nur buchen, wenn die Positionen jedes Steuersatzes '
                            'zusammen einen positiven Betrag ergeben ({rate} %: {amount}).')
MIXED_NO_REVENUE_COA = ('Für {rate} % ist kein Erlöskonto hinterlegt – bei einer Rechnung mit mehreren Steuersätzen lässt es sich '
                        'nicht wählen. Bitte das Erlöskonto für {rate} % im Kontenrahmen anlegen.')
MIXED_RECEIVABLE_MISMATCH = ('Die Forderungsbuchungen dieser Rechnung passen nicht zu ihren Steuersätzen – bitte die Forderung prüfen '
                             '(Prüfung unter Sonstiges).')
MIXED_ALLOCATION_UNREADABLE = ('Die bisherigen Zahlungen dieser Rechnung lassen sich nicht nach Steuersätzen aufteilen (eine Zuordnung '
                               'ohne die Buchungssätze der App, oder sie wurden von Hand geändert) – bitte die Zuordnungen prüfen.')
# Die Erstattung einer Stornorechnung zu einem Beleg mit Steuersatz je Position (``assign_storno_refund``) und der Umzug der Zahlungen
MIXED_REFUND_REVENUE_AMBIGUOUS = ('Die Zahlungen dieser Rechnung liegen für {rate} % auf verschiedenen Erlöskonten – bei einer Rechnung '
                                  'mit mehreren Steuersätzen lässt sich das Erlöskonto der Erstattung nicht wählen. Bitte die '
                                  'Buchungssätze der Zahlungen prüfen.')
MIXED_REFUND_SPLIT = ('Die Erstattung lässt sich nicht nach Steuersätzen aufteilen: Die Zahlungen dieser Rechnung haben sich nach '
                      'einer früheren Erstattung geändert. Bitte die Erstattungen prüfen.')
# Das Entfernen einer Zahlung, nach der zu so einem Beleg erstattet wurde (``_refund_removal_blocker``): Die Summe ginge noch auf, aber je Satz wäre mehr
# erstattet als gezahlt (oder Betrag und Steuer gingen nicht auf). Gebaut wie ``ALREADY_REFUNDED_MESSAGE`` (die Summenprüfung), dazu der Satz und die Nummer
# der Stornorechnung, an der die Erstattung entfernt wird. ``{rate} %``: der Satz (``rate_sentence``), ``{number}``: die Nummer der Stornorechnung
MIXED_REMOVAL_REFUSED = ('Zu dieser Rechnung wurde bereits erstattet – für {rate} % wäre danach mehr erstattet als gezahlt (oder Betrag und Steuer '
                         'gingen nicht auf). Erst die Erstattung an der Stornorechnung {number} entfernen.')
# Der Hinweis beim Versenden einer Ersatzrechnung (``_move_replaced_payments``): Bei einem Beleg mit Steuersatz je Position zieht nichts um. ``{number}``: die
# Nummer der stornierten Rechnung, ``{amount}``: ein Betrag (``fmt_eur``). ``MOVE_NONE_HINT`` und ``MOVE_REFUNDED_HINT`` sind dieselben Sätze wie beim Beleg
# mit einem Satz – dort wie hier wortgleich
MOVE_MIXED_HINT = ('Die Zahlungen der stornierten Rechnung {number} ({amount}) bleiben dort als Guthaben des Kunden: Eine der beiden '
                   'Rechnungen trägt mehrere Steuersätze, die Zahlungen ziehen in dieser Version nicht automatisch um. Sie lassen '
                   'sich dort erstatten oder entfernen und hier neu zuordnen.')
MOVE_NONE_HINT = 'Es wurden keine Zahlungen der stornierten Rechnung {number} übernommen.'
MOVE_REFUNDED_HINT = '{amount} sind bereits erstattet und bleiben an der stornierten Rechnung.'
PAYMENT_CHANGED_MESSAGE = 'Rechnung oder Bankbewegung wurde inzwischen geändert – bitte erneut versuchen.'

# Ein Beleg mit einem Steuersatz aus den Stammdaten (2.8.0), dessen Positionen ein eigenes Erlöskonto tragen (``InvoiceItems.RevenueCoaId``,
# ``invoice_entry_accounts``): Der Umzug der Zahlungen auf die Ersatzrechnung unterbleibt wie bei ``MOVE_MIXED_HINT`` – er bucht mit dem Erlöskonto neu, das heute
# für den Satz hinterlegt ist, und je Momentaufnahme zu verallgemeinern ist Etappe 3 von ``docs/aufgaben.md``, deren Bedingungen sich nicht lockern. Dieselben
# Platzhalter wie ``MOVE_MIXED_HINT``; nach einer Erstattung folgen ``MOVE_NONE_HINT`` und ``MOVE_REFUNDED_HINT``.
MOVE_ENTRY_HINT = ('Die Zahlungen der stornierten Rechnung {number} ({amount}) bleiben dort als Guthaben des Kunden: Eine der beiden '
                   'Rechnungen trägt einen Steuersatz mit eigenem Erlöskonto aus den Stammdaten, die Zahlungen ziehen in dieser Version nicht '
                   'automatisch um. Sie lassen sich dort erstatten oder entfernen und hier neu zuordnen.')
# Die Zahlung auf einen Beleg, dessen Momentaufnahme ein Erlöskonto trägt, das keines (mehr) ist (``InvoiceItems.RevenueCoaId`` zeigt auf ein Konto außerhalb
# 4000–4999 oder ins Leere – nur nach einem Eingriff an der App vorbei, die Prüfliste nennt es): Das Konto der Momentaufnahme gilt immer, eine Wahl gibt es nicht.
# ``{rate}``: der Satz der Gruppe, mit ``rate_sentence`` eingesetzt („19 %“, „5,5 %“, „steuerfreie Leistungen“). Auch die Erstattung nennt ihn, wenn die Zahlungen eines
# Belegs mit Momentaufnahme auf einem Konto liegen, das keines (mehr) ist (``_refund_revenue_coas_by_rate``)
TAX_PAYMENT_NO_REVENUE = ('Das Erlöskonto, das diese Rechnung für {rate} trägt, ist kein Erlöskonto (mehr) – die Zahlung lässt sich nicht kontieren. '
                          'Bitte das Konto im Kontenrahmen prüfen (Prüfung unter Sonstiges).')
# Die Angaben aus den Stammdaten an den Positionen eines Belegs (die Momentaufnahmen eines Steuersatzes, 2.8.0) widersprechen sich – ein fester Satz für JEDEN Widerspruch
# der Momentaufnahme (``InvoicePaymentsMixin._read_snapshots``, ``_single_snapshot``): verschiedene Angaben in einer Satzgruppe, teils mit, teils ohne, die Art „steuerfrei“ mit
# einem Satz über 0, ein Satz in Hundertstel Prozent, der keiner ist oder nicht der der Position, Angaben ohne den Satz dazu, ein Kopfsatz, der nicht passt. Kein Beleg mit
# einem solchen Widerspruch lässt sich buchen: Versenden, Zahlung und Stornieren lehnen vor dem ersten Schreiben ab. ``MIXED_GROUPS_MISMATCH`` bleibt für das, was er sagt
# (die Positionen passen nicht zu den Summen)
TAX_ENTRY_CONTRADICTION = ('Die Angaben aus den Stammdaten an den Positionen dieser Rechnung widersprechen sich – sie lässt sich so nicht buchen '
                           '(Prüfung unter Sonstiges).')
# Die Befunde der Kategorie ``tax_entry_mismatch`` der Prüfliste (``_tax_entry_findings``), nach Art: feste Sätze ohne Beträge. ``{rate} %`` wird mit
# ``rate_sentence`` zum Satz der Gruppe („19 %“, „5,5 %“, „steuerfreie Leistungen“), ``{position}`` und ``{head}`` sind Sätze, wie sie dastehen
TAX_ENTRY_FINDINGS = {
    'differs': ('Für {rate} % tragen die Positionen der Rechnung verschiedene Angaben aus den Stammdaten (Art, Befreiungsgrund oder Erlöskonto) oder teils '
                'keine – die Rechnung lässt sich so nicht buchen.'),
    'rate': 'Für {rate} % passt der Satz aus den Stammdaten nicht zum Steuersatz einer Position ({position}) – die Rechnung lässt sich so nicht buchen.',
    'exempt_rate': 'Eine Position der Art „steuerfrei“ trägt den Satz {rate} % – eine steuerfreie Leistung hat den Satz 0.',
    'exempt_reason': 'Die steuerfreien Positionen der Rechnung tragen keinen Grund der Steuerbefreiung.',
    'revenue': 'Für {rate} % ist das Erlöskonto der Rechnung kein Erlöskonto (mehr) – die Zahlung lässt sich nicht kontieren.',
    'head': ('Der Steuersatz im Kopf der Rechnung ({head}) passt nicht zum Satz aus den Stammdaten ({rate} %) – die Rechnung lässt sich so nicht '
             'buchen.'),
    'half': 'Eine Position trägt Angaben aus den Stammdaten ohne den Satz dazu – die Rechnung lässt sich so nicht buchen.',
}

# So heißt eine steuerfreie Gruppe in einem SATZ (``rate_sentence``): „Für steuerfrei ist kein Erlöskonto hinterlegt“ liest sich holprig. Tabellen und PDF nennen sie weiter
# „steuerfrei“ (``tax_groups.group_label``)
EXEMPT_SENTENCE_LABEL = 'steuerfreie Leistungen'


def rate_sentence(template, pct, exempt=False, **names):
    """Ein fester Satz mit dem Satz einer Gruppe: ``{rate} %`` der Vorlage (oder ein bloßes ``{rate}``) wird zu ``tax_groups.group_label(pct, False)`` („19 %“, „5,5 %“), bei
    einer steuerfreien Gruppe (``exempt``) zu „steuerfreie Leistungen“ (``EXEMPT_SENTENCE_LABEL``); ``names``: die übrigen Platzhalter. Für jeden Satz außer dem einer
    steuerfreien Gruppe ist das Ergebnis Zeichen für Zeichen das von ``template.format(rate=pct_text(pct), …)`` – die ``MIXED_*``-Sätze (und ihre Tests) bleiben, wie sie
    waren; nur eine steuerfreie Gruppe heißt „steuerfreie Leistungen“ statt „0 %“. ``group_label`` selbst (``steuerfrei``, für das PDF und die Listen) ändert sich nicht."""
    return template.replace('{rate} %', '{rate}').format(rate=EXEMPT_SENTENCE_LABEL if exempt else group_label(pct, False), **names)


class MixedNotBookable(ValueError):
    """Ein Beleg mit Steuersatz je Position lässt sich nicht buchen; ``str(exc)`` ist der feste Klartext für den Nutzer (``MIXED_*``). Wo ein
    Abbruch über mehrere Ebenen nötig ist, ohne dass jede einen Rückgabewert prüft: Jeder Aufrufer setzt sie auf seinen Ablehnungsweg um
    (``_Refusal``, ``_refused``, ein ``ValueError`` an die Route …)."""


#: Die Momentaufnahme eines Steuersatzes aus den Stammdaten, wie sie die Positionen EINER Satzgruppe tragen (``InvoicePaymentsMixin.group_snapshots``):
#: ``exempt`` – die Art ist „steuerfrei“ (``TaxCategory`` ``'E'``), ``reason`` – der Grund der Befreiung (``TaxExemptReason``), ``revenue_coa_id`` – das Erlöskonto des
#: Eintrags (``RevenueCoaId``; ``None``: keines, der Satz bucht nach den bisherigen Regeln). Hashbar und vergleichbar: Sie gehört in den Schlüssel, den eine
#: Transaktion mit ihrer Vorbereitung vergleicht.
Snapshot = namedtuple('Snapshot', 'exempt reason revenue_coa_id')


class _SnapshotConflict(Exception):
    """Die Momentaufnahmen der Positionen eines Belegs widersprechen sich (``InvoicePaymentsMixin._read_snapshots``); ``str(exc)`` ist nur für das Server-Log."""


#: Die Spalten einer Position (``InvoiceItems``), die die Momentaufnahme eines Steuersatzes aus den Stammdaten tragen (Schema 2008), mit ihren
#: Schlüsseln in ``item_data`` (``insert_invoice_item``). ``TaxCategory`` gab es schon (``'S'``, mit der Momentaufnahme eines steuerfreien Eintrags
#: ``'E'``); die anderen drei hängen hinten an der Tabelle an (Index 11 bis 13). NULL – bei ``TaxCategory`` ``'S'`` – heißt „keine Momentaufnahme“:
#: Der Satz steht allein in ``TaxRate``, wie beim ganzen Bestand. Jeder Weg, der eine Position kopiert (Stornorechnung, Ersatzrechnung, Angebot →
#: Rechnung), nimmt alle vier mit. Die Konstanten stehen hier, im untersten der Rechnungsmodule (``db.invoices`` reicht sie weiter), damit auch
#: ``invoice_lock`` und ``invoice_versions`` sie ohne Zyklus lesen können.
ITEM_SNAPSHOT_KEYS = {'TaxCategory': 'tax_category', 'TaxRateBp': 'tax_rate_bp', 'TaxExemptReason': 'tax_exempt_reason',
                      'RevenueCoaId': 'revenue_coa_id'}
ITEM_SNAPSHOT_COLUMNS = tuple(ITEM_SNAPSHOT_KEYS)


def has_mixed_rates(value):
    """Trägt der Beleg einen Steuersatz je Position? ``value`` ist ``Invoices.MixedTaxRates`` – die EINE Stelle, die ihn liest, jede andere
    fragt sie. Streng: Nur die ganze Zahl 1 gilt; NULL, 0, Text, ``True``, 1.0 und jede andere Zahl (die Spalte lässt sich per SQL-Konsole oder
    zurückgespielter Sicherung beschreiben) heißen „ein Satz für den ganzen Beleg“ – wie ``db.accounts.is_business_only`` liest sie streng."""
    return type(value) is int and value == 1


def valid_rate_bp(bp):
    """Ist ``bp`` ein Satz in Hundertstel Prozent, wie ihn die Momentaufnahme einer Position trägt (``InvoiceItems.TaxRateBp``)? Nur eine ganze Zahl (kein Wahrheitswert), die 0
    ist oder von ``MIN_RATE_BP`` (1 %) bis ``MAX_RATE_BP`` (100 %) reicht – so lässt ``tax_groups.check_tax_entry`` einen Satz zu, und so liest ihn ``export.einvoice_model.item_bp``
    (dort für das Dokument: Was nicht passt, gilt als „keine Momentaufnahme“). Hier ist es die Regel der Buchungen: Ein Wert, der nicht passt, ist ein Widerspruch
    (``TAX_ENTRY_CONTRADICTION``), nie „keine Momentaufnahme“. Dieselbe Regel steht hier und dort, weil ``export`` diese Schicht importiert (ein Import in die andere Richtung
    wäre ein Zyklus); ``tests/test_tax_entries_bookings.py`` hält beide gegeneinander. Ein Satz zwischen 0 und 1 % ist keiner: ``rate_pct`` läse ihn als Bruch."""
    return type(bp) is int and (bp == 0 or MIN_RATE_BP <= bp <= MAX_RATE_BP)


#: Das Erlöskonto eines Belegs ohne Steuerausweis (§ 19 UStG, Kopfsatz −1) hängt am Datum der Bankbewegung der Zahlung (bzw. der Erstattung), nicht am Rechnungsdatum: Bis zum
#: 31.12.2025 bucht der Erlös auf 4185, ab 1.1.2026 auf 4184 (die Nummer im Standard-Kontenrahmen ab 2026). Eine Rechnung von 2025, die 2026 bezahlt wird, bucht auf 4184.
#: Die Konstanten stehen hier, im untersten der Rechnungsmodule (``db.invoices`` reicht sie weiter).
SMALL_BUSINESS_SWITCH = datetime.date(2026, 1, 1)
SMALL_BUSINESS_REVENUE_BEFORE = 4185
SMALL_BUSINESS_REVENUE_FROM = 4184
SMALL_BUSINESS_REVENUE_NUMBERS = frozenset({SMALL_BUSINESS_REVENUE_BEFORE, SMALL_BUSINESS_REVENUE_FROM})
SMALL_BUSINESS_REVENUE_NAMES = {
    SMALL_BUSINESS_REVENUE_FROM: 'Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG',
    SMALL_BUSINESS_REVENUE_BEFORE: 'Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG',
}
# Ein Tag je Konto, an dem die Datumsregel dieses Konto liefert (``small_business_day``)
_SMALL_BUSINESS_DAYS = {SMALL_BUSINESS_REVENUE_BEFORE: SMALL_BUSINESS_SWITCH - datetime.timedelta(days=1),
                        SMALL_BUSINESS_REVENUE_FROM: SMALL_BUSINESS_SWITCH}
_ISO_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def uses_small_business_rule(tax_rate):
    """Folgt das Erlöskonto eines Belegs mit diesem Kopfsatz (``Invoices.TaxRate``) der Datumsregel der Kleinunternehmer-Erlöse (4185 bis 2025, 4184 ab 2026)?

    **Die EINE Stelle dieser Entscheidung.** Nur der Platzhalter „ohne Steuerausweis“ (< 0, § 19) folgt der Datumsregel; ein Satz von 0 % MIT Steuerausweis bleibt fest auf 4185
    (er teilt mit dem Platzhalter den Topf der gelernten Erlöskonten, ``Database._revenue_rate_key``, ist aber kein Beleg nach § 19). Soll auch 0 % der Datumsregel folgen, ist
    die Bedingung hier ``tax_rate <= 0`` – kein anderer Ort rechnet es nach."""
    return tax_rate is not None and tax_rate < 0


def small_business_revenue_number(day):
    """Das Kleinunternehmer-Erlöskonto für das Datum einer Bankbewegung: 4185 vor ``SMALL_BUSINESS_SWITCH``, 4184 ab diesem Tag – oder ``None``, wenn ``day`` kein Datum ist.

    ``day``: ein ``datetime.date``/``datetime.datetime`` oder ein Text genau ``JJJJ-MM-TT`` (ein Kalendertag, Jahr 1900–2200, ohne Leerraum und ohne andere Schreibweise –
    dieselbe Regel wie ``db.invoice_versions.is_iso_date``; dieses Modul darf es nicht importieren). Alles andere – ``None``, Leerraum, ``2026-1-1``, ``2026-02-30``, ein Datum mit
    Uhrzeit, eine Zahl, Bytes – ergibt ``None``: Ein Datum wird nie geraten, und davon hängt ein Konto ab."""
    if isinstance(day, datetime.datetime):
        day = day.date()
    elif isinstance(day, str):
        if not _ISO_DAY.fullmatch(day):
            return None
        try:
            day = datetime.date.fromisoformat(day)
        except ValueError:
            return None
        if not 1900 <= day.year <= 2200:
            return None
    elif not isinstance(day, datetime.date):
        return None
    return SMALL_BUSINESS_REVENUE_FROM if day >= SMALL_BUSINESS_SWITCH else SMALL_BUSINESS_REVENUE_BEFORE


def small_business_day(number):
    """Ein Tag, an dem die Datumsregel das Konto ``number`` (4185 oder 4184) liefert – für Auflösungen je Konto (Umzug, Neuaufbau): 4185 → 31.12.2025, 4184 → 1.1.2026."""
    return _SMALL_BUSINESS_DAYS[number]


def _revenue_today():
    """Heute – für das Erlöskonto, wo kein Datum einer Bankbewegung bekannt ist (Anzeigen, Tests). Eine eigene Funktion, damit Tests sie festsetzen können; sie liest die Uhr über
    ``datetime.date.today()`` (die festgesetzte Uhr der Schnappschuss-Tests fängt das)."""
    return datetime.date.today()


# Die Erstattung einer Stornorechnung (``assign_storno_refund``) und der Schutz des Guthabens
REFUND_ONLY_STORNO_MESSAGE = 'Erstatten lässt sich nur auf eine Stornorechnung.'
REFUND_ONLY_OUTGOING_MESSAGE = 'Nur Zahlungsausgänge lassen sich als Erstattung zuordnen.'
REFUND_NO_CREDIT_MESSAGE = 'Zu dieser Stornorechnung gibt es kein Guthaben zu erstatten.'
INCOMING_ONLY_MESSAGE = 'Nur Zahlungseingänge lassen sich einer Rechnung zuordnen.'
NOTHING_TO_ASSIGN_MESSAGE = 'Kein offener Betrag zuordenbar (Rechnung oder Buchung ausgeschöpft)'
REFUND_EXHAUSTED_MESSAGE = 'Kein offener Betrag zuordenbar (Guthaben oder Buchung ausgeschöpft)'
ALREADY_REFUNDED_MESSAGE = ('Zu dieser Rechnung wurde bereits erstattet – erst die Erstattung an der Stornorechnung '
                            'entfernen.')
MIXED_REVENUE_MESSAGE = ('Die Zahlungen dieser Rechnung liegen auf verschiedenen Erlöskonten – bitte das Erlöskonto für '
                         'die Erstattung wählen.')

# Ändert der Nutzer Betrag, Bankkonto oder Datum einer Bankbewegung, der eine Zahlung oder Erstattung zugeordnet ist
# (``_assigned_bank_change_blocker``): Die Zuordnung (Betrag, Rechnungsstatus, Sätze) bliebe stehen und stimmte nicht mehr. Die
# Platzhalter: die Nummern der Belege, was geändert werden soll („den Betrag“, „das Bankkonto“, „das Datum“, mit „und“ verbunden)
BANK_CHANGE_PAYMENT_MESSAGE = ('Dieser Bankbewegung ist eine Rechnungszahlung zugeordnet (Rechnung {numbers}). Bitte zuerst die '
                               'Zuordnung an der Rechnung entfernen, dann {changes} ändern.')
BANK_CHANGE_REFUND_MESSAGE = ('Dieser Bankbewegung ist eine Erstattung zugeordnet (Stornorechnung {numbers}). Bitte zuerst die '
                              'Erstattung an der Stornorechnung entfernen, dann {changes} ändern.')

# Auf diese Status lässt sich eine Zahlung zuordnen: ab „versendet“ ist die Rechnung ein
# offener Posten. 'partial' ist der Alt-Wert von 'partial_payment'.
PAYABLE_STATUSES = ('sent', 'overdue', 'partial_payment', 'partial')

# Ab „versendet“ ist die Rechnung ein offener Posten und hat eine Forderungsbuchung; davor
# (Entwurf, abgeschlossen) und nach einem Storno von Hand (Altbestand, ohne Stornorechnung) nicht.
# Zustands- statt übergangsgesteuert, damit auch Sprünge wie abgeschlossen → bezahlt richtig landen.
# Eine Rechnung mit Stornorechnung behält ihre Forderung in jedem Fall (``_drop_receivable``).
RECEIVABLE_STATUSES = frozenset({'sent', 'partial_payment', 'overdue', 'paid'})

NO_REVENUE_COA = ('Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt – bitte ein '
                  'Erlöskonto wählen.')
NOT_A_REVENUE_COA = 'Das gewählte Konto ist kein Erlöskonto.'
# Das Erlöskonto eines Belegs ohne Steuerausweis hängt am Datum der Bankbewegung (``small_business_revenue_number``): Lässt es sich nicht lesen, wird die Zahlung (bzw.
# Erstattung) abgelehnt – vor dem Auflösen der Konten, nichts geschrieben, kein Konto angelegt
REVENUE_DATE_UNREADABLE = 'Das Datum der Bankbewegung ist ungültig – davon hängt das Erlöskonto ab. Bitte das Datum korrigieren.'

# Die Spalten, die ``_assign_in_tx`` für jeden Satz einer Zuordnung schreibt, in der Reihenfolge des INSERT (dazu die festen
# Werte BookingType 'entry' und AutoMirror 0). Daran erkennt der Umzug der Zahlungen beim Versenden einer Ersatzrechnung, ob
# die Sätze einer Zuordnung noch die der App sind (``payment_rows``, ``InvoicePaymentsMixin._same_payment_row``).
PAYMENT_ROW_COLUMNS = ('DateBooking', 'DateTax', 'RecipientClient', 'Contact_ID', 'COA_ID', 'CounterCOA_ID', 'Amount',
                       'Currency', 'TaxRate', 'TaxAmount', 'Text', 'DocumentNumber', 'ParentBooking_ID', 'Status',
                       'EntryForm')
# Dazu prüft der Vergleich: BookingType (fest 'entry') und was die App an so einem Satz nie schreibt – er muss leer sein, gesetzt
# hat es ein Nutzer oder ein Import
_PAYMENT_ROW_UNWRITTEN = ('BookingType', 'Account_ID', 'ForeignBankAccount', 'Category_ID', 'SourceGroup')
_PAYMENT_ROW_INSERT = ('INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, Contact_ID, '
                       'COA_ID, CounterCOA_ID, Amount, Currency, TaxRate, TaxAmount, Text, '
                       "DocumentNumber, BookingType, ParentBooking_ID, AutoMirror, Status, "
                       "EntryForm) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, 0, ?, ?)")

# Ein Satz einer Zuordnung: ``values`` {Spalte: Wert} für ``PAYMENT_ROW_COLUMNS``, ``description`` für das Replay-Log
PaymentRow = namedtuple('PaymentRow', 'values description')

# Der Teil einer Zahlung, der auf einen Satz entfällt (Beleg mit Steuersatz je Position, ``payment_rows(parts=…)``): das Wartekonto des Satzes
# (COA-ID, ``None`` ohne Forderung), sein Erlöskonto, der Betrag (Minor Units), der Satz als Bruch (``tax_groups.booking_rate``, ``None`` bei 0 %) und
# die Steuer (Minor Units, ``None`` ohne Satz)
PaymentPart = namedtuple('PaymentPart', 'waiting revenue amount rate tax')


def payment_rows(*, number, customer_id, bank_id, bank_date, bank_tax_date, recipient, currency, amount, tax_rate,
                 tax_value, bank_coa, revenue_coa, debtor_side=None, waiting_side=None, parts=None):
    """Die Buchungssätze, die die App für eine Zuordnung schreibt – die EINE Stelle, die ihre Werte festlegt: ``_assign_in_tx``
    schreibt sie, der Umzug der Zahlungen vergleicht den Bestand damit.

    ``number``: Nummer der Rechnung (Beleg-Nr.), ``customer_id``: ihr Kunde, ``bank_id``/``bank_date``/``bank_tax_date``/
    ``recipient``/``currency``: die Bankbewegung, ``amount``: der zugeordnete Betrag (Minor Units; bei der Erstattung einer
    Stornorechnung, ``assign_storno_refund``, negativ – ebenso ``tax_value``, und ``number`` ist dann die Nummer der
    Stornorechnung), ``tax_rate`` (als
    Bruchteil, ``None`` ohne Steuer) und ``tax_value`` (Minor Units) die Steuer der Zuordnung, ``bank_coa``/``revenue_coa``:
    Konto der Bank und Erlöskonto (COA-IDs). Mit Forderung (``debtor_side`` und ``waiting_side`` gegeben) sind es „Bank an
    Debitoren“ (``liquid_first``, ohne Steuer) und „Wartekonto an Erlöskonto“ (``transfer``, mit Steuer), ohne Forderung
    der Einzelsatz „Erlöskonto an Bank“ (``purpose_first``, mit Steuer). Die Formen stehen fest, nicht aus der Kontenliste
    abgeleitet.

    ``parts``: nur für einen Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates``; sonst ``None`` – die Ausgabe ist dann
    Zeichen für Zeichen die von immer): je Satz ein :class:`PaymentPart` mit seinem Betrag (nie 0) und seiner Steuer; ihre Beträge ergeben
    ``amount`` (sonst ``ValueError``). Dann gibt es mit Forderung (``debtor_side``, jeder Teil mit seinem Wartekonto) „Bank an Debitoren“ über
    ``amount`` und je Teil „Wartekonto an Erlöskonto“, ohne Forderung je Teil „Erlöskonto an Bank“ – Konten, Betrag, Satz und Steuer je Teil.
    ``tax_rate``, ``tax_value``, ``revenue_coa`` und ``waiting_side`` gelten dann nicht."""
    def row(coa_id, counter_coa_id, form, text, taxed, description, row_amount=amount, row_rate=tax_rate, row_tax=tax_value):
        return PaymentRow({
            'DateBooking': bank_date, 'DateTax': bank_tax_date, 'RecipientClient': recipient or '',
            'Contact_ID': customer_id, 'COA_ID': coa_id, 'CounterCOA_ID': counter_coa_id, 'Amount': row_amount,
            'Currency': currency or 'EUR', 'TaxRate': row_rate if taxed else None,
            'TaxAmount': row_tax if taxed else None, 'Text': text, 'DocumentNumber': number,
            'ParentBooking_ID': bank_id, 'Status': PAYMENT_MARK, 'EntryForm': form}, description)

    # Eine Zahlung ist positiv, eine Erstattung negativ – der Text nennt, was es ist (die Zahlungssätze bleiben wortgleich)
    paid, moved = ('Zahlung', 'Umb.') if amount >= 0 else ('Erstattung', 'Umb. Erstattung')
    if parts is not None:
        parts = list(parts)
        if not parts or any(part.amount == 0 for part in parts) or sum(part.amount for part in parts) != amount:
            raise ValueError('Die Teile einer Zahlung haben je einen Betrag und ergeben zusammen ihren Betrag.')
        if debtor_side:
            if any(part.waiting is None for part in parts):
                raise ValueError('Mit Forderung braucht jeder Teil ein Wartekonto.')
            return [row(bank_coa, debtor_side, FORM_LIQUID_FIRST, f'{paid} zu Re. {number}', False,
                        'Payment child (bank to debtor)')] + [
                row(part.waiting, part.revenue, FORM_TRANSFER, f'{moved} zu Re. {number}', True,
                    'Payment child (open item to revenue)', part.amount, part.rate, part.tax) for part in parts]
        return [row(part.revenue, bank_coa, FORM_PURPOSE_FIRST, f'{paid} zu Re. {number}', True,
                    'Payment child (revenue to bank)', part.amount, part.rate, part.tax) for part in parts]
    if debtor_side and waiting_side:
        return [row(bank_coa, debtor_side, FORM_LIQUID_FIRST, f'{paid} zu Re. {number}', False,
                    'Payment child (bank to debtor)'),
                row(waiting_side, revenue_coa, FORM_TRANSFER, f'{moved} zu Re. {number}', True,
                    'Payment child (open item to revenue)')]
    return [row(revenue_coa, bank_coa, FORM_PURPOSE_FIRST, f'{paid} zu Re. {number}', True,
                'Payment child (revenue to bank)')]


# Warum eine Zuordnung beim Umzug auf die Ersatzrechnung an der stornierten Rechnung bleibt: Kennung → Klartext. In dieser
# Reihenfolge nennt der Hinweis die vorkommenden Gründe, je Grund einmal.
MOVE_SKIP_REASONS = {
    'import': 'Zuordnung aus einem Import',
    'unmarked': 'die Buchungssätze der Zahlung stammen aus einer älteren Version oder wurden von Hand angelegt',
    'no_skr': 'das Bankkonto hat keine SKR-Nummer',
    'overbooked': 'die Bankbewegung ist überbucht',
    'edited': 'die Buchungssätze der Zahlung wurden von Hand geändert oder ergänzt oder tragen einen Beleg',
    'tax_date': ('die Buchungssätze der Zahlung tragen ein anderes Steuerdatum als die Bankbewegung '
                 '(z. B. nach einem DATEV-Export)'),
    'revenue_account': 'das Erlöskonto der Zahlung ist nicht das für Steuersatz und Zahlungsdatum hinterlegte',
    'revenue_date': 'zum Datum der Bankbewegung lässt sich kein Erlöskonto bestimmen',
    'multiple': 'die Bankbewegung ist mehrfach zugeordnet',
    'no_incoming': 'keine Bankbewegung mit Zahlungseingang',
    'rebook': 'die Zuordnung ließ sich nicht neu buchen',
}

# Die stornierte Rechnung, wie der Umzug sie braucht: ID, Nummer, Brutto und Steuer (Minor Units), Steuersatz (wie in
# ``Invoices.TaxRate``) und Kunde
_Replaced = namedtuple('_Replaced', 'id number gross tax_total rate customer_id')

# Eine geprüfte und ausgerechnete Erstattung (``assign_storno_refund``): Nummer und Kunde der Stornorechnung, die stornierte
# Rechnung und ihr Steuersatz (wie in ``Invoices.TaxRate``), die Bankbewegung (Konto, Datum, Steuerdatum, Empfänger, Währung),
# der erstattete Betrag und seine Steuer (positiv, Minor Units; ``tax`` ist ``None`` ohne Steuersatz), das Erlöskonto der
# Zahlungen (``None``: keine Zuordnung mit markierten Sätzen, oder ``mixed``) und ``mixed``: Die markierten Sätze der Zahlungen
# liegen auf mehr als einem Erlöskonto – dann wählt der Nutzer das der Erstattung. ``parts``: nur bei einem Beleg mit Steuersatz je Position
# (``Invoices.MixedTaxRates``; sonst ``None``) die Teile der Erstattung je Satz – ein :class:`RefundPart` je Satz mit Betrag; ``amount`` ist dann
# die Summe der Teile und ``tax`` ihre Steuer, ``rate``, ``revenue_coa`` und ``mixed`` gelten nicht
_Refund = namedtuple('_Refund', 'number customer_id original_id rate account_id bank_date bank_tax_date recipient '
                                'currency amount tax revenue_coa mixed parts', defaults=(None,))
# Der Teil einer Erstattung, der auf einen Satz entfällt (alles positiv, Minor Units): der Satz als Prozentzahl, der Betrag, die darin enthaltene
# Steuer und das Erlöskonto (COA-ID), auf dem die Zahlungen dieses Satzes liegen
RefundPart = namedtuple('RefundPart', 'pct amount tax revenue')


def next_payment_status(current, gross, paid, due_date, today):
    """Status einer Rechnung, nachdem sich ihre Zahlungen geändert haben.

    ``gross``/``paid`` in Minor Units, ``due_date``/``today`` als ISO-Datum. Vorzeichenbewusst
    (die WISO-Gutschrift hat einen negativen Brutto- und negative Zahlungen):

    * ``cancelled`` bleibt ``cancelled`` (der Rest wird trotzdem neu gerechnet);
    * Zahlung in Höhe der Rechnung (Rest unter einem Cent) → ``paid``, sonst ``partial_payment``;
    * keine Zahlung mehr, Status bezahlt/Teilzahlung → ``overdue`` (Fälligkeit überschritten)
      bzw. ``sent`` – nie ``finalized``: ab „versendet“ ist die Rechnung eingefroren;
    * alles andere bleibt, wie es ist. Entwurf/abgeschlossen setzt diese Funktion nie.
    """
    if current == 'cancelled':
        return current
    sign = (gross > 0) - (gross < 0)
    if paid * sign > 0:
        return 'paid' if is_covered(gross, paid) else 'partial_payment'
    if current in ('paid', 'partial_payment', 'partial'):
        return 'overdue' if due_date and str(due_date)[:10] < today else 'sent'
    return current


class PaymentAssignment(namedtuple('PaymentAssignment',
                                   'ok message payment_id revenue_coa_id')):
    """Ergebnis von ``assign_invoice_payment`` und ``assign_storno_refund``: ob die Zuordnung
    zustande kam, bei einer Ablehnung der Klartext für die Oberfläche (sonst ``None``), die ID
    der Zuordnung und das Erlöskonto (COA-ID), auf das gebucht wurde."""
    __slots__ = ()


def _refused(message):
    return PaymentAssignment(False, message, None, None)


class PaymentMoveFailed(ValueError):
    """Der Umzug der Zahlungen einer stornierten Rechnung auf ihre Ersatzrechnung ist an etwas Unerwartetem gescheitert (ein
    Datenbankfehler mitten im Umzug); ``str(exc)`` ist der Klartext. ``Database.set_invoice_status`` macht daraus eine Ablehnung
    und rollt damit alles zurück, was der Wechsel in seiner Transaktion geschrieben hat (``db.invoice_lock._Refusal`` ist von
    hier aus nicht erreichbar: Zirkelimport). Eine Zuordnung, die sich nur nicht neu buchen lässt, ist dagegen erwartet: Sie
    bleibt an der stornierten Rechnung, und der Versand gelingt (``_move_replaced_payments``)."""


class PaymentRemovalRefused(ValueError):
    """Das Entfernen einer Zahlung wird abgelehnt, weil das Guthaben eines Storno-Paares sonst negativ würde (mehr erstattet
    als gezahlt – bei einem Paar mit Steuersatz je Position auch je Satz, ``MIXED_REMOVAL_REFUSED``); ``str(exc)`` ist der Klartext für die Oberfläche.
    Es betrifft jeden Weg, der eine Zahlung entfernt:
    ``unassign_invoice_payment``, ``remove_assignment_for_child`` und ``delete_transaction`` (Kindsatz wie Bankbewegung) –
    in der Transaktion geprüft, vor dem ersten Schreiben, nichts ist dann geändert."""


def _id_or_none(value):
    """Eine ID zum Vergleichen: leer ist ``None``, eine Zahl als Text dieselbe Zahl (``'3'`` = 3) – ein unverändert
    zurückgeschicktes Konto darf nie als Änderung gelten, nur weil ein Aufrufer es als Text übergibt."""
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class BankChangeRefused(ValueError):
    """Das Ändern von Betrag, Bankkonto oder Datum einer Bankbewegung wird abgelehnt, weil ihr eine Rechnungszahlung oder
    Erstattung zugeordnet ist (``Database.save_bank_with_rows``, ``_assigned_bank_change_blocker``); ``str(exc)`` ist der Klartext
    für die Oberfläche. Es wird nichts automatisch umgerechnet: Der Nutzer entfernt erst die Zuordnung. Die Transaktion ist
    zurückgerollt, nichts ist geändert."""


def payment_blocker(doc_type, status, gross, kind=None):
    """Warum sich auf diese Rechnung keine Zahlung zuordnen lässt – ``None``, wenn doch. ``kind`` ist
    ``Invoices.InvoiceKind``: Einer Stornorechnung wird nie eine Zahlung zugeordnet."""
    if (doc_type or 'invoice') != 'invoice':
        return 'Rechnung nicht gefunden'
    if kind == STORNO_KIND:
        return STORNO_PAYMENT_MESSAGE
    if status == 'paid':
        return 'Die Rechnung ist bereits bezahlt.'
    if status == 'cancelled':
        return 'Die Rechnung ist storniert.'
    if status not in PAYABLE_STATUSES:
        return ('Die Rechnung ist noch nicht versendet – Zahlungen lassen sich erst nach dem '
                'Versenden zuordnen.')
    if (gross or 0) <= 0:
        return ('Nur Rechnungen mit positivem Bruttobetrag lassen sich durch eine Zahlung '
                'ausgleichen.')
    return None


def booked_by_rate(allocations, rows_of, pcts, waiting_ids):
    """Was die Zuordnungen eines Belegs mit Steuersatz je Position je Satz tragen: ``{Satz: (Betrag, Steuer)}`` in Minor Units, mit Vorzeichen (eine
    Erstattung ist negativ) – der reine Kern von ``InvoicePaymentsMixin._mixed_booked``, der ihn für EINEN Beleg aus seinen Abfragen füttert; die Prüfliste
    füttert ihn für alle Paare aus je einer Abfrage (dieselbe Lesart, keine Abfrage je Beleg).

    ``allocations``: ``[(Bankbewegung, Summe der Zuordnungen des Belegs an dieser Bewegung)]``. ``rows_of``: ``{Bankbewegung: [(Form, COA, Betrag, Satz, Steuer)]}``
    – die markierten Sätze der App (``_payment_children``: Marke, Nummer des Belegs, keine Quellklammer) unter dieser Bewegung, in ID-Reihenfolge, die
    Werte wie gespeichert. Gezählt werden die Umbuchungen „Wartekonto an Erlöskonto“ (``transfer`` mit einem Wartekonto aus ``waiting_ids`` als ``COA``)
    und die Einzelsätze „Erlöskonto an Bank“ (``purpose_first``) – „Bank an Debitoren“ trägt keine Steuer und zählt nicht –, der Satz ist ``rate_pct`` ihres
    ``TaxRate`` (NULL heißt 0 %). ``pcts``: die Sätze des Belegs.

    ``MixedNotBookable`` mit ``MIXED_ALLOCATION_UNREADABLE``, wenn sich eine Zuordnung nicht so aufteilen lässt: keine markierten Sätze (Import, Altbestand,
    von Hand), ein Satz, den der Beleg nicht trägt, ein Betrag oder eine Steuer, die keine ganze Zahl ist, oder die gezählten Beträge ergeben nicht den
    Betrag der Zuordnung. Eine Zuordnung, die sich nicht aufteilen lässt, bräuchte eine Annahme – die App rät nicht."""
    booked = {}
    for bank_id, allocated in allocations:
        rows = rows_of.get(bank_id)
        if not rows or type(allocated) is not int:
            raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE)
        counted = 0
        for form, coa, amount, rate, tax in rows:
            if not ((form == FORM_TRANSFER and coa in waiting_ids) or form == FORM_PURPOSE_FIRST):
                continue
            try:
                pct = rate_pct(rate)
            except (ArithmeticError, ValueError):
                raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE) from None
            pct = Decimal(0) if pct is None else pct
            tax = 0 if tax is None else tax
            if pct not in pcts or type(amount) is not int or type(tax) is not int:
                raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE)
            counted += amount
            done_amount, done_tax = booked.get(pct, (0, 0))
            booked[pct] = (done_amount + amount, done_tax + tax)
        if counted != allocated:
            raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE)
    return booked


class InvoicePaymentsMixin:
    # ── Bausteine ────────────────────────────────────────────────────────────
    def _recalc_invoice(self, conn, invoice_id, adjust_status=True, today=None):
        """AmountDue (und, mit ``adjust_status``, der Status) aus den Zahlungen neu berechnen –
        in der Transaktion von ``conn``, ohne zu committen. Der Status einer Stornorechnung ändert sich
        dabei nie."""
        row = conn.execute('SELECT SumGross, Status, PaymentDueDate, InvoiceKind FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        gross, status, due_date, kind = row[0] or 0, row[1], row[2], row[3]
        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments '
                            'WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0]
        if adjust_status and kind != STORNO_KIND:
            status = next_payment_status(status, gross, paid, due_date,
                                         today or datetime.date.today().isoformat())
        conn.execute('UPDATE Invoices SET AmountDue = ?, Status = ?, UpdatedAt = CURRENT_TIMESTAMP '
                     'WHERE ID = ?', (gross - paid, status, invoice_id))

    @staticmethod
    def _bank_effective_sql(bank_coa_ids):
        """SQL-Bedingung (ohne ``AND``) und Parameter: Der Buchungssatz ``k`` ist bankwirksam – er berührt ein eigenes
        Bankkonto oder ist nicht ganz kontiert (``db.matching.is_bank_effective``). Die Bedingung für den freien Rest
        eines Zahlungseingangs (``_free_minor_sql``)."""
        marks = ','.join('?' * len(bank_coa_ids))
        return (f'(k.COA_ID IS NULL OR k.CounterCOA_ID IS NULL '
                f'OR k.COA_ID IN ({marks}) OR k.CounterCOA_ID IN ({marks}))'), list(bank_coa_ids) * 2

    @staticmethod
    def _free_minor_sql(bank_coa_ids, alias='b'):
        """SQL-Ausdruck (und Parameter) für den freien Rest einer Bankbewegung in Minor Units:

            Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Buchungssätze darunter)

        Die EINE Formel für Zuordnung, Kandidatenlisten und Kontakt-Guthaben. Der Rest kann
        negativ sein (überbucht). Bankwirksam: ``db.matching.is_bank_effective``. Für einen Ausgang gilt
        ``_outgoing_free_minor``."""
        effective, params = InvoicePaymentsMixin._bank_effective_sql(bank_coa_ids)
        expr = (f'{alias}.Amount - MAX('
                f'COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip '
                f'WHERE ip.BookingID = {alias}.ID), 0), '
                f'COALESCE((SELECT SUM(k.Amount) FROM Bookings k '
                f'WHERE k.ParentBooking_ID = {alias}.ID '
                f'AND {effective}), 0))')
        return expr, params

    def _booking_free_minor(self, conn, booking_id, bank_coa_ids):
        """Freier Rest einer Bankbewegung in Minor Units (siehe ``_free_minor_sql``)."""
        expr, params = self._free_minor_sql(bank_coa_ids)
        row = conn.execute(f'SELECT {expr} FROM Bookings b WHERE b.ID = ?',
                           params + [booking_id]).fetchone()
        return row[0] if row and row[0] is not None else 0

    def _outgoing_free_minor(self, conn, bank_coa_ids, booking_id=None, contact_id=None):
        """Die ausgehenden Bankbewegungen (Betrag < 0) mit ihrem freien Rest in Minor Units – der Stand der Verbindung ``conn``
        (in einer Transaktion also deren Stand): ``[(ID, DateBooking, RecipientClient, Contact_ID, Amount, Frei)]``, nach ID.
        ``Frei`` ist positiv; ≤ 0 heißt ausgeschöpft oder überbucht. ``booking_id`` und ``contact_id`` schränken auf diese
        Bewegung bzw. die Bewegungen dieses Kontakts ein. ``bank_coa_ids``: die eigenen Bankkonten.

        Frei ist ``|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze darunter)``. Die Wirkung eines Satzes auf das Konto der
        Bewegung ist die, mit der die Split-Summe rechnet (``db.matching.bank_effect``): ein Spiegel mit bekannter Lesart
        (``mirror_coa``/``mirror_counter``) wirkt nach seiner Position auf dem Konto der Bewegung – der Import-Spiegel „1810 an
        1800“ mit +50 unter einem Ausgang über 50 verbraucht ihn –, jeder andere Satz zählt mit seinem Betrag, wenn er ein
        eigenes Bankkonto berührt oder nicht ganz kontiert ist (``is_bank_effective``), sonst nicht. Anders als
        ``_free_minor_sql`` (Eingang), das einen Spiegel roh rechnet – bewusst: Der Ausgang einer Erstattung folgt der
        Split-Summe.

        Gerechnet wird in Python über zwei gesammelte Abfragen (Zuordnungen je Bewegung mit ``GROUP BY``, Sätze je Bewegung)
        und das Konto jeder Bewegung: Die Zahl der Anweisungen wächst nicht mit der Zahl der Bewegungen oder Zuordnungen."""
        where, params = "b.BookingType = 'bank' AND b.Amount < 0", []
        if booking_id is not None:
            where += ' AND b.ID = ?'
            params.append(booking_id)
        if contact_id is not None:
            where += ' AND b.Contact_ID = ?'
            params.append(contact_id)
        movements = conn.execute(
            f'SELECT b.ID, b.DateBooking, b.RecipientClient, b.Contact_ID, b.Amount, b.Account_ID FROM Bookings b '
            f'WHERE {where} ORDER BY b.ID', params).fetchall()
        allocated = dict(conn.execute(
            f'SELECT ip.BookingID, SUM(ip.Amount) FROM InvoicePayments ip JOIN Bookings b ON b.ID = ip.BookingID '
            f'WHERE {where} GROUP BY ip.BookingID', params).fetchall())
        rows_of = defaultdict(list)
        for parent, amount, coa_id, counter_coa_id, form in conn.execute(
                f'SELECT k.ParentBooking_ID, k.Amount, k.COA_ID, k.CounterCOA_ID, k.EntryForm FROM Bookings k '
                f'JOIN Bookings b ON b.ID = k.ParentBooking_ID WHERE {where}', params):
            rows_of[parent].append((form, amount, coa_id, counter_coa_id))
        account_coas = self._account_coa_sets(conn.cursor())
        result = []
        for ident, date, recipient, contact, amount, account_id in movements:
            parent_coas = account_coas.get(account_id)
            effect = sum(bank_effect(form, row_amount, coa_id, counter_coa_id, parent_coas, bank_coa_ids)
                         for form, row_amount, coa_id, counter_coa_id in rows_of.get(ident, ()))
            # nie mehr frei als der Betrag der Bewegung (eine positive Zuordnung unter einem Ausgang – die Bewegung wurde nach
            # der Zahlung umgedreht – machte ihn sonst „freier“, als er ist)
            free = min(-amount - max(-allocated.get(ident, 0), -effect), -amount)
            result.append((ident, date, recipient, contact, amount, free))
        return result

    def _booking_free_outgoing_minor(self, conn, booking_id, bank_coa_ids):
        """Freier Rest einer ausgehenden Bankbewegung in Minor Units, positiv (siehe ``_outgoing_free_minor``); 0, wenn die
        Buchung keine ausgehende Bankbewegung ist."""
        rows = self._outgoing_free_minor(conn, bank_coa_ids, booking_id=booking_id)
        return rows[0][5] if rows else 0

    @staticmethod
    def _booking_tax_rate(rate):
        """Der Steuersatz, den die Sätze einer Zuordnung tragen, aus ``Invoices.TaxRate``: Der steht teils als 0.19, teils
        als 19; -1 ist der Sentinel für „§ 19, keine USt“ (wie 0 oder kein Satz: ``None``)."""
        return None if not rate or rate < 0 else (rate if rate <= 1 else rate / 100)

    @staticmethod
    def _prorated_tax(tax_total, amount, gross):
        """Die Steuer eines Teils der Rechnung: anteilig am Betrag, auf den Cent gerundet (halb auf)."""
        return round_minor(prorate(tax_total, amount, gross), 2)

    def _allocations_tax(self, conn, invoice_id, number, gross, tax_total, except_booking=None):
        """Die Steuer, die die Zuordnungen einer Rechnung tragen: Hat eine markierte Sätze, zählt deren gebuchte Steuer (nur
        die Zahlungssätze tragen Steuer, nicht „Bank an Debitoren“); sonst (Altbestand, WISO-Import) ihr anteiliger Betrag –
        eine Suche über die Beleg-Nr. fände Sätze mit anderer oder fehlender Nummer nicht. ``except_booking``: eine
        Bankbewegung, deren Zuordnung nicht mitzählt. Die EINE Rechenstelle für den schließenden Teil einer Zahlung
        (``_remaining_tax``) und die Erstattung einer Stornorechnung (``assign_storno_refund``)."""
        skip, params = ((' AND BookingID <> ?', (invoice_id, except_booking)) if except_booking is not None
                        else ('', (invoice_id,)))
        taxed = 0
        for other_booking, other_amount in conn.execute(
                f'SELECT BookingID, SUM(Amount) FROM InvoicePayments WHERE InvoiceID = ?{skip} GROUP BY BookingID',
                params).fetchall():
            marked = self._payment_children(conn, other_booking, number)
            if marked:
                taxed += conn.execute(
                    f"SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE ID IN "
                    f"({','.join('?' * len(marked))})", marked).fetchone()[0]
            else:
                taxed += self._prorated_tax(tax_total, other_amount, gross)
        return taxed

    def _remaining_tax(self, conn, invoice_id, number, gross, tax_total, except_booking=None):
        """Die Steuer des Teils, der die Rechnung schließt: Rechnungssteuer minus die Steuer der anderen Zuordnungen
        (``_allocations_tax``). ``except_booking``: eine Bankbewegung, deren Zuordnung nicht mitzählt – so fragt der Umzug,
        was diese Zuordnung als schließender Teil bekommen hätte."""
        return tax_total - self._allocations_tax(conn, invoice_id, number, gross, tax_total, except_booking)

    @staticmethod
    def _payment_children(conn, bank_id, invoice_number):
        """IDs der Buchungssätze, die die App für die Zuordnung (Bankbewegung, Rechnung)
        angelegt hat: die markierten Kinder mit der Rechnungsnummer. Sätze mit Quellklammer
        (WISO) gehören nie dazu."""
        return [row[0] for row in conn.execute(
            "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? "
            "AND DocumentNumber = ? AND (SourceGroup IS NULL OR SourceGroup = '') ORDER BY ID",
            (bank_id, PAYMENT_MARK, invoice_number))]

    @staticmethod
    def _payment_revenue_coa(conn, child_ids):
        """Das Erlöskonto (COA-ID), auf das die Sätze ``child_ids`` einer Zuordnung gebucht sind – in der Form, in
        der die App sie schreibt: das Gegenkonto der Umbuchung „Wartekonto an Erlöskonto“, sonst das Konto des
        Einzelsatzes „Erlöskonto an Bank“. ``None``, wenn die Sätze keine der beiden Formen haben."""
        if not child_ids:
            return None
        rows = conn.execute(
            f"SELECT EntryForm, COA_ID, CounterCOA_ID FROM Bookings WHERE ID IN ({','.join('?' * len(child_ids))}) "
            "ORDER BY ID", list(child_ids)).fetchall()
        for form, _coa, counter in rows:
            if form == FORM_TRANSFER:
                return counter
        for form, coa, _counter in rows:
            if form == FORM_PURPOSE_FIRST:
                return coa
        return None

    # ── Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates``): Gruppen, Forderung und Zahlungen je Satz ──────
    @staticmethod
    def _mixed_groups(conn, invoice_id):
        """Die Satzgruppen eines Belegs mit Steuersatz je Position (``tax_groups.mixed_groups``: nach Satz absteigend), gelesen aus seinen
        Summen und Positionen in der Verbindung oder Transaktion von ``conn`` (kein ``BEGIN``). ``RateGroupsMismatch``, wenn die Positionen
        nicht zu den Summen passen – auch, wenn es den Beleg nicht gibt. Statisch: Der Schutz beim Entfernen einer Zahlung
        (``_refund_removal_blocker``) liest sie ohne Instanz."""
        head = conn.execute('SELECT SumNet, TaxAmount, SumGross FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
        if head is None:
            raise RateGroupsMismatch('Den Beleg gibt es nicht.')
        # Der Satz in Hundertstel Prozent (``TaxRateBp``, die Momentaufnahme eines Eintrags der Stammdaten; sonst NULL) geht mit: Er bestimmt den Satz der Position
        # und lässt ihre Gruppe einstufig runden (``tax_groups.group_tax``) – sonst rechnete eine 5,5-%-Gruppe zweistufig und passte nicht zu den Summen
        pairs = conn.execute('SELECT TaxRate, TotalNet, TaxRateBp FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID',
                             (invoice_id,)).fetchall()
        return mixed_groups(*head, pairs)

    def _checked_mixed_groups(self, conn, invoice_id):
        """Die Satzgruppen (``_mixed_groups``), zum Buchen geprüft: Die Momentaufnahmen der Positionen widersprechen sich nicht (``group_snapshots``) und jede Gruppe hat
        einen positiven Betrag – eine Forderungszeile auf 10000 mit Betrag 0 oder darunter wäre nach ``receivable_condition`` keine Forderung.
        ``MixedNotBookable`` mit dem festen Klartext: ``TAX_ENTRY_CONTRADICTION`` bei einem Widerspruch der Momentaufnahmen, ``MIXED_GROUPS_MISMATCH``, wenn die Positionen nicht zu
        den Summen passen, bzw. ``MIXED_GROUP_NOT_POSITIVE`` mit dem ersten Satz, dessen Positionen zusammen nicht positiv sind (eine steuerfreie Gruppe heißt „steuerfreie
        Leistungen“, ``rate_sentence``); der Grund im Detail steht im Server-Log. Die Momentaufnahmen kommen zuerst: Sie prüfen auch den Satz in Hundertstel Prozent jeder
        Position gegen ihren Steuersatz, den ``_mixed_groups`` sonst als „Positionen passen nicht zu den Summen“ meldete – das ist kein Fehler der Summen. Ein Beleg ohne
        Momentaufnahme meldet wie immer."""
        snapshots = self.group_snapshots(conn, invoice_id)
        try:
            groups = self._mixed_groups(conn, invoice_id)
        except RateGroupsMismatch as exc:
            print(f'Rechnung {invoice_id}: {exc}')
            raise MixedNotBookable(MIXED_GROUPS_MISMATCH) from None
        for group in groups:
            if group.gross <= 0:
                snapshot = snapshots.get(group.pct)
                raise MixedNotBookable(rate_sentence(MIXED_GROUP_NOT_POSITIVE, group.pct, bool(snapshot and snapshot.exempt),
                                                     amount=fmt_eur(from_minor(group.gross))))
        return groups

    # ── Momentaufnahme eines Steuersatzes aus den Stammdaten (2.8.0): je Satzgruppe, und für den Beleg mit EINEM Satz ────────
    @staticmethod
    def _read_snapshots(conn, invoice_id):
        """Die Momentaufnahmen der Satzgruppen eines Belegs aus seinen Positionen: ``{Satz: Snapshot}`` – der Satz als Prozentzahl (``tax_groups.bp_pct``, wie
        ``RateGroup.pct``), nach Satz absteigend –, nur für Gruppen, deren Positionen eine tragen (``InvoiceItems.TaxRateBp`` nicht NULL; **das** ist das
        Merkmal: Die Art ``'E'`` allein ist keines – das Beispiel eines Belegs nach § 19 trägt sie ohne Satz in Hundertstel Prozent, und das ist kein Widerspruch). Ein Beleg
        ohne eine solche Position – und ohne die halbe Momentaufnahme (s. u.) – liefert ``{}`` (nichts wird geprüft), und eine Gruppe ohne Momentaufnahme fehlt im Ergebnis: Für
        sie gelten die bisherigen Regeln.

        ``_SnapshotConflict`` (mit einem Satz für das Server-Log), wenn sich die Angaben widersprechen. Jeder dieser Fälle ist beim Buchen ein Widerspruch
        (``TAX_ENTRY_CONTRADICTION``) und in der Prüfliste ein Befund (``_tax_entry_findings`` liest dieselben Regeln):

        * der Satz in Hundertstel Prozent ist keiner (``valid_rate_bp``: eine ganze Zahl, die 0 ist oder von 100 bis 10000 reicht – ein Wert von 1 bis 99 gilt hier nicht als
          „keine Momentaufnahme“, wie im Dokument, sondern ist ein Widerspruch);
        * er ist nicht der Satz der Position (``rate_pct`` ihres ``TaxRate`` mal 100) – auch bei einem Beleg mit EINEM Satz (beim Beleg mit Steuersatz je Position prüft
          ``tax_groups.mixed_groups`` es ebenfalls, ``_checked_mixed_groups`` fragt aber zuerst hier);
        * eine steuerfreie Position (Art ``'E'``) hat einen Satz über 0;
        * Positionen EINER Gruppe tragen verschiedene Angaben (Art, Befreiungsgrund, Erlöskonto) oder teils eine Momentaufnahme, teils keine;
        * die **halbe Momentaufnahme**: eine Position ohne Satz in Hundertstel Prozent, aber mit Befreiungsgrund oder Erlöskonto (``IS NOT NULL``) – Angaben aus den
          Stammdaten ohne den Satz dazu.

        Das kann kein Weg der App schreiben (``handlers._tax_fields``: je Satz höchstens ein Eintrag der Stammdaten) – es heißt, dass jemand an der App vorbei etwas
        geändert hat. Statisch: Der Schutz beim Entfernen einer Zahlung und die Texte der Prüfliste lesen sie ohne Instanz."""
        rows = conn.execute(
            'SELECT TaxRate, TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID',
            (invoice_id,)).fetchall()
        if all(row[2] is None and row[3] is None and row[4] is None for row in rows):
            return {}
        found, plain = {}, set()
        for number, (rate, category, bp, reason, coa) in enumerate(rows, 1):
            try:
                position_pct = rate_pct(rate)
            except (ArithmeticError, ValueError):
                position_pct = None
            if bp is None:
                if reason is not None or coa is not None:
                    raise _SnapshotConflict(f'Position {number}: Angaben aus den Stammdaten (Befreiungsgrund oder Erlöskonto) ohne den Satz in Hundertstel Prozent dazu.')
                if position_pct is not None:
                    plain.add(position_pct)
                continue
            if not valid_rate_bp(bp):
                raise _SnapshotConflict(f'Position {number}: Der Satz in Hundertstel Prozent ist keine ganze Zahl, die 0 ist oder von {MIN_RATE_BP} bis {MAX_RATE_BP} reicht.')
            if position_pct is None or bp != position_pct * 100:
                raise _SnapshotConflict(f'Position {number}: Der Satz in Hundertstel Prozent ({bp}) ist nicht der Steuersatz der Position.')
            if category == 'E' and bp != 0:
                raise _SnapshotConflict(f'Position {number}: Eine steuerfreie Position hat den Satz 0, hier {bp} Hundertstel Prozent.')
            found.setdefault(bp_pct(bp), set()).add(Snapshot(category == 'E', reason, coa))
        for pct, shots in found.items():
            if len(shots) > 1:
                raise _SnapshotConflict(f'Die Positionen mit {group_label(pct, False)} tragen verschiedene Angaben aus den Stammdaten.')
            if pct in plain:
                raise _SnapshotConflict(f'Die Positionen mit {group_label(pct, False)} tragen teils eine Momentaufnahme der Stammdaten, teils keine.')
        return {pct: next(iter(found[pct])) for pct in sorted(found, reverse=True)}

    @staticmethod
    def group_snapshots(conn, invoice_id):
        """Die Momentaufnahme je Satzgruppe eines Belegs (``_read_snapshots``: ``{Satz: Snapshot}``, ``{}`` ohne Momentaufnahme), gelesen in der Verbindung
        oder Transaktion von ``conn`` (kein ``BEGIN``). ``MixedNotBookable`` mit ``TAX_ENTRY_CONTRADICTION``, wenn sich die Angaben widersprechen – der Grund
        steht im Server-Log.

        **Wer bucht, geht darüber:** Versenden (auch das einer Ersatzrechnung, mit dem Umzug der Zahlungen), die Zahlung und das Stornieren
        (``_checked_mixed_groups`` für den Beleg mit Steuersatz je Position, ``document_entry`` für den mit einem Satz) – ein Beleg mit widersprüchlichen Angaben lässt sich
        nicht buchen, vor dem ersten Schreiben. **Die Erstattung prüft die Momentaufnahme nicht, bewusst:** Sie folgt den Sätzen der Zahlungen (je Satz Betrag, Steuer und das Erlöskonto,
        auf dem die Zahlungen liegen), denn ein Guthaben muss sich auch aus einem widersprüchlichen Stand erstatten lassen – die Prüfliste nennt den Widerspruch trotzdem. Das
        Entfernen einer Zahlung liest sie nur für die Texte (``_exempt_pcts``). Ein Fall bleibt: Nennen die Zahlungssätze eines Belegs mit EINEM Satz kein Erlöskonto (Import ohne
        markierte Sätze, oder eines, das keines (mehr) ist), fragt die Erstattung das Konto der Momentaufnahme (``_document_revenue``) – dort lehnt ein Widerspruch ab."""
        try:
            return InvoicePaymentsMixin._read_snapshots(conn, invoice_id)
        except _SnapshotConflict as exc:
            print(f'Rechnung {invoice_id}: {exc}')
            raise MixedNotBookable(TAX_ENTRY_CONTRADICTION) from None

    @staticmethod
    def _exempt_pcts(conn, invoice_id):
        """Die Sätze (Prozentzahlen) der steuerfreien Gruppen eines Belegs – für die Texte der Meldungen („steuerfreie Leistungen“ statt „0 %“, ``rate_sentence``). Leer ohne
        Momentaufnahme und, still, wenn sich die Angaben widersprechen (der Text einer Meldung darf nicht an etwas scheitern, das sie erklärt)."""
        try:
            return {pct for pct, snapshot in InvoicePaymentsMixin._read_snapshots(conn, invoice_id).items() if snapshot.exempt}
        except _SnapshotConflict:
            return set()

    @staticmethod
    def _single_snapshot(conn, invoice_id, head_rate):
        """Die Momentaufnahme eines Belegs mit EINEM Satz (``Invoices.MixedTaxRates`` nicht gesetzt) als ``(Satz, Snapshot)`` – ``None`` ohne eine
        (der ganze Bestand: Es gelten die bisherigen Regeln). Der Beleg hat nur eine Satzgruppe, und sie trägt den Satz im Kopf (``head_rate``: ``Invoices.TaxRate``);
        alles andere – mehr als eine Gruppe mit Momentaufnahme, ein Kopfsatz, der nicht der Satz der Momentaufnahme ist (auch der Platzhalter −1 für „kein
        Steuerausweis“) – ist ein Widerspruch: ``MixedNotBookable`` mit ``TAX_ENTRY_CONTRADICTION`` (wie jeder Widerspruch der Momentaufnahme, ``group_snapshots``), der Grund im
        Server-Log."""
        snapshots = InvoicePaymentsMixin.group_snapshots(conn, invoice_id)
        if not snapshots:
            return None
        try:
            head = rate_pct(head_rate)
        except (ArithmeticError, ValueError):
            head = None
        if len(snapshots) != 1 or head not in snapshots:
            print(f'Rechnung {invoice_id}: Der Satz im Kopf passt nicht zum Satz der Momentaufnahme der Stammdaten.')
            raise MixedNotBookable(TAX_ENTRY_CONTRADICTION)
        (pct, snapshot), = snapshots.items()
        return pct, snapshot

    def document_entry(self, invoice_id, head_rate):
        """Die Momentaufnahme eines Belegs mit EINEM Satz (``_single_snapshot``) in einer eigenen Verbindung: ``(Satz, Snapshot)`` oder ``None``.
        ``head_rate``: ``Invoices.TaxRate`` des Belegs. ``MixedNotBookable`` (``TAX_ENTRY_CONTRADICTION``) bei einem Widerspruch."""
        conn = self._get_connection()
        try:
            return self._single_snapshot(conn, invoice_id, head_rate)
        finally:
            conn.close()

    @staticmethod
    def _entry_accounts(conn, invoice_id):
        """Trägt eine Position des Belegs ein Erlöskonto aus den Stammdaten (``InvoiceItems.RevenueCoaId`` nicht NULL), in der Verbindung oder Transaktion von
        ``conn``? Das ist die Bedingung, an der der Umzug der Zahlungen, der Neuaufbau der Zuordnung, das Lernen des Erlöskontos und die Wahl des Erlöskontos in den
        Masken hängen: Das Konto gilt, ohne Wahl und ohne Umzug."""
        return conn.execute('SELECT 1 FROM InvoiceItems WHERE InvoiceId = ? AND RevenueCoaId IS NOT NULL LIMIT 1', (invoice_id,)).fetchone() is not None

    @staticmethod
    def _carries_snapshot(conn, invoice_id):
        """Trägt eine Position des Belegs die Momentaufnahme eines Steuersatzes aus den Stammdaten (``InvoiceItems.TaxRateBp`` nicht NULL – das Merkmal, ``_read_snapshots``), in
        der Verbindung oder Transaktion von ``conn``? Nur die Frage, ob es eine gibt: Sie prüft nichts und wirft nie – die Erstattung, die sie braucht (``_refund_revenue_coas_by_rate``),
        liest die Momentaufnahme nicht."""
        return conn.execute('SELECT 1 FROM InvoiceItems WHERE InvoiceId = ? AND TaxRateBp IS NOT NULL LIMIT 1', (invoice_id,)).fetchone() is not None

    def invoice_entry_accounts(self, invoice_id):
        """Trägt der Beleg ein Erlöskonto aus den Stammdaten (``_entry_accounts``)? In einer eigenen Verbindung, rein lesend – für die Masken: Dann gibt es dort
        keine Wahl des Erlöskontos."""
        conn = self._get_connection()
        try:
            return self._entry_accounts(conn, invoice_id)
        finally:
            conn.close()

    def _receivable_sides_by_rate(self, conn, number):
        """``(Debitorenkonto, {Satz: Wartekonto})`` der Forderungen zu dieser Rechnungsnummer – die Konten, auf die die Teile einer Zuordnung
        „Bank an Debitoren“ und je Satz „Wartekonto an Erlöskonto“ buchen –, ``(None, {})`` ohne Forderung (dann ist es je Satz der Einzelsatz
        „Erlöskonto an Bank“). Wie ``_receivable_sides`` (``receivable_condition``), nur alle Zeilen der Nummer: Ein Beleg mit Steuersatz je Position
        bucht je Satz eine. Der Satz einer Zeile ist ``rate_pct`` ihres ``TaxRate`` (NULL heißt 0 %). ``MixedNotBookable`` mit
        ``MIXED_RECEIVABLE_MISMATCH``, wenn zwei Zeilen denselben Satz oder verschiedene Debitorenkonten tragen (oder ein Satz sich nicht lesen
        lässt)."""
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        rows = conn.execute(
            f"SELECT COA_ID, CounterCOA_ID, TaxRate FROM Bookings WHERE DocumentNumber = ? "
            f"AND BookingType = 'entry' AND {receivable_sql} ORDER BY ID", [number] + receivable_params).fetchall()
        debtors = self._debtor_coa_ids(conn)
        debtor, waiting = None, {}
        for coa, counter, rate in rows:
            # Normalfall „10000 an Wartekonto“; negativ gebucht steht 10000 als Gegenkonto
            row_debtor, row_waiting = (coa, counter) if coa in debtors else (counter, coa)
            try:
                pct = rate_pct(rate)
            except (ArithmeticError, ValueError):
                raise MixedNotBookable(MIXED_RECEIVABLE_MISMATCH) from None
            pct = Decimal(0) if pct is None else pct
            if (debtor is not None and row_debtor != debtor) or pct in waiting:
                raise MixedNotBookable(MIXED_RECEIVABLE_MISMATCH)
            debtor, waiting[pct] = row_debtor, row_waiting
        return debtor, waiting

    @classmethod
    def _mixed_booked(cls, conn, invoice_id, number, pcts, except_booking=None, except_payments=()):
        """Was die Zuordnungen einer Rechnung mit Steuersatz je Position je Satz tragen: ``{Satz: (Betrag, Steuer)}`` in Minor Units, mit Vorzeichen
        (eine Erstattung ist negativ). Gelesen werden die markierten Sätze jeder Zuordnung (``_payment_children``) und mit ``booked_by_rate`` gezählt – die
        EINE Lesart, auch die der Prüfliste. Wie ``_allocations_tax`` (``except_booking``: eine Bankbewegung, deren Zuordnung nicht mitzählt), nur je Satz.
        ``pcts``: die Sätze des Belegs. ``except_payments``: Zuordnungen (``InvoicePayments.ID``), die nicht mitzählen – so fragt der Schutz beim Entfernen
        einer Zahlung, wie der Stand danach wäre (``_refund_removal_blocker``). Eine Klassenmethode, aus demselben Grund.

        ``MixedNotBookable`` mit ``MIXED_ALLOCATION_UNREADABLE``, wenn sich eine Zuordnung nicht so aufteilen lässt (siehe ``booked_by_rate``). Eine Zuordnung,
        die sich nicht aufteilen lässt, bräuchte eine Annahme – die App rät nicht."""
        skip, params = '', [invoice_id]
        if except_booking is not None:
            skip, params = ' AND BookingID <> ?', [invoice_id, except_booking]
        if except_payments:
            skip += f" AND ID NOT IN ({','.join('?' * len(except_payments))})"
            params = [*params, *except_payments]
        allocations = conn.execute(
            f'SELECT BookingID, SUM(Amount) FROM InvoicePayments WHERE InvoiceID = ?{skip} GROUP BY BookingID', params).fetchall()
        rows_of = {}
        for bank_id, _allocated in allocations:
            marked = cls._payment_children(conn, bank_id, number)
            rows_of[bank_id] = conn.execute(
                f"SELECT EntryForm, COA_ID, Amount, TaxRate, TaxAmount FROM Bookings WHERE ID IN ({','.join('?' * len(marked))}) ORDER BY ID",
                marked).fetchall() if marked else []
        return booked_by_rate(allocations, rows_of, pcts, cls._waiting_coa_ids(conn))

    def _bank_coa_of(self, account_id, what='Zahlung'):
        """Das SKR-Konto (COA-ID) des Bankkontos einer Bewegung: ``(Fehlertext, None)`` oder ``(None, COA-ID)``. Ohne
        SKR-Nummer lässt sich nichts kontieren. ``what``: das Wort in der Meldung ('Zahlung' oder 'Erstattung')."""
        account = self.get_account_by_id(account_id) if account_id else None
        if account is None:
            return f'Für die {what} fehlt das Bankkonto.', None
        if not account[7]:
            return ('Das Bankkonto hat kein Konto im Kontenrahmen – bitte unter Stammdaten → Bankkonten eines wählen (üblich: 1800), '
                    f'sonst lässt sich die {what} nicht kontieren.'), None
        bank_coa = self.get_coa_id_by_account_number(account[7])
        if bank_coa is None:
            return (f'Das Bankkonto trägt die Nummer {account[7]}, die nicht im Kontenrahmen steht – bitte unter Stammdaten → Bankkonten '
                    f'ein Konto aus dem Kontenrahmen wählen (üblich: 1800), sonst lässt sich die {what} nicht kontieren.'), None
        return None, bank_coa

    def _payment_prerequisites(self, invoice_id, account_id, revenue_coa_id=None, on=None):
        """Was vor einer Zuordnung geklärt sein muss, in eigenen Verbindungen: Beim Auflösen
        des Erlöskontos werden bei Bedarf Konten angelegt – das darf nicht innerhalb von
        ``BEGIN IMMEDIATE`` geschehen (ein zweiter Schreiber sperrte sich selbst).

        Das Erlöskonto ergibt sich aus der Rechnung (``resolve_revenue_coa``); ``revenue_coa_id``
        zählt nur, wenn dafür keines hinterlegt ist, und muss dann ein Erlöskonto sein.

        **``on``: das Datum der Bankbewegung** (ein ISO-Datum; die Zuordnung nennt das der Bewegung, die Maske das Datumsfeld des Formulars). Bei einem Beleg ohne
        Steuerausweis (§ 19, ``uses_small_business_rule``) hängt das Erlöskonto daran: 4185 bis 2025, 4184 ab 2026 (``small_business_revenue_number``). Ist ``on`` genannt
        und kein Datum, lehnt ``REVENUE_DATE_UNREADABLE`` ab – vor dem Auflösen, es entsteht kein Konto. Ohne ``on`` (nur Tests) gilt heute (``_revenue_today``). Der
        Kontext trägt dann ``revenue_date_number``, das Konto, für das die Vorprüfung aufgelöst hat: ``_assign_in_tx`` lehnt ab, wenn das Datum der Bewegung in der
        Transaktion zu einem anderen gehört.

        **Ein Beleg mit Steuersatz je Position** (``Invoices.MixedTaxRates``) hat kein EINES Erlöskonto, und der Nutzer wählt keines: Seine
        Satzgruppen (``_checked_mixed_groups``) werden gelesen – ``MixedNotBookable`` (ein fester Satz) lehnt ab, wenn sie sich nicht buchen
        lassen –, und je Gruppe das Erlöskonto ihres Satzes (``_group_revenue_coa``); fehlt eines, lehnt ``MIXED_NO_REVENUE_COA`` ab (``revenue_coa_id``
        gilt dort nie). Der Kontext trägt dann ``mixed`` = ``{'groups': (RateGroup, …), 'revenue': {Satz: COA-ID}}`` und ``revenue_coa`` ``None``;
        ``_assign_in_tx`` bucht nur mit diesem Schlüssel je Satz (ein Kontext ohne ihn – Neuaufbau, Umzug – lehnt einen solchen Beleg ab).

        **Ein Steuersatz aus den Stammdaten** (2.8.0, die Momentaufnahme an den Positionen): Trägt die Satzgruppe – bei einem Beleg mit einem Satz der Beleg – ein
        Erlöskonto (``InvoiceItems.RevenueCoaId``), gilt dieses Konto, immer (``_group_revenue_coa``, ``_document_revenue``); ist es keines (mehr), lehnt
        ``TAX_PAYMENT_NO_REVENUE`` ab, eine Wahl gibt es nicht. Ohne Momentaufnahme – und mit einer ohne Erlöskonto, den 19 und 7 % der Vorbelegung – bleibt jede Regel
        von heute.

        Returns ``(Fehlertext, None)`` oder ``(None, Kontext)``."""
        invoice = self.get_document(invoice_id, 'invoice')
        if invoice is None:
            return 'Rechnung nicht gefunden', None
        # Spalten der Rechnungszeile: 38 = SumGross, 40 = Status, 45 = DocumentType, 50 = InvoiceKind, 55 = MixedTaxRates
        blocked = payment_blocker(invoice[45], invoice[40], invoice[38],
                                  invoice[50] if len(invoice) > 50 else None)
        if blocked:
            return blocked, None
        mixed = has_mixed_rates(invoice[55] if len(invoice) > 55 else None)
        groups = snapshots = None
        if mixed:
            conn = self._get_connection()
            try:
                groups = self._checked_mixed_groups(conn, invoice_id)
                snapshots = self.group_snapshots(conn, invoice_id)
            except MixedNotBookable as exc:
                return str(exc), None
            finally:
                conn.close()
        error, bank_coa = self._bank_coa_of(account_id)
        if error:
            return error, None
        if mixed:
            revenue = {}
            for group in groups:
                snapshot = snapshots.get(group.pct)
                coa = self._group_revenue_coa(group.pct, snapshot)
                if coa is None:
                    if snapshot is not None and snapshot.revenue_coa_id is not None:
                        return rate_sentence(TAX_PAYMENT_NO_REVENUE, group.pct, snapshot.exempt), None
                    return rate_sentence(MIXED_NO_REVENUE_COA, group.pct, bool(snapshot and snapshot.exempt)), None
                revenue[group.pct] = coa
            return None, {
                'tax_rate': invoice[35], 'account_id': account_id, 'bank_coa': bank_coa, 'revenue_coa': None,
                'bank_coa_ids': self.get_bank_coa_ids(), 'mixed': {'groups': tuple(groups), 'revenue': revenue},
            }
        by_date = on is not None and uses_small_business_rule(invoice[35])
        if by_date and small_business_revenue_number(on) is None and not self.invoice_entry_accounts(invoice_id):
            return REVENUE_DATE_UNREADABLE, None
        revenue_coa, error = self._document_revenue(invoice_id, invoice[35], revenue_coa_id, on=on)
        if error:
            return error, None
        context = {
            'tax_rate': invoice[35], 'account_id': account_id, 'bank_coa': bank_coa,
            'revenue_coa': revenue_coa,
            'bank_coa_ids': self.get_bank_coa_ids(),
        }
        if by_date:
            context['revenue_date_number'] = small_business_revenue_number(on)
        return None, context

    def payment_precheck(self, invoice_id, account_id, revenue_coa_id=None, on=None):
        """Fehlertext, wenn sich auf diese Rechnung über dieses Bankkonto keine Zahlung zuordnen
        lässt – sonst ``None``. Für Masken, die die Bankbewegung erst noch anlegen: Wer prüft,
        bevor er schreibt, hinterlässt keine Bewegung ohne Zuordnung. ``revenue_coa_id`` wie
        bei ``assign_invoice_payment``, ``on``: das Datum der neuen Bankbewegung (``_payment_prerequisites``)."""
        return self._payment_prerequisites(invoice_id, account_id, revenue_coa_id, on)[0]

    # ── Erlöskonto: Wahl, wenn keines hinterlegt ist ────────────────────────
    @staticmethod
    def _income_coas(cursor):
        """Die Erlöskonten 4000–4999 ohne die Wartekonten (``db.reporting.is_income_account``)
        als ``[(COA_ID, Kontonummer, Name)]`` nach Kontonummer."""
        rows = cursor.execute(
            'SELECT ID, AccountNumber, Name FROM ChartOfAccounts '
            'WHERE AccountNumber BETWEEN ? AND ? ORDER BY AccountNumber, ID',
            REVENUE_ACCOUNT_RANGE).fetchall()
        return [row for row in rows if is_income_account(row[1])]

    def get_revenue_coas(self):
        """Die Konten, aus denen der Nutzer das Erlöskonto einer Zahlung wählen kann: Erlöskonten
        4000–4999 (``REVENUE_ACCOUNT_RANGE``) ohne die Wartekonten, die keine Erlöse sind
        (``db.reporting.is_income_account``). Returns ``[(COA_ID, Kontonummer, Name)]`` nach
        Kontonummer."""
        conn = self._get_connection()
        try:
            return self._income_coas(conn.cursor())
        finally:
            conn.close()

    def _chosen_revenue_coa(self, revenue_coa_id):
        """Das vom Nutzer gewählte Erlöskonto, geprüft: ``(COA-ID, None)`` oder
        ``(None, Fehlertext)``. Gilt nur, wenn ``resolve_revenue_coa`` keines liefert."""
        if revenue_coa_id is None or revenue_coa_id == '':
            return None, NO_REVENUE_COA
        try:
            chosen = int(revenue_coa_id)
        except (TypeError, ValueError):
            return None, NOT_A_REVENUE_COA
        if chosen not in {row[0] for row in self.get_revenue_coas()}:
            return None, NOT_A_REVENUE_COA
        return chosen, None

    def _document_revenue(self, invoice_id, tax_rate, revenue_coa_id=None, on=None):
        """Das Erlöskonto eines Belegs mit EINEM Satz für eine Zahlung oder Erstattung: ``(COA-ID, None)`` oder ``(None, Fehlertext)``. ``tax_rate``:
        ``Invoices.TaxRate``, ``revenue_coa_id``: die Wahl des Nutzers, ``on``: das Datum der Bankbewegung (bei einem Beleg ohne Steuerausweis hängt das Erlöskonto
        daran – 4185 bis 2025, 4184 ab 2026, ``resolve_revenue_coa``; der Aufrufer prüft vorher, dass es ein Datum ist: ``REVENUE_DATE_UNREADABLE``).

        Trägt der Beleg die Momentaufnahme eines Steuersatzes aus den Stammdaten (``document_entry``) und ist dort ein Erlöskonto eingetragen – oder der Satz 0,
        der nie ein Standardkonto hat und nie 4184/4185 bekommt (``_entry_binds``) –, gilt das Konto der Momentaufnahme (``document_revenue_coa``), immer:
        ``TAX_PAYMENT_NO_REVENUE``, wenn es keines (mehr) ist, eine Wahl des Nutzers gibt es nicht. Sonst (ohne Momentaufnahme, und mit einer ohne Erlöskonto)
        ist es wie immer: das hinterlegte oder gelernte Erlöskonto des Satzes (``resolve_revenue_coa``), und fehlt das, die Wahl des Nutzers
        (``_chosen_revenue_coa``). ``MixedNotBookable`` der Momentaufnahme wird zum Fehlertext."""
        try:
            revenue_coa = self.document_revenue_coa(invoice_id, tax_rate, on=on)
            if revenue_coa is not None:
                return revenue_coa, None
            entry = self.document_entry(invoice_id, tax_rate)
        except MixedNotBookable as exc:
            return None, str(exc)
        if self._entry_binds(entry):
            return None, rate_sentence(TAX_PAYMENT_NO_REVENUE, entry[0], entry[1].exempt)
        return self._chosen_revenue_coa(revenue_coa_id)

    def revenue_coa_choice_needed(self, tax_rate):
        """Muss der Nutzer das Erlöskonto wählen, weil für diesen Steuersatz keines hinterlegt
        ist? Steuerfrei/§ 19 nie: Das Konto (4185 bis 2025, 4184 ab 2026) legt die Zuordnung bei Bedarf selbst an."""
        if self._revenue_rate_key(tax_rate) == 0:
            return False
        return self.resolve_revenue_coa(tax_rate, create=False) is None

    def suggest_revenue_coa(self, tax_rate):
        """Vorauswahl für die Kontenwahl – nur das, nie eine stille Entscheidung: das Erlöskonto
        (``get_revenue_coas``), das unter den Buchungen mit diesem Steuersatz am häufigsten als
        Konto oder Gegenkonto vorkommt (derselbe Vergleich des Satzes wie in
        ``resolve_revenue_coa``), bei Gleichstand das mit der kleinsten Kontonummer. Gibt es
        keinen Treffer: ``None``. Rein lesend."""
        revenue = {row[0]: row[1] for row in self.get_revenue_coas()}
        if not revenue:
            return None
        key = self._revenue_rate_key(tax_rate)
        rate_sql = self._revenue_rate_key_sql('TaxRate')
        marks = ','.join('?' * len(revenue))
        conn = self._get_connection()
        try:
            counts = conn.execute(
                f'SELECT coa, COUNT(*) FROM ('
                f'SELECT COA_ID AS coa FROM Bookings WHERE {rate_sql} = ? AND COA_ID IN ({marks}) '
                f'UNION ALL '
                f'SELECT CounterCOA_ID FROM Bookings WHERE {rate_sql} = ? '
                f'AND CounterCOA_ID IN ({marks})) GROUP BY coa',
                [key, *revenue, key, *revenue]).fetchall()
        finally:
            conn.close()
        if not counts:
            return None
        return min(counts, key=lambda row: (-row[1], revenue[row[0]]))[0]

    # ── Zuordnen ─────────────────────────────────────────────────────────────
    def assign_invoice_payment(self, invoice_id, booking_id, amount=None, revenue_coa_id=None):
        """Eine Bankbewegung (ganz oder teilweise) einer Rechnung als Zahlung zuordnen.

        Der Betrag ist min(Rest der Rechnung, freier Rest der Bewegung, ``amount``);
        ``amount=None`` heißt „so viel wie möglich“. Eine Überzahlung bleibt als freier Rest
        der Bewegung stehen (Guthaben beim Kunden), eine Sammelzahlung sind mehrere Zuordnungen
        derselben Bewegung, eine Teilzahlung mehrere Bewegungen auf eine Rechnung.

        Zugelassen sind nur Zahlungseingänge (Bewegung mit Betrag > 0) auf versendete
        Rechnungen; das Bankkonto braucht eine SKR-Nummer. Negative Zuordnungen gibt es sonst nur
        im WISO-Import (``link_invoice_to_transaction``); die Erstattung einer Stornorechnung
        legt ``assign_storno_refund`` an.

        Geschrieben werden die InvoicePayments-Zeile und die Buchungssätze, alle mit Datum,
        Steuerdatum und Empfänger der Bewegung, der Rechnungsnummer als Beleg-Nr. und der
        Marke ``PAYMENT_MARK``:

        * Mit Forderung (Debitoren an Wartekonto, beim Versenden gebucht):
          ``Bank an Debitoren`` (ohne Steuer) und ``Wartekonto an Erlöskonto`` (mit Steuer).
        * Ohne Forderung (Steuersatz ohne Wartekonto, Bestand vor den Forderungsbuchungen):
          ``Erlöskonto an Bank`` mit Steuer – über den zugeordneten Betrag, nicht den der
          Bewegung.

        Die Steuer eines Teils ist der anteilige Rechnungsbetrag (auf den Cent gerundet); der
        Teil, der die Rechnung schließt, bekommt den Rest – die Summe stimmt so exakt mit der
        Rechnung überein. Abgezogen wird die Steuer der anderen Zuordnungen der Rechnung: bei
        markierten Sätzen die tatsächlich gebuchte, bei Zuordnungen ohne markierte Sätze
        (Altbestand, WISO-Import) ihr anteiliger Betrag.

        Der Betrag wird auf Cent gerundet (halb auf), bevor er gekappt wird – ein Bruchteil eines
        Cents bliebe sonst als Rest auf dem Debitorenkonto stehen und ließe sich nicht exportieren.

        Das Erlöskonto ergibt sich aus der Rechnung (``resolve_revenue_coa``). Nur wenn dafür
        keines hinterlegt ist (z. B. 5 %, 16 % ohne Historie), wählt der Nutzer es:
        ``revenue_coa_id`` muss dann ein Erlöskonto sein (``get_revenue_coas``), sonst lehnt die
        Zuordnung ab. Ist eines hinterlegt, wird ``revenue_coa_id`` ignoriert. Geraten wird nie.

        Returns ``PaymentAssignment(ok, message, payment_id, revenue_coa_id)``; ``message`` ist
        bei einer Ablehnung ein Klartext für die Oberfläche, ``revenue_coa_id`` das Erlöskonto,
        auf das gebucht wurde.
        """
        if self.get_document(invoice_id, 'invoice') is None:
            return _refused('Rechnung nicht gefunden')
        booking = self.get_booking_by_id(booking_id)
        if booking is None:
            return _refused('Buchung nicht gefunden')
        if booking[17] != 'bank':
            return _refused('Nur Bankbewegungen lassen sich einer Rechnung zuordnen.')
        if any(a[1] == invoice_id for a in self.get_booking_allocations(booking_id)):
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        # Der Betrag vor den Vorbedingungen: Sie können ein Konto anlegen (4184/4185), und eine abgelehnte Zuordnung legt keines an
        requested = None
        if amount is not None:
            requested = round_minor(to_minor(amount), 2)
            if requested <= 0:
                return _refused('Der Betrag muss größer 0 sein.')
        # Ebenso vor den Vorbedingungen, was die Transaktion ohnehin ablehnte: ein Zahlungsausgang und eine ausgeschöpfte Bewegung
        # (die Transaktion prüft beides noch einmal am frisch gelesenen Stand)
        if (booking[11] or 0) <= 0:
            return _refused(INCOMING_ONLY_MESSAGE)
        conn = self._get_connection()
        try:
            free = self._booking_free_minor(conn, booking_id, self._get_bank_coa_ids(conn.cursor()))
        finally:
            conn.close()
        if free <= 0:
            return _refused(NOTHING_TO_ASSIGN_MESSAGE)
        error, ctx = self._payment_prerequisites(invoice_id, booking[4], revenue_coa_id, on=booking[1])
        if error:
            return _refused(error)

        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            result = self._assign_in_tx(tx, invoice_id, booking_id, requested, ctx)
        return result

    def _assign_in_tx(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled=False):
        """Der Teil von ``assign_invoice_payment``, der in einer Transaktion läuft: prüfen, die Zuordnung und
        ihre Buchungssätze schreiben, die Rechnung neu rechnen. ``tx`` hat die Schreibsperre schon (``BEGIN
        IMMEDIATE`` – der Aufrufer holt sie); der Baustein committet nie. Er wird vom Umzug der Zahlungen beim
        Versenden einer Ersatzrechnung wieder benutzt (``_move_replaced_payments``).

        ``requested``: der gewünschte Betrag in Minor Units, schon auf Cent gerundet, oder ``None`` („so viel
        wie möglich“). ``ctx``: die Schlüssel ``tax_rate, account_id, bank_coa, revenue_coa, bank_coa_ids``
        (``_payment_prerequisites``), bei einem Beleg mit Steuersatz je Position dazu ``mixed`` (``_assign_mixed``). Bei einem Beleg ohne
        Steuerausweis, dessen Erlöskonto am Datum der Bankbewegung hängt (``uses_small_business_rule``), dazu ``revenue_date_number``: das Kleinunternehmer-Konto (4185 oder
        4184), für das die Vorbereitung aufgelöst hat – gehört das Datum der Bewegung in der Transaktion zu einem anderen, lehnt der Baustein mit dem „geändert“-Satz ab (ein
        Kontext ohne den Schlüssel, etwa von Hand gebaut, bindet nichts).
        ``allow_cancelled``: Auch eine stornierte Rechnung (mit positivem Betrag, keine
        Stornorechnung) nimmt die Zuordnung an – nur für den Rest, der beim Umzug an der stornierten Rechnung bleibt.

        **Das Kennzeichen des Belegs und der Kontext müssen zusammenpassen:** Trägt der Beleg ``MixedTaxRates`` = 1, braucht der Kontext
        ``mixed`` – sonst (ein Kontext von Neuaufbau oder Umzug, die so einen Beleg nicht je Satz buchen, oder ein Beleg, der inzwischen gekennzeichnet
        wurde) lehnt der Baustein mit dem „geändert“-Satz ab, nichts geschrieben; ebenso ein Kontext mit ``mixed`` für einen Beleg ohne das
        Kennzeichen.

        Jede Ablehnung kommt vor dem ersten Schreiben (auch der ``IntegrityError`` beim Einfügen): Eine
        abgelehnte Zuordnung hinterlässt nichts. Returns :class:`PaymentAssignment`."""
        conn = tx.conn
        invoice = conn.execute(
            'SELECT InvoiceNumber, DocumentType, Status, SumGross, TaxAmount, TaxRate, '
            'CustomerId, InvoiceKind, MixedTaxRates FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
        if invoice is None:
            return _refused('Rechnung nicht gefunden')
        number, doc_type, status, gross, tax_total, rate, customer_id, kind, mixed = invoice
        if (doc_type or 'invoice') != 'invoice':
            return _refused('Rechnung nicht gefunden')
        bank = conn.execute(
            'SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, '
            'Currency FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        if bank is None or bank[0] != 'bank':
            return _refused('Nur Bankbewegungen lassen sich einer Rechnung zuordnen.')
        # Das Paar zuerst: ein zweiter, gleichzeitiger Klick bekommt diese Meldung, auch wenn
        # der erste die Rechnung inzwischen bezahlt gemacht hat.
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (invoice_id, booking_id)).fetchone():
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        blocked = (None if allow_cancelled and status == 'cancelled' and kind != STORNO_KIND
                   and (gross or 0) > 0 else payment_blocker(doc_type, status, gross, kind))
        if blocked:
            return _refused(blocked)
        if has_mixed_rates(mixed) != bool(ctx.get('mixed')):
            return _refused(PAYMENT_CHANGED_MESSAGE)
        _, bank_amount, account_id, bank_date, bank_tax_date, recipient, currency = bank
        if rate != ctx['tax_rate'] or account_id != ctx['account_id']:
            return _refused(PAYMENT_CHANGED_MESSAGE)
        if ctx.get('revenue_date_number') is not None and small_business_revenue_number(bank_date) != ctx['revenue_date_number']:
            return _refused(PAYMENT_CHANGED_MESSAGE)             # das Datum der Bewegung gehört inzwischen zum anderen Kleinunternehmer-Erlöskonto
        if (bank_amount or 0) <= 0:
            return _refused(INCOMING_ONLY_MESSAGE)

        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments '
                            'WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0]
        due = gross - paid
        free = self._booking_free_minor(conn, booking_id, ctx['bank_coa_ids'])
        alloc = min(due, free, due if requested is None else requested)
        if alloc <= 0:
            return _refused(NOTHING_TO_ASSIGN_MESSAGE)
        if ctx.get('mixed'):
            return self._assign_mixed(tx, invoice_id, booking_id, number, customer_id, bank, alloc, ctx)

        tax_total = tax_total or 0
        # Der schließende Teil bekommt den Rest der Rechnungssteuer, jeder andere den anteiligen Betrag
        if alloc == due:
            tax_part = self._remaining_tax(conn, invoice_id, number, gross, tax_total)
        else:
            tax_part = self._prorated_tax(tax_total, alloc, gross)
        tax_rate = self._booking_tax_rate(rate)
        tax_value = tax_part if tax_rate else None

        debtor_side, waiting_side = self._receivable_sides(conn, number)

        # Die Formen stehen fest, nicht aus der Kontenliste abgeleitet: „Bank an Debitoren“ hat das Geldkonto vorne,
        # „Wartekonto an Erlöskonto“ ist eine Umbuchung, „Erlöskonto an Bank“ das Zweckkonto vorne (``payment_rows``).
        rows = payment_rows(number=number, customer_id=customer_id, bank_id=booking_id, bank_date=bank_date,
                            bank_tax_date=bank_tax_date, recipient=recipient, currency=currency, amount=alloc,
                            tax_rate=tax_rate, tax_value=tax_value, bank_coa=ctx['bank_coa'],
                            revenue_coa=ctx['revenue_coa'], debtor_side=debtor_side, waiting_side=waiting_side)
        payment_id = self._write_assignment(tx, invoice_id, booking_id, alloc, bank_date, rows)
        if payment_id is None:
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        return PaymentAssignment(True, None, payment_id, ctx['revenue_coa'])

    def _assign_mixed(self, tx, invoice_id, booking_id, number, customer_id, bank, alloc, ctx):
        """Der Teil von ``_assign_in_tx`` für einen Beleg mit Steuersatz je Position: den Betrag ``alloc`` (Minor Units, schon auf Rest der Rechnung
        und freien Rest der Bewegung gekappt) auf die Sätze verteilen (``tax_groups.split_payment``) und die Zuordnung schreiben – „Bank an
        Debitoren“ über den ganzen Betrag und je Satz „Wartekonto an Erlöskonto“ (ohne Forderung je Satz „Erlöskonto an Bank“), jeden Satz mit seinem
        Betrag, seinem Satz (``tax_groups.booking_rate``) und seiner Steuer; ein Satz ohne Anteil bekommt keine Zeile. ``bank``: die Bankbewegung
        (die Zeile, wie ``_assign_in_tx`` sie liest), ``ctx`` mit ``mixed`` (``_payment_prerequisites``). Es gibt KEINE Wahl des Erlöskontos: Das
        Ergebnis trägt keines (``revenue_coa_id`` ``None``).

        Geprüft wird in der Transaktion am frisch gelesenen Stand, alles vor dem ersten Schreiben (jede Ablehnung ist ein fester Satz): die
        Gruppen sind noch die der Vorprüfung („geändert“-Satz), jede Gruppe ist buchbar (``_checked_mixed_groups``), die Forderungen tragen genau
        die Sätze des Belegs (``MIXED_RECEIVABLE_MISMATCH``; ganz ohne Forderung ist es der Einzelsatz je Satz), und die bisherigen Zuordnungen
        lassen sich je Satz lesen (``_mixed_booked``, ``MIXED_ALLOCATION_UNREADABLE``) – nur dann ist bekannt, wie viel jeder Satz noch offen hat."""
        _type, _amount, _account, bank_date, bank_tax_date, recipient, currency = bank
        conn = tx.conn
        try:
            groups = self._checked_mixed_groups(conn, invoice_id)
            if tuple(groups) != ctx['mixed']['groups']:
                return _refused(PAYMENT_CHANGED_MESSAGE)
            pcts = {group.pct for group in groups}
            debtor_side, waiting = self._receivable_sides_by_rate(conn, number)
            if waiting and set(waiting) != pcts:
                return _refused(MIXED_RECEIVABLE_MISMATCH)
            booked = self._mixed_booked(conn, invoice_id, number, pcts)
        except MixedNotBookable as exc:
            return _refused(str(exc))
        try:
            split = split_payment(groups, booked, alloc)
        except ValueError:
            return _refused(MIXED_ALLOCATION_UNREADABLE)

        revenue = ctx['mixed']['revenue']
        rows = payment_rows(
            number=number, customer_id=customer_id, bank_id=booking_id, bank_date=bank_date, bank_tax_date=bank_tax_date,
            recipient=recipient, currency=currency, amount=alloc, tax_rate=None, tax_value=None, bank_coa=ctx['bank_coa'],
            revenue_coa=None, debtor_side=debtor_side,
            parts=[PaymentPart(waiting.get(part.pct), revenue[part.pct], part.amount, booking_rate(part.pct),
                               part.tax if booking_rate(part.pct) else None) for part in split if part.amount])
        payment_id = self._write_assignment(tx, invoice_id, booking_id, alloc, bank_date, rows)
        if payment_id is None:
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        return PaymentAssignment(True, None, payment_id, None)

    def _write_assignment(self, tx, invoice_id, booking_id, alloc, bank_date, rows, description='Link invoice to booking'):
        """Eine Zuordnung in der Transaktion ``tx`` schreiben: die ``InvoicePayments``-Zeile über ``alloc`` und die Buchungssätze ``rows``
        (``payment_rows``), dann die Rechnung neu rechnen – die EINE Schreibstelle für den Beleg mit einem Satz und den mit Steuersatz je Position
        (eine Zahlung, und mit negativem ``alloc`` und ``description`` die Erstattung einer Stornorechnung: ``_refund_mixed``).
        Die Einträge fürs Replay-Log kommen in ``tx.log`` (geschrieben erst nach dem Commit). Returns die ID der Zuordnung, ``None``, wenn es das Paar
        (Rechnung, Bewegung) schon gibt (der eindeutige Index) – dann ist nichts geschrieben."""
        conn = tx.conn
        try:
            cursor = conn.execute(
                'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                'VALUES (?, ?, ?, ?)', (invoice_id, booking_id, alloc, bank_date))
        except sqlite3.IntegrityError:
            # der eindeutige Index auf (Rechnung, Bewegung) – dasselbe wie die Prüfung am Anfang von ``_assign_in_tx``
            return None
        payment_id = cursor.lastrowid
        tx.log.append(('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, '
                       'PaymentDate) VALUES (?, ?, ?, ?)',
                       (invoice_id, booking_id, alloc, bank_date), description))
        for row in rows:
            params = tuple(row.values[column] for column in PAYMENT_ROW_COLUMNS)
            conn.execute(_PAYMENT_ROW_INSERT, params)
            tx.log.append((_PAYMENT_ROW_INSERT, params, row.description))
        self._recalc_invoice(conn, invoice_id)
        return payment_id

    # ── Erstattung einer Stornorechnung ──────────────────────────────────────
    def assign_storno_refund(self, storno_id, booking_id, amount=None, revenue_coa_id=None):
        """Eine ausgehende Bankbewegung (ganz oder teilweise) als Erstattung einer Stornorechnung zuordnen.

        Eigener Weg neben ``assign_invoice_payment``. Das Guthaben des Kunden – Σ Zuordnungen beider Belege des Paares
        (stornierte Rechnung und Stornorechnung, ``get_storno_credit``) – gleicht die Erstattung aus: Sie ist die Zuordnung
        eines Zahlungsausgangs (Bewegung mit Betrag < 0) an der Stornorechnung, mit negativem Betrag. Der erstattete Betrag
        ist min(Guthaben, freier Rest der Bewegung, ``amount``); ``amount=None`` heißt „so viel wie möglich“. Der freie Rest
        eines Ausgangs ist ``|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze auf das Konto der Bewegung)``
        (``_outgoing_free_minor``; die Wirkung rechnet ``db.matching.bank_effect`` wie die Split-Summe), eine
        Sammel-Überweisung also mehrere Erstattungen derselben Bewegung. Das Bankkonto braucht eine SKR-Nummer; der Betrag
        wird auf Cent gerundet (halb auf), bevor er gekappt wird.

        Geschrieben werden die InvoicePayments-Zeile (an der Stornorechnung, Betrag −r) und die Buchungssätze, alle mit
        Datum, Steuerdatum, Empfänger und Währung der Bewegung, der **Nummer der Stornorechnung** als Beleg-Nr. und der Marke
        ``PAYMENT_MARK`` – über ``payment_rows``, dieselbe Stelle wie die Zuordnung einer Zahlung, mit negativem Betrag:
        Hat die Stornorechnung eine Gegenbuchung (``_receivable_sides``), ``Bank an Debitoren`` (ohne Steuer) und
        ``Wartekonto an Erlöskonto`` (mit Steuer), sonst der Einzelsatz ``Erlöskonto an Bank``.

        Die Steuer nimmt zurück, was die Zahlungen an der stornierten Rechnung gebucht haben (``_allocations_tax``): Gleicht
        die Erstattung das Guthaben ganz aus, ist es der Rest – die Steuer der Zahlungen abzüglich der früherer Erstattungen –,
        sonst der anteilige Betrag (Cent, halb auf; nie mehr als der Rest). Die Summe über alle Erstattungen ist so genau die
        Steuer der Zahlungen, **solange sich die Zahlungen der stornierten Rechnung nach der ersten Erstattung nicht ändern**:
        Wird danach eine Zahlung entfernt oder zieht ein Teil auf die Ersatzrechnung um, kann die Summe um Cent abweichen.
        Ohne Steuersatz (§ 19, 0 %) gibt es keine.

        Das Erlöskonto ist das der Zahlungen (``_payment_revenue_coa`` über die markierten Sätze ihrer Zuordnungen), wenn alle
        dasselbe tragen; liegen sie auf **mehr als einem** Erlöskonto (ein Satz wurde im Editor geändert), wählt der Nutzer das
        der Erstattung (``revenue_coa_id``, ein Erlöskonto, sonst Ablehnung mit ``MIXED_REVENUE_MESSAGE``). Hat keine Zuordnung
        markierte Sätze (Import), ist es das Erlöskonto der Momentaufnahme, wenn der Beleg einen Steuersatz aus den Stammdaten mit Erlöskonto trägt
        (``_document_revenue``: es gilt immer, eine Wahl gibt es dann nicht), sonst das für den Steuersatz hinterlegte (``resolve_revenue_coa``) – bei einem Beleg ohne
        Steuerausweis nach dem Datum der Erstattung (4185 bis 2025, 4184 ab 2026; ist es kein Datum, lehnt ``REVENUE_DATE_UNREADABLE`` ab) –, und gibt es
        auch das nicht, wählt der Nutzer es (``revenue_coa_id``, ein Erlöskonto, sonst Ablehnung). Mit markierten Sätzen gilt das Konto der Zahlungen, nie das Datum
        der Erstattung: Die Erstattung nimmt den Erlös dort zurück, wo die Zahlung ihn gebucht hat. Liegen Zahlungen auf 4185 und auf 4184 (eine Teilzahlung über den
        Jahreswechsel), sind es mehrere Erlöskonten, und der Nutzer wählt (``MIXED_REVENUE_MESSAGE``). Konten, die das Auflösen anlegen könnte,
        entstehen vor der Transaktion in eigenen Verbindungen – erst, wenn die Erstattung sich buchen lässt.

        **Ein Beleg mit Steuersatz je Position** (``Invoices.MixedTaxRates``, Stornorechnung und stornierte Rechnung tragen es beide) erstattet je Satz:
        Den Betrag verteilt ``tax_groups.split_payment`` auf die Sätze – gerechnet an dem, was die Zahlungen je Satz tragen (``_mixed_booked``), abzüglich
        der früheren Erstattungen –, und ``Bank an Debitoren`` (über den ganzen Betrag) und je Satz ``Wartekonto an Erlöskonto`` mit Betrag, Satz und
        Steuer des Anteils (ohne Gegenbuchung je Satz ``Erlöskonto an Bank``) bucht ``payment_rows(parts=…)``. Das Erlöskonto je Satz ist das, auf dem die
        Zahlungen dieses Satzes liegen (``_refund_revenue_coas_by_rate``); der Nutzer wählt keines (``revenue_coa_id`` gilt nie). Lässt sich die Erstattung
        nicht nach Sätzen aufteilen (eine Zuordnung ohne die Sätze der App, die Zahlungen haben sich nach einer früheren Erstattung geändert, ein Satz liegt auf
        mehr als einem Erlöskonto), lehnt ein fester Klartext ab (``MixedNotBookable`` im Plan, ``MIXED_*``) – nichts geschrieben, kein Konto angelegt.

        Abgelehnt wird mit Klartext, nichts geschrieben: keine Stornorechnung, keine ausgehende Bankbewegung, das Paar ist
        schon zugeordnet, es gibt kein Guthaben, die Bewegung ist ausgeschöpft, das Bankkonto hat keine SKR-Nummer, kein
        Erlöskonto. Geprüft wird vor der Transaktion und, mit frisch gelesenem Stand, in ihr (``BEGIN IMMEDIATE``).

        Returns ``PaymentAssignment(ok, message, payment_id, revenue_coa_id)`` wie ``assign_invoice_payment`` (bei einem Beleg mit Steuersatz je Position
        ohne ein einzelnes Erlöskonto: ``revenue_coa_id`` ``None``)."""
        requested = None
        if amount is not None:
            requested = round_minor(to_minor(amount), 2)
            if requested <= 0:
                return _refused('Der Betrag muss größer 0 sein.')
        bank_coa_ids = self.get_bank_coa_ids()

        # Vorprüfung ohne Schreibsperre: abgelehnt wird, bevor ein Konto angelegt wird
        conn = self._get_connection()
        try:
            refusal, plan = self._refund_plan(conn, storno_id, booking_id, requested, bank_coa_ids)
        finally:
            conn.close()
        if refusal:
            return _refused(refusal)
        error, bank_coa = self._bank_coa_of(plan.account_id, 'Erstattung')
        if error:
            return _refused(error)
        fallback = fallback_number = None
        if plan.parts is not None:
            pass                         # je Satz: Die Zahlungen nennen das Erlöskonto, der Nutzer wählt keines
        elif plan.mixed:
            # Die Zahlungen liegen auf verschiedenen Erlöskonten: kein hinterlegtes Konto entscheidet das, der Nutzer wählt
            fallback, error = self._chosen_revenue_coa(revenue_coa_id)
            if error:
                return _refused(MIXED_REVENUE_MESSAGE if error == NO_REVENUE_COA else error)
        elif plan.revenue_coa is None:
            # ohne markierte Sätze der Zahlungen: das Konto der Momentaufnahme (Steuersatz aus den Stammdaten), sonst das für den Satz hinterlegte, sonst die Wahl.
            # Bei einem Beleg ohne Steuerausweis hängt das hinterlegte Konto am Datum der Erstattung (4185 bis 2025, 4184 ab 2026); ist es kein Datum, lehnt die
            # Erstattung ab, bevor ein Konto entsteht. Mit markierten Sätzen gilt immer das Konto der Zahlungen (``_refund_revenue_coa``), nie das Datum der Erstattung
            if uses_small_business_rule(plan.rate):
                fallback_number = small_business_revenue_number(plan.bank_date)
                if fallback_number is None and not self.invoice_entry_accounts(plan.original_id):
                    return _refused(REVENUE_DATE_UNREADABLE)
            fallback, error = self._document_revenue(plan.original_id, plan.rate, revenue_coa_id, on=plan.bank_date)
            if error:
                return _refused(error)
        account_id, rate, mixed, parts = plan.account_id, plan.rate, plan.mixed, plan.parts

        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            refusal, plan = self._refund_plan(conn, storno_id, booking_id, requested, bank_coa_ids)
            if refusal:
                return _refused(refusal)
            if parts is not None or plan.parts is not None:
                # Beleg mit Steuersatz je Position: Sind die Teile je Satz (mit ihren Erlöskonten) und das Bankkonto der Bewegung noch die der
                # Vorprüfung – und trägt der Beleg das Kennzeichen noch (``None`` gegen Teile ist ungleich) –, bucht ``_refund_mixed``
                if plan.parts != parts or plan.account_id != account_id:
                    return _refused(PAYMENT_CHANGED_MESSAGE)
                return self._refund_mixed(tx, storno_id, booking_id, plan, bank_coa)
            revenue_coa = fallback if plan.mixed else plan.revenue_coa or fallback
            if (plan.rate != rate or plan.account_id != account_id or plan.mixed != mixed or revenue_coa is None
                    or (fallback_number is not None and small_business_revenue_number(plan.bank_date) != fallback_number)):
                return _refused('Rechnung oder Bankbewegung wurde inzwischen geändert – bitte erneut versuchen.')

            debtor_side, waiting_side = self._receivable_sides(conn, plan.number)
            try:
                cursor = conn.execute(
                    'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                    (storno_id, booking_id, -plan.amount, plan.bank_date))
            except sqlite3.IntegrityError:
                # der eindeutige Index auf (Rechnung, Bewegung) – dasselbe wie die Prüfung im Plan
                return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
            payment_id = cursor.lastrowid
            tx.log.append(('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                           (storno_id, booking_id, -plan.amount, plan.bank_date), 'Link storno invoice to booking (refund)'))

            tax_rate = self._booking_tax_rate(plan.rate)
            for row in payment_rows(number=plan.number, customer_id=plan.customer_id, bank_id=booking_id,
                                    bank_date=plan.bank_date, bank_tax_date=plan.bank_tax_date,
                                    recipient=plan.recipient, currency=plan.currency, amount=-plan.amount,
                                    tax_rate=tax_rate, tax_value=-plan.tax if tax_rate else None, bank_coa=bank_coa,
                                    revenue_coa=revenue_coa, debtor_side=debtor_side, waiting_side=waiting_side):
                params = tuple(row.values[column] for column in PAYMENT_ROW_COLUMNS)
                conn.execute(_PAYMENT_ROW_INSERT, params)
                tx.log.append((_PAYMENT_ROW_INSERT, params, row.description))

            self._recalc_invoice(conn, storno_id)
        return PaymentAssignment(True, None, payment_id, revenue_coa)

    def _refund_mixed(self, tx, storno_id, booking_id, plan, bank_coa):
        """Der Teil von ``assign_storno_refund`` für einen Beleg mit Steuersatz je Position, in der Transaktion ``tx`` (``BEGIN IMMEDIATE``, der Aufrufer
        hat den Plan gerade frisch gelesen und mit der Vorprüfung verglichen): die Zuordnung an der Stornorechnung (Betrag −``plan.amount``) und ihre Sätze
        schreiben – „Bank an Debitoren“ über den ganzen Betrag und je Teil „Wartekonto an Erlöskonto“ (ohne Gegenbuchung je Teil „Erlöskonto an Bank“),
        alle negativ, jeder Teil mit seinem Satz (``tax_groups.booking_rate``), seiner Steuer und dem Erlöskonto, auf dem die Zahlungen dieses Satzes
        liegen –, dann die Stornorechnung neu rechnen (``_write_assignment``). Die Wartekonten je Satz sind die der Gegenbuchung der Stornorechnung
        (``_receivable_sides_by_rate``; der Plan hat geprüft, dass sie genau die Sätze des Belegs tragen). Returns :class:`PaymentAssignment` ohne ein
        einzelnes Erlöskonto."""
        try:
            debtor_side, waiting = self._receivable_sides_by_rate(tx.conn, plan.number)
        except MixedNotBookable as exc:
            return _refused(str(exc))
        rows = payment_rows(
            number=plan.number, customer_id=plan.customer_id, bank_id=booking_id, bank_date=plan.bank_date, bank_tax_date=plan.bank_tax_date,
            recipient=plan.recipient, currency=plan.currency, amount=-plan.amount, tax_rate=None, tax_value=None, bank_coa=bank_coa,
            revenue_coa=None, debtor_side=debtor_side,
            parts=[PaymentPart(waiting.get(part.pct), part.revenue, -part.amount, booking_rate(part.pct),
                               -part.tax if booking_rate(part.pct) else None) for part in plan.parts])
        payment_id = self._write_assignment(tx, storno_id, booking_id, -plan.amount, plan.bank_date, rows,
                                            'Link storno invoice to booking (refund)')
        if payment_id is None:
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        return PaymentAssignment(True, None, payment_id, None)

    def _mixed_refund_parts(self, conn, storno_id, number, original_id, original_number, credit, refund):
        """Die Teile einer Erstattung zu einem Beleg mit Steuersatz je Position – ``(RefundPart, …)`` in der Reihenfolge der Sätze (absteigend), nur die
        mit einem Betrag. ``storno_id``/``number``: die Stornorechnung und ihre Nummer, ``original_id``/``original_number``: die stornierte Rechnung,
        ``credit``: das Guthaben und ``refund``: der erstattete Betrag (Minor Units, ``refund`` ≤ ``credit``), in der Verbindung oder Transaktion
        ``conn``. Nur lesend; ``MixedNotBookable`` mit dem festen Klartext, wenn sich die Erstattung nicht nach Sätzen aufteilen lässt.

        Der Topf, aus dem erstattet wird, ist je Satz das, was die Zahlungen der stornierten Rechnung getragen haben (``_mixed_booked``: Betrag und
        Steuer) – nicht das Brutto der Gruppe: Eine Teilzahlung hat ihn noch nicht gefüllt. Was schon erstattet ist (die Zuordnungen der Stornorechnung,
        mit umgekehrtem Vorzeichen), zählt als gebucht. ``tax_groups.split_payment`` verteilt dann wie bei einer Zahlung: Der Cent-Rest geht an den Satz
        mit dem größten offenen Teil, und die Erstattung, die das Guthaben ganz ausgleicht, nimmt von jedem Satz den ganzen Rest samt Steuer zurück. Gibt es
        keine Gegenbuchung, ist es je Satz der Einzelsatz „Erlöskonto an Bank“ – hat die Stornorechnung Forderungszeilen, tragen sie genau die Sätze des
        Belegs (``MIXED_RECEIVABLE_MISMATCH``)."""
        try:
            groups = self._mixed_groups(conn, original_id)
        except RateGroupsMismatch as exc:
            print(f'Rechnung {original_id}: {exc}')
            raise MixedNotBookable(MIXED_GROUPS_MISMATCH) from None
        pcts = {group.pct for group in groups}
        paid = self._mixed_booked(conn, original_id, original_number, pcts)
        refunded = {pct: (-amount, -tax) for pct, (amount, tax) in self._mixed_booked(conn, storno_id, number, pcts).items()}
        # Das Guthaben rechnet die Prüfung aus den Zuordnungen; die Sätze müssen genau dasselbe ergeben, sonst wäre ein Teil unlesbar
        if sum(amount for amount, _tax in paid.values()) - sum(amount for amount, _tax in refunded.values()) != credit:
            raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE)
        _debtor, waiting = self._receivable_sides_by_rate(conn, number)
        if waiting and set(waiting) != pcts:
            raise MixedNotBookable(MIXED_RECEIVABLE_MISMATCH)
        try:
            split = split_payment(refund_pool(paid), refunded, refund)
        except ValueError:
            raise MixedNotBookable(MIXED_REFUND_SPLIT) from None
        revenue = self._refund_revenue_coas_by_rate(conn, original_id, original_number)
        parts = []
        for part in split:
            if part.amount:
                if revenue.get(part.pct) is None:
                    raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE)
                parts.append(RefundPart(part.pct, part.amount, part.tax, revenue[part.pct]))
        return tuple(parts)

    def _refund_revenue_coas_by_rate(self, conn, original_id, original_number):
        """Das Erlöskonto je Satz (``{Satz: COA-ID}``), auf dem die Zahlungen einer stornierten Rechnung mit Steuersatz je Position liegen: je Zuordnung nennen
        ihre markierten Sätze es – bei „Wartekonto an Erlöskonto“ (``transfer``) das Gegenkonto, beim Einzelsatz „Erlöskonto an Bank“ (``purpose_first``) das
        Konto; es sind dieselben Sätze, die ``_mixed_booked`` zählt. ``MixedNotBookable`` mit ``MIXED_REFUND_REVENUE_AMBIGUOUS`` und dem Satz, wenn die
        Zahlungen eines Satzes auf mehr als einem Konto liegen (ein Satz wurde im Editor geändert) – die App wählt keines für den Nutzer, und der wählt hier nie.
        Liegen sie auf EINEM Konto, das kein Erlöskonto (mehr) ist (``_income_coas``), gilt dasselbe, nur ist „verschiedene Erlöskonten“ dort falsch: Trägt der Beleg eine
        Momentaufnahme (``_carries_snapshot``), nennt die Meldung das Konto, das die Rechnung für den Satz trägt (``TAX_PAYMENT_NO_REVENUE``, mit dem Satz der Gruppe); ohne
        Momentaufnahme bleibt es bei ``MIXED_REFUND_REVENUE_AMBIGUOUS``, wie es war. Die Momentaufnahme selbst wird hier nicht geprüft (``group_snapshots``): Die Erstattung
        folgt den Zahlungen."""
        waiting_ids = self._waiting_coa_ids(conn)
        found = {}
        for (bank_id,) in conn.execute('SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', (original_id,)).fetchall():
            marked = self._payment_children(conn, bank_id, original_number)
            if not marked:
                continue
            for form, coa, counter, rate in conn.execute(
                    f"SELECT EntryForm, COA_ID, CounterCOA_ID, TaxRate FROM Bookings WHERE ID IN ({','.join('?' * len(marked))}) ORDER BY ID",
                    marked).fetchall():
                if form == FORM_TRANSFER and coa in waiting_ids:
                    account = counter
                elif form == FORM_PURPOSE_FIRST:
                    account = coa
                else:
                    continue
                try:
                    pct = rate_pct(rate)
                except (ArithmeticError, ValueError):
                    raise MixedNotBookable(MIXED_ALLOCATION_UNREADABLE) from None
                accounts = found.setdefault(Decimal(0) if pct is None else pct, [])
                if account not in accounts:
                    accounts.append(account)
        income = {row[0] for row in self._income_coas(conn.cursor())}
        for pct, accounts in found.items():
            if len(accounts) > 1:
                raise MixedNotBookable(rate_sentence(MIXED_REFUND_REVENUE_AMBIGUOUS, pct, pct in self._exempt_pcts(conn, original_id)))
            if accounts[0] not in income:
                # das eine Konto, auf dem die Zahlungen dieses Satzes liegen, ist kein Erlöskonto (mehr): mit Momentaufnahme das Konto der Rechnung für den Satz
                template = TAX_PAYMENT_NO_REVENUE if self._carries_snapshot(conn, original_id) else MIXED_REFUND_REVENUE_AMBIGUOUS
                raise MixedNotBookable(rate_sentence(template, pct, pct in self._exempt_pcts(conn, original_id)))
        return {pct: accounts[0] for pct, accounts in found.items()}

    def _refund_plan(self, conn, storno_id, booking_id, requested, bank_coa_ids):
        """Eine Erstattung prüfen und ausrechnen – nur lesend, in der Verbindung (Transaktion) ``conn``: die Stornorechnung
        ``storno_id`` mit ihrer stornierten Rechnung, die Bankbewegung ``booking_id``, das Guthaben, der freie Rest der
        Bewegung (``bank_coa_ids``: die eigenen Bankkonten) und der gewünschte Betrag ``requested`` (Minor Units, auf Cent
        gerundet, oder ``None``). Returns ``(Klartext, None)`` bei einer Ablehnung, sonst ``(None, _Refund)``.

        Tragen Stornorechnung und stornierte Rechnung beide das Kennzeichen „Steuersatz je Position“, trägt der Plan die Teile je Satz
        (``_Refund.parts``, ``_mixed_refund_parts``); trägt nur eine von beiden es, lehnt ``MIXED_GROUPS_MISMATCH`` ab."""
        storno = conn.execute(
            'SELECT InvoiceNumber, DocumentType, InvoiceKind, CancelsInvoiceId, CustomerId, SumGross, TaxAmount, MixedTaxRates '
            'FROM Invoices WHERE ID = ?', (storno_id,)).fetchone()
        if storno is None or (storno[1] or 'invoice') != 'invoice':
            return 'Rechnung nicht gefunden', None
        number, _type, kind, original_id, customer_id, gross, tax_total, storno_mixed = storno
        original = None
        if kind == STORNO_KIND and original_id is not None:
            original = conn.execute('SELECT InvoiceNumber, SumGross, TaxAmount, TaxRate, MixedTaxRates FROM Invoices WHERE ID = ?',
                                    (original_id,)).fetchone()
        if original is None:
            return REFUND_ONLY_STORNO_MESSAGE, None
        per_rate = has_mixed_rates(storno_mixed)          # Steuersatz je Position – nicht zu verwechseln mit ``mixed`` unten: Die Zahlungen liegen auf verschiedenen Erlöskonten
        if per_rate != has_mixed_rates(original[4]):
            # die Stornorechnung übernimmt das Kennzeichen der stornierten Rechnung (``_copied_head``): Weichen beide ab, hat jemand an der App vorbei
            # etwas geändert – dann gälte für die Erstattung je Satz, was für den anderen Beleg nicht stimmt
            return MIXED_GROUPS_MISMATCH, None
        bank = conn.execute(
            'SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, Currency '
            'FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        if bank is None:
            return 'Buchung nicht gefunden', None
        if bank[0] != 'bank' or (bank[1] or 0) >= 0:
            return REFUND_ONLY_OUTGOING_MESSAGE, None
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (storno_id, booking_id)).fetchone():
            return 'Buchung ist bereits mit dieser Rechnung verknüpft', None
        original_number, original_gross, original_tax, rate, _original_mixed = original

        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                            (original_id,)).fetchone()[0]
        refunded = -conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                                 (storno_id,)).fetchone()[0]
        credit = paid - refunded
        if paid <= 0 or credit <= 0:
            return REFUND_NO_CREDIT_MESSAGE, None
        free = self._booking_free_outgoing_minor(conn, booking_id, bank_coa_ids)
        if free <= 0:
            return REFUND_EXHAUSTED_MESSAGE, None
        refund = min(credit, free, credit if requested is None else requested)

        if per_rate:
            try:
                parts = self._mixed_refund_parts(conn, storno_id, number, original_id, original_number, credit, refund)
            except MixedNotBookable as exc:
                return str(exc), None
            return None, _Refund(number, customer_id, original_id, rate, bank[2], bank[3], bank[4], bank[5], bank[6],
                                 refund, sum(part.tax for part in parts), None, False, parts)

        tax = None
        if self._booking_tax_rate(rate):
            paid_tax = self._allocations_tax(conn, original_id, original_number, original_gross or 0, original_tax or 0)
            refunded_tax = -self._allocations_tax(conn, storno_id, number, gross or 0, tax_total or 0)
            if refund == credit:
                tax = paid_tax - refunded_tax
            else:
                tax = min(self._prorated_tax(paid_tax, refund, paid), paid_tax - refunded_tax)
            tax = max(tax, 0)
        revenue_coa, mixed = self._refund_revenue_coa(conn, original_id, original_number)
        return None, _Refund(number, customer_id, original_id, rate, bank[2], bank[3], bank[4], bank[5], bank[6],
                             refund, tax, revenue_coa, mixed)

    def _refund_revenue_coa(self, conn, original_id, original_number):
        """``(Erlöskonto, gemischt)`` für die Erstattung: das Erlöskonto (COA-ID), auf das die Zahlungen der stornierten
        Rechnung gebucht sind – je Zuordnung nennen ihre markierten Sätze eines (``_payment_revenue_coa``). Tragen alle
        dasselbe, ist es das, wenn es ein Erlöskonto ist, sonst ``None``; ``None`` auch, wenn keine Zuordnung markierte Sätze
        hat (Import, Altbestand). Tragen sie **mehr als ein** Erlöskonto (ein Satz wurde von Hand geändert), ist ``gemischt``
        wahr und das Erlöskonto ``None``: Der Nutzer wählt das der Erstattung."""
        found = []
        for (bank_id,) in conn.execute('SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID DESC',
                                       (original_id,)).fetchall():
            coa = self._payment_revenue_coa(conn, self._payment_children(conn, bank_id, original_number))
            if coa is not None and coa not in found:
                found.append(coa)
        if len(found) > 1:
            return None, True
        if found and found[0] in {row[0] for row in self._income_coas(conn.cursor())}:
            return found[0], False
        return None, False

    def refund_revenue_coas_differ(self, storno_id):
        """Liegen die Zahlungen der stornierten Rechnung dieser Stornorechnung auf mehr als einem Erlöskonto? Dann wählt der
        Nutzer das Erlöskonto der Erstattung (``assign_storno_refund``, ``MIXED_REVENUE_MESSAGE``); die Seite der
        Stornorechnung zeigt dafür die Auswahl. Rein lesend; ``False`` für jeden Beleg, der keine Stornorechnung ist – und für eine
        Stornorechnung mit Steuersatz je Position: Dort gibt es nie eine Wahl, das Erlöskonto je Satz ist das der Zahlungen
        (``_refund_revenue_coas_by_rate``), und liegen sie auf mehreren, lehnt die Erstattung ab (``MIXED_REFUND_REVENUE_AMBIGUOUS``)."""
        original = self.find_cancelled_invoice(storno_id)
        if original is None:
            return False
        conn = self._get_connection()
        try:
            flag = conn.execute('SELECT MixedTaxRates FROM Invoices WHERE ID = ?', (storno_id,)).fetchone()
            if flag is not None and has_mixed_rates(flag[0]):
                return False
            return self._refund_revenue_coa(conn, original[0], original[1])[1]
        finally:
            conn.close()

    # ── Entfernen ────────────────────────────────────────────────────────────
    def _after_payment_removed(self, conn, invoice_id):
        """Nach dem Entfernen von Zahlungen, in der Transaktion von ``conn``:

        * Die Forderung gilt wieder als offen, wenn die übrigen Sätze „… an Debitoren“ sie nicht
          mehr ausgleichen (Stufe 7 des Auto-Abgleichs hatte sie auf 'resolved' gesetzt, siehe
          ``money.is_covered``). Trägt die Rechnungsnummer mehrere Forderungszeilen, zählt ihre
          Summe, und alle aufgelösten gehen gemeinsam zurück (``_standalone_receivable_rows``: nur, was
          ``receivable_condition`` eine Forderung nennt, mit allen Debitorenkonten). Gerechnet wird wie in
          Stufe 7 (``matching.receivables_covered``): Ein Betrag, der keine Zahl ist, entscheidet nichts –
          die Zeilen bleiben, wie sie sind, und das Entfernen scheitert nicht.
        * AmountDue und Status der Rechnung werden neu gerechnet.
        * Ist keine Zahlung mehr übrig und der Status der Rechnung kein Forderungsstatus
          (storniert, Entwurf, abgeschlossen), geht auch die Forderungsbuchung – so wie beim
          Statuswechsel (``RECEIVABLE_STATUSES``) und nur die, die die App selbst gebucht hat
          (``_drop_receivable``: nie ein Satz mit ``SourceGroup``). Die Reihenfolge „erst
          stornieren, dann die Zahlung entfernen“ endet damit im selben Zustand wie die
          umgekehrte – außer bei einer Rechnung mit Stornorechnung: Deren Forderung bleibt, samt der
          Gegenbuchung der Stornorechnung (``_drop_receivable``)."""
        row = conn.execute('SELECT InvoiceNumber FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        number = row[0]
        debtor_ids = sorted(self._debtor_coa_ids(conn))
        if debtor_ids:
            standalone = self._standalone_receivable_rows(conn.cursor(), number)
            # Ohne aufgelöste Zeile gibt es nichts zurückzunehmen: dann wird auch kein Betrag gelesen (wie bisher – ein Betrag,
            # der keine Zahl ist, darf das Entfernen einer Zahlung nicht scheitern lassen)
            if any(status == 'resolved' for _id, _amount, status in standalone):
                covered = receivables_covered([amount for _id, amount, _status in standalone],
                                              self._debtor_settled_amounts(conn.cursor(), number, debtor_ids))
                if covered is False:                 # None: nicht entscheidbar – dann bleibt alles, wie es ist
                    for booking_id, _amount, status in standalone:
                        if status == 'resolved':
                            conn.execute('UPDATE Bookings SET Status = NULL WHERE ID = ?', (booking_id,))
        self._recalc_invoice(conn, invoice_id)

        status = conn.execute('SELECT Status FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()[0]
        if status not in RECEIVABLE_STATUSES:
            self._drop_receivable(conn, invoice_id, number)

    def _unassign(self, conn, payment_id):
        """Eine Zuordnung samt ihren Buchungssätzen in der Transaktion von ``conn`` entfernen.

        Die Sätze gehen nur mit, wenn für das Paar (Rechnung, Bewegung) keine weitere
        Zuordnung bleibt (Doppel aus Altbeständen). Returns die Rechnungs-ID oder ``None``,
        wenn es die Zuordnung nicht gibt."""
        row = conn.execute('SELECT ip.InvoiceID, ip.BookingID, i.InvoiceNumber '
                           'FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
                           'WHERE ip.ID = ?', (payment_id,)).fetchone()
        if row is None:
            return None
        invoice_id, booking_id, number = row
        conn.execute('DELETE FROM InvoicePayments WHERE ID = ?', (payment_id,))
        if not conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                            (invoice_id, booking_id)).fetchone():
            children = self._payment_children(conn, booking_id, number)
            if children:
                self._delete_booking_rows(conn, children)
        self._after_payment_removed(conn, invoice_id)
        return invoice_id

    def unassign_invoice_payment(self, payment_id):
        """Eine Zuordnung entfernen: ihre Buchungssätze, die Zuordnung selbst, danach die
        Rechnung neu rechnen (Rest, Status: zurück auf versendet bzw. überfällig, nie auf
        abgeschlossen; eine stornierte Rechnung bleibt storniert). Die Forderung bleibt
        stehen und ist damit wieder offen – außer die Rechnung ist kein offener Posten mehr
        (storniert von Hand, ohne Stornorechnung) und es war ihre letzte Zahlung
        (``_after_payment_removed``). Alles in einer Transaktion.

        Die Erstattung einer Stornorechnung (``assign_storno_refund``) geht auf demselben Weg: Zuordnung und markierte Sätze
        gehen, Forderung und Gegenbuchung des Paares bleiben, das Guthaben ist danach wieder da. Eine **Zahlung** an einer
        stornierten Rechnung mit Stornorechnung lässt sich dagegen nicht entfernen, wenn danach mehr erstattet als gezahlt wäre – bei einem Paar
        mit Steuersatz je Position auch nicht, wenn es nur für einen Satz so wäre
        (:class:`PaymentRemovalRefused`, in der Transaktion geprüft) – erst die Erstattung entfernen. Dasselbe gilt für jeden
        anderen Weg, der eine Zahlung entfernt (``remove_assignment_for_child``, ``delete_transaction``).

        Returns die Rechnungs-ID; ``ValueError``, wenn es die Zuordnung nicht gibt, :class:`PaymentRemovalRefused` (auch ein
        ``ValueError``), wenn das Guthaben sonst negativ würde."""
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            blocker = self._refund_removal_blocker(tx.conn, [payment_id])
            if blocker:
                raise PaymentRemovalRefused(blocker)
            invoice_id = self._unassign(tx.conn, payment_id)
        if invoice_id is None:
            raise ValueError(f"InvoicePayment {payment_id} not found")
        return invoice_id

    @classmethod
    def _refund_removal_blocker(cls, conn, payment_ids):
        """Klartext, wenn das Entfernen dieser Zuordnungen (``InvoicePayments.ID``, alle zusammen) das Guthaben eines
        Storno-Paares negativ machte, sonst ``None``. Betroffen ist jedes Paar, dessen stornierte Rechnung eine Zahlung
        (Betrag > 0) unter ihnen hat: Bleibt von den Zuordnungen beider Belege nach dem Entfernen weniger als 0 (Zahlungen
        minus Erstattungen), wird abgelehnt (``ALREADY_REFUNDED_MESSAGE``).

        **Ein Paar mit Steuersatz je Position** (die stornierte Rechnung ODER ihre Stornorechnung trägt ``MixedTaxRates``) bekommt, wenn die Summe nicht
        schon ablehnt, dieselbe Prüfung je Satz (``_unbalanced_rate_after_removal``): Die Summe kann aufgehen, und doch wäre für einen Satz mehr erstattet als
        gezahlt – dann lehnt ``MIXED_REMOVAL_REFUSED`` ab, mit dem ersten solchen Satz und der Nummer der Stornorechnung. Ein Paar, dessen Stand je Satz sich
        nicht lesen lässt (``MixedNotBookable``: eine Zuordnung ohne die Sätze der App, von Hand geänderte Sätze), bleibt bei der Summe: Das Entfernen ist dort
        der Weg, den Zustand zu bereinigen, und wird nie gesperrt. Ein Paar mit einem Satz bleibt, wie es war.

        Eine Erstattung (Betrag < 0) zu entfernen löst nie etwas aus. In der Transaktion von ``conn``; die EINE Prüfung, die jeder Weg vor dem Entfernen
        einer Zahlung fragt (``unassign_invoice_payment``, ``remove_assignment_for_child``, ``delete_transaction``, ``split_removal_blocker``). Eine
        Klassenmethode: Sie läuft auch mit einer rohen Verbindung ohne Instanz."""
        ids = sorted(set(payment_ids))
        if not ids:
            return None
        marks = ','.join('?' * len(ids))
        pairs = conn.execute(
            'SELECT DISTINCT s.CancelsInvoiceId, s.ID, o.InvoiceNumber, s.InvoiceNumber, o.MixedTaxRates, s.MixedTaxRates FROM InvoicePayments ip '
            'JOIN Invoices s ON s.CancelsInvoiceId = ip.InvoiceID AND s.InvoiceKind = ? LEFT JOIN Invoices o ON o.ID = s.CancelsInvoiceId '
            f'WHERE ip.ID IN ({marks}) AND ip.Amount > 0 ORDER BY s.CancelsInvoiceId, s.ID', [STORNO_KIND, *ids]).fetchall()
        for pair in pairs:
            left = conn.execute(
                f'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID IN (?, ?) AND ID NOT IN ({marks})',
                [pair[0], pair[1], *ids]).fetchone()[0]
            if left < 0:
                return ALREADY_REFUNDED_MESSAGE
        for original_id, storno_id, original_number, storno_number, original_flag, storno_flag in pairs:
            if has_mixed_rates(original_flag) or has_mixed_rates(storno_flag):
                pct = cls._unbalanced_rate_after_removal(conn, original_id, original_number, storno_id, storno_number, ids)
                if pct is not None:
                    return rate_sentence(MIXED_REMOVAL_REFUSED, pct, pct in cls._exempt_pcts(conn, original_id), number=storno_number)
        return None

    @classmethod
    def _unbalanced_rate_after_removal(cls, conn, original_id, original_number, storno_id, storno_number, removed):
        """Der erste Satz (Prozentzahl), bei dem nach dem Entfernen der Zuordnungen ``removed`` (``InvoicePayments.ID``) mehr erstattet wäre als gezahlt – oder
        Betrag und Steuer nicht aufgingen –, sonst ``None``; ``None`` auch, wenn sich der Stand je Satz nicht lesen lässt. Nur lesend, in der Verbindung oder
        Transaktion von ``conn``.

        Der Stand danach: ``paid`` = was die verbleibenden Zahlungen der stornierten Rechnung je Satz tragen, ``refunded`` = was die verbleibenden Erstattungen
        an der Stornorechnung je Satz zurückgenommen haben (``_mixed_booked``, mit den entfernten Zuordnungen ausgenommen – eine Erstattung, die im selben
        Zug geht, zählt nicht mehr), dieselben Bausteine wie ``_mixed_refund_parts``. Gefragt wird ``tax_groups.booked_fault`` an dem Topf der Zahlungen
        (``refund_pool``): Es ist genau der Stand, in dem ``split_payment`` die nächste Erstattung ablehnen würde, oder ein Rest von Cent zwischen den Sätzen
        stehen bliebe (ein Satz hat mehr Betrag oder mehr Steuer erstattet, als die Zahlungen trugen; ein Satz ist ganz erstattet, aber nicht mit der ganzen
        Steuer; ein Satz hat erstattet, ohne dass eine Zahlung ihn trägt). Aus einem solchen Stand käme das Guthaben nicht mehr heraus."""
        try:
            pcts = {group.pct for group in cls._mixed_groups(conn, original_id)}
            paid = cls._mixed_booked(conn, original_id, original_number, pcts, except_payments=removed)
            refunded = {pct: (-amount, -tax) for pct, (amount, tax) in
                        cls._mixed_booked(conn, storno_id, storno_number, pcts, except_payments=removed).items()}
        except (MixedNotBookable, RateGroupsMismatch):
            return None
        fault = booked_fault(refund_pool(paid), refunded)
        return fault[0] if fault else None

    @staticmethod
    def _assignment_ids(conn, bank_id, invoice_number):
        """IDs der Zuordnungen (``InvoicePayments``) der Bankbewegung ``bank_id`` zur Rechnung mit dieser Nummer – die, die das
        Entfernen eines markierten Satzes der Zuordnung mitnimmt (``remove_assignment_for_child``)."""
        return [r[0] for r in conn.execute(
            'SELECT ip.ID FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
            'WHERE ip.BookingID = ? AND i.InvoiceNumber = ? ORDER BY ip.ID', (bank_id, invoice_number))]

    def remove_assignment_for_child(self, conn, child_id):
        """Einen markierten Buchungssatz einer Zuordnung löschen heißt: die ganze Zuordnung
        entfernen (Zahlungs- und Umbuchungssatz bleiben nie einzeln übrig). In der Transaktion
        von ``conn``. Gibt es zum Satz keine Zuordnung mehr, wird nur er selbst gelöscht.

        Ist es die Zahlung einer stornierten Rechnung mit Stornorechnung, und wäre danach mehr erstattet als gezahlt, lehnt
        der Baustein vor dem ersten Schreiben ab (:class:`PaymentRemovalRefused`, ``_refund_removal_blocker``)."""
        row = conn.execute('SELECT ParentBooking_ID, DocumentNumber FROM Bookings WHERE ID = ?',
                           (child_id,)).fetchone()
        if row is None:
            return
        payment_ids = self._assignment_ids(conn, *row)
        blocker = self._refund_removal_blocker(conn, payment_ids)
        if blocker:
            raise PaymentRemovalRefused(blocker)
        for payment_id in payment_ids:
            self._unassign(conn, payment_id)
        if conn.execute('SELECT 1 FROM Bookings WHERE ID = ?', (child_id,)).fetchone():
            self._delete_booking_rows(conn, [child_id])

    def _assigned_bank_change_blocker(self, conn, bank_id, stored, fields):
        """Klartext, wenn ``fields`` – die Spalten, die der Aufrufer an der Bankbewegung ``bank_id`` schreibt (Namen und Euro-Werte
        wie bei ``update_booking_fields``) – Betrag, Bankkonto oder Buchungsdatum ändern, obwohl ihr noch eine Zuordnung
        (``InvoicePayments``: Zahlung oder Erstattung) gehört; sonst ``None``. Ihr ``InvoicePayments.Amount``, der Status der
        Rechnung und die Buchungssätze der Zuordnung (Betrag, Datum, Bankkonto) stimmten danach nicht mehr mit der Bewegung
        überein; die App rechnet das nicht um, der Nutzer entfernt erst die Zuordnung.

        In der Transaktion von ``conn`` und NACH den Schreibschritten: Was der Nutzer im Editor derselben Anfrage an Zuordnungen
        entfernt hat, zählt nicht mehr – so geht „✕ an allen Zeilen der Zahlung + neuer Betrag“ in einem Speichern. ``stored`` ist
        der Stand der Bankbewegung vor dem ersten Schreiben: ``(Betrag in Minor Units, Account_ID, DateBooking)``. Ein unverändert
        zurückgeschickter Wert ist keine Änderung (``'119'`` gegen gespeicherte 119,0000). Empfänger, Text, Währung und Steuerdatum
        sind frei. Sind ALLE Zuordnungen Erstattungen (Stornorechnungen), sagt der Text das.

        Zur Bewegung gehört auch eine Zuordnung, die an einem ihrer Buchungssätze hängt (``InvoicePayments.BookingID`` zeigt auf
        ein Kind): So ordnet ein Import eine Zahlung einem eigenständigen Satz zu, der später unter die Bankbewegung kommt. Hängt
        der Nutzer den Satz im selben Speichern ab, zählt sie nicht mehr."""
        changes = []
        if 'amount' in fields and to_minor(fields['amount'] or 0) != stored[0]:
            changes.append('den Betrag')
        if 'account_id' in fields and _id_or_none(fields['account_id']) != _id_or_none(stored[1]):
            changes.append('das Bankkonto')
        if 'date_booking' in fields and (fields['date_booking'] or '') != (stored[2] or ''):
            changes.append('das Datum')
        if not changes:
            return None
        assigned = conn.execute(
            "SELECT COALESCE(NULLIF(i.InvoiceNumber, ''), CAST(i.ID AS TEXT)), i.InvoiceKind FROM InvoicePayments ip "
            'JOIN Invoices i ON i.ID = ip.InvoiceID WHERE ip.BookingID = ? '
            'OR ip.BookingID IN (SELECT ID FROM Bookings WHERE ParentBooking_ID = ?) ORDER BY ip.ID',
            (bank_id, bank_id)).fetchall()
        if not assigned:
            return None
        numbers = ', '.join(dict.fromkeys(number for number, _kind in assigned))
        what = changes[0] if len(changes) == 1 else f"{', '.join(changes[:-1])} und {changes[-1]}"
        refunds = all(kind == STORNO_KIND for _number, kind in assigned)
        return (BANK_CHANGE_REFUND_MESSAGE if refunds else BANK_CHANGE_PAYMENT_MESSAGE).format(numbers=numbers, changes=what)

    def _debtor_coa_ids(self, conn):
        return {r[0] for r in conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                                           (self.DEBTOR_ACCOUNT_NUMBER,))}

    def _receivable_sides(self, conn, number):
        """``(Debitorenkonto, Wartekonto)`` der Forderung zu dieser Rechnungsnummer – die Konten, auf die eine Zuordnung
        „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ bucht –, ``(None, None)`` ohne Forderung (dann ist es der
        Einzelsatz „Erlöskonto an Bank“). Die Forderung wird wie überall gesucht (``receivable_condition``): ein Satz mit
        10000 im Soll und der Rechnungsnummer, kein WISO-Storno „10000 an 1810“; die älteste gilt."""
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        receivable = conn.execute(
            f"SELECT COA_ID, CounterCOA_ID FROM Bookings WHERE DocumentNumber = ? "
            f"AND BookingType = 'entry' AND {receivable_sql} ORDER BY ID LIMIT 1",
            [number] + receivable_params).fetchone()
        if not receivable:
            return None, None
        # Normalfall „10000 an Wartekonto“; negativ gebucht steht 10000 als Gegenkonto
        return receivable if receivable[0] in self._debtor_coa_ids(conn) else (receivable[1], receivable[0])

    # ── Umzug auf die Ersatzrechnung ─────────────────────────────────────────
    def _same_payment_row(self, stored, expected, ignore_tax_date=False):
        """Ist der gespeicherte Satz (Spalte → Wert) genau der erwartete (``payment_rows``)? Der Steuersatz zählt als gleich,
        wenn es derselbe Satz ist (0.19 wie 19, ``_tax_rate_key``), ein leerer Empfänger wie ein fehlender. Was die App nie
        schreibt – Konto, Fremdkonto, Kategorie, Quellklammer – muss leer sein. ``ignore_tax_date`` lässt das Steuerdatum
        aus: nur, um den Grund zu benennen (der DATEV-Export stempelt es auf die exportierten Sätze) – umgezogen wird so
        ein Satz trotzdem nicht."""
        for column in PAYMENT_ROW_COLUMNS:
            if ignore_tax_date and column == 'DateTax':
                continue
            have, want = stored[column], expected[column]
            if column == 'TaxRate':
                same = self._tax_rate_key(have) == self._tax_rate_key(want)
            elif column == 'RecipientClient':
                same = (have or '') == (want or '')
            else:
                same = have == want
            if not same:
                return False
        return (stored['BookingType'] == 'entry' and stored['Account_ID'] is None and not stored['ForeignBankAccount']
                and stored['Category_ID'] is None and not stored['SourceGroup'])

    def _revenue_coa_if_as_written(self, conn, old, bank_id, bank, amount, bank_coa, ignore_tax_date=False):
        """Das Erlöskonto der Zuordnung (stornierte Rechnung ``old``, Bankbewegung ``bank_id`` mit den Angaben ``bank``, wie
        ``_movable_payment`` sie liest, über ``amount``, Bankkonto ``bank_coa``), wenn ihre Sätze GENAU die sind, die
        ``_assign_in_tx`` dafür schreibt – sonst ``None``. Der Umzug entfernt die Sätze und schreibt sie neu: Was ein Nutzer
        daran geändert hat, ginge still verloren, und ein Satz mit anderer Beleg-Nr. bliebe als Doppelbuchung stehen. Es gilt:

        1. Satzbestand: unter der Bewegung stehen zu dieser Zuordnung (``Status`` = Marke, ``DocumentNumber`` = Nummer der
           Rechnung) genau die erwarteten Sätze – „Bank an Debitoren“ (``liquid_first``) und „Wartekonto an Erlöskonto“
           (``transfer``) oder der Einzelsatz „Erlöskonto an Bank“ (``purpose_first``), keiner mehr, keiner weniger;
        2. jede Spalte hat den Wert, den ``payment_rows`` für diese Zuordnung ergibt: Datum, Steuerdatum, Empfänger und Währung
           der Bewegung, Kunde der Rechnung, Betrag, Text, Steuersatz, das Bankkonto der Bewegung; die übrigen Konten in der
           Rolle, die die Form verlangt (Debitoren, ein Wartekonto, ein Erlöskonto); Kategorie, Konto und Fremdkonto leer;
        3. der Steuerbetrag ist einer der beiden, die die Zuordnungslogik ergibt: anteilig (``_prorated_tax``) oder, als
           schließender Teil, der Rest (``_remaining_tax``, ohne die Zuordnung selbst);
        4. an keinem Satz hängt etwas, das ``_delete_booking_rows`` mit ihm wegräumt (``_booking_attachments_exist``);
        5. unter der Bewegung steht kein markierter Satz, dessen Beleg-Nr. zu keiner Rechnung gehört, die der Bewegung
           zugeordnet ist.

        ``ignore_tax_date`` prüft dasselbe ohne das Steuerdatum – ``_movable_payment`` benennt damit den Grund."""
        if conn.execute(
                "SELECT 1 FROM Bookings k WHERE k.ParentBooking_ID = ? AND k.Status = ? AND NOT EXISTS ("
                "SELECT 1 FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "WHERE ip.BookingID = k.ParentBooking_ID AND i.InvoiceNumber = k.DocumentNumber) LIMIT 1",
                (bank_id, PAYMENT_MARK)).fetchone():
            return None
        # Ein Satz ohne Marke unter der Bewegung, der zur Rechnung gehört (ihre Beleg-Nr.) oder die Debitoren berührt – etwa
        # ein von Hand ergänztes Skonto „… an 10000“ –, bliebe beim Umzug an der stornierten Rechnung zurück: Die neue
        # Rechnung zeigte einen offenen Rest, den es nicht gibt
        debtor_ids = sorted(self._debtor_coa_ids(conn))
        debtor_marks = ','.join('?' * len(debtor_ids)) or 'NULL'
        if conn.execute(
                "SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND (Status IS NULL OR Status <> ?) AND ("
                f"DocumentNumber = ? OR COA_ID IN ({debtor_marks}) OR CounterCOA_ID IN ({debtor_marks})) LIMIT 1",
                [bank_id, PAYMENT_MARK, old.number, *debtor_ids, *debtor_ids]).fetchone():
            return None
        columns = ('ID', *PAYMENT_ROW_COLUMNS, *_PAYMENT_ROW_UNWRITTEN)
        rows = [dict(zip(columns, values)) for values in conn.execute(
            f"SELECT {', '.join(columns)} FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? AND DocumentNumber = ? "
            "ORDER BY ID", (bank_id, PAYMENT_MARK, old.number))]
        # Debitoren- und Wartekonto sind die der Forderung, wie ``_assign_in_tx`` sie heute fände – nicht irgendein Konto
        # in dieser Rolle
        debtor_side, waiting_side = self._receivable_sides(conn, old.number)
        wanted_forms = [FORM_LIQUID_FIRST, FORM_TRANSFER] if debtor_side and waiting_side else [FORM_PURPOSE_FIRST]
        if sorted(row['EntryForm'] or '' for row in rows) != wanted_forms:
            return None
        if self._booking_attachments_exist(conn, [row['ID'] for row in rows]):
            return None

        by_form = {row['EntryForm']: row for row in rows}
        revenue_coa = self._payment_revenue_coa(conn, [row['ID'] for row in rows])
        if revenue_coa not in {row[0] for row in self._income_coas(conn.cursor())}:
            return None
        if len(rows) == 2 and (debtor_side not in debtor_ids or waiting_side not in self._waiting_coa_ids(conn)):
            return None

        tax_rate = self._booking_tax_rate(old.rate)
        taxes = {None}
        if tax_rate:
            taxes = {self._prorated_tax(old.tax_total, amount, old.gross)}
            # Den Rest der Rechnungssteuer bekommt nur der Teil, der die Rechnung geschlossen hat – also nur, wenn sie voll
            # bezahlt war. Bei einer offenen Rechnung wäre „der Rest“ die ganze übrige Steuer: ein Wert, den die App nie
            # schreibt, ein Nutzer von Hand aber schon
            paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                                (old.id,)).fetchone()[0]
            if paid >= old.gross:
                taxes.add(self._remaining_tax(conn, old.id, old.number, old.gross, old.tax_total,
                                              except_booking=bank_id))
        for tax_value in taxes:
            expected = payment_rows(
                number=old.number, customer_id=old.customer_id, bank_id=bank_id, bank_date=bank[3], bank_tax_date=bank[4],
                recipient=bank[5], currency=bank[6], amount=amount, tax_rate=tax_rate, tax_value=tax_value,
                bank_coa=bank_coa, revenue_coa=revenue_coa, debtor_side=debtor_side, waiting_side=waiting_side)
            if all(self._same_payment_row(by_form[row.values['EntryForm']], row.values, ignore_tax_date)
                   for row in expected):
                return revenue_coa
        return None

    def _movable_payment(self, conn, old, new_id, bank_id, amount, bank_coas, bank_coa_ids):
        """Lässt sich die Zuordnung (stornierte Rechnung ``old``, Bankbewegung ``bank_id``) über ``amount`` umziehen? Nur, was
        die App genau so geschrieben hat, wie ``_assign_in_tx`` es schreiben würde, und was sich eindeutig neu schreiben lässt:
        eine Bankbewegung mit Zahlungseingang, deren Konto eine SKR-Nummer hat (``bank_coas``) und die nicht überbucht ist;
        keine Sätze mit Quellklammer (WISO) darunter; genau eine Zuordnung zum Paar und keine der neuen Rechnung zur selben
        Bewegung; und markierte Sätze, die unverändert sind (``_revenue_coa_if_as_written``).

        ``old``: die stornierte Rechnung (``_Replaced``). Returns ``((Account_ID der Bewegung, Erlöskonto der bisherigen
        Sätze, Datum der Bewegung), None)``, sonst ``(None, Grund)`` – der Grund ist ein Schlüssel von ``MOVE_SKIP_REASONS``."""
        bank = conn.execute('SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, Currency '
                            'FROM Bookings WHERE ID = ?', (bank_id,)).fetchone()
        if bank is None or bank[0] != 'bank' or (bank[1] or 0) <= 0:
            return None, 'no_incoming'
        if amount <= 0:
            return None, 'import'                                    # eine negative Zuordnung kennt nur der WISO-Import
        if bank[2] not in bank_coas:
            return None, 'no_skr'
        if conn.execute("SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND SourceGroup IS NOT NULL "
                        "AND SourceGroup <> '' LIMIT 1", (bank_id,)).fetchone():
            return None, 'import'
        if not self._payment_children(conn, bank_id, old.number):
            # keine markierten Sätze: die Zuordnung eines Imports – oder Sätze ohne Marke zu dieser Rechnung (eine ältere
            # Version der App, von Hand angelegt)
            unmarked = conn.execute('SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND DocumentNumber = ? LIMIT 1',
                                    (bank_id, old.number)).fetchone()
            return None, 'unmarked' if unmarked else 'import'
        if (conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                         (old.id, bank_id)).fetchone()[0] != 1
                or conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                                (new_id, bank_id)).fetchone()):
            return None, 'multiple'
        revenue_coa = self._revenue_coa_if_as_written(conn, old, bank_id, bank, amount, bank_coas[bank[2]])
        if revenue_coa is None:
            # Weicht allein das Steuerdatum ab (der DATEV-Export stempelt es auf die exportierten Sätze), sagt der
            # Hinweis das – die Zuordnung bleibt trotzdem: Die Sätze stehen so in einem Export
            only_tax_date = self._revenue_coa_if_as_written(conn, old, bank_id, bank, amount, bank_coas[bank[2]],
                                                            ignore_tax_date=True) is not None
            return None, 'tax_date' if only_tax_date else 'edited'
        if self._booking_free_minor(conn, bank_id, bank_coa_ids) < 0:
            return None, 'overbooked'
        return (bank[2], revenue_coa, bank[3]), None

    def _move_replaced_payments(self, tx, row, move):
        """Beim Versenden einer Ersatzrechnung (``Invoices.ReplacesInvoiceId``, ``db/invoice_storno.py``) gehen die
        Zahlungen der stornierten Rechnung auf sie über – in der Transaktion ``tx`` des Statuswechsels
        (``Database.set_invoice_status``), nach der Forderungsbuchung der neuen Rechnung (``row``: ihre Zeile). ``move``:
        was ``Database._payment_move_context`` vor der Transaktion aufgelöst hat (``replaced_id``, ``revenue_coa``,
        ``bank_coas`` {Account_ID: COA-ID}, ``bank_coa_ids``). Trägt die neue Rechnung keinen Steuerausweis (§ 19), hängt ihr Erlöskonto am Datum der Bankbewegung
        jeder Zahlung (4185 bis 2025, 4184 ab 2026): ``revenue_coa`` ist dann ``None`` und ``revenue_by_number`` {Kontonummer: COA-ID} nennt die Konten, die die Zahlungen
        brauchen. Jede Zahlung zieht auf das Konto um, das für ihr Datum gilt – bei gleichem Steuersatz nur, wenn sie schon auf diesem liegt (``revenue_account``):
        Eine Zahlung ab 2026, die eine ältere Version auf 4185 gebucht hat, bleibt an der stornierten Rechnung; der Umzug verschöbe sonst Erlös im vergangenen Zeitraum.

        Je Zuordnung der stornierten Rechnung, die älteste Zahlung zuerst und solange die neue Rechnung noch offen ist:
        Die Zuordnung samt ihren Sätzen wird entfernt (``_unassign``), die neue Rechnung bekommt den Betrag, den sie
        noch braucht, mit ihren eigenen Sätzen (Datum der Bankbewegung, ihr Steuersatz und ihr Erlöskonto,
        ``_assign_in_tx``), und ein Rest bleibt an der stornierten Rechnung – dort wieder mit Sätzen auf dem bisherigen
        Erlöskonto (``allow_cancelled``). Die Steuer jedes Teils rechnet ``_assign_in_tx`` wie bei jeder Zuordnung; die
        Forderung der stornierten Rechnung samt Gegenbuchung bleibt stehen.

        Umgezogen wird nur, was die App genau so geschrieben hat (``_movable_payment``): Import, eine überbuchte Bewegung, ein
        Bankkonto ohne SKR-Nummer, Sätze, die jemand von Hand geändert oder ergänzt hat oder an denen ein Beleg hängt – es
        bleibt an der stornierten Rechnung und steht im Hinweis, mit dem Grund. Beurteilt wird jede Zuordnung gegen den
        Stand VOR dem ersten Umzug: Der Steuer-Rest des schließenden Teils hängt von den anderen Zuordnungen ab, die sich mit
        jedem Umzug ändern. Nichts zieht um, wenn Kunde oder Währung abweichen oder für den Steuersatz der neuen Rechnung kein
        Erlöskonto hinterlegt ist – und nie, wenn die stornierte oder die neue Rechnung einen Steuersatz je Position trägt
        (``MixedTaxRates``) oder ein Erlöskonto aus den Stammdaten (``InvoiceItems.RevenueCoaId``, ``_entry_accounts``): Die Zahlungen bleiben als Guthaben an der
        stornierten Rechnung. Der Hinweis ist ``MOVE_MIXED_HINT`` bzw. ``MOVE_ENTRY_HINT`` mit dem verbleibenden
        Guthaben, bei einer Teilerstattung dazu ``MOVE_REFUNDED_HINT``; ist alles erstattet, sind es dieselben Sätze wie beim Beleg mit einem Satz
        (``MOVE_NONE_HINT``, ``MOVE_REFUNDED_HINT``) – ohne den Halbsatz zum Erstatten und Neu-Zuordnen, denn es gibt nichts mehr –, und ohne Zahlung
        gibt es gar keinen Hinweis.
        Wurde zur stornierten Rechnung schon erstattet (``assign_storno_refund``; Σ Zuordnungen der
        Stornorechnung < 0), zieht höchstens das verbleibende Guthaben um – die Zahlungen der stornierten Rechnung (ihre positiven
        Zuordnungen; eine negative des WISO-Imports bleibt, wie sonst, unberührt, mindert aber das Guthaben) minus die Erstattungen vor
        dem Umzug (``movable_left``): Je umziehbarer Zuordnung ist der Teil min(Betrag, Rest der neuen Rechnung,
        ``movable_left``), endet er bei 0 der Umzug, und nach einem gelungenen Umzug sinkt ``movable_left`` um den Teil. Der
        Rest einer Zuordnung bleibt wie sonst an der stornierten Rechnung; so ist danach nie mehr erstattet als gezahlt, und der
        Hinweis nennt das Erstattete. Ohne Erstattung ändert sich nichts: ``movable_left`` ist dann die Summe aller Zahlungen und
        bremst nie. Eine Sammelzahlung zieht nur mit der Zuordnung um, die zur stornierten Rechnung gehört.

        Jede Zuordnung läuft in einem Savepoint: Lässt sie sich nicht vollständig mit den erwarteten Beträgen neu buchen,
        geht der Savepoint zurück (die Zuordnung liegt unverändert an der stornierten Rechnung, ihre Einträge im Replay-Log
        ``tx.log`` entfallen), sie zählt als übersprungen, und die nächste kommt an die Reihe. Nur ein Datenbankfehler mitten
        im Umzug ist unerwartet: Er löst :class:`PaymentMoveFailed` aus, und der Aufrufer rollt alles zurück.

        Returns ``(Betrag in Minor Units, der umgezogen ist, Hinweis für den Nutzer oder None)``."""
        conn = tx.conn
        new_id, old_id = row['ID'], move['replaced_id']
        old = conn.execute('SELECT InvoiceNumber, DocumentType, Status, TaxRate, CustomerId, Currency, SumGross, TaxAmount, '
                           'MixedTaxRates FROM Invoices WHERE ID = ?', (old_id,)).fetchone()
        if (old is None or (old[1] or 'invoice') != 'invoice' or old[2] != 'cancelled'
                or not conn.execute('SELECT 1 FROM Invoices WHERE CancelsInvoiceId = ?', (old_id,)).fetchone()):
            return 0, None
        old_number, _type, _status, old_rate, old_customer, old_currency, old_gross, old_tax, old_mixed = old
        replaced = _Replaced(old_id, old_number, old_gross or 0, old_tax or 0, old_rate, old_customer)
        total = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                             (old_id,)).fetchone()[0]
        if total <= 0:
            return 0, None
        # Was schon erstattet ist (Zuordnungen an der Stornorechnung), steht nicht mehr zur Verfügung
        refunded = max(-conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID IN '
                                     '(SELECT ID FROM Invoices WHERE CancelsInvoiceId = ?)', (old_id,)).fetchone()[0], 0)

        def euro(minor):
            return fmt_eur(from_minor(minor))

        def stays(sentence):
            """Nichts zieht um; der Hinweis sagt, was bleibt – mit denselben Sätzen wie beim Beleg mit einem Satz (``MOVE_NONE_HINT``, ``MOVE_REFUNDED_HINT``): Ist
            das Guthaben erstattet, gibt es nichts mehr zu erstatten oder neu zuzuordnen (``total`` > 0: Dann ist ``refunded`` > 0); sonst bleibt es an der
            stornierten Rechnung (``sentence``: ``MOVE_MIXED_HINT`` oder ``MOVE_ENTRY_HINT``), und eine Teilerstattung wird genannt."""
            credit = max(total - refunded, 0)
            if not credit:
                return 0, ' '.join([MOVE_NONE_HINT.format(number=old_number), MOVE_REFUNDED_HINT.format(amount=euro(refunded))])
            notes = [sentence.format(number=old_number, amount=euro(credit))]
            if refunded:
                notes.append(MOVE_REFUNDED_HINT.format(amount=euro(refunded)))
            return 0, ' '.join(notes)

        if has_mixed_rates(old_mixed) or has_mixed_rates(row.get('MixedTaxRates')):
            # Der Umzug vergleicht die Sätze der App mit ``payment_rows`` EINES Satzes (``_revenue_coa_if_as_written``, ``_same_payment_row``) und
            # bucht sie mit EINEM Erlöskonto neu – je Satz zu verallgemeinern (Etappe 3) rührte an einer Regel, die nicht gelockert wird
            return stays(MOVE_MIXED_HINT)
        if self._entry_accounts(conn, old_id) or self._entry_accounts(conn, new_id):
            # Ebenso bei einem Erlöskonto aus den Stammdaten (2.8.0): Der Umzug bucht neu mit dem Erlöskonto, das heute für den Satz hinterlegt ist
            # (``resolve_revenue_coa``) – für die Momentaufnahme je Beleg gälte dieselbe Verallgemeinerung (Etappe 3), und ihre Bedingungen werden nicht gelockert
            return stays(MOVE_ENTRY_HINT)
        if row['CustomerId'] != old_customer or (row['Currency'] or 'EUR') != (old_currency or 'EUR'):
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) bleiben dort: '
                       'Kunde oder Währung dieser Rechnung weichen ab.')
        if move.get('revenue_by_number') is None and move['revenue_coa'] is None:
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) wurden nicht übernommen: '
                       'Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt. Bitte die Zuordnung dort '
                       'entfernen und hier mit Erlöskonto zuordnen.')

        def booked(result, expected):
            """Die Zuordnung kam mit genau dem Betrag zustande."""
            if not result.ok:
                return False
            amount = conn.execute('SELECT Amount FROM InvoicePayments WHERE ID = ?', (result.payment_id,)).fetchone()
            return amount is not None and amount[0] == expected

        payments = conn.execute(
            'SELECT ID, BookingID, Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY PaymentDate, ID',
            (old_id,)).fetchall()
        positive = sum(amount for _pay_id, _bank_id, amount in payments if amount > 0)   # eine negative bleibt unberührt
        # Höchstens das verbleibende Guthaben zieht um: ALLE Zuordnungen der stornierten Rechnung – auch eine negative des
        # WISO-Imports (Rücklastschrift), die selbst nie umzieht – abzüglich der Erstattungen. Sonst wäre danach mehr erstattet
        # als gezahlt. Ohne Erstattung und ohne negative Zuordnung ist es die Summe aller Zahlungen und bremst nie.
        movable_left = total - refunded
        same_rate = self._tax_rate_key(old_rate) == self._tax_rate_key(row['TaxRate'])

        def required(bank_date):
            """Das Erlöskonto, auf das die neue Rechnung die Zahlung mit diesem Datum der Bankbewegung bucht: bei einem Beleg ohne Steuerausweis das für das Datum
            hinterlegte (4185 bis 2025, 4184 ab 2026 – ``_payment_move_context`` hat beide, die gebraucht werden, vorab aufgelöst), sonst das EINE der Rechnung."""
            if move.get('revenue_by_number') is None:
                return move['revenue_coa']
            return move['revenue_by_number'].get(small_business_revenue_number(bank_date))

        def verdict(bank_id, amount):
            found, reason = self._movable_payment(conn, replaced, new_id, bank_id, amount, move['bank_coas'],
                                                  move['bank_coa_ids'])
            if found is None:
                return None, reason
            coa = required(found[2])
            if coa is None:
                return None, 'revenue_date'
            # Bei gleichem Steuersatz bleibt der Erlös auf seinem Konto: Liegt die Zahlung auf einem anderen als dem heute für Satz und Zahlungsdatum
            # hinterlegten (von Hand geändert, inzwischen ein anderes gelernt, oder – ein Kleinunternehmer – ab 2026 noch auf 4185 gebucht), buchte der
            # Umzug sie im vergangenen Zeitraum um
            if same_rate and found[1] != coa:
                return None, 'revenue_account'
            return (found[0], found[1], coa, found[2]), None

        verdicts = [verdict(bank_id, amount) for _pay_id, bank_id, amount in payments]
        moved = skipped = 0
        reasons = set()
        for (pay_id, bank_id, amount), (found, reason) in zip(payments, verdicts):
            if found is None:
                if amount > 0:
                    skipped += amount
                    reasons.add(reason)
                continue
            due_new = (row['SumGross'] or 0) - conn.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?', (new_id,)).fetchone()[0]
            part = min(amount, due_new, movable_left)
            if part <= 0:
                break                                  # die neue Rechnung ist bezahlt, oder das Guthaben ist aufgebraucht
            account_id, revenue_coa_old, revenue_coa_new, bank_date = found
            new_context = {'tax_rate': row['TaxRate'], 'account_id': account_id, 'bank_coa': move['bank_coas'][account_id],
                           'revenue_coa': revenue_coa_new, 'bank_coa_ids': move['bank_coa_ids']}
            if move.get('revenue_by_number') is not None:
                new_context['revenue_date_number'] = small_business_revenue_number(bank_date)
            log_length = len(tx.log)
            try:
                conn.execute('SAVEPOINT payment_move')
                self._unassign(conn, pay_id)
                rebooked = booked(self._assign_in_tx(tx, new_id, bank_id, part, new_context), part)
                if rebooked and amount > part:
                    rebooked = booked(self._assign_in_tx(
                        tx, old_id, bank_id, amount - part,
                        {'tax_rate': old_rate, 'account_id': account_id, 'bank_coa': move['bank_coas'][account_id],
                         'revenue_coa': revenue_coa_old, 'bank_coa_ids': move['bank_coa_ids']},
                        allow_cancelled=True), amount - part)
                if not rebooked:
                    conn.execute('ROLLBACK TO payment_move')
                    del tx.log[log_length:]
                conn.execute('RELEASE payment_move')
            except sqlite3.Error as exc:
                raise PaymentMoveFailed(
                    f'Die Zahlungen der stornierten Rechnung {old_number} ließen sich nicht übernehmen: '
                    f'Datenbankfehler ({exc}). Die Rechnung wurde nicht versendet.') from exc
            if rebooked:
                moved += part
                movable_left -= part
            else:
                skipped += amount
                reasons.add('rebook')

        # Was an der stornierten Rechnung bleibt, ist ihr Guthaben: das Übersprungene höchstens bis dorthin (ein Teil davon kann
        # schon erstattet sein – er steht in seinem eigenen Satz und zählt nicht zweimal), der Rest ohne Hindernis
        credit_left = max(total - moved - refunded, 0)
        skipped = min(skipped, credit_left)
        kept = credit_left - skipped
        notes = []
        if moved:
            notes.append(f'Zahlungen der stornierten Rechnung {old_number} über {euro(moved)} sind jetzt dieser '
                         'Rechnung zugeordnet.')
        else:
            notes.append(MOVE_NONE_HINT.format(number=old_number))
        if refunded:
            notes.append(MOVE_REFUNDED_HINT.format(amount=euro(refunded)))
        if kept > 0:
            notes.append(f'{euro(kept)} bleiben als Guthaben des Kunden an der stornierten Rechnung.')
        if moved and self._tax_rate_key(old_rate) != self._tax_rate_key(row['TaxRate']):
            notes.append('Der Steuersatz weicht ab – die Umsatzsteuer am Zahlungstag ändert sich.')
        if skipped:
            why = '; '.join(text for key, text in MOVE_SKIP_REASONS.items() if key in reasons)
            notes.append(f'{euro(skipped)} ließen sich nicht automatisch übernehmen und bleiben an der stornierten '
                         f'Rechnung: {why}.')
        return moved, ' '.join(notes)

    # ── Prüfung ──────────────────────────────────────────────────────────────
    def find_payment_inconsistencies(self, invoice_id=None):
        """Unstimmigkeiten der Zahlungszuordnungen – rein lesend, ändert nichts.

        ``invoice_id`` grenzt auf eine Rechnung ein. Returns eine Liste von Befunden
        ``{'category', 'invoice_id', 'invoice_number', 'text'}`` (Klartext, nicht maskiert);
        ``invoice_id`` ist ``None``, wenn die Rechnungsnummer keiner Rechnung gehört.
        Kategorien:

        * ``no_settlement``      Zuordnung ohne Satz „Bank an Debitoren“ (die Rechnung hat
                                 eine Forderung);
        * ``settlement_without_receivable`` Sätze „Bank an Debitoren“ der App, aber keine
                                 Forderung (mehr) zur Rechnung – 10000 steht im Haben-Überhang;
        * ``receivable_attached`` die Forderung hängt an einer Bankbewegung;
        * ``orphan_payment_rows`` Zahlungssatz unter einer Bankbewegung ohne Zuordnung: die
                                 Sätze der App (Marke, Text) und bankwirksame Erlössätze mit
                                 einer Rechnungsnummer als Beleg-Nr., zu denen es für das Paar
                                 (Rechnung, Bewegung) keine Zuordnung gibt;
        * ``transfer_mismatch``  zugeordneter Betrag ≠ auf das Erlöskonto umgebuchter;
        * ``payment_on_entry``   Zuordnung hängt an einem Buchungssatz, nicht an einer
                                 Bankbewegung;
        * ``duplicate_payment``  dasselbe Paar (Rechnung, Bewegung) mehrfach zugeordnet;
        * ``outgoing_payment``   Zuordnung auf einen Ausgang;
        * ``status_unsent``      Forderung gebucht, Status aber Entwurf/abgeschlossen;
        * ``amount_due_mismatch`` offener Rest passt nicht zu den Zuordnungen;
        * ``storno_counter_mismatch`` bei einer Rechnung mit Stornorechnung steht nur eine der beiden
                                 Buchungen („Forderung“ der Rechnung, Gegenbuchung der Stornorechnung);
        * ``legacy_cancelled_open`` eine von Hand stornierte Rechnung ohne Stornorechnung, deren
                                 Forderung noch in den Büchern steht;
        * ``refund_exceeds_credit`` zu einer Stornorechnung ist mehr erstattet als zur stornierten Rechnung
                                 gezahlt wurde (Σ Zuordnungen beider Belege < 0). Die App lässt es auf keinem Weg
                                 mehr entstehen (``_refund_removal_blocker``); die Kategorie meldet Eingriffe an
                                 ihr vorbei (SQL-Konsole, zurückgespielte Sicherung, Bestand älterer Versionen).
                                 Eine intakte Erstattung (Zuordnung an der Stornorechnung mit ihren Sätzen)
                                 ergibt in keiner Kategorie einen Befund;
        * ``rate_split_mismatch`` ein Beleg mit Steuersatz je Position (``MixedTaxRates``; Rechnung wie Stornorechnung), dessen
                                 Positionen nicht zu seinen Summen passen (die Sätze lassen sich nicht prüfen), dessen
                                 Forderungszeilen der App nicht genau die Satzgruppen der Rechnung buchen, oder der ganz
                                 bezahlt ist, ohne dass je Steuersatz sein Betrag und seine Steuer auf die Erlöskonten umgebucht
                                 wären (``_rate_findings``);
        * ``rate_refund_mismatch`` ein Storno-Paar mit Steuersatz je Position (die stornierte Rechnung oder die Stornorechnung trägt das
                                 Kennzeichen), dessen Stand je Satz sich lesen lässt und bei dem je Satz mehr erstattet ist als gezahlt –
                                 oder Betrag und Steuer nicht aufgehen: Die Summe kann aufgehen (``refund_exceeds_credit`` schweigt),
                                 und doch hat ein Satz mehr zurückbekommen, als er getragen hat. Die App lässt es auf keinem Weg entstehen
                                 (``_refund_removal_blocker`` prüft dasselbe je Satz); die Kategorie meldet Eingriffe an ihr vorbei, an der
                                 stornierten Rechnung (gemessen mit ``tax_groups.booked_fault``). Ein Paar ohne das Kennzeichen ist nie betroffen;
        * ``unflagged_rate_mix`` ein Beleg OHNE dieses Kennzeichen, dessen Positionen verschiedene Steuersätze tragen (Positionen
                                 ohne Satz zählen nicht, ein Beleg ohne Steuerausweis ist nie betroffen): Er wird mit EINEM Satz
                                 geführt, und seine Buchungen rechnen mit dem Satz im Kopf. Ein Bestand aus einer Version vor
                                 2.7.0 kann so etwas tragen; es wird nur genannt, nie umgedeutet;
        * ``tax_entry_mismatch`` die Momentaufnahmen eines Steuersatzes aus den Stammdaten (2.8.0, an den Positionen) widersprechen
                                 sich oder passen nicht zusammen: verschiedene Angaben (Art, Befreiungsgrund, Erlöskonto) bei
                                 gleichem Satz, ein Satz in Hundertstel Prozent, der keiner ist (0 oder 1 bis 100 %) oder nicht der Satz der
                                 Position ist, eine steuerfreie Position mit Satz über 0 oder ohne Grund, ein Erlöskonto, das keines (mehr)
                                 ist, ein Kopfsatz, der nicht zur Momentaufnahme passt, Angaben aus den Stammdaten ohne den Satz dazu
                                 (die halbe Momentaufnahme; ``_tax_entry_findings``). Kein Weg der App lässt es entstehen; die Kategorie
                                 meldet Eingriffe an ihr vorbei. Ein Beleg ohne Momentaufnahme ist nie betroffen – auch nicht einer mit
                                 der Art „steuerfrei“ allein, wie das Beispiel eines Belegs nach § 19 (das Merkmal ist ``TaxRateBp``).

        ``no_settlement`` und ``transfer_mismatch`` betrachten nur Zuordnungen, deren Buchung
        eine Bankbewegung ohne Sätze mit Quellklammer (``SourceGroup``) ist: Der WISO-Import
        liefert seine Sätze selbst (und kennt Barzahlungen ohne Bankbewegung). Die beiden
        letzten Kategorien zählen nur Forderungen, die die App selbst gebucht hat
        (``_app_receivable_amounts``), damit ein Import keinen Fehlalarm auslöst; ein intaktes Paar
        mit Guthaben (Zahlungen an der stornierten Rechnung) ergibt keinen Befund.

        Die Abfragen gehen von den Zuordnungen aus bzw. fassen je Rechnung vorab zusammen; die
        Rechnungsnummern mit Forderung und die Forderungen der App (mit ihren Beträgen) werden
        je einmal gelesen. Der Aufwand wächst so linear mit dem Bestand (4000 Bewegungen/Forderungen,
        1000 Zuordnungen: unter einer Sekunde), die Zahl der Anweisungen nicht mit der Zahl der Paare.
        """
        conn = self._get_connection()
        try:
            return self._payment_findings(conn, invoice_id)
        finally:
            conn.close()

    def _payment_findings(self, conn, invoice_id):
        debtor = self.DEBTOR_ACCOUNT_NUMBER
        waiting = tuple(number for number, _name in self.OPEN_REVENUE_ACCOUNTS.values())
        cursor = conn.cursor()
        is_invoice = "(i.DocumentType = 'invoice' OR i.DocumentType IS NULL)"
        only = ' AND i.ID = ?' if invoice_id is not None else ''
        only_params = [invoice_id] if invoice_id is not None else []
        # Bankbewegung mit Sätzen aus dem Quellsystem (WISO): die Sätze gehören dem Import
        has_source_group = ("EXISTS (SELECT 1 FROM Bookings w WHERE w.ParentBooking_ID = b.ID "
                            "AND w.SourceGroup IS NOT NULL AND w.SourceGroup <> '')")
        findings = []

        def add(category, inv_id, number, text):
            findings.append({'category': category, 'invoice_id': inv_id,
                             'invoice_number': number or '', 'text': text})

        def euro(minor):
            return fmt_eur(from_minor(minor or 0))

        # Rechnungsnummern, zu denen eine Forderung gebucht ist (``receivable_condition``),
        # einmal gelesen – nicht je Zuordnung nachgeschlagen
        receivable_sql, receivable_params = self._receivable_sql(cursor, alias='r')
        number_filter, number_params = '', []
        if invoice_id is not None:
            row = conn.execute('SELECT InvoiceNumber FROM Invoices WHERE ID = ?',
                               (invoice_id,)).fetchone()
            if row is None:
                return findings
            number_filter, number_params = ' AND r.DocumentNumber = ?', [row[0]]
        with_receivable = {row[0] for row in conn.execute(
            f"SELECT DISTINCT r.DocumentNumber FROM Bookings r WHERE r.BookingType = 'entry' "
            f"AND r.DocumentNumber IS NOT NULL AND {receivable_sql}{number_filter}",
            receivable_params + number_params)}

        for inv, number, pay_id, bank_id, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, ip.BookingID, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "JOIN Bookings b ON b.ID = ip.BookingID AND b.BookingType = 'bank' "
                "WHERE NOT EXISTS (SELECT 1 FROM Bookings s WHERE s.ParentBooking_ID = b.ID "
                "AND s.DocumentNumber = i.InvoiceNumber AND s.CounterCOA_ID IN "
                f"(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?)) "
                f"AND NOT {has_source_group}{only} ORDER BY i.InvoiceNumber, ip.ID",
                [debtor] + only_params):
            if number in with_receivable:
                add('no_settlement', inv, number,
                    f'Zuordnung #{pay_id} ({euro(amount)}) auf Bewegung #{bank_id}: Der '
                    f'Buchungssatz „Bank an Debitoren“ fehlt.')

        for inv, number, count, total in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, COUNT(*), SUM(k.Amount) FROM Bookings k "
                f"JOIN Invoices i ON i.InvoiceNumber = k.DocumentNumber AND {is_invoice} "
                "WHERE k.Status = ? AND k.ParentBooking_ID IS NOT NULL AND k.CounterCOA_ID IN "
                f"(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?){only} "
                "GROUP BY i.ID, i.InvoiceNumber ORDER BY i.InvoiceNumber",
                [PAYMENT_MARK, debtor] + only_params):
            if number not in with_receivable:
                add('settlement_without_receivable', inv, number,
                    f'Es gibt {count} Zahlungssatz(sätze) „Bank an Debitoren“ ({euro(total)}), '
                    f'aber keine Forderung mehr zur Rechnung – auf dem Debitorenkonto steht ein '
                    f'Haben-Überhang.')

        for rec_id, number, bank_id, amount, inv in conn.execute(
                "SELECT r.ID, r.DocumentNumber, r.ParentBooking_ID, r.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = r.DocumentNumber) "
                f"FROM Bookings r WHERE r.BookingType = 'entry' AND r.ParentBooking_ID IS NOT NULL "
                f"AND {receivable_sql} ORDER BY r.ID", receivable_params):
            if invoice_id is None or inv == invoice_id:
                add('receivable_attached', inv, number,
                    f'Die Forderung #{rec_id} ({euro(amount)}) hängt an der Bankbewegung '
                    f'#{bank_id}; sie gehört zur Rechnung, nicht zur Bewegung.')

        # Zahlungssätze ohne Zuordnung: die Marke oder der Text der App – oder ein bankwirksamer
        # Erlössatz (Erlöskonto gegen ein Zahlungskonto) mit einer Rechnungsnummer als Beleg-Nr.,
        # unabhängig vom Text (Altbestand „Zahlung verbuchen mit Konto“ nach dem Entfernen)
        revenue_ids = [row[0] for row in self._income_coas(cursor)]
        payment_ids = sorted(self._get_payment_coa_ids(cursor))
        rev = ','.join('?' * len(revenue_ids))
        pay = ','.join('?' * len(payment_ids))
        for row_id, bank_id, number, amount, inv in conn.execute(
                "SELECT k.ID, k.ParentBooking_ID, k.DocumentNumber, k.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = k.DocumentNumber) "
                "FROM Bookings k WHERE k.ParentBooking_ID IS NOT NULL "
                "AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                "AND (k.Status = ? OR k.Text LIKE 'Zahlung zu Re. %' OR k.Text LIKE 'Umb. zu Re. %' "
                "OR (k.BookingType = 'entry' "
                f"AND ((k.COA_ID IN ({rev}) AND k.CounterCOA_ID IN ({pay})) "
                f"OR (k.CounterCOA_ID IN ({rev}) AND k.COA_ID IN ({pay}))) "
                "AND EXISTS (SELECT 1 FROM Bookings p WHERE p.ID = k.ParentBooking_ID "
                "AND p.BookingType = 'bank') "
                "AND EXISTS (SELECT 1 FROM Invoices x WHERE x.InvoiceNumber = k.DocumentNumber "
                "AND (x.DocumentType = 'invoice' OR x.DocumentType IS NULL)))) "
                "AND NOT EXISTS (SELECT 1 FROM InvoicePayments ip "
                "JOIN Invoices i2 ON i2.ID = ip.InvoiceID WHERE ip.BookingID = k.ParentBooking_ID "
                "AND i2.InvoiceNumber = k.DocumentNumber) ORDER BY k.ID",
                [PAYMENT_MARK] + revenue_ids + payment_ids + revenue_ids + payment_ids):
            if invoice_id is None or inv == invoice_id:
                add('orphan_payment_rows', inv, number,
                    f'Zahlungssatz #{row_id} ({euro(amount)}) unter Bewegung #{bank_id} hat '
                    f'keine Zuordnung zur Rechnung.')

        # Zugeordnet (Bankbewegungen ohne Sätze des Imports) gegen umgebucht (Sätze „Wartekonto
        # an Erlöskonto“ der App/des Altbestands), je Rechnung vorab zusammengefasst
        marks = ','.join('?' * len(waiting))
        for inv, number, paid, moved in conn.execute(
                "WITH paid AS (SELECT ip.InvoiceID AS InvoiceID, SUM(ip.Amount) AS Total "
                "FROM InvoicePayments ip JOIN Bookings b ON b.ID = ip.BookingID "
                f"WHERE b.BookingType = 'bank' AND NOT {has_source_group} "
                "GROUP BY ip.InvoiceID), "
                "moved AS (SELECT k.DocumentNumber AS Number, SUM(k.Amount) AS Total "
                "FROM Bookings k JOIN ChartOfAccounts c ON c.ID = k.COA_ID "
                "WHERE k.ParentBooking_ID IS NOT NULL AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                f"AND c.AccountNumber IN ({marks}) GROUP BY k.DocumentNumber) "
                "SELECT i.ID, i.InvoiceNumber, COALESCE(paid.Total, 0), COALESCE(moved.Total, 0) "
                "FROM Invoices i LEFT JOIN paid ON paid.InvoiceID = i.ID "
                "LEFT JOIN moved ON moved.Number = i.InvoiceNumber "
                f"WHERE {is_invoice}{only} AND COALESCE(paid.Total, 0) <> COALESCE(moved.Total, 0) "
                "ORDER BY i.InvoiceNumber", list(waiting) + only_params):
            if number in with_receivable:
                add('transfer_mismatch', inv, number,
                    f'Zugeordnet sind {euro(paid)}, auf das Erlöskonto umgebucht {euro(moved)}.')

        for inv, number, pay_id, booking_id, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, b.ID, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "JOIN Bookings b ON b.ID = ip.BookingID "
                "WHERE COALESCE(b.BookingType, 'entry') <> 'bank' "
                f"AND (b.SourceGroup IS NULL OR b.SourceGroup = ''){only} "
                "ORDER BY i.InvoiceNumber, ip.ID", only_params):
            add('payment_on_entry', inv, number,
                f'Zuordnung #{pay_id} ({euro(amount)}) hängt am Buchungssatz #{booking_id}, '
                f'nicht an einer Bankbewegung.')

        for inv, number, bank_id, count, total in conn.execute(
                "SELECT ip.InvoiceID, i.InvoiceNumber, ip.BookingID, COUNT(*), SUM(ip.Amount) "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                f"WHERE 1 = 1{only} GROUP BY ip.InvoiceID, ip.BookingID HAVING COUNT(*) > 1 "
                "ORDER BY i.InvoiceNumber", only_params):
            add('duplicate_payment', inv, number,
                f'Bewegung #{bank_id} ist {count}-mal zugeordnet (zusammen {euro(total)}).')

        for inv, number, pay_id, bank_id, bank_amount, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, b.ID, b.Amount, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                f"JOIN Bookings b ON b.ID = ip.BookingID WHERE b.Amount < 0 AND ip.Amount > 0{only} "
                "ORDER BY i.InvoiceNumber, ip.ID", only_params):
            add('outgoing_payment', inv, number,
                f'Zuordnung #{pay_id}: Bewegung #{bank_id} ist ein Ausgang ({euro(bank_amount)}), '
                f'zugeordnet sind {euro(amount)}.')

        for inv, number, status in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, i.Status FROM Invoices i "
                f"WHERE i.Status IN ('draft', 'finalized') AND {is_invoice}{only} "
                "ORDER BY i.InvoiceNumber", only_params):
            if number in with_receivable:
                add('status_unsent', inv, number,
                    f'Die Rechnung steht als Forderung in den Büchern, hat aber noch den Status '
                    f'„{status}“ (nicht versendet).')

        for inv, number, gross, due, paid, kind in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, i.SumGross, i.AmountDue, "
                "COALESCE((SELECT SUM(Amount) FROM InvoicePayments WHERE InvoiceID = i.ID), 0), "
                "i.InvoiceKind "
                f"FROM Invoices i WHERE {is_invoice}{only} ORDER BY i.InvoiceNumber",
                only_params):
            # Der Rest einer Stornorechnung ist ihr negativer Betrag (es gibt keine Zahlung): kein Befund
            overpaid = due is not None and due < 0 and kind != STORNO_KIND
            if due is None or overpaid or due != (gross or 0) - paid:
                add('amount_due_mismatch', inv, number,
                    f'Offen sind {euro(due)}, nach den Zuordnungen müssten es '
                    f'{euro((gross or 0) - paid)} sein.')

        # Storno-Paare: Die Forderung der Rechnung und die Gegenbuchung der Stornorechnung gehören zusammen
        pair_only = ' AND (o.ID = ? OR s.ID = ?)' if invoice_id is not None else ''
        pairs = conn.execute(
            "SELECT o.ID, o.InvoiceNumber, s.InvoiceNumber FROM Invoices s "
            "JOIN Invoices o ON o.ID = s.CancelsInvoiceId "
            f"WHERE s.InvoiceKind = ?{pair_only} ORDER BY o.InvoiceNumber, s.ID",
            [STORNO_KIND, *only_params, *only_params]).fetchall()
        # Von Hand storniert (Altbestand), keine Stornorechnung – und die Forderung steht noch in den Büchern
        legacy = conn.execute(
            "SELECT i.ID, i.InvoiceNumber FROM Invoices i WHERE i.Status = 'cancelled' "
            "AND COALESCE(i.SumGross, 0) > 0 AND i.InvoiceKind IS NOT ? "
            f"AND NOT EXISTS (SELECT 1 FROM Invoices s WHERE s.CancelsInvoiceId = i.ID) AND {is_invoice}{only} "
            "ORDER BY i.InvoiceNumber", [STORNO_KIND, *only_params]).fetchall()

        # Die Forderungen der App werden für alle Paare und Rechnungen zusammen einmal gelesen – je Rechnung nachzuschlagen
        # wäre quadratisch (``DocumentNumber`` ist nicht indiziert). Der Filter auf eine Rechnung liest gezielt.
        app_receivables = self._app_receivable_amounts(conn) if invoice_id is None and (pairs or legacy) else None

        def app_receivables_of(number):
            if app_receivables is not None:
                return app_receivables.get(number, [])
            return self._app_receivable_amounts(conn, number).get(number, [])

        for original_id, original_number, storno_number in pairs:
            has_receivable = bool(app_receivables_of(original_number))
            has_counter = bool(app_receivables_of(storno_number))
            if has_receivable and not has_counter:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Zur Rechnung steht die Forderung, aber die Gegenbuchung der Stornorechnung '
                    f'{storno_number} fehlt – auf dem Debitorenkonto bleibt der Betrag stehen.')
            elif has_counter and not has_receivable:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Die Stornorechnung {storno_number} hat eine Gegenbuchung, aber zur Rechnung steht '
                    f'keine Forderung der App – auf dem Debitorenkonto steht ein Haben-Überhang.')

        for inv, number in legacy:
            receivable = app_receivables_of(number)
            if receivable:
                booked = sum(amount for _booking_id, amount in receivable)
                add('legacy_cancelled_open', inv, number,
                    f'Die Rechnung ist storniert, ihre Forderung steht aber noch in den Büchern ({euro(booked)}). '
                    f'Abhilfe: „Stornorechnung nachträglich erstellen“.')

        # Mehr erstattet als gezahlt: die Zuordnungen des Paares (Zahlungen minus Erstattungen) ergeben ein negatives Guthaben,
        # etwa nach einem Eingriff an der App vorbei. Je Rechnung vorab zusammengefasst, nicht je Paar nachgeschlagen.
        for storno_id, storno_number, original_number, paid, refunded in conn.execute(
                "WITH sums AS (SELECT InvoiceID, SUM(Amount) AS Total FROM InvoicePayments GROUP BY InvoiceID) "
                "SELECT s.ID, s.InvoiceNumber, o.InvoiceNumber, COALESCE(po.Total, 0), -COALESCE(ps.Total, 0) "
                "FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId "
                "LEFT JOIN sums po ON po.InvoiceID = o.ID LEFT JOIN sums ps ON ps.InvoiceID = s.ID "
                f"WHERE s.InvoiceKind = ?{pair_only} AND COALESCE(po.Total, 0) + COALESCE(ps.Total, 0) < 0 "
                "ORDER BY o.InvoiceNumber, s.ID", [STORNO_KIND, *only_params, *only_params]):
            add('refund_exceeds_credit', storno_id, storno_number,
                f'Zur Stornorechnung {storno_number} ist mehr erstattet ({euro(refunded)}) als zur Rechnung '
                f'{original_number} gezahlt wurde ({euro(paid)}).')

        findings.extend(self._rate_findings(conn, invoice_id))
        return findings

    def _rate_findings(self, conn, invoice_id):
        """Die Befunde zu den Steuersätzen der Belege (``rate_split_mismatch``, ``rate_refund_mismatch``, ``unflagged_rate_mix``, ``tax_entry_mismatch``) in der
        Form von ``find_payment_inconsistencies`` – nur lesend, in der Verbindung oder Transaktion ``conn``, linear im Bestand: Für alle Belege mit dem Kennzeichen
        (``invoice_id`` grenzt ein) je eine Abfrage über die Köpfe, die Positionen, die Zuordnungen, die Forderungszeilen der App und die markierten
        Umbuchungen, für alle Storno-Paare mit dem Kennzeichen je eine über die Köpfe samt Positionen der stornierten Rechnungen, die Zuordnungen je
        Bankbewegung und die markierten Sätze, für alle ohne das Kennzeichen eine über die Sätze ihrer Positionen, für alle Belege mit einer Momentaufnahme
        eine über ihre Positionen – die Zahl der Anweisungen wächst nicht mit der Zahl der Belege. Ein Beleg mit einem Satz ist von den ersten drei nie
        betroffen (``tests/test_single_rate_snapshots.py``). Die Positionen werden mit ihrem Satz in Hundertstel Prozent gelesen (``InvoiceItems.TaxRateBp``, die
        Momentaufnahme eines Steuersatzes aus den Stammdaten): Die Gruppen rechnen so wie beim Speichern (einstufig, ``tax_groups.group_tax``), und eine steuerfreie
        Gruppe heißt in den Listen der Meldungen „steuerfrei“ (``tax_groups.group_label``), in ihren Sätzen „steuerfreie Leistungen“ (``rate_sentence``) – für jeden anderen
        Satz sind die Texte Zeichen für Zeichen die von 2.7.0.

        ``rate_split_mismatch`` (Rechnung wie Stornorechnung, jeder Status): (1) die Positionen passen nicht zu den Summen (``tax_groups.mixed_groups``) –
        dann lässt sich nichts weiter prüfen; (2) es gibt Forderungszeilen der App, und ihre Menge aus Satz, Betrag und Steuer ist nicht die der Satzgruppen
        (eine fehlt, eine ist zu viel, eine trägt einen anderen Betrag); (3) die Zuordnungen ergeben genau das Brutto (≠ 0), aber die markierten
        Umbuchungen auf die Erlöskonten tragen je Satz nicht dessen Betrag und Steuer. Gerechnet wird in Minor Units und mit dem Satz nach ``rate_pct``
        (0,19 und 19 sind derselbe); ein Betrag, der keine ganze Zahl ist, zählt nicht mit (der Befund nennt dann, was lesbar ist). ``rate_refund_mismatch`` (an
        der stornierten Rechnung, ein Befund je Paar): Der Stand je Satz – was die Zahlungen der stornierten Rechnung und die Erstattungen an der Stornorechnung
        je Satz tragen – wird wie beim Schutz vor dem Entfernen einer Zahlung gelesen (``booked_by_rate``: ``_mixed_booked`` für alle Paare auf einmal) und
        mit ``tax_groups.booked_fault`` am Topf der Zahlungen (``refund_pool``) gemessen; lässt er sich nicht lesen (eine Zuordnung ohne die Sätze der App,
        von Hand geänderte Sätze, Positionen, die nicht zu den Summen passen), gibt es diesen Befund nicht. ``unflagged_rate_mix``:
        die Positionen eines Belegs ohne das Kennzeichen tragen mehr als einen Satz (nach ``rate_pct``, bei einer Momentaufnahme nach ihrem Satz; ohne Satz
        zählt nicht; Kopf unter 0, der Platzhalter für „ohne Steuerausweis“, nie).

        ``tax_entry_mismatch`` (je Beleg und Art des Befundes ein Satz, ohne Beträge, mit dem Satz der Gruppe als ``rate_sentence``: „steuerfreie Leistungen“ für eine
        steuerfreie Gruppe; nur Rechnungen – eine Stornorechnung wie jede –, jeder Status): Die Momentaufnahmen der Positionen (``group_snapshots``) widersprechen sich oder
        passen nicht zusammen –
        (1) Positionen EINES Satzes tragen verschiedene Angaben (Art, Befreiungsgrund, Erlöskonto) oder teils eine Momentaufnahme, teils keine; (2) der Satz in
        Hundertstel Prozent ist nicht der Satz der Position (``rate_pct`` mal 100) oder keine ganze Zahl, die 0 ist oder von 1 bis 100 % reicht (``valid_rate_bp``: ein Wert
        von 1 bis 99 Hundertstel Prozent ist ein Befund); (3) eine steuerfreie Position (Art ``'E'``, mit Satz in Hundertstel Prozent) trägt einen Satz über 0 oder keinen Grund
        der Befreiung; (4) das Erlöskonto einer Position ist kein Erlöskonto (mehr: ``_income_coas``); (5) bei einem Beleg ohne das Kennzeichen passt der Satz im Kopf nicht zum
        Satz der Momentaufnahme; (6) die **halbe Momentaufnahme** (``half``): eine Position ohne Satz in Hundertstel Prozent, aber mit Befreiungsgrund oder Erlöskonto – ein
        Befund je Beleg; beim Buchen ist sie ein Widerspruch (``TAX_ENTRY_CONTRADICTION``). **Das Merkmal ist ``TaxRateBp`` nicht NULL:** Eine Position mit der Art ``'E'`` und
        sonst nichts – das Beispiel eines Belegs nach § 19 – ist kein Befund. Gelesen wird jede Position eines Belegs, der eine Momentaufnahme oder eine halbe trägt; ein Beleg
        ohne beides (der ganze Bestand) ist nie betroffen."""
        flagged = "typeof(i.MixedTaxRates) = 'integer' AND i.MixedTaxRates = 1"
        is_invoice = "(i.DocumentType = 'invoice' OR i.DocumentType IS NULL)"
        only = ' AND i.ID = ?' if invoice_id is not None else ''
        only_params = [invoice_id] if invoice_id is not None else []
        findings = []

        def add(category, inv_id, number, text):
            findings.append({'category': category, 'invoice_id': inv_id, 'invoice_number': number or '', 'text': text})

        def euro(minor):
            return fmt_eur(from_minor(minor))

        def shown(entries, exempt=()):
            """``19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €)`` – nach Satz absteigend; ``exempt``: die Sätze der steuerfreien Gruppen
            („steuerfrei: …“ statt „0 %: …“)."""
            return '; '.join(f'{group_label(pct, pct in exempt)}: {euro(amount)} (Steuer {euro(tax)})'
                             for pct, amount, tax in sorted(entries, reverse=True)) or 'nichts'

        def pct_of(rate):
            """Der Satz als Prozentzahl (``rate_pct``; NULL und leer heißen 0 %), ``None``, wenn er sich nicht lesen lässt."""
            try:
                pct = rate_pct(rate)
            except (ArithmeticError, ValueError):
                return None
            return Decimal(0) if pct is None else pct

        heads = conn.execute(
            f'SELECT i.ID, i.InvoiceNumber, i.SumNet, i.TaxAmount, i.SumGross FROM Invoices i WHERE {flagged} AND {is_invoice}{only} '
            'ORDER BY i.InvoiceNumber, i.ID', only_params).fetchall()
        if heads:
            # Je Position (Satz, Netto, Satz in Hundertstel Prozent der Momentaufnahme – NULL ohne): ``mixed_groups`` rechnet damit wie das Speichern. ``exempt_of``:
            # die Sätze der steuerfreien Gruppen eines Belegs (Art ``'E'``), nur für die Texte
            positions, exempt_of = {}, {}
            for inv, rate, net, bp, category in conn.execute(
                    'SELECT p.InvoiceId, p.TaxRate, p.TotalNet, p.TaxRateBp, p.TaxCategory FROM InvoiceItems p JOIN Invoices i ON i.ID = p.InvoiceId '
                    f'WHERE {flagged} AND {is_invoice}{only} ORDER BY p.InvoiceId, p.Position, p.ID', only_params):
                positions.setdefault(inv, []).append((rate, net, bp))
                if category == 'E' and type(bp) is int:
                    exempt_of.setdefault(inv, set()).add(bp_pct(bp))
            paid = dict(conn.execute(
                'SELECT ip.InvoiceID, SUM(ip.Amount) FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
                f'WHERE {flagged} AND {is_invoice}{only} GROUP BY ip.InvoiceID', only_params))

            # Die Forderungszeilen der App (``_is_app_receivable``) je Beleg-Nr.: ``{Nr.: [(Satz, Betrag, Steuer)]}``. ``DocumentNumber`` hat keinen Index:
            # Ohne Filter auf einen Beleg wird die Tabelle einmal gelesen und in Python auf die Nummern der gekennzeichneten Belege eingegrenzt
            numbers = {head[1] for head in heads}
            waiting_ids = self._waiting_coa_ids(conn)
            receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
            by_number, number_params = ((' AND DocumentNumber = ?', [heads[0][1]]) if invoice_id is not None else ('', []))
            receivables = {}
            for number, rate, amount, tax, source_group, counter in conn.execute(
                    "SELECT DocumentNumber, TaxRate, Amount, COALESCE(TaxAmount, 0), COALESCE(SourceGroup, ''), CounterCOA_ID FROM Bookings "
                    "WHERE BookingType = 'entry' AND DocumentNumber IS NOT NULL AND typeof(Amount) = 'integer' "
                    f"AND typeof(COALESCE(TaxAmount, 0)) = 'integer' AND {receivable_sql}{by_number} ORDER BY ID",
                    [*receivable_params, *number_params]):
                pct = pct_of(rate)
                if number in numbers and pct is not None and self._is_app_receivable(source_group, counter, waiting_ids):
                    receivables.setdefault(number, []).append((pct, amount, tax))

            # Was die markierten Umbuchungen der Zuordnungen je Satz tragen – dieselben Sätze, die ``_mixed_booked`` zählt: ``{Nr.: {Satz: (Betrag, Steuer)}}``
            marks = ','.join('?' * len(waiting_ids)) or 'NULL'
            moved = {}
            for number, rate, amount, tax in conn.execute(
                    'SELECT k.DocumentNumber, k.TaxRate, k.Amount, COALESCE(k.TaxAmount, 0) FROM Bookings k '
                    "WHERE k.Status = ? AND k.ParentBooking_ID IS NOT NULL AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                    "AND typeof(k.Amount) = 'integer' AND typeof(COALESCE(k.TaxAmount, 0)) = 'integer' "
                    f'AND ((k.EntryForm = ? AND k.COA_ID IN ({marks})) OR k.EntryForm = ?) '
                    f'AND k.DocumentNumber IN (SELECT i.InvoiceNumber FROM Invoices i WHERE {flagged} AND {is_invoice}{only}) ORDER BY k.ID',
                    [PAYMENT_MARK, FORM_TRANSFER, *sorted(waiting_ids), FORM_PURPOSE_FIRST, *only_params]):
                pct = pct_of(rate)
                if pct is not None:
                    done_amount, done_tax = moved.setdefault(number, {}).get(pct, (0, 0))
                    moved[number][pct] = (done_amount + amount, done_tax + tax)

            for inv, number, sum_net, tax_amount, sum_gross in heads:
                try:
                    groups = mixed_groups(sum_net, tax_amount, sum_gross, positions.get(inv, []))
                except RateGroupsMismatch:
                    add('rate_split_mismatch', inv, number,
                        'Die Positionen der Rechnung passen nicht zu ihren Summen – die Steuersätze lassen sich nicht prüfen.')
                    continue
                wanted = [(group.pct, group.gross, group.tax) for group in groups]
                exempt = exempt_of.get(inv, ())
                booked = receivables.get(number)
                if booked and sorted(booked) != sorted(wanted):
                    add('rate_split_mismatch', inv, number,
                        f'Die Forderungsbuchungen der App passen nicht zu den Steuersätzen der Rechnung: erwartet {shown(wanted, exempt)}, '
                        f'gebucht {shown(booked, exempt)}.')
                if sum_gross and paid.get(inv) == sum_gross:
                    done = moved.get(number, {})
                    if done != {pct: (gross, tax) for pct, gross, tax in wanted}:
                        add('rate_split_mismatch', inv, number,
                            'Die Rechnung ist ganz bezahlt, aber auf die Erlöskonten ist je Steuersatz nicht ihr Betrag umgebucht: '
                            f'erwartet {shown(wanted, exempt)}, umgebucht {shown([(pct, amount, tax) for pct, (amount, tax) in done.items()], exempt)}.')

        # Mehr erstattet als gezahlt, je Satz: die Storno-Paare, von denen die stornierte Rechnung oder die Stornorechnung das Kennzeichen trägt. Dieselben
        # Bausteine wie der Schutz beim Entfernen einer Zahlung (``booked_by_rate``, ``refund_pool``, ``booked_fault``), nur je Art EINE Abfrage für alle Paare:
        # die Köpfe und Positionen der stornierten Rechnungen, die Zuordnungen beider Belege je Bankbewegung, die markierten Sätze zu ihren Nummern
        pair_args = [STORNO_KIND] + ([invoice_id, invoice_id] if invoice_id is not None else [])
        pair_from = ('FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId WHERE s.InvoiceKind = ? '
                     "AND ((typeof(o.MixedTaxRates) = 'integer' AND o.MixedTaxRates = 1) OR (typeof(s.MixedTaxRates) = 'integer' AND s.MixedTaxRates = 1))"
                     f"{' AND (o.ID = ? OR s.ID = ?)' if invoice_id is not None else ''}")
        refund_pairs = conn.execute('SELECT o.ID, o.InvoiceNumber, o.SumNet, o.TaxAmount, o.SumGross, s.ID, s.InvoiceNumber '
                                    f'{pair_from} ORDER BY o.InvoiceNumber, s.ID', pair_args).fetchall()
        if refund_pairs:
            original_items, original_exempt = {}, {}
            for inv, rate, net, bp, category in conn.execute(
                    f'SELECT p.InvoiceId, p.TaxRate, p.TotalNet, p.TaxRateBp, p.TaxCategory FROM InvoiceItems p WHERE p.InvoiceId IN (SELECT o.ID {pair_from}) '
                    'ORDER BY p.InvoiceId, p.Position, p.ID', pair_args):
                original_items.setdefault(inv, []).append((rate, net, bp))
                if category == 'E' and type(bp) is int:
                    original_exempt.setdefault(inv, set()).add(bp_pct(bp))
            allocations = {}
            for inv, bank_id, total in conn.execute(
                    'SELECT ip.InvoiceID, ip.BookingID, SUM(ip.Amount) FROM InvoicePayments ip WHERE ip.InvoiceID IN '
                    f'(SELECT o.ID {pair_from} UNION SELECT s.ID {pair_from}) GROUP BY ip.InvoiceID, ip.BookingID ORDER BY ip.InvoiceID, ip.BookingID',
                    pair_args * 2):
                allocations.setdefault(inv, []).append((bank_id, total))
            marked_rows = {}
            for number, bank_id, form, coa, amount, rate, tax in conn.execute(
                    'SELECT k.DocumentNumber, k.ParentBooking_ID, k.EntryForm, k.COA_ID, k.Amount, k.TaxRate, k.TaxAmount FROM Bookings k '
                    "WHERE k.Status = ? AND k.ParentBooking_ID IS NOT NULL AND (k.SourceGroup IS NULL OR k.SourceGroup = '') AND k.DocumentNumber IN "
                    f'(SELECT o.InvoiceNumber {pair_from} UNION SELECT s.InvoiceNumber {pair_from}) ORDER BY k.ID', [PAYMENT_MARK, *pair_args * 2]):
                marked_rows.setdefault(number, {}).setdefault(bank_id, []).append((form, coa, amount, rate, tax))
            waiting_ids = self._waiting_coa_ids(conn)
            for original_id, original_number, sum_net, tax_amount, sum_gross, storno_id, storno_number in refund_pairs:
                try:
                    pcts = {group.pct for group in mixed_groups(sum_net, tax_amount, sum_gross, original_items.get(original_id, []))}
                    paid = booked_by_rate(allocations.get(original_id, []), marked_rows.get(original_number, {}), pcts, waiting_ids)
                    refunded = {pct: (-amount, -tax) for pct, (amount, tax) in
                                booked_by_rate(allocations.get(storno_id, []), marked_rows.get(storno_number, {}), pcts, waiting_ids).items()}
                except (MixedNotBookable, RateGroupsMismatch):
                    continue                                    # der Stand je Satz lässt sich nicht lesen: Die Summe prüfen die anderen Kategorien
                if booked_fault(refund_pool(paid), refunded):
                    exempt = original_exempt.get(original_id, ())
                    add('rate_refund_mismatch', original_id, original_number,
                        'Je Steuersatz ist mehr erstattet als gezahlt (oder Betrag und Steuer gehen nicht auf): '
                        f'gezahlt {shown([(pct, amount, tax) for pct, (amount, tax) in paid.items()], exempt)} – '
                        f'erstattet {shown([(pct, amount, tax) for pct, (amount, tax) in refunded.items()], exempt)}.')

        # Ein Beleg ohne das Kennzeichen, dessen Positionen verschiedene Sätze tragen: Er wird mit dem Satz im Kopf gebucht. Der Satz einer Position ist bei einer
        # Momentaufnahme der ihre (``tax_groups.item_rate_of``), sonst ``rate_pct`` ihres Satzes
        rates = {}
        for inv, number, rate, bp in conn.execute(
                'SELECT DISTINCT i.ID, i.InvoiceNumber, p.TaxRate, p.TaxRateBp FROM InvoiceItems p JOIN Invoices i ON i.ID = p.InvoiceId '
                f"WHERE NOT ({flagged}) AND {is_invoice} AND COALESCE(i.TaxRate, 0) >= 0 AND p.TaxRate IS NOT NULL AND p.TaxRate <> ''{only} "
                'ORDER BY i.InvoiceNumber, i.ID', only_params):
            pct = pct_of(rate) if type(bp) is not int else item_rate_of(rate, bp)
            if pct is not None:
                rates.setdefault((inv, number), set()).add(pct)
        for (inv, number), found in rates.items():
            if len(found) > 1:
                add('unflagged_rate_mix', inv, number,
                    'Die Positionen der Rechnung tragen verschiedene Steuersätze, die Rechnung wird aber mit einem Satz geführt – '
                    'ihre Buchungen rechnen mit dem Satz im Kopf.')

        self._tax_entry_findings(conn, invoice_id, add, pct_of)
        return findings

    def _tax_entry_findings(self, conn, invoice_id, add, pct_of):
        """Der Befund ``tax_entry_mismatch`` (siehe ``_rate_findings``): EINE Abfrage über die Positionen aller Rechnungen, die eine Momentaufnahme eines Steuersatzes
        aus den Stammdaten tragen (``TaxRateBp`` nicht NULL – das Merkmal; die Art ``'E'`` allein ist keines) oder Angaben aus den Stammdaten ohne den Satz dazu
        (Befreiungsgrund oder Erlöskonto: die halbe Momentaufnahme) – jede ihrer Positionen: ob eine Gruppe Positionen mit und ohne Momentaufnahme mischt, zeigt erst die ganze
        Rechnung –, dazu die Erlöskonten, die es gibt (``_income_coas``). Je Beleg, Art und Satz ein Befund, mit den festen Sätzen ``TAX_ENTRY_FINDINGS``.
        ``add`` und ``pct_of`` gibt ``_rate_findings`` weiter, ``invoice_id`` grenzt auf einen Beleg ein.

        Dieselben Regeln wie beim Buchen (``_read_snapshots``): Der Satz in Hundertstel Prozent ist eine ganze Zahl, die 0 ist oder von 100 bis 10000 reicht (``valid_rate_bp``)
        und der Satz der Position. Eine Position mit der Art ``'E'`` und sonst nichts – das Beispiel eines Belegs nach § 19, ohne Satz, Grund und Konto – ist kein Befund."""
        flagged = "typeof(i.MixedTaxRates) = 'integer' AND i.MixedTaxRates = 1"
        only = ' AND i.ID = ?' if invoice_id is not None else ''
        rows = conn.execute(
            'SELECT i.ID, i.InvoiceNumber, i.TaxRate, p.TaxRate, p.TaxCategory, p.TaxRateBp, p.TaxExemptReason, p.RevenueCoaId, '
            f'CASE WHEN {flagged} THEN 1 ELSE 0 END FROM InvoiceItems p JOIN Invoices i ON i.ID = p.InvoiceId '
            "WHERE (i.DocumentType = 'invoice' OR i.DocumentType IS NULL) AND i.ID IN "
            f"(SELECT InvoiceId FROM InvoiceItems WHERE TaxRateBp IS NOT NULL OR TaxExemptReason IS NOT NULL OR RevenueCoaId IS NOT NULL){only} "
            'ORDER BY i.InvoiceNumber, i.ID, p.Position, p.ID', [invoice_id] if invoice_id is not None else []).fetchall()
        if not rows:
            return
        income = {row[0] for row in self._income_coas(conn.cursor())}
        documents = {}
        for inv, number, head_rate, rate, category, bp, reason, coa, mixed in rows:
            documents.setdefault((inv, number, head_rate, mixed), []).append((rate, category, bp, reason, coa))
        for (inv, number, head_rate, mixed), positions in documents.items():
            problems = {}                 # (Art, Satz) → (steuerfrei?, Platzhalter des Satzes): je Art und Satz ein Befund, in der Reihenfolge ihres Auftretens
            shots, plain = {}, set()      # Satz → die Momentaufnahmen seiner Positionen (Art, Grund, Erlöskonto); die Sätze der Positionen ohne eine
            for rate, category, bp, reason, coa in positions:
                position_pct = pct_of(rate)
                exempt = category == 'E'
                has_reason = isinstance(reason, str) and bool(reason.strip())
                if bp is None:
                    if position_pct is not None:
                        plain.add(position_pct)
                    if reason is not None or coa is not None:     # die halbe Momentaufnahme: Angaben aus den Stammdaten ohne den Satz dazu – ein Befund je Beleg
                        problems.setdefault(('half', Decimal(0)), (False, {}))
                    continue                                       # die Art „steuerfrei“ allein (§ 19) ist keiner
                whole = valid_rate_bp(bp)
                pct = bp_pct(bp) if whole else (Decimal(0) if position_pct is None else position_pct)
                if not whole or position_pct is None or bp != position_pct * 100:
                    problems.setdefault(('rate', pct), (exempt, {'position': 'ohne lesbaren Satz' if position_pct is None
                                                               else group_label(position_pct, False)}))
                if exempt:
                    if whole and bp != 0:
                        problems.setdefault(('exempt_rate', pct), (False, {}))
                    if not has_reason:
                        problems.setdefault(('exempt_reason', pct), (True, {}))
                if coa is not None and coa not in income:
                    problems.setdefault(('revenue', pct), (exempt, {}))
                shots.setdefault(pct, set()).add((exempt, reason, coa))
            for pct, found in shots.items():
                if len(found) > 1 or pct in plain:
                    problems.setdefault(('differs', pct), (any(shot[0] for shot in found), {}))
            if not mixed and len(shots) == 1:      # ein Beleg mit einem Satz: Der Kopf trägt genau diesen Satz (der Platzhalter −1 und jeder andere heißt: nein)
                (pct,) = shots
                try:
                    head = rate_pct(head_rate)
                except (ArithmeticError, ValueError):
                    head = None
                if head != pct:
                    shown_head = 'ohne lesbaren Satz' if head is None else 'ohne Steuerausweis' if head < 0 else group_label(head, False)
                    problems.setdefault(('head', pct), (any(shot[0] for shot in shots[pct]), {'head': shown_head}))
            for (kind, pct), (exempt, names) in problems.items():
                add('tax_entry_mismatch', inv, number, rate_sentence(TAX_ENTRY_FINDINGS[kind], pct, exempt, **names))
