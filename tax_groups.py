# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Steuer eines Belegs je Satzgruppe – EINE Stelle für Summen, E-Rechnung und Buchungen.

Positionen eines Belegs können verschiedene Steuersätze tragen. Nach EN 16931 (BR-CO-17) wird die Steuer je Satz aus der SUMME der
Nettobeträge der Gruppe gerechnet und kaufmännisch auf den Cent gerundet; die Steuer des Belegs ist die Summe der Gruppen – nicht die
Summe der je Position gerundeten Steuern. Dieses Modul ist genau diese Regel, rein: keine Datenbank, keine Uhr, nur ``money`` und die
Standardbibliothek, damit ``db/``, ``server/`` und ``export/`` sie gleichermaßen nutzen können.

Es rechnet für beliebig viele und beliebige Sätze (19 %, 7 %, 5,5 % …, auch 0 %): Kein Satz und keine Zahl von Sätzen ist fest
vorgegeben. Beträge sind Minor Units (``money``, ganzzahlig), ein Satz ist eine Prozentzahl als ``Decimal`` (19, 7, 5,5).

Für die Buchungen eines Belegs mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1) kommen dazu: ``mixed_groups`` (die Gruppen eines
Belegs, geprüft gegen seine gespeicherten Summen), ``split_payment`` (wie sich eine Zahlung auf die Gruppen verteilt – Betrag und Steuer je
Satz), ``booking_rate`` (die EINE Form, in der der Satz einer Gruppe in ``Bookings.TaxRate`` steht) und ``pct_text`` (der Satz für Texte). Für die Erstattung
und den Schutz beim Entfernen einer Zahlung: ``refund_pool`` (die Gruppen, aus denen erstattet wird) und ``booked_fault`` (ob der gebuchte Stand je Satz
zu ihnen passt – die Eingangsprüfung von ``split_payment``, von hier aus auch vorab zu fragen).

Für die Stammdaten „Steuersätze“ (2.8.0) kommen dazu: der Satz als Hundertstel Prozent – die EINE ganze Zahl, in der ein Eintrag der Stammdaten und die
Position eines Belegs ihn führen (``parse_rate_bp`` liest ihn streng aus einem Text, ``bp_pct`` macht die Prozentzahl daraus) –, ``group_tax`` (die
Steuer einer Gruppe: zweistufig gerundet wie bisher oder, für Positionen, die ihren Satz in Hundertstel Prozent mitbringen, einstufig nach EN 16931),
``group_label`` (der Satz einer Gruppe für Texte, „steuerfrei“ für eine steuerbefreite) und ``check_tax_entry`` (die Regeln eines Eintrags der Stammdaten
mit ihren festen Sätzen ``TAX_ENTRY_*``). Eine Position ohne Satz in Hundertstel Prozent rechnet und gruppiert genau wie vorher.
"""
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from money import prorate, round_minor, tax_from_net

# Die Vorbelegung der Stammdaten „Steuersätze“ (Schema 2008: Die Tabelle bekommt beim Anlegen genau diese Sätze) und die Positivliste des bisherigen
# Payloads: die Sätze, aus denen ein Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1) seine Positionen ohne einen Eintrag der Stammdaten
# wählen darf, als Prozentzahlen in der Form von ``rate_pct`` – die EINE Stelle dafür: ``server.handlers._tax_fields`` lehnt jeden anderen Satz ab. Kein
# Code nimmt „zwei Sätze“ oder „19 und 7“ fest an, Gruppen, Prüfungen und Schleifen arbeiten für beliebig viele. Jeder Satz hat höchstens zwei
# Nachkommastellen (``rate_pct`` rundet darauf, die Summen beim Speichern rechnen mit dem Satz, wie er dasteht – mit einer dritten liefen gespeicherte
# Steuer und Dokument auseinander); ``tests/test_mixed_tax_documents.py`` prüft es.
MIXED_TAX_RATES = (Decimal('19'), Decimal('7'))

# Die Art eines Eintrags der Stammdaten (``TaxRates.Kind``) und die Obergrenze des Satzes: 100 % in Hundertstel Prozent (1900 = 19 %, 550 = 5,5 %)
EXEMPT, STANDARD = 'exempt', 'standard'
MAX_RATE_BP = 10000
# Der kleinste Satz über 0: 1 % – ``rate_pct`` liest einen Wert zwischen 0 und 1 als Bruch (0,5 wäre dort 50 %), ein Satz unter 1 % ließe sich in keinem
# Beleg verwenden (``mixed_groups``, ``server.handlers._tax_fields``)
MIN_RATE_BP = 100


def rate_pct(value):
    """Der Steuersatz als Prozentzahl, oder ``None`` ohne Angabe (``None`` und ``''``; die Vorgabe wählt der Aufrufer).

    Die Datenbank speichert den Satz teils als Bruch (0,19), teils als Prozentzahl (19): Ein Wert zwischen 0 und 1 gilt als Bruch
    und wird mit 100 multipliziert, alles andere – 0, 1, 7, 19, auch ein negativer Wert wie der Platzhalter −1 – ist schon die
    Prozentzahl. Das Ergebnis ist auf zwei Nachkommastellen gerundet und ohne überflüssige Nullen (``Decimal('19')``, nicht
    ``Decimal('19.00')``; ``normalize`` schreibt eine runde Zahl mit Nullen am Ende als Zehnerpotenz, ``Decimal('1E+2')`` ist 100 –
    gleich groß, nur anders dargestellt).

    Das ist die Regel von ``export.einvoice_model.rate_to_pct``, ohne deren Vorgabewert. ``server.handlers._rate_to_pct`` ist bewusst
    nicht damit vereinheitlicht: Sie liefert die Zahl, wie sie dasteht (``Decimal('19.00')``, ``Decimal('5.555')`` bleibt 5,555 statt
    5,56), und wirft bei einem unendlichen oder riesigen Wert nicht – jede Stelle behält ihre Zahlen, damit ihre Ausgaben dieselben
    bleiben."""
    if value is None or value == '':
        return None
    pct = Decimal(str(value))
    if 0 < pct < 1:
        pct = pct * 100
    return pct.quantize(Decimal('0.01')).normalize() if pct else Decimal('0')


# ``[0-9]`` statt ``\d``: ``\d`` passt auch auf Ziffern anderer Schriften
_RATE_TEXT = re.compile(r'([0-9]{1,3})(?:[.,]([0-9]{1,2}))?')


def parse_rate_bp(text):
    """Ein Steuersatz aus einem Text (Formularfeld) als ganze Zahl in Hundertstel Prozent – ``19`` → 1900, ``5,5`` und ``5.50`` → 550, ``100`` → 10000 –
    oder ``None``, wenn der Text keiner ist (nichts wird geraten, es gibt keinen Fehler).

    Gelesen wird streng: ein bis drei ASCII-Ziffern, danach – wenn überhaupt – ein Komma oder ein Punkt und ein bis zwei Nachkommastellen, von 0 bis 100
    Prozent (``MAX_RATE_BP``). Kein Leerraum, auch nicht am Rand (wer das Feld liest, entfernt ihn vorher), kein Vorzeichen, kein Prozentzeichen, keine
    Zehnerpotenz (``1e2``), kein ``nan``, keine Ziffern anderer Schriften, keine dritte Nachkommastelle (``5,555``, auch nicht ``5,500``), kein Satz über
    100 und alles, was kein Text ist. Kein ``float()``."""
    if not isinstance(text, str):
        return None
    match = _RATE_TEXT.fullmatch(text)
    if match is None:
        return None
    whole, fraction = match.groups()
    bp = int(whole) * 100 + int((fraction or '').ljust(2, '0'))
    return bp if bp <= MAX_RATE_BP else None


def bp_pct(bp):
    """Der Satz aus Hundertstel Prozent als Prozentzahl, exakt: 550 → ``Decimal('5.5')``, 1900 → ``Decimal('19')``, 1000 → ``Decimal('10')``. Das Ergebnis hat nie
    eine Zehnerpotenz als Exponenten (``normalize`` schriebe 10 als ``1E+1``) und ist gleich groß wie ``rate_pct`` desselben Satzes – bis auf Sätze unter 1
    Prozent: ``rate_pct`` liest einen Wert zwischen 0 und 1 als Bruch (0,5 wäre dort 50 %), ``bp_pct(50)`` ist 0,5 Prozent. ``bp``: eine ganze Zahl von 0 bis
    ``MAX_RATE_BP``."""
    return Decimal(bp) / 100


@dataclass(frozen=True)
class RateGroup:
    """Die Positionen eines Belegs mit einem Steuersatz: ``net`` ihre Nettosumme, ``tax`` die Steuer darauf (auf den Cent gerundet),
    ``gross`` = ``net`` + ``tax`` – alles Minor Units; ``pct`` der Satz als Prozentzahl."""
    pct: Decimal
    net: int
    tax: int
    gross: int


def group_tax(net, pct, exact=False):
    """Die Steuer einer Satzgruppe in Minor Units, immer auf den Cent gerundet. ``net``: die Nettosumme der Gruppe (Minor Units), ``pct``: der Satz als
    Prozentzahl (``Decimal``).

    ``exact`` falsch (der Standard, und für jede Position ohne Satz in Hundertstel Prozent): in zwei Schritten wie bisher – ``money.tax_from_net`` rundet
    auf die Minor Unit, ``round_minor`` danach auf den Cent. ``exact`` wahr: EINMAL, kaufmännisch (halb weg von null), aus dem ungerundeten Produkt – so
    rechnet die Norm (EN 16931, BR-CO-17: Basis mal Satz, auf zwei Nachkommastellen gerundet).

    Beide Wege sind gleich, solange der Satz ganzzahlig ist und das Netto in ganzen Cent: Die Steuer ist dann schon eine ganze Zahl von Minor Units, der
    erste Schritt rundet nichts. Sie weichen ab, wenn die exakte Steuer knapp unter einem halben Cent liegt (49,5 bis unter 50 Minor Units über einem
    ganzen Cent) – das braucht einen Satz mit Nachkommastellen (5,5 % auf 0,09 €: zweistufig 0,01 €, einstufig 0,00 €) oder ein Netto mit Bruchteil eines
    Cents. Beide sind spiegelbildlich: ``group_tax(-n, …)`` ist ``-group_tax(n, …)``, die Gruppen einer Stornorechnung also die negierten der Rechnung."""
    if not exact:
        return round_minor(tax_from_net(net, pct), 2)
    return int((Decimal(int(net)) * pct / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100


def _unpack(line):
    """Eine Zeile als ``(a, b, c)``: Ein Zweier-Tupel ``(a, b)`` – die Form von vor 2.8.0 – bekommt ``c = None``, ein Dreier-Tupel bringt ``c`` mit; jede andere
    Länge wirft ``ValueError`` wie das Auspacken."""
    if len(line) == 2:
        first, second = line
        return first, second, None
    first, second, third = line
    return first, second, third


def rate_groups(lines):
    """Die Satzgruppen eines Belegs, nach Satz absteigend.

    ``lines``: je Position ``(Satz als Prozentzahl, Nettobetrag in Minor Units)`` – der Satz ist schon normalisiert (``rate_pct``) – oder mit einem dritten
    Wert, ``(Satz, Nettobetrag, exact)``. Gleiche Sätze bilden EINE Gruppe; die Steuer entsteht je Gruppe einmal aus der Summe ihrer Nettobeträge
    (``group_tax``), die Steuer des Belegs ist die Summe der ``tax`` der Gruppen. Eine Gruppe mit Nettosumme 0 bleibt stehen.

    Gerundet wird zweistufig wie bisher – für jedes Zweier-Tupel, für ``exact`` falsch oder ``None`` –, einstufig nur, wenn ALLE Zeilen der Gruppe ``exact`` sind:
    eine Gruppe, in der eine Zeile es nicht ist, rechnet wie vorher."""
    net_by_rate = {}
    exact_by_rate = {}
    for line in lines:
        pct, net, exact = _unpack(line)
        net_by_rate[pct] = net_by_rate.get(pct, 0) + net
        exact_by_rate[pct] = exact_by_rate.get(pct, True) and bool(exact)
    groups = []
    for pct in sorted(net_by_rate, reverse=True):
        net = net_by_rate[pct]
        tax = group_tax(net, pct, exact_by_rate[pct])
        groups.append(RateGroup(pct=pct, net=net, tax=tax, gross=net + tax))
    return groups


def item_rate(value, default=None):
    """Der Steuersatz einer Position als Prozentzahl (``rate_pct``); fehlt die Angabe (``None``, ``''``), gilt ``default`` – eine
    Prozentzahl, wie in ``export.einvoice_model.build_einvoice`` der Satz im Kopf des Belegs."""
    pct = rate_pct(value)
    return default if pct is None else pct


def item_rate_of(rate, bp, default=None):
    """Der Satz einer Position als Prozentzahl: Bringt sie ihren Satz in Hundertstel Prozent mit (``bp``, nicht ``None``), ist es dieser (``bp_pct``), sonst
    wie bisher ``item_rate(rate, default)``."""
    return item_rate(rate, default) if bp is None else bp_pct(bp)


def item_groups(pairs, default=None):
    """Die Satzgruppen eines Belegs aus seinen Positionen – die EINE Stelle für das PDF (Rechnung, Stornorechnung, Angebot); die Buchungen
    nehmen dieselbe.

    ``pairs``: je Position ``(Satz, Nettobetrag in Minor Units)`` oder ``(Satz, Nettobetrag, bp)``. Der Satz steht so da, wie die Datenbank ihn führt (Bruch
    oder Prozentzahl, ``InvoiceItems.TaxRate``) und wird über ``item_rate`` normalisiert (fehlt er, gilt ``default``); der Nettobetrag ist
    ``InvoiceItems.TotalNet`` in Minor Units – so liest ihn ``export.einvoice_model.build_einvoice`` (``to_minor``). Gruppiert und gerundet wird wie in
    ``rate_groups``: Damit sind die Gruppen die der E-Rechnung und, summiert, die gespeicherten Summen des Belegs.

    ``bp``: der Satz in Hundertstel Prozent, den die Position als Momentaufnahme trägt (``InvoiceItems.TaxRateBp``, sonst ``None``). Er geht dem Satz vor
    (``item_rate_of``) und lässt ihre Gruppe einstufig rechnen (``group_tax``, nach EN 16931) – sind alle Positionen der Gruppe so, sonst bleibt es bei
    der zweistufigen Rundung."""
    lines = []
    for pair in pairs:
        rate, net, bp = _unpack(pair)
        lines.append((item_rate_of(rate, bp, default), net, bp is not None))
    return rate_groups(lines)


class RateGroupsMismatch(ValueError):
    """Die Positionen eines Belegs mit Steuersatz je Position lassen sich nicht zu Satzgruppen machen oder passen nicht zu seinen
    gespeicherten Summen (``mixed_groups``). Ein Beleg, den die App speichert, hat das nie; es heißt, dass jemand an der App vorbei
    etwas geändert hat. ``str(exc)`` ist nur für das Server-Log gedacht – der Klartext für den Nutzer ist ein fester Satz des Aufrufers."""


def mixed_groups(sum_net, tax_amount, sum_gross, pairs):
    """Die Satzgruppen eines Belegs mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1), nach Satz absteigend – geprüft gegen
    seine gespeicherten Summen. Die EINE Stelle, aus der die Buchungen (Forderung je Satz, Verteilung einer Zahlung) ihre Gruppen nehmen.

    ``sum_net``, ``tax_amount``, ``sum_gross``: ``Invoices.SumNet``/``TaxAmount``/``SumGross`` (Minor Units), ``pairs``: je Position
    ``(InvoiceItems.TaxRate wie gespeichert, InvoiceItems.TotalNet in Minor Units)`` oder mit dem Satz in Hundertstel Prozent, den die Position als
    Momentaufnahme trägt, ``(TaxRate, TotalNet, InvoiceItems.TaxRateBp)`` (``None`` ohne). Gruppiert und gerundet wird wie im PDF und in der
    E-Rechnung (``item_groups``).

    :class:`RateGroupsMismatch`, wenn der Beleg keine Position hat, eine Position keinen oder keinen lesbaren Satz trägt (``None``, ``''``,
    Text), der Satz negativ ist (der Platzhalter −1 für „kein Steuerausweis“ und jeder andere negative Wert: ``rate_groups`` rechnete damit
    eine negative Steuer) oder über 100 liegt, ein Nettobetrag keine ganze Zahl ist (ein Wahrheitswert zählt nicht), der Satz in Hundertstel Prozent
    keine ganze Zahl ist oder nicht genau der Satz der Position (``rate_pct`` mal 100: ein Wert zwischen 0 und 1 gilt dort als Bruch, ein Satz unter 1
    Prozent passt also nie) oder eine der drei Summen nicht genau die Summe der Gruppen ist. Dass jede Gruppe einen positiven Betrag hat, wird hier nicht
    verlangt – eine Stornorechnung trägt negative Gruppen; wer bucht, prüft es selbst."""
    pairs = list(pairs)
    if not pairs:
        raise RateGroupsMismatch('Der Beleg hat keine Position.')
    for number, pair in enumerate(pairs, 1):
        rate, net, bp = _unpack(pair)
        if type(net) is not int:
            raise RateGroupsMismatch(f'Position {number}: Der Nettobetrag ist keine ganze Zahl.')
        try:
            pct = rate_pct(rate)
        except (ArithmeticError, ValueError):
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz ist keine Zahl.') from None
        if pct is None or not 0 <= pct <= 100:
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz fehlt oder liegt nicht zwischen 0 und 100 Prozent.')
        if bp is not None and (type(bp) is not int or bp != pct * 100):
            raise RateGroupsMismatch(f'Position {number}: Der Satz in Hundertstel Prozent passt nicht zum Steuersatz der Position.')
    groups = item_groups(pairs)
    stored = (sum_net, tax_amount, sum_gross)
    found = (sum(group.net for group in groups), sum(group.tax for group in groups), sum(group.gross for group in groups))
    if any(type(value) is not int for value in stored) or stored != found:
        raise RateGroupsMismatch(f'Die Summen des Belegs {stored} passen nicht zu seinen Satzgruppen {found}.')
    return groups


def booking_rate(pct):
    """Der Satz einer Gruppe, wie er in ``Bookings.TaxRate`` steht: als Bruch (19 → 0,19; 7 → 0,07; 5,5 → 0,055), ``None`` für 0 – so schreiben
    auch ``_book_receivable`` und ``_booking_tax_rate`` einen Beleg ohne Steuersatz. Dieselbe Gleitkommazahl wie beim Beleg mit EINEM Satz (dort
    kommt 0,19 vom Formular), damit jede Stelle, die einen Satz liest (Schlüssel der Erlöskonten, EÜR, DATEV, Summen- und Saldenliste), ihn
    gleich liest. ``pct``: die Prozentzahl einer Gruppe (``rate_pct``)."""
    return None if pct == 0 else float(pct / Decimal(100))


def pct_text(pct):
    """Der Satz einer Gruppe für einen Text: mit Komma und ohne überflüssige Nullen (``19``, ``7``, ``5,5``). ``rate_pct`` selbst wird nie
    ausgegeben (``normalize`` schreibt 10 als ``1E+1``)."""
    return format(pct, 'f').replace('.', ',')


def group_label(pct, exempt):
    """Der Satz einer Gruppe für einen Text – ``19 %``, ``5,5 %``, bei einer steuerfreien Gruppe (``exempt``) „steuerfrei“ statt „0 %“. Für die Meldungen zu
    einem Beleg mit Steuersatz je Position und für das PDF. ``pct``: die Prozentzahl der Gruppe."""
    return 'steuerfrei' if exempt else f'{pct_text(pct)} %'


@dataclass(frozen=True)
class SplitPart:
    """Der Teil einer Zahlung, der auf einen Satz entfällt: ``amount`` (Betrag) und ``tax`` (die darin enthaltene Steuer) in Minor Units;
    ``pct`` der Satz der Gruppe."""
    pct: Decimal
    amount: int
    tax: int


def _whole(value):
    """``value`` ist eine ganze Zahl (ein Wahrheitswert zählt nicht)?"""
    return type(value) is int


def refund_pool(paid):
    """Die Gruppen, aus denen eine Erstattung nimmt (Eingabe von ``split_payment``): je Satz, dessen Zahlungen etwas getragen haben, eine Gruppe mit genau
    dem als Brutto und der Steuer darin – nach Satz absteigend, wie ``mixed_groups``. ``paid``: ``{Satz: (Betrag, Steuer)}``, was die Zahlungen der
    stornierten Rechnung je Satz gebucht haben (Minor Units). Der Topf ist nicht das Brutto der Rechnung: Eine Teilzahlung hat es noch nicht gefüllt. Ein
    Satz ohne Zahlung hat keine Gruppe – wer dort erstattet hat, hat mehr erstattet als gezahlt (``booked_fault``). Die EINE Stelle für die Erstattung, die
    Prüfung beim Entfernen einer Zahlung und die Prüfliste."""
    return [RateGroup(pct, 0, paid[pct][1], paid[pct][0]) for pct in sorted(paid, reverse=True) if paid[pct][0] > 0]


def booked_fault(groups, booked):
    """Der erste Satz, dessen gebuchter Stand ``booked`` nicht zu seiner Gruppe passt – als ``(Satz, Grund)`` (der Grund ist ein Satz für das Server-Log) –,
    sonst ``None``. ``groups``: die Gruppen (``mixed_groups`` bzw. ``refund_pool``), ``booked``: ``{Satz: (Betrag, Steuer)}`` (Minor Units).

    Es passt nicht, wenn ein Satz mehr gebucht hat als das Brutto seiner Gruppe oder mehr Steuer als deren Steuer (auch unter 0 oder keine ganze Zahl), wenn
    er ganz bezahlt ist, ohne dass seine Steuer ganz gebucht wäre, oder wenn ein Satz gebucht ist, den die Gruppen nicht tragen (zuletzt, nach den Gruppen in
    ihrer Reihenfolge, absteigend). **Das ist genau die Eingangsprüfung von ``split_payment``**: Sie lehnt in diesen Fällen ab. Wer den Stand vor der nächsten
    Zahlung oder Erstattung prüfen will – etwa ob nach dem Entfernen einer Zahlung je Satz noch erstattet werden kann –, fragt diese Funktion, statt die
    Bedingungen nachzubauen. Die Gruppen selbst prüft sie nicht (``split_payment`` tut es vorher)."""
    for group in groups:
        done_amount, done_tax = booked.get(group.pct, (0, 0))
        if not (_whole(done_amount) and _whole(done_tax) and 0 <= done_amount <= group.gross and 0 <= done_tax <= group.tax):
            return group.pct, f'Gebucht ist mehr, als der Satz {group.pct} hergibt.'
        if done_amount == group.gross and done_tax != group.tax:
            return group.pct, f'Der Satz {group.pct} ist ganz bezahlt, seine Steuer aber nicht ganz gebucht.'
    unknown = sorted(set(booked) - {group.pct for group in groups}, reverse=True)
    return (unknown[0], 'Es ist ein Satz gebucht, den der Beleg nicht trägt.') if unknown else None


def split_payment(groups, booked, amount):
    """Wie sich der Betrag ``amount`` einer Zahlung auf die Satzgruppen eines Belegs verteilt – je Satz Betrag und Steuer, alles Minor Units.

    ``groups``: die Gruppen des Belegs (``mixed_groups``: verschiedene Sätze, jede mit positivem Brutto, in der Reihenfolge, in der Reste
    verteilt werden – absteigend nach Satz), ``booked``: ``{Satz: (Betrag, Steuer)}`` – was die ANDEREN Zuordnungen je Satz tragen (ein fehlender
    Satz zählt als (0, 0)), ``amount``: der neue Teil, eine ganze Zahl über 0. Rückgabe: je Gruppe ein :class:`SplitPart`, in der Reihenfolge der
    Gruppen, auch einer mit Betrag 0 – für ihn schreibt der Aufrufer keine Zeile (und er trägt nie eine Steuer).

    ``ValueError``, wenn der Betrag nicht über 0 liegt oder über dem liegt, was noch offen ist, ein Satz mehr gebucht hat als sein Brutto oder
    mehr Steuer als seine Steuer, ein Satz ganz bezahlt ist, ohne dass seine Steuer ganz gebucht wäre (der Bestand ist dann nicht stimmig; das
    prüft ``booked_fault``, die auch fragen kann, wer den Stand vorab prüfen will), oder die Gruppen nicht verschieden und positiv sind – der Aufrufer
    lehnt dann mit Klartext ab.

    **Betrag:** Die schließende Zahlung (sie deckt alles Offene) schließt jede Gruppe mit ihrem Rest. Jede andere Zahlung zielt je Satz auf den
    Anteil am Brutto, den die Summe der Zahlungen nach ihr erreicht (``Brutto der Gruppe × gezahlt / Brutto des Belegs``, auf den Cent gerundet,
    **bei genau einem halben Cent nach unten**: sonst bekäme die kleinere Gruppe den Cent) und nimmt davon, was ihr fehlt (nie unter 0, nie über
    ihrem Rest). Die Gruppe mit dem größten offenen Betrag (bei Gleichstand der höhere Satz) bekommt, was übrig bleibt – der Cent-Rest geht so an
    die größere Gruppe. Passt dieser Rest nicht (mehr als offen: der Überhang; weniger als 0: der Fehlbetrag), geht er in der Reihenfolge der
    Gruppen an die anderen. Die Summe der Teile ist immer ``amount``.

    **Steuer:** Hat die Zahlung eine Gruppe geschlossen (Teil gleich Rest – auch die schließende Zahlung), bekommt sie den Rest der Steuer der
    Gruppe; sonst ist es der anteilige Betrag, auf den Cent gerundet, höchstens der Rest. So hat nach der letzten Zahlung jede Gruppe genau ihre
    Steuer, und nie steht ein Teil mit Betrag 0 und einer Steuer da."""
    groups = list(groups)
    if not _whole(amount) or amount <= 0:
        raise ValueError('Der Betrag muss eine ganze Zahl über 0 sein.')
    if not groups or len({group.pct for group in groups}) != len(groups) or any(group.gross <= 0 for group in groups):
        raise ValueError('Die Gruppen müssen verschiedene Sätze und je ein positives Brutto haben.')
    fault = booked_fault(groups, booked)
    if fault:
        raise ValueError(fault[1])
    done, taxed, still_open = [], [], []
    for group in groups:
        done_amount, done_tax = booked.get(group.pct, (0, 0))
        done.append(done_amount)
        taxed.append(done_tax)
        still_open.append(group.gross - done_amount)
    open_total = sum(still_open)
    if amount > open_total:
        raise ValueError('Der Betrag liegt über dem, was noch offen ist.')

    if amount == open_total:
        parts = list(still_open)
    else:
        gross_total = sum(group.gross for group in groups)
        paid_after = gross_total - open_total + amount
        largest = max(range(len(groups)), key=lambda i: (still_open[i], groups[i].pct))
        parts = [0] * len(groups)
        for i, group in enumerate(groups):
            if i == largest:
                continue
            # auf den Cent gerundet (100 Minor Units), bei genau einem halben Cent nach unten – ganzzahlig, damit nichts an der Genauigkeit scheitert
            cents, remainder = divmod(group.gross * paid_after, gross_total * 100)
            if 2 * remainder > gross_total * 100:
                cents += 1
            parts[i] = min(max(cents * 100 - done[i], 0), still_open[i])
        parts[largest] = amount - sum(parts)
        if parts[largest] > still_open[largest]:
            over = parts[largest] - still_open[largest]
            parts[largest] = still_open[largest]
            for i in range(len(groups)):
                if i != largest:
                    take = min(still_open[i] - parts[i], over)
                    parts[i] += take
                    over -= take
        elif parts[largest] < 0:
            short = -parts[largest]
            parts[largest] = 0
            for i in range(len(groups)):
                if i != largest:
                    take = min(parts[i], short)
                    parts[i] -= take
                    short -= take

    result = []
    for i, group in enumerate(groups):
        left = group.tax - taxed[i]
        if parts[i] == 0:
            tax = 0
        elif parts[i] == still_open[i]:
            tax = left
        else:
            tax = min(max(round_minor(prorate(group.tax, parts[i], group.gross), 2), 0), left)
        result.append(SplitPart(group.pct, parts[i], tax))
    return result


# ── Die Regeln eines Eintrags der Stammdaten „Steuersätze“ ───────────────────────────────────────────────────────────────────────

MAX_LABEL_LENGTH = 60
MAX_REASON_LENGTH = 300

# Die festen Sätze: der Klartext für den Nutzer, nie ein Ausnahmetext. ``check_tax_entry`` liefert sie; die Regel „nie den letzten wählbaren Eintrag löschen
# oder abschalten“ prüft die Datenbankschicht (sie kennt die anderen Einträge) – ihr Satz steht trotzdem hier, an der einen Stelle der Texte.
TAX_ENTRY_LABEL = f'Bitte eine Bezeichnung angeben (höchstens {MAX_LABEL_LENGTH} Zeichen).'
TAX_ENTRY_RATE = 'Bitte den Steuersatz als Zahl angeben: 0 oder 1 bis 100, mit höchstens zwei Nachkommastellen.'
TAX_ENTRY_KIND = 'Bitte eine gültige Art wählen.'
TAX_ENTRY_EXEMPT_RATE = 'Eine steuerfreie Leistung hat den Satz 0.'
TAX_ENTRY_REASON = f'Bitte den Grund der Steuerbefreiung angeben – er steht auf der Rechnung (höchstens {MAX_REASON_LENGTH} Zeichen).'
TAX_ENTRY_REVENUE_REQUIRED = 'Bitte ein Erlöskonto wählen – für diesen Steuersatz gibt es kein Standardkonto.'
TAX_ENTRY_REVENUE_NOT_INCOME = 'Das gewählte Konto ist kein Erlöskonto.'
TAX_ENTRY_LAST_ACTIVE = 'Der letzte wählbare Steuersatz lässt sich nicht löschen oder abschalten.'


def _plain_text(text, limit):
    """``text`` ist ein Text von 1 bis ``limit`` Zeichen, nicht nur Leerraum, ohne Steuer- und Formatzeichen (Unicode-Kategorien ``Cc`` und ``Cf``, auch
    Zeilenumbruch und Tabulator) – Bezeichnung und Befreiungsgrund stehen in Auswahl, PDF und E-Rechnung."""
    return (isinstance(text, str) and 0 < len(text) <= limit and bool(text.strip())
            and not any(unicodedata.category(char) in ('Cc', 'Cf') for char in text))


def check_tax_entry(label, bp, kind, reason, revenue_coa_id, has_default_revenue, revenue_is_income=True):
    """Die Regeln eines Eintrags der Stammdaten – die EINE Stelle dafür, rein (keine Datenbank): der feste Ablehnungssatz der ersten Regel, die verletzt ist
    (``TAX_ENTRY_*``), sonst ``None``. Die Regeln, in dieser Reihenfolge:

    1. ``label``: so, wie er gespeichert wird (der Aufrufer hat den Leerraum am Rand schon entfernt) – ein Text von 1 bis ``MAX_LABEL_LENGTH`` Zeichen,
       nicht nur Leerraum, ohne Steuer- und Formatzeichen (Unicode-Kategorien ``Cc`` und ``Cf``, auch Zeilenumbruch und Tabulator).
    2. ``bp``: der Satz in Hundertstel Prozent als ganze Zahl – 0 oder von ``MIN_RATE_BP`` (1 %) bis ``MAX_RATE_BP`` –, das Ergebnis von ``parse_rate_bp``
       (``None`` für einen Text, der keiner ist). Ein Satz zwischen 0 und 1 % wird abgelehnt: Kein Beleg könnte ihn tragen.
    3. ``kind``: genau ``STANDARD`` oder ``EXEMPT``.
    4. ``EXEMPT``: der Satz ist 0.
    5. ``EXEMPT``: ``reason`` (der Grund der Befreiung, er steht auf der Rechnung) ist ein Text von 1 bis ``MAX_REASON_LENGTH`` Zeichen, nicht nur
       Leerraum, ohne Steuer- und Formatzeichen (wie die Bezeichnung). Bei ``STANDARD`` zählt ``reason`` hier nicht (wer speichert, legt dort keinen ab).
    6. Erlöskonto: ``revenue_coa_id`` ist die ID des Kontos oder ``None``. Ohne Konto muss der Satz ein Standardkonto haben (``has_default_revenue``, vom
       Aufrufer: für 19 und 7 Prozent ja, für jeden anderen Satz und für jede steuerfreie Leistung nein), sonst ``TAX_ENTRY_REVENUE_REQUIRED``. Mit
       Konto muss es eines der Erlöse sein (``revenue_is_income``, vom Aufrufer, der die Konten kennt), sonst ``TAX_ENTRY_REVENUE_NOT_INCOME``.

    Nicht hier: ob das Konto vorhanden ist und ob der letzte wählbare Eintrag bleibt (``TAX_ENTRY_LAST_ACTIVE``) – beides kennt nur die Datenbankschicht."""
    if not _plain_text(label, MAX_LABEL_LENGTH):
        return TAX_ENTRY_LABEL
    if type(bp) is not int or not (bp == 0 or MIN_RATE_BP <= bp <= MAX_RATE_BP):
        return TAX_ENTRY_RATE
    if kind not in (STANDARD, EXEMPT):
        return TAX_ENTRY_KIND
    if kind == EXEMPT:
        if bp != 0:
            return TAX_ENTRY_EXEMPT_RATE
        if not _plain_text(reason, MAX_REASON_LENGTH):
            return TAX_ENTRY_REASON
    if revenue_coa_id is None:
        return None if has_default_revenue else TAX_ENTRY_REVENUE_REQUIRED
    return None if revenue_is_income else TAX_ENTRY_REVENUE_NOT_INCOME
