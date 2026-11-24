# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Summen- und Saldenliste (SuSa): je Konto Anfangssaldo, Soll und Haben (Monat und aufgelaufen) und Endsaldo – ohne Datenbank, ohne Uhr.

Die Liste liest dieselben Zeilen wie der Export des Buchungsstapels für das Steuerbüro (``Database.get_susa``: ``accounts.LEDGER_ROWS_WHERE``,
dieselben weggelassenen Eingangs-Sätze gepaarter Überträge) und ordnet sie mit derselben Lesart ein (``matching.ledger_form``,
``matching.coa_in_soll``, ``datev_keys.booking_kind``) – es gibt hier keine dritte Lesart von Form, Soll und Haben. Dieses Modul hält die
Regeln, die der Export nicht kennt: den Bruttobetrag in Netto und Steuerkonto zerlegen (``entry_postings``), die Konten in Klassen ordnen
und vortragen (``GROUPS``, ``is_carried``), den Anfangssaldo bilden und alles zum Bericht summieren (``build_susa``).

Gerechnet wird in ganzen Minor Units (``int``, ``money``); nichts wird nachgerechnet, was gespeichert ist – auch nicht der Steuerbetrag.
Jede Buchung schreibt gleich viel ins Soll wie ins Haben, was ihr fehlt, steht auf „Nicht kontiert“ (``NOT_POSTED``): So ist in jeder Spalte
Summe Soll gleich Summe Haben, und die Summe der Anfangssalden – die Erfolgskonten tragen nicht vor, ihre Summe steht als „Ergebnis der
Vorjahre (rechnerisch)“ (``PRIOR_RESULT``) beim Eigenkapital – und der Endsalden ist null. Was sich nicht buchen lässt (ein Spiegel
unbekannter Lesart, ein nicht lesbarer Betrag, ein Datum, das kein Kalendertag ist, eine Zeile ohne Buchungsart, ein Satz mit Betrag 0 und
einem Steuerbetrag), steht nur in den Hinweisen (``SusaNotice``) – nichts fällt still weg. Zwei Hinweise nennen Sätze, die gebucht sind, aber den
Abgleich mit den Bankbewegungen verschieben (``CHILD_OTHER_PERIOD``, ``CHILD_OTHER_ACCOUNT``).

Die Regeln hier nicht still ändern: Sie sind in ``docs/regeln/auswertungen.md`` und ``DB_MODEL.md`` beschrieben und durch ``tests/test_susa.py`` festgehalten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import NamedTuple

from money import format_de
from . import datev_keys
from .bwa import MONTH_NAMES, bwa_periods, format_amount
from .matching import (ENTRY_FORMS, FORM_LIQUID_FIRST, FORM_MIRROR_LEGACY, FORM_PURPOSE_FIRST, FORM_TRANSFER, MIRROR_FORMS, coa_in_soll,
                       ledger_form)
from .reporting import NEUTRAL_ACCOUNT_NUMBERS

# ── Schlüssel und Gruppen ─────────────────────────────────────────────────────

#: Die virtuellen Konten: Sie haben keine Nummer und stehen am Ende ihrer Gruppe
NOT_POSTED = 'not_posted'            # die Gegenseite von Bankbewegungen ohne Buchungssatz und von Sätzen ohne Konto
PRIOR_RESULT = 'prior_result'        # die Summe der Erfolgskonten der Vorjahre (sie tragen nicht vor)
TAX_UNASSIGNED = 'tax_unassigned'    # Steuer zu einem Satz, für den es kein Steuerkonto gibt

#: Die Wartekonten der offenen Posten: Sie liegen in 4000–4999, halten aber den Bruttobetrag einer Rechnung bis zur Zahlung über den Jahreswechsel
OPEN_ITEM_NUMBERS = tuple(number for number in NEUTRAL_ACCOUNT_NUMBERS if 4000 <= number <= 4999)


@dataclass(frozen=True)
class SusaGroup:
    """Eine Kontenklasse der Liste: Bereich der Nummern (``None`` = kein Bereich: eigene Nummern oder virtuelles Konto) und ob ihre Konten
    ihren Saldo ins nächste Jahr vortragen (Bestandskonten) oder bei null beginnen (Erfolgskonten)."""
    key: str
    label: str
    low: int | None
    high: int | None
    carried: bool


#: Die Gruppen in der Reihenfolge der Liste – EINE Stelle für Klasse und Vortrag eines Kontos
GROUPS = (
    SusaGroup('fixed_assets', 'Anlagevermögen', 0, 999, True),
    SusaGroup('current_assets', 'Umlaufvermögen und Finanzkonten', 1000, 1999, True),
    SusaGroup('equity', 'Eigenkapital und Privat', 2000, 2999, True),
    SusaGroup('liabilities', 'Rückstellungen, Verbindlichkeiten und Steuern', 3000, 3999, True),
    SusaGroup('revenue', 'Erlöse und Erträge', 4000, 4999, False),
    SusaGroup('open_items', 'Offene Posten (Wartekonten)', None, None, True),
    SusaGroup('expenses', 'Aufwand und übrige Erfolgskonten', 5000, 7999, False),
    SusaGroup('other', 'Vortrags- und statistische Konten', 8000, 9999, True),
    SusaGroup('personal', 'Personenkonten', 10000, None, True),
    SusaGroup('not_posted', 'Nicht kontiert', None, None, True),
)
GROUP_BY_KEY = {group.key: group for group in GROUPS}
_RANGED_GROUPS = tuple(group for group in GROUPS if group.low is not None)
TOTAL_GROUP = SusaGroup('total', 'Gesamtsumme', None, None, True)


def group_key(key) -> str:
    """Die Gruppe eines Kontos: der Schlüssel eines virtuellen Kontos oder die Kontonummer (``int``). Eine Nummer außerhalb aller Bereiche
    (negativ) und alles, was keine ist, gehört zu „Nicht kontiert“."""
    if key == PRIOR_RESULT:
        return 'equity'
    if key == TAX_UNASSIGNED:
        return 'liabilities'
    if isinstance(key, bool) or not isinstance(key, int) or key < 0:
        return 'not_posted'
    if key in OPEN_ITEM_NUMBERS:
        return 'open_items'
    for group in _RANGED_GROUPS:
        if group.low <= key and (group.high is None or key <= group.high):
            return group.key
    return 'not_posted'


def is_carried(key) -> bool:
    """Trägt das Konto seinen Saldo ins nächste Jahr vor? Bestandskonten ja, Erfolgskonten (4000–7999 ohne die Wartekonten) nein."""
    return GROUP_BY_KEY[group_key(key)].carried


# ── Steuer ────────────────────────────────────────────────────────────────────

TAX_VAT, TAX_INPUT = 'vat', 'input'           # Umsatzsteuer, Vorsteuer

#: Das Steuerkonto je Art und Steuersatz (Prozent, ``datev_keys.percent``): 16 % und 5 % liegen auf den Konten von 19 % und 7 % – wie die
#: Vorsteuer in der EÜR; eigene Konten dafür gibt es nicht. Ein anderer Satz hat hier kein Konto: Er steht auf dem Konto, das ein Eintrag der
#: Steuersätze der Stammdaten nennt (``tax_account(…, extra)``), sonst auf ``TAX_UNASSIGNED``.
TAX_ACCOUNTS = {
    TAX_INPUT: {19.0: 1406, 16.0: 1406, 7.0: 1401, 5.0: 1401},
    TAX_VAT: {19.0: 3806, 16.0: 3806, 7.0: 3801, 5.0: 3801},
}

#: Die Namen der Steuerkonten für einen Bestand, dessen Kontenrahmen sie nicht führt: ein Bestand von vor 2.6.1 (der Standard-Kontenrahmen führt
#: 3801 erst seitdem; ein älterer Bestand bekommt es über „fehlende Standardkonten“) oder ein gelöschtes Konto. Wo der Kontenrahmen das Konto führt,
#: gilt sein Name.
TAX_ACCOUNT_NAMES = {1401: 'Abziehbare Vorsteuer 7 %', 1406: 'Abziehbare Vorsteuer 19 %',
                     3801: 'Umsatzsteuer 7 %', 3806: 'Umsatzsteuer 19 %'}
NOT_IN_CHART_SUFFIX = ' (nicht im Kontenrahmen)'

VIRTUAL_NAMES = {PRIOR_RESULT: 'Ergebnis der Vorjahre (rechnerisch)', TAX_UNASSIGNED: 'Steuer ohne Steuerkonto',
                 NOT_POSTED: 'Nicht kontiert (Gegenseite fehlt)'}


def tax_account(tax_type: str, rate, extra=None):
    """Das Steuerkonto zu Art (``TAX_VAT``/``TAX_INPUT``) und Steuersatz der Buchung (0.19 und 19 sind beide 19 %) – ``TAX_UNASSIGNED``,
    wenn der Satz keines hat (anderer Satz, kein Satz, kein lesbarer Satz).

    Zuerst gilt die feste Tabelle (``TAX_ACCOUNTS``: 19, 16, 7 und 5 %), dann ``extra`` – die Steuerkonten der Stammdaten, ``{Art: {Prozentzahl: Kontonummer}}``
    (``Database.get_susa`` liest sie aus den Steuersätzen); für einen Satz der Tabelle ändert ``extra`` also nie etwas."""
    pct = datev_keys.percent(rate)
    found = TAX_ACCOUNTS[tax_type].get(pct)
    if found is None and extra:
        found = extra.get(tax_type, {}).get(pct)
    return TAX_UNASSIGNED if found is None else found


def tax_side(form, kind, coa_number, counter_number, waiting_numbers):
    """Auf welcher Seite eines Satzes steht der Betrag, aus dem die Steuer herausgerechnet wird?

    Returns ``(Seite, Steuerart, still)``: die Seite ``'coa'`` (Feld „Konto“) oder ``'counter'`` (Gegenkonto) oder ``None`` (keine Zerlegung),
    die Steuerart ``'vat'`` (Umsatzsteuer) oder ``'input'`` (Vorsteuer), ``still`` ist wahr bei der Forderung einer Rechnung (und der Gegenbuchung
    ihrer Stornorechnung): Sie wird nie zerlegt – das Wartekonto hält den Bruttobetrag bis zur Zahlung – und braucht keinen Hinweis.

    Nach der Art der Buchung (``datev_keys.booking_kind``, dieselbe Art, nach der der Export den BU-Schlüssel ableitet): Eine Zahlung zerlegt
    am Zweckkonto (``purpose_first``: das Konto, ``liquid_first``: das Gegenkonto), eine Umbuchung an der Erlös- (Umsatzsteuer) bzw. Aufwands- oder
    Anlagenseite (Vorsteuer), zuerst am Gegenkonto, sonst am Konto; die Rechnungskette „Wartekonto an Erlöskonto“ an der Erlösseite, die kein
    Wartekonto ist. Ohne Art mit Schlüssel (``KIND_NONE``: Spiegel, Debitor als Zweckkonto, Umbuchung ohne Erlös-, Aufwands- oder Anlagekonto)
    und ohne Konto auf der Seite gibt es keine Zerlegung."""
    sides = {'coa': coa_number, 'counter': counter_number}
    if kind in (datev_keys.KIND_VAT, datev_keys.KIND_INPUT):
        tax_type = TAX_VAT if kind == datev_keys.KIND_VAT else TAX_INPUT
        if form in (FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST):
            side = 'coa' if form == FORM_PURPOSE_FIRST else 'counter'
            return (side, tax_type, False) if sides[side] is not None else (None, None, False)
        if form == FORM_TRANSFER:
            fits = datev_keys.is_revenue_number if kind == datev_keys.KIND_VAT else datev_keys.is_expense_or_asset_number
            for side in ('counter', 'coa'):
                if fits(sides[side]):
                    return side, tax_type, False
        return None, None, False
    if kind == datev_keys.KIND_INVOICE_CHAIN:
        if datev_keys.is_debtor_number(coa_number) or datev_keys.is_debtor_number(counter_number):
            return None, None, True
        waiting = {datev_keys.account_number(number) for number in waiting_numbers}
        for side in ('counter', 'coa'):
            if datev_keys.is_revenue_number(sides[side]) and sides[side] not in waiting:
                return side, TAX_VAT, False
    return None, None, False


# ── Buchungsregel je Satz ─────────────────────────────────────────────────────

#: Die Hinweise, die ein Satz auslösen kann (``entry_postings`` und ``Database.get_susa``)
MISSING_SIDE, LEGACY_MIRROR, MONEY_FROM_PARENT, NO_FORM = 'missing_side', 'legacy_mirror', 'money_from_parent', 'no_form'
NOT_READABLE, BAD_DATE, UNBOOKED, UNBOOKED_NO_NUMBER = 'not_readable', 'bad_date', 'unbooked', 'unbooked_no_number'
TAX_UNASSIGNED_NOTICE, TAX_NOT_SPLIT, UNBALANCED, DOUBTFUL = 'tax_unassigned', 'tax_not_split', 'unbalanced', 'doubtful'
UNKNOWN_TYPE, ZERO_AMOUNT_TAX = 'unknown_type', 'zero_amount_tax'
CHILD_OTHER_PERIOD, CHILD_OTHER_ACCOUNT = 'child_other_period', 'child_other_account'
#: Bei diesen Hinweisen zählt der Steuerbetrag, bei allen anderen der Bruttobetrag
TAX_NOTICES = frozenset({TAX_UNASSIGNED_NOTICE, TAX_NOT_SPLIT})
#: Die Hinweise, deren Satz einen Betrag nennt (``{s}``); ``unbooked`` nennt Eingänge und Ausgänge, ``not_readable`` und ``doubtful`` keinen Betrag
AMOUNT_NOTICES = frozenset({UNBOOKED_NO_NUMBER, MISSING_SIDE, MONEY_FROM_PARENT, LEGACY_MIRROR, NO_FORM, BAD_DATE, UNKNOWN_TYPE, ZERO_AMOUNT_TAX,
                            TAX_UNASSIGNED_NOTICE, TAX_NOT_SPLIT, UNBALANCED, CHILD_OTHER_PERIOD, CHILD_OTHER_ACCOUNT})


@dataclass(frozen=True)
class Posting:
    """Eine Buchung auf einem Konto: ``key`` ist die Kontonummer oder ein virtueller Schlüssel (``NOT_POSTED`` …), ``soll`` und ``haben`` sind
    Minor Units – die eine Seite trägt den Betrag, die andere ist 0."""
    key: int | str
    soll: int
    haben: int


def entry_postings(form, side, kind, coa_number, counter_number, amount, tax_amount, tax_rate,
                   waiting_numbers, *, extra_tax_accounts=None) -> tuple[list[Posting], set[str]]:
    """Die Buchungen eines Satzes (oder einer Gruppe gleich gelesener Sätze: die Regel ist linear, also gilt sie für die Summen) – und die
    Hinweise, die er auslöst.

    ``form`` und ``side`` sind Form und Soll-Seite (``matching.ledger_form``), ``kind`` die Art (``datev_keys.booking_kind``),
    ``coa_number``/``counter_number`` die Kontonummern der beiden Seiten (``int`` oder ``None``), ``amount`` der Bruttobetrag mit Vorzeichen,
    ``tax_amount`` der gespeicherte Steuerbetrag (wie der Betrag mit Vorzeichen; ``None`` und 0: keiner), ``tax_rate`` der Steuersatz.

    **Soll und Haben** wie Spalte 1 des Exports (``matching.coa_in_soll``): Bei ``purpose_first`` steht ein positiver Betrag (Einnahme) im Soll auf
    dem Gegenkonto und im Haben auf dem Konto, ein negativer umgekehrt; bei ``liquid_first``, ``transfer`` und ``mirror_coa`` im Soll auf dem Konto,
    bei ``mirror_counter`` auf dem Gegenkonto. Gebucht wird der Betrag ohne Vorzeichen. Ein Satz mit Betrag 0 und ein ``mirror_legacy`` (die Lesart
    seines Betrags ist unbekannt, sie wird nie gedeutet) buchen nichts; Letzterer meldet ``legacy_mirror``. Eine Seite ohne Nummer steht auf
    ``NOT_POSTED`` (Hinweis ``missing_side``).

    **Steuer:** Der gespeicherte Steuerbetrag ``t`` (negativ bei einem negativen Betrag, mit dem Vorzeichen des Soll-Haben-Satzes) wird aus der Seite
    herausgerechnet (``tax_side``): Sie bekommt ``Betrag − t``, das Steuerkonto ``t`` auf derselben Seite (``tax_account``; ohne Konto zum Satz:
    ``TAX_UNASSIGNED``, Hinweis ``tax_unassigned``). ``extra_tax_accounts`` sind die Steuerkonten der Stammdaten für Sätze außerhalb der festen
    Tabelle (``tax_account``): Hat der Satz dort ein Konto, steht die Steuer darauf und der Hinweis entfällt. Gibt es keine Seite (und ist es nicht die
    Forderung einer Rechnung), steht der Satz brutto, Hinweis ``tax_not_split``."""
    if form == FORM_MIRROR_LEGACY:
        return [], {LEGACY_MIRROR}
    if not amount:
        return [], set()
    notices: set[str] = set()
    inflow = amount > 0
    value = abs(amount)
    keys = {}
    for name, number in (('coa', coa_number), ('counter', counter_number)):
        if number is None:
            notices.add(MISSING_SIDE)
        keys[name] = NOT_POSTED if number is None else number
    debit, credit = ('coa', 'counter') if coa_in_soll(inflow, side) else ('counter', 'coa')
    postings = [Posting(keys[debit], value, 0), Posting(keys[credit], 0, value)]

    tax = tax_amount or 0
    if tax:
        tax = tax if inflow else -tax
        split, tax_type, quiet = tax_side(form, kind, coa_number, counter_number, waiting_numbers)
        if split is not None:
            account = tax_account(tax_type, tax_rate, extra_tax_accounts)
            if account == TAX_UNASSIGNED:
                notices.add(TAX_UNASSIGNED_NOTICE)
            if split == debit:
                postings = [Posting(keys[debit], value - tax, 0), Posting(account, tax, 0), Posting(keys[credit], 0, value)]
            else:
                postings = [Posting(keys[debit], value, 0), Posting(keys[credit], 0, value - tax), Posting(account, 0, tax)]
        elif not quiet:
            notices.add(TAX_NOT_SPLIT)
    return postings, notices


# ── Eine Gruppe gleich gelesener Sätze buchen ─────────────────────────────────

@dataclass(frozen=True)
class SusaContext:
    """Was die Buchungsregel nachschlägt: die Zahlungskonten (``Database.get_payment_coa_ids``), die Nummern der Konten des Kontenrahmens
    (``{COA-ID: Nummer oder None}``) und der eigenen Bankkonten (``{Accounts.ID: SKR-Nummer oder None}``), die Wartekonten und die Steuerkonten der
    Stammdaten für Sätze außerhalb der festen Tabelle (``tax_accounts``: ``{Art: {Prozentzahl: Kontonummer}}``, ``None`` = keine)."""
    liquid_coa_ids: frozenset
    coa_numbers: dict
    account_numbers: dict
    waiting_numbers: tuple
    tax_accounts: dict | None = None

    @cached_property
    def own_numbers(self) -> frozenset:
        """Die SKR-Nummern der eigenen Bank- und Kassenkonten."""
        return frozenset(number for number in self.account_numbers.values() if number is not None)


class GroupRow(NamedTuple):
    """Eine Gruppe von Sätzen, die gleich gelesen werden: gleiche Form, gleiche Konten, gleiche Richtung des Betrags, gleicher Steuersatz,
    gleiche Richtung des Steuerbetrags, gleicher Zeitraum und gleiches Konto der Bankbewegung – ``count`` Sätze mit den Summen ``amount`` und
    ``tax`` (Minor Units, mit Vorzeichen). Weil die Buchungsregel linear ist, ergibt die Gruppe dieselben Summen wie ihre Sätze einzeln."""
    stored_form: str | None
    coa_id: int | None
    counter_id: int | None
    inflow: bool
    tax_rate: float | None
    bucket: str
    parent_account: int | None
    count: int
    amount: int
    tax: int


class Tally:
    """Sammelt die Buchungen und Hinweise, die ``Database.get_susa`` liest: je Zeitraum und Konto Soll und Haben, je Hinweis Zahl und Betrag.
    ``sign`` −1 zieht ab (die weggelassenen Eingangs-Sätze gepaarter Überträge: sie wurden mitgelesen und gehen wieder heraus)."""

    def __init__(self):
        self._sums: dict[tuple[str, int | str], list[int]] = {}
        self._notices: dict[str, list[int]] = {}

    def post(self, bucket: str, posting: Posting, sign: int = 1) -> None:
        found = self._sums.setdefault((bucket, posting.key), [0, 0])
        found[0] += sign * posting.soll
        found[1] += sign * posting.haben

    def notice(self, key: str, count: int, amount: int = 0, incoming: int = 0, outgoing: int = 0, sign: int = 1) -> None:
        found = self._notices.setdefault(key, [0, 0, 0, 0])
        for index, value in enumerate((count, amount, incoming, outgoing)):
            found[index] += sign * value

    def postings(self) -> list[tuple[str, Posting]]:
        return [(bucket, Posting(key, soll, haben)) for (bucket, key), (soll, haben) in self._sums.items() if soll or haben]

    def notices(self) -> list[SusaNotice]:
        """Die Hinweise mit mindestens einem Fall. Nur ``unbooked`` nennt Eingänge und Ausgänge, nur die Hinweise, deren Satz einen Betrag
        nennt, tragen ihn."""
        result = []
        for key, (count, amount, incoming, outgoing) in self._notices.items():
            if count > 0:
                result.append(SusaNotice(key, count, amount if key in AMOUNT_NOTICES else None, incoming if key == UNBOOKED else None,
                                         outgoing if key == UNBOOKED else None))
        return result


def book_group(tally: Tally, context: SusaContext, row: GroupRow, sign: int = 1) -> None:
    """Die Buchungen und Hinweise einer Gruppe gleich gelesener Sätze ins ``tally`` buchen (``sign`` −1: abziehen).

    Form und Soll-Seite liest ``matching.ledger_form``, die Art ``datev_keys.booking_kind`` – wie der Export –, Soll und Haben sowie die Steuer
    ``entry_postings``. Dazu drei Fälle, die nur diese Liste kennt: Hat ein Satz unter einer Bankbewegung keine Geldseite (``purpose_first``
    ohne Gegenkonto, ``liquid_first`` ohne Konto), ist es das Konto der Bankbewegung – EÜR und Split-Summe zählen ihn genauso (Hinweis
    ``money_from_parent``; hat die Bewegung keine Nummer, bleibt die Seite leer und steht auf „Nicht kontiert“). Eine Zeile ohne
    gespeicherte Form wird mit den Zahlungskonten von heute gelesen, wie im Export (Hinweis ``no_form``). Und ein gebuchter Satz unter einer
    Bankbewegung, dessen eine Seite die Nummer eines anderen eigenen Kontos trägt (Bank oder Kasse; auch wenn das Konto der Bewegung keine
    Nummer hat), bucht dorthin – der Abgleich mit den Bankbewegungen kann um ihn abweichen (Hinweis ``child_other_account``); ein Spiegel
    zwischen eigenen Konten berührt das andere Konto absichtlich und meldet nichts."""
    form, side = ledger_form(row.stored_form, row.coa_id, row.counter_id, context.liquid_coa_ids)
    coa_number = context.coa_numbers.get(row.coa_id)
    counter_number = context.coa_numbers.get(row.counter_id)
    notices: set[str] = set()
    parent_number = context.account_numbers.get(row.parent_account) if row.parent_account is not None else None
    if parent_number is not None:
        if form == FORM_PURPOSE_FIRST and row.counter_id is None:
            counter_number = parent_number
            notices.add(MONEY_FROM_PARENT)
        elif form == FORM_LIQUID_FIRST and row.coa_id is None:
            coa_number = parent_number
            notices.add(MONEY_FROM_PARENT)
    kind = datev_keys.booking_kind(form, coa_number, counter_number, row.inflow, context.waiting_numbers)
    postings, found = entry_postings(form, side, kind, coa_number, counter_number, row.amount, row.tax, row.tax_rate, context.waiting_numbers,
                                     extra_tax_accounts=context.tax_accounts)
    notices |= found
    if row.stored_form not in ENTRY_FORMS and form != FORM_MIRROR_LEGACY:
        notices.add(NO_FORM)
    if (postings and row.parent_account is not None and form not in MIRROR_FORMS
            and any(number in context.own_numbers and number != parent_number for number in (coa_number, counter_number))):
        notices.add(CHILD_OTHER_ACCOUNT)
    for posting in postings:
        tally.post(row.bucket, posting, sign)
    for key in notices:
        tally.notice(key, row.count, abs(row.tax) if key in TAX_NOTICES else abs(row.amount), sign=sign)


# ── Der Bericht ───────────────────────────────────────────────────────────────

@dataclass
class SusaAccount:
    """Ein Konto der Liste. ``key`` ist die Nummer oder der Schlüssel eines virtuellen Kontos (``number`` ist dann ``None``), ``outside_chart``:
    Der Kontenrahmen führt es nicht (der Name trägt den Zusatz). Intern steht Soll positiv: ``opening`` und ``closing`` sind Soll minus Haben."""
    key: int | str
    number: int | None
    name: str
    group: str
    virtual: bool
    outside_chart: bool
    opening: int = 0
    month_soll: int = 0
    month_haben: int = 0
    ytd_soll: int = 0
    ytd_haben: int = 0

    @property
    def closing(self) -> int:
        return self.opening + self.ytd_soll - self.ytd_haben


@dataclass
class SusaGroupTotal:
    """Eine Gruppe mit ihren Konten (nach Nummer, die virtuellen am Ende) und deren Summen; ``accounts`` hält auch die Konten, die nicht
    sichtbar sind (``visible``) – sie tragen nur Nullen bei."""
    group: SusaGroup
    accounts: list[SusaAccount] = field(default_factory=list)
    opening: int = 0
    month_soll: int = 0
    month_haben: int = 0
    ytd_soll: int = 0
    ytd_haben: int = 0

    @property
    def closing(self) -> int:
        return self.opening + self.ytd_soll - self.ytd_haben


@dataclass(frozen=True)
class SusaNotice:
    """Ein Hinweis zu den Buchungen: ``key`` (``NOTICE_TEXTS``), die Zahl, der Betrag in Minor Units (wo der Text einen nennt) und bei
    ``unbooked`` die Summe der Eingänge und der Ausgänge."""
    key: str
    count: int
    amount: int | None = None
    incoming: int | None = None
    outgoing: int | None = None


@dataclass(frozen=True)
class BankCheck:
    """Der Abgleich eines Bankkontos: der Endsaldo der Liste gegen die Summe aller Bankbewegungen bis zum Monatsende."""
    name: str
    number: int
    closing: int
    movements: int

    @property
    def difference(self) -> int:
        return self.closing - self.movements


@dataclass
class SusaReport:
    year: int
    month: int
    periods: dict[str, tuple[str, str]]
    groups: list[SusaGroupTotal]
    total: SusaGroupTotal
    notices: list[SusaNotice]
    bank_checks: list[BankCheck]
    bank_skipped: list[str]


#: Die Hinweise in der Reihenfolge, in der die Seite sie nennt
NOTICE_ORDER = (UNBOOKED, UNBOOKED_NO_NUMBER, MISSING_SIDE, MONEY_FROM_PARENT, LEGACY_MIRROR, NO_FORM, NOT_READABLE, BAD_DATE, UNKNOWN_TYPE,
                ZERO_AMOUNT_TAX, TAX_UNASSIGNED_NOTICE, TAX_NOT_SPLIT, UNBALANCED, CHILD_OTHER_PERIOD, CHILD_OTHER_ACCOUNT, DOUBTFUL)


def _new_account(key, names) -> SusaAccount:
    """Ein leeres Konto zu einem Schlüssel: der Name aus ``names`` (``{Nummer: (Name, außerhalb des Kontenrahmens)}``); ohne Eintrag der
    eines Steuerkontos oder keiner (dann steht nur die Nummer da). Ein Konto außerhalb des Kontenrahmens trägt den Zusatz."""
    if key in VIRTUAL_NAMES:
        return SusaAccount(key, None, VIRTUAL_NAMES[key], group_key(key), True, False)
    name, outside = names.get(key) or (TAX_ACCOUNT_NAMES.get(key, ''), True)
    return SusaAccount(key, key, (name + NOT_IN_CHART_SUFFIX) if outside else name, group_key(key), False, outside)


def build_susa(year, month, postings, names, notices, bank_checks, bank_skipped) -> SusaReport:
    """Den Bericht aus den Buchungen zusammenstellen.

    ``postings``: ``(Zeitraum, Posting)`` – ``'prior'`` (vor dem 1. Januar), ``'year'`` (vom 1. Januar bis vor den Monat) oder ``'month'``.
    ``names``: ``{Kontonummer: (Name, außerhalb des Kontenrahmens)}``. Ein Bestandskonto trägt seine Summe vor dem 1. Januar als Anfangssaldo
    vor, ein Erfolgskonto (``is_carried``) nicht: Seine Summe geht in ``PRIOR_RESULT``, so ist die Summe aller Anfangssalden null. Die Buchungen
    des Jahres vor dem Monat zählen nur aufgelaufen, die des Monats in beiden Blöcken."""
    accounts: dict = {}

    def account(key):
        found = accounts.get(key)
        if found is None:
            found = accounts[key] = _new_account(key, names)
        return found

    for period, posting in postings:
        if period == 'prior':
            net = posting.soll - posting.haben
            account(posting.key if is_carried(posting.key) else PRIOR_RESULT).opening += net
            continue
        target = account(posting.key)
        target.ytd_soll += posting.soll
        target.ytd_haben += posting.haben
        if period == 'month':
            target.month_soll += posting.soll
            target.month_haben += posting.haben
    for key in VIRTUAL_NAMES:
        account(key)

    groups = {group.key: SusaGroupTotal(group) for group in GROUPS}
    total = SusaGroupTotal(TOTAL_GROUP)
    for found in sorted(accounts.values(), key=lambda item: (item.virtual, item.number if item.number is not None else -1)):
        group = groups[found.group]
        group.accounts.append(found)
        for target in (group, total):
            target.opening += found.opening
            target.month_soll += found.month_soll
            target.month_haben += found.month_haben
            target.ytd_soll += found.ytd_soll
            target.ytd_haben += found.ytd_haben
    ordered = sorted(notices, key=lambda notice: NOTICE_ORDER.index(notice.key))
    spans = bwa_periods(year, month)
    return SusaReport(year, month, {key: spans[key] for key in ('month', 'ytd')}, [groups[group.key] for group in GROUPS], total, ordered,
                      list(bank_checks), list(bank_skipped))


# ── Sichtbarkeit und Anzeige ──────────────────────────────────────────────────

def visible(account) -> bool:
    """Ein Konto steht in der Liste, wenn einer seiner sechs Werte nicht null ist – auch bei aufgelaufen Soll gleich Haben, aber nicht null."""
    return any((account.opening, account.month_soll, account.month_haben, account.ytd_soll, account.ytd_haben, account.closing))


def visible_accounts(group: SusaGroupTotal) -> list[SusaAccount]:
    return [account for account in group.accounts if visible(account)]


def visible_groups(report: SusaReport) -> list[SusaGroupTotal]:
    """Die Gruppen mit mindestens einem sichtbaren Konto, in der Reihenfolge der Liste."""
    return [group for group in report.groups if visible_accounts(group)]


def format_balance(minor: int) -> str:
    """Ein Saldo mit Seite: ``1.234,56 S`` (Soll) oder ``1.234,56 H`` (Haben) – ein Kennzeichen statt eines Vorzeichens, weil Aktiv- und
    Passivkonten nebeneinander stehen. Null (auch ein Betrag, der auf 0,00 rundet) steht als ``0,00``."""
    text = format_de(abs(minor), 2)
    if text == '0,00':
        return text
    return f'{text} {"S" if minor > 0 else "H"}'


COLUMN_LABELS = ('Anfangssaldo', 'Soll', 'Haben', 'Soll', 'Haben', 'Endsaldo')


def cells(row, is_sum: bool) -> list[str]:
    """Die sechs Zellen einer Zeile (Konto oder Gruppe): Anfangssaldo, Soll und Haben im Monat, Soll und Haben aufgelaufen, Endsaldo. In einer
    Kontozeile bleiben Nullbeträge leer (nur der Endsaldo zeigt ``0,00``), eine Summenzeile zeigt alles."""
    def amount(minor):
        return format_amount(minor) if (is_sum or minor) else ''
    opening = format_balance(row.opening) if (is_sum or row.opening) else ''
    return [opening, amount(row.month_soll), amount(row.month_haben), amount(row.ytd_soll), amount(row.ytd_haben), format_balance(row.closing)]


def block_headers(year: int, month: int) -> tuple[str, str]:
    """Die Überschriften der beiden Blöcke: der Monat und aufgelaufen (im Januar ist beides derselbe Zeitraum)."""
    name = MONTH_NAMES[month - 1]
    if month == 1:
        return f'{name} {year}', f'{name} {year} (aufgelaufen)'
    return f'{name} {year}', f'Januar bis {name} {year}'


def susa_filename(year: int, month: int, extension: str = 'pdf') -> str:
    """``Summen_und_Salden_2026-10.pdf``; ``extension`` ersetzt das ``pdf`` (``Summen_und_Salden_2026-10.csv``)."""
    return f'Summen_und_Salden_{year:04d}-{month:02d}.{extension}'


# ── Texte (Seite und PDF) ─────────────────────────────────────────────────────

TITLE = 'Summen- und Saldenliste'
SUSA_HINT_TEXT = ('Aus den Buchungssätzen dieser App gerechnet, je Konto im Soll und Haben – so, wie der Export für das Steuerbüro sie liest. '
                  'Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros. Ohne Zusicherung der Richtigkeit.')
SALDO_TEXT = 'S = Soll, H = Haben.'
OPENING_TEXT = ('Anfangssaldo: Bestandskonten tragen die Summe aller Buchungen vor dem 1. Januar vor; Erfolgskonten (4000–7999 ohne die Wartekonten) '
                'beginnen bei null, ihre Summe aus den Vorjahren steht als „Ergebnis der Vorjahre (rechnerisch)“ beim Eigenkapital. Privat- und '
                'Steuerkonten werden nicht abgeschlossen, sie tragen ihre Summe vor. Eine Eröffnungsbilanz gibt es nicht: Ein Konto ohne Buchungen '
                'vor dem ersten Kontoauszug beginnt bei null.')
NOT_POSTED_TEXT = ('„Nicht kontiert“ nimmt die Gegenseite von Bankbewegungen ohne Buchungssatz und von Sätzen ohne Konto auf; so geht die Liste auf. '
                   'Steht dort ein Betrag, fehlt in den Büchern eine Kontierung.')
TRANSFER_TEXT = ('Ein Übertrag zwischen eigenen Konten, der auf beiden Bankbewegungen kontiert ist, zählt einmal, mit dem Datum seiner '
                 'Ausgangsseite.')
TAX_TEXT = ('Steueranteile stehen auf 1401/1406 (Vorsteuer) und 3801/3806 (Umsatzsteuer), 16 % und 5 % bei 19 % und 7 %. Ein Steuerkonto, das der '
            'Kontenrahmen nicht führt, steht mit dem Zusatz „nicht im Kontenrahmen“. Umsatzsteuer und Vorsteuer werden nicht verrechnet, einen '
            'Jahresabschluss gibt es nicht.')
EUER_DIFF_TEXT = ('Die Erfolgskonten zählen jeden Buchungssatz mit seinem Datum. Die Einnahmenüberschussrechnung zählt nur Zahlungen, Kassen- und '
                  'Verrechnungssätze und die Abschreibungen – Sätze, die noch keiner Bankbewegung zugeordnet sind, und Umbuchungen ohne Zahlung '
                  'stehen nur hier. Die Umsatzsteuer steht hier nach Sätzen getrennt, in der Einnahmenüberschussrechnung gemeinsam. Bei den Konten '
                  '6884–6892 steht der Steueranteil hier bei der Umsatzsteuer, in der Einnahmenüberschussrechnung bei der Vorsteuer; ein Satz mit '
                  'Betrag 0 und Steuerbetrag fehlt hier (siehe Hinweise).')
BANK_CHECK_TEXT = ('Für jedes eigene Bankkonto mit eigener SKR-Nummer: Der Endsaldo der Liste soll die Summe aller Bankbewegungen bis zum '
                   'Monatsende sein – so lässt er sich mit dem Kontoauszug vergleichen.')
BANK_DIFF_TEXT = ('Eine Abweichung entsteht durch Bankbewegungen, deren Buchungssätze nicht ihren Betrag ergeben, durch Sätze auf dem Bankkonto '
                  'ohne Bankbewegung, durch Sätze mit einem anderen Datum oder einem anderen eigenen Konto als ihre Bankbewegung, durch '
                  'Überträge, deren beide Seiten verschiedene Daten tragen, oder durch nicht lesbare Werte (siehe Hinweise).')
BANK_SKIPPED_TEXT = 'Ohne Abgleich (keine oder eine doppelte SKR-Nummer): {names}.'
SUSA_FOOTER = 'Aus den Buchungssätzen dieser App gerechnet. Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.'

BANK_HEADING = 'Abgleich mit den Bankbewegungen'
NOTICES_HEADING = 'Hinweise zu den Buchungen bis zum {datum}'
BANK_COLUMNS = ('Konto', 'Bankkonto', 'Endsaldo der Liste', 'Summe der Bankbewegungen bis {datum}', 'Abweichung')
COLUMN_ACCOUNT, COLUMN_NAME = 'Konto', 'Bezeichnung'
SUM_LABEL = 'Summe {gruppe}'

#: Die Hinweise: (Einzahl, Mehrzahl). ``{n}`` die Zahl, ``{s}`` der Betrag, ``{e}``/``{a}`` Eingänge und Ausgänge (jeweils ohne „ €“).
NOTICE_TEXTS = {
    UNBOOKED: ('{n} Bankbewegung ohne Buchungssatz (Eingänge {e} €, Ausgänge {a} €) steht gegen „Nicht kontiert“.',
               '{n} Bankbewegungen ohne Buchungssatz (Eingänge {e} €, Ausgänge {a} €) stehen gegen „Nicht kontiert“.'),
    UNBOOKED_NO_NUMBER: ('{n} Bankbewegung ohne Buchungssatz auf einem Konto ohne SKR-Nummer ({s} €) fehlt in der Liste.',
                         '{n} Bankbewegungen ohne Buchungssatz auf einem Konto ohne SKR-Nummer ({s} €) fehlen in der Liste.'),
    MISSING_SIDE: ('{n} Buchungssatz ohne Konto, ohne Gegenkonto oder auf einem Konto ohne Nummer ({s} €): '
                   'Die fehlende Seite steht unter „Nicht kontiert“.',
                   '{n} Buchungssätze ohne Konto, ohne Gegenkonto oder auf einem Konto ohne Nummer ({s} €): '
                   'Die fehlende Seite steht unter „Nicht kontiert“.'),
    MONEY_FROM_PARENT: ('{n} Buchungssatz unter einer Bankbewegung ohne Geldkonto ({s} €) ist dem Konto der Bankbewegung zugerechnet.',
                        '{n} Buchungssätze unter einer Bankbewegung ohne Geldkonto ({s} €) sind dem Konto der Bankbewegung zugerechnet.'),
    LEGACY_MIRROR: ('{n} Umbuchung zwischen Geldkonten aus einem älteren Bestand, deren Richtung unbekannt ist ({s} €), fehlt in der Liste.',
                    '{n} Umbuchungen zwischen Geldkonten aus einem älteren Bestand, deren Richtung unbekannt ist ({s} €), fehlen in der Liste.'),
    NO_FORM: ('{n} Buchungssatz ohne gespeicherte Form ({s} €) ist mit den Zahlungskonten von heute gelesen.',
              '{n} Buchungssätze ohne gespeicherte Form ({s} €) sind mit den Zahlungskonten von heute gelesen.'),
    NOT_READABLE: ('{n} Buchungssatz oder Bankbewegung mit einem Betrag, der keine ganze Zahl in Cent-Bruchteilen der App ist oder außerhalb des '
                   'lesbaren Bereichs liegt, fehlt in der Liste.',
                   '{n} Buchungssätze oder Bankbewegungen mit einem Betrag, der keine ganze Zahl in Cent-Bruchteilen der App ist oder außerhalb '
                   'des lesbaren Bereichs liegt, fehlen in der Liste.'),
    BAD_DATE: ('{n} Zeile mit einem Datum, das kein Kalendertag ist ({s} €), lässt sich keinem Monat zuordnen.',
               '{n} Zeilen mit einem Datum, das kein Kalendertag ist ({s} €), lassen sich keinem Monat zuordnen.'),
    UNKNOWN_TYPE: ('{n} Zeile ohne Buchungsart ({s} €) ist nicht enthalten.',
                   '{n} Zeilen ohne Buchungsart ({s} €) sind nicht enthalten.'),
    ZERO_AMOUNT_TAX: ('{n} Buchungssatz mit Betrag 0 und einem Steuerbetrag ({s} €) ist nicht gebucht: Ohne Betrag gibt es keine Seite für die '
                      'Steuer.',
                      '{n} Buchungssätze mit Betrag 0 und einem Steuerbetrag ({s} €) sind nicht gebucht: Ohne Betrag gibt es keine Seite für '
                      'die Steuer.'),
    TAX_UNASSIGNED_NOTICE: ('{n} Steuerbetrag zu einem Steuersatz ohne Steuerkonto ({s} €) steht unter „Steuer ohne Steuerkonto“ – '
                            'ein Steuerkonto je Satz lässt sich in den Stammdaten unter ‚Steuersätze‘ hinterlegen.',
                            '{n} Steuerbeträge zu einem Steuersatz ohne Steuerkonto ({s} €) stehen unter „Steuer ohne Steuerkonto“ – '
                            'ein Steuerkonto je Satz lässt sich in den Stammdaten unter ‚Steuersätze‘ hinterlegen.'),
    TAX_NOT_SPLIT: ('{n} Steuerbetrag an Sätzen ohne Erlös-, Aufwands- oder Anlagekonto ({s} €) ist nicht abgespalten; der Satz steht brutto.',
                    '{n} Steuerbeträge an Sätzen ohne Erlös-, Aufwands- oder Anlagekonto ({s} €) sind nicht abgespalten; der Satz steht brutto.'),
    UNBALANCED: ('{n} Bankbewegung, deren Buchungssätze nicht ihren Betrag ergeben (Rest {s} €): '
                 'Das Bankkonto weicht um diesen Rest von den Bankbewegungen ab.',
                 '{n} Bankbewegungen, deren Buchungssätze nicht ihren Betrag ergeben (Rest {s} €): '
                 'Das Bankkonto weicht um diesen Rest von den Bankbewegungen ab.'),
    CHILD_OTHER_PERIOD: ('{n} Buchungssatz ({s} €) trägt ein anderes Datum als seine Bankbewegung, und das Monatsende liegt dazwischen – '
                         'um ihn kann der Abgleich abweichen.',
                         '{n} Buchungssätze ({s} €) tragen ein anderes Datum als ihre Bankbewegung, und das Monatsende liegt dazwischen – '
                         'um sie kann der Abgleich abweichen.'),
    CHILD_OTHER_ACCOUNT: ('{n} Buchungssatz ({s} €) unter einer Bankbewegung bucht auf ein anderes eigenes Konto als das der Bewegung – '
                          'um ihn kann der Abgleich dieser Konten abweichen.',
                          '{n} Buchungssätze ({s} €) unter einer Bankbewegung buchen auf ein anderes eigenes Konto als das der Bewegung – '
                          'um sie kann der Abgleich dieser Konten abweichen.'),
    DOUBTFUL: ('{n} Übertrag zwischen eigenen Konten steht im gewählten Jahr möglicherweise doppelt in der Liste – im Umfeld ist nicht jede '
               'Bankbewegung kontiert, oder es gibt zusätzlich einen eigenständigen Umbuchungssatz.',
               '{n} Überträge zwischen eigenen Konten stehen im gewählten Jahr möglicherweise doppelt in der Liste – im Umfeld ist nicht jede '
               'Bankbewegung kontiert, oder es gibt zusätzlich einen eigenständigen Umbuchungssatz.'),
}


def notice_text(notice: SusaNotice) -> str:
    """Der Satz zu einem Hinweis (Einzahl bei genau einem, sonst Mehrzahl); Beträge deutsch mit zwei Stellen, ohne das „ €“, das im Satz steht."""
    singular, plural = NOTICE_TEXTS[notice.key]

    def amount(minor):
        return format_de(minor or 0, 2)
    return (singular if notice.count == 1 else plural).format(n=notice.count, s=amount(notice.amount), e=amount(notice.incoming),
                                                              a=amount(notice.outgoing))
