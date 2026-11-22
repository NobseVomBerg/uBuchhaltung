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
Satz), ``booking_rate`` (die EINE Form, in der der Satz einer Gruppe in ``Bookings.TaxRate`` steht) und ``pct_text`` (der Satz für Texte).
"""
from dataclasses import dataclass
from decimal import Decimal

from money import prorate, round_minor, tax_from_net

# Die Sätze, aus denen ein Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1) seine Positionen wählen darf, als Prozentzahlen
# in der Form von ``rate_pct`` – die EINE Stelle dafür: ``server.handlers._tax_fields`` lehnt jeden anderen Satz ab. Später kommen Sätze dazu
# (steuerfreie Positionen, 5,5 % …); kein Code nimmt „zwei Sätze“ oder „19 und 7“ fest an, Gruppen, Prüfungen und Schleifen arbeiten für beliebig
# viele.
MIXED_TAX_RATES = (Decimal('19'), Decimal('7'))


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


@dataclass(frozen=True)
class RateGroup:
    """Die Positionen eines Belegs mit einem Steuersatz: ``net`` ihre Nettosumme, ``tax`` die Steuer darauf (auf den Cent gerundet),
    ``gross`` = ``net`` + ``tax`` – alles Minor Units; ``pct`` der Satz als Prozentzahl."""
    pct: Decimal
    net: int
    tax: int
    gross: int


def rate_groups(lines):
    """Die Satzgruppen eines Belegs, nach Satz absteigend.

    ``lines``: je Position ``(Satz als Prozentzahl, Nettobetrag in Minor Units)`` – der Satz ist schon normalisiert (``rate_pct``).
    Gleiche Sätze bilden EINE Gruppe; die Steuer entsteht je Gruppe einmal aus der Summe ihrer Nettobeträge (kaufmännisch auf den
    Cent gerundet), die Steuer des Belegs ist die Summe der ``tax`` der Gruppen. Eine Gruppe mit Nettosumme 0 bleibt stehen."""
    net_by_rate = {}
    for pct, net in lines:
        net_by_rate[pct] = net_by_rate.get(pct, 0) + net
    groups = []
    for pct in sorted(net_by_rate, reverse=True):
        net = net_by_rate[pct]
        tax = round_minor(tax_from_net(net, pct), 2)
        groups.append(RateGroup(pct=pct, net=net, tax=tax, gross=net + tax))
    return groups


def item_rate(value, default=None):
    """Der Steuersatz einer Position als Prozentzahl (``rate_pct``); fehlt die Angabe (``None``, ``''``), gilt ``default`` – eine
    Prozentzahl, wie in ``export.einvoice_model.build_einvoice`` der Satz im Kopf des Belegs."""
    pct = rate_pct(value)
    return default if pct is None else pct


def item_groups(pairs, default=None):
    """Die Satzgruppen eines Belegs aus seinen Positionen – die EINE Stelle für das PDF (Rechnung, Stornorechnung, Angebot); die Buchungen
    nehmen dieselbe.

    ``pairs``: je Position ``(Satz, Nettobetrag in Minor Units)``. Der Satz steht so da, wie die Datenbank ihn führt (Bruch oder Prozentzahl,
    ``InvoiceItems.TaxRate``) und wird über ``item_rate`` normalisiert (fehlt er, gilt ``default``); der Nettobetrag ist ``InvoiceItems.TotalNet``
    in Minor Units – so liest ihn ``export.einvoice_model.build_einvoice`` (``to_minor``). Gruppiert und gerundet wird wie in ``rate_groups``:
    Damit sind die Gruppen die der E-Rechnung und, summiert, die gespeicherten Summen des Belegs."""
    return rate_groups([(item_rate(rate, default), net) for rate, net in pairs])


class RateGroupsMismatch(ValueError):
    """Die Positionen eines Belegs mit Steuersatz je Position lassen sich nicht zu Satzgruppen machen oder passen nicht zu seinen
    gespeicherten Summen (``mixed_groups``). Ein Beleg, den die App speichert, hat das nie; es heißt, dass jemand an der App vorbei
    etwas geändert hat. ``str(exc)`` ist nur für das Server-Log gedacht – der Klartext für den Nutzer ist ein fester Satz des Aufrufers."""


def mixed_groups(sum_net, tax_amount, sum_gross, pairs):
    """Die Satzgruppen eines Belegs mit Steuersatz je Position (``Invoices.MixedTaxRates`` = 1), nach Satz absteigend – geprüft gegen
    seine gespeicherten Summen. Die EINE Stelle, aus der die Buchungen (Forderung je Satz, Verteilung einer Zahlung) ihre Gruppen nehmen.

    ``sum_net``, ``tax_amount``, ``sum_gross``: ``Invoices.SumNet``/``TaxAmount``/``SumGross`` (Minor Units), ``pairs``: je Position
    ``(InvoiceItems.TaxRate wie gespeichert, InvoiceItems.TotalNet in Minor Units)``. Gruppiert und gerundet wird wie im PDF und in der
    E-Rechnung (``item_groups``).

    :class:`RateGroupsMismatch`, wenn der Beleg keine Position hat, eine Position keinen oder keinen lesbaren Satz trägt (``None``, ``''``,
    Text), der Satz negativ ist (der Platzhalter −1 für „kein Steuerausweis“ und jeder andere negative Wert: ``rate_groups`` rechnete damit
    eine negative Steuer) oder über 100 liegt, ein Nettobetrag keine ganze Zahl ist (ein Wahrheitswert zählt nicht) oder eine der drei
    Summen nicht genau die Summe der Gruppen ist. Dass jede Gruppe einen positiven Betrag hat, wird hier nicht verlangt – eine
    Stornorechnung trägt negative Gruppen; wer bucht, prüft es selbst."""
    pairs = list(pairs)
    if not pairs:
        raise RateGroupsMismatch('Der Beleg hat keine Position.')
    for number, (rate, net) in enumerate(pairs, 1):
        if type(net) is not int:
            raise RateGroupsMismatch(f'Position {number}: Der Nettobetrag ist keine ganze Zahl.')
        try:
            pct = rate_pct(rate)
        except (ArithmeticError, ValueError):
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz ist keine Zahl.') from None
        if pct is None or not 0 <= pct <= 100:
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz fehlt oder liegt nicht zwischen 0 und 100 Prozent.')
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


def split_payment(groups, booked, amount):
    """Wie sich der Betrag ``amount`` einer Zahlung auf die Satzgruppen eines Belegs verteilt – je Satz Betrag und Steuer, alles Minor Units.

    ``groups``: die Gruppen des Belegs (``mixed_groups``: verschiedene Sätze, jede mit positivem Brutto, in der Reihenfolge, in der Reste
    verteilt werden – absteigend nach Satz), ``booked``: ``{Satz: (Betrag, Steuer)}`` – was die ANDEREN Zuordnungen je Satz tragen (ein fehlender
    Satz zählt als (0, 0)), ``amount``: der neue Teil, eine ganze Zahl über 0. Rückgabe: je Gruppe ein :class:`SplitPart`, in der Reihenfolge der
    Gruppen, auch einer mit Betrag 0 – für ihn schreibt der Aufrufer keine Zeile (und er trägt nie eine Steuer).

    ``ValueError``, wenn der Betrag nicht über 0 liegt oder über dem liegt, was noch offen ist, ein Satz mehr gebucht hat als sein Brutto oder
    mehr Steuer als seine Steuer, ein Satz ganz bezahlt ist, ohne dass seine Steuer ganz gebucht wäre (der Bestand ist dann nicht stimmig), oder
    die Gruppen nicht verschieden und positiv sind – der Aufrufer lehnt dann mit Klartext ab.

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
    if set(booked) - {group.pct for group in groups}:
        raise ValueError('Es ist ein Satz gebucht, den der Beleg nicht trägt.')
    done, taxed, still_open = [], [], []
    for group in groups:
        done_amount, done_tax = booked.get(group.pct, (0, 0))
        if not (_whole(done_amount) and _whole(done_tax) and 0 <= done_amount <= group.gross and 0 <= done_tax <= group.tax):
            raise ValueError(f'Gebucht ist mehr, als der Satz {group.pct} hergibt.')
        if done_amount == group.gross and done_tax != group.tax:
            raise ValueError(f'Der Satz {group.pct} ist ganz bezahlt, seine Steuer aber nicht ganz gebucht.')
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
