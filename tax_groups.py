# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Steuer eines Belegs je Satzgruppe – EINE Stelle für Summen, E-Rechnung und (später) Buchungen.

Positionen eines Belegs können verschiedene Steuersätze tragen. Nach EN 16931 (BR-CO-17) wird die Steuer je Satz aus der SUMME der
Nettobeträge der Gruppe gerechnet und kaufmännisch auf den Cent gerundet; die Steuer des Belegs ist die Summe der Gruppen – nicht die
Summe der je Position gerundeten Steuern. Dieses Modul ist genau diese Regel, rein: keine Datenbank, keine Uhr, nur ``money`` und die
Standardbibliothek, damit ``db/``, ``server/`` und ``export/`` sie gleichermaßen nutzen können.

Es rechnet für beliebig viele und beliebige Sätze (19 %, 7 %, 5,5 % …, auch 0 %): Kein Satz und keine Zahl von Sätzen ist fest
vorgegeben. Beträge sind Minor Units (``money``, ganzzahlig), ein Satz ist eine Prozentzahl als ``Decimal`` (19, 7, 5,5).
"""
from dataclasses import dataclass
from decimal import Decimal

from money import round_minor, tax_from_net

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
