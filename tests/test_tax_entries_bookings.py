# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Buchungen eines Belegs mit einem Steuersatz aus den Stammdaten (2.8.0, Etappe 5): Erlöskonto, Wartekonto, Versenden, Zahlung, Storno, Erstattung, Umzug,
Neuaufbau und Prüfliste.

Die Positionen eines Belegs tragen die **Momentaufnahme** eines Eintrags der Stammdaten (``InvoiceItems.TaxRateBp`` ≠ NULL: der Satz in Hundertstel Prozent; dazu
``TaxCategory`` ``'E'`` für „steuerfrei“, ``TaxExemptReason`` und ``RevenueCoaId``). Kein Weg der App schreibt sie in dieser Etappe – die Tests legen die Belege direkt an
(``insert_invoice``/``insert_invoice_item``, die Summen aus ``tax_groups.item_groups``) und gehen dann über die Wege der App: ``set_invoice_status``,
``assign_invoice_payment``, ``cancel_invoice``, ``assign_storno_refund``.

Die festen Belege (Beträge in Cent in den Vergleichen):

* **F** (``F-100``): ganz steuerfrei, 100,00 – Kopf 0, eine Position mit Art ``'E'``, Grund und Erlöskonto 4120;
* **G** (``G-100``): 19 % × 100,00 (eine Position wie heute) und steuerfrei × 80,00 (mit Momentaufnahme, Konto 4120) – zusammen 199,00;
* **H** (``H-100``): 19 % × 100,00 und 5,5 % × 200,00 (Momentaufnahme, Konto 4150) – Steuer 19,00 und 11,00, zusammen 330,00.

* **Erlöskonto:** Trägt die Satzgruppe (bzw. der Beleg mit einem Satz) eine Momentaufnahme mit Konto, gilt es, immer – nie 4184/4185, nie ein gelerntes; ohne Momentaufnahme
  bleibt jede Regel von heute. Eine Rechnung mit Erlöskonto aus den Stammdaten lehrt kein Erlöskonto, und die Masken bieten dort keine Wahl.
* **Wartekonto** (keine neuen): 19 → 4405, 7 → 4345, Satz 0 → 4340; jeder andere Satz keines – der ganze Beleg bucht keine Forderung, jede Zahlung „Erlöskonto an Bank“ je
  Satz, und die Antwort des Versendens trägt ``NO_WAITING_HINT``. Für einen Beleg mit EINEM Satz und Momentaufnahme gilt der genaue Satz (7,4 % ist nicht 7 %).
* **Versenden** prüft das Erlöskonto der Stammdaten (``TAX_SEND_NO_REVENUE``, 409, nichts geschrieben); der Schlüssel, den die Transaktion mit der Vorbereitung vergleicht,
  trägt die Momentaufnahmen mit.
* **Umzug** auf die Ersatzrechnung unterbleibt (``MOVE_ENTRY_HINT``), **Neuaufbau** rechnet nur den offenen Betrag (Grund ``tax_entry``).
* **Prüfliste:** ``tax_entry_mismatch`` für widersprüchliche Momentaufnahmen; ein sauberer Beleg ergibt keinen Befund.
* **Die Momentaufnahme wird streng und überall gleich gelesen** (``InvoicePaymentsMixin._read_snapshots``): Der Satz in Hundertstel Prozent ist 0 oder 100 bis 10000 und der Satz der
  Position, auch bei einem Beleg mit EINEM Satz; Angaben ohne den Satz dazu (die halbe Momentaufnahme) sind ein Widerspruch. **Jeder Widerspruch hat einen eigenen festen Satz**
  (``TAX_ENTRY_CONTRADICTION``), ``MIXED_GROUPS_MISMATCH`` bleibt für „Positionen passen nicht zu den Summen“. Die **Erstattung prüft die Momentaufnahme nicht** (sie folgt den Zahlungen),
  ein abgelehntes **Versenden legt kein Konto an**, die Art „steuerfrei“ allein (das Beispiel eines Belegs nach § 19) ist **kein Befund**, und eine steuerfreie Gruppe heißt in
  Sätzen „steuerfreie Leistungen“.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (``CLOCK`` ist der festgesetzte Tag des Servers).
"""
import datetime
from collections import namedtuple
from decimal import Decimal

import pytest

from db.invoice_lock import CHANGED_MESSAGE, NO_WAITING_HINT, TAX_SEND_NO_REVENUE
from db.invoice_payments import (EXEMPT_SENTENCE_LABEL, MIXED_GROUPS_MISMATCH, MIXED_GROUP_NOT_POSITIVE, MIXED_REFUND_REVENUE_AMBIGUOUS, MIXED_REMOVAL_REFUSED,
                                 MOVE_ENTRY_HINT, MOVE_MIXED_HINT, MOVE_NONE_HINT, MOVE_REFUNDED_HINT, NO_REVENUE_COA, PaymentAssignment, PaymentRemovalRefused,
                                 TAX_ENTRY_CONTRADICTION, TAX_ENTRY_FINDINGS, TAX_PAYMENT_NO_REVENUE, MixedNotBookable, Snapshot, rate_sentence, valid_rate_bp)
from db.invoice_repair import REBUILD_SKIP_REASONS
from db.invoices import DEFAULT_REVENUE_ACCOUNT_NUMBERS
from money import from_minor, to_minor
from server.pages_invoice import PageInvoice, revenue_coa_choice
from server.pages_transactions import PageTransactions
from tax_groups import booking_rate, group_label, item_groups
from tests.invoice_world import add_payment, columns, query_rows, run_sql
from tests.test_invoice_reissue import allocations, reissue
from tests.test_invoice_storno import ONE_ITEM, USER, balance, cancel, sent, tables, world   # noqa: F401  (``world`` ist die Fixture)
from tests.test_mixed_tax_bookings import marked, pay_on, receivables, standing
from tests.test_mixed_tax_locks import drop_accounts, incoming, outgoing

D = Decimal
CLOCK = datetime.date(2026, 7, 31)                  # der Tag, den der Server für „heute“ hält (``today=`` von ``cancel_invoice``)
DAY = '2026-06-07'                                  # das Rechnungsdatum der festen Belege
STORNO_DAY = '2026-07-02'
REASON = 'Steuerfreie Leistung (erfundener Testgrund)'
FREE, REDUCED = 4120, 4150                          # Erlöskonten, die der Standard-Kontenrahmen nicht führt: nie ein Standardkonto, das die Erwartung von allein erfüllt
TEXT = 'Rechnung {} Malermeister Pinsel'

Part = namedtuple('Part', 'pct net entry')           # Satz in Prozent (Decimal), Netto (Text), die Momentaufnahme der Position (Dict) oder None – eine Position wie heute


# ── Hilfen ───────────────────────────────────────────────────────────────────

def account(db, number):
    """Das Erlöskonto ``number`` (COA-ID); gibt es das Konto nicht, entsteht es im Standard-Kontenrahmen."""
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, f'Erlöse (erfundenes Testkonto {number})', 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


def plain(pct, net):
    """Eine Position wie heute: ohne Momentaufnahme (``TaxRateBp`` NULL)."""
    return Part(D(pct), net, None)


def free(net, coa, reason=REASON):
    """Eine steuerfreie Position mit der Momentaufnahme ihres Eintrags: Art ``'E'``, Satz 0, Grund, Erlöskonto."""
    return Part(D(0), net, {'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': reason, 'revenue_coa_id': coa})


def entry(pct, net, coa):
    """Eine Position mit der Momentaufnahme eines Eintrags mit Satz ``pct`` (Art ``'S'``, Satz in Hundertstel Prozent, Erlöskonto ``coa`` oder ``None``)."""
    return Part(D(pct), net, {'tax_category': 'S', 'tax_rate_bp': int(D(pct) * 100), 'tax_exempt_reason': None, 'revenue_coa_id': coa})


def entry_invoice(w, number, parts, day=DAY, **over):
    """Ein Entwurf aus ``parts``: die Summen rechnet ``tax_groups.item_groups`` (so rechnet auch das Speichern der App), der Kopf trägt den höchsten Satz (als Bruch) und, bei mehr als
    einem Satz, das Kennzeichen ``MixedTaxRates``. Positionen und Kopf gehen über ``insert_invoice``/``insert_invoice_item`` – kein Weg der App schreibt die Momentaufnahme."""
    groups = item_groups([(float(part.pct), to_minor(D(part.net)), None if part.entry is None else part.entry['tax_rate_bp']) for part in parts])
    net, tax = sum(group.net for group in groups), sum(group.tax for group in groups)
    invoice_id = w.db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'delivery_date': day, 'payment_due_date': '2099-12-31', 'own_company_id': w.own, 'customer_id': w.cust,
        'seller_name': 'Eigene Firma GmbH', 'seller_company': 'Eigene Firma GmbH', 'buyer_name': 'Max Mustermann', 'buyer_company': 'Malermeister Pinsel',
        'bank_account_id': w.bank, 'currency': 'EUR', 'tax_category': 'S', 'tax_rate': float(max(group.pct for group in groups) / 100),
        'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': from_minor(net + tax), 'amount_due': from_minor(net + tax),
        'status': 'draft', 'mixed_tax_rates': 1 if len(groups) > 1 else None, **over})
    for position, part in enumerate(parts, 1):
        w.db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': f'Position {position}', 'quantity': 1, 'unit': 'C62',
                                  'price_per_unit': D(part.net), 'total_net': D(part.net), 'tax_rate': float(part.pct), **(part.entry or {})})
    return invoice_id


def send(w, invoice_id):
    """Versenden über den Weg der App (``set_invoice_status``; Dokumente erzeugt der Handler, nicht diese Schicht); liefert das Ergebnis, das ``ok`` ist."""
    change = w.db.set_invoice_status(invoice_id, 'sent', USER)
    assert change.ok, change.message
    return change


def free_invoice(w, number='F-100', **over):
    """F: ganz steuerfrei, 100,00."""
    return entry_invoice(w, number, [free('100.00', account(w.db, FREE))], **over)


def mixed_free_invoice(w, number='G-100', **over):
    """G: 19 % × 100,00 (wie heute) und steuerfrei × 80,00 (Momentaufnahme)."""
    return entry_invoice(w, number, [plain(19, '100.00'), free('80.00', account(w.db, FREE))], **over)


def reduced_invoice(w, number='H-100', **over):
    """H: 19 % × 100,00 (wie heute) und 5,5 % × 200,00 (Momentaufnahme, Konto 4150)."""
    return entry_invoice(w, number, [plain(19, '100.00'), entry('5.5', '200.00', account(w.db, REDUCED))], **over)


def findings(db, invoice_id=None, category=None):
    return [f for f in db.find_payment_inconsistencies(invoice_id) if category is None or f['category'] == category]


def refund_of(w, storno_id, amount, day='2026-07-10'):
    """Eine ausgehende Bankbewegung über ``amount`` € als Erstattung zuordnen; liefert das Ergebnis."""
    return w.db.assign_storno_refund(storno_id, outgoing(w, -amount, day))


def cancelled(w, invoice_id, day=STORNO_DAY):
    """Die Rechnung stornieren (Datenbankschicht, der Tag des Servers ist ``CLOCK``): liefert das Ergebnis, das ``ok`` ist."""
    storno = w.db.cancel_invoice(invoice_id, day, USER, today=CLOCK)
    assert storno.ok is True, storno.message
    return storno


# ═════════════════════════════════════════════════════════════════════════════
# Ganz steuerfrei (F): Forderung auf 4340, Zahlung auf das Erlöskonto der Momentaufnahme
# ═════════════════════════════════════════════════════════════════════════════

def test_the_fixture_documents_carry_the_snapshot_and_the_sums_of_item_groups(world):
    free_id, mixed_id, reduced_id = free_invoice(world), mixed_free_invoice(world), reduced_invoice(world)

    assert [(c['SumNet'], c['TaxAmount'], c['SumGross'], c['MixedTaxRates'], c['TaxRate']) for c in (columns(world.db, i) for i in (free_id, mixed_id, reduced_id))] == [
        (1000000, 0, 1000000, None, 0.0), (1800000, 190000, 1990000, 1, 0.19), (3000000, 300000, 3300000, 1, 0.19)]
    assert query_rows(world.db, 'SELECT TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', mixed_id) == [
        {'TaxCategory': 'S', 'TaxRate': 19.0, 'TaxRateBp': None, 'TaxExemptReason': None, 'RevenueCoaId': None},
        {'TaxCategory': 'E', 'TaxRate': 0.0, 'TaxRateBp': 0, 'TaxExemptReason': REASON, 'RevenueCoaId': account(world.db, FREE)}]


def test_a_completely_tax_free_invoice_books_its_receivable_on_the_waiting_account_for_0_percent(world):
    invoice_id = free_invoice(world)

    change = send(world, invoice_id)

    assert (change.code, change.message, change.payment_hint) == (200, None, None)
    assert receivables(world.db, 'F-100') == [(10000, 4340, 10000, None, None, 'transfer', DAY, TEXT.format('F-100'))]       # kein Satz, keine Steuer
    assert standing(world.db, invoice_id) == ('sent', 10000)


def test_the_payment_of_a_tax_free_invoice_books_on_the_account_of_the_snapshot_never_on_4184_or_4185(world):
    invoice_id = free_invoice(world)
    send(world, invoice_id)

    pay_on(world, invoice_id, 100.0)

    assert marked(world.db, 'F-100') == [(1800, 10000, 10000, None, None, 'liquid_first'), (4340, FREE, 10000, None, None, 'transfer')]
    assert standing(world.db, invoice_id) == ('paid', 0)
    assert balance(world.db, 4184) == 0 and balance(world.db, 4185) == 0 and balance(world.db, 4340) == 0 and balance(world.db, 10000) == 0
    assert findings(world.db) == []


def test_an_invoice_with_one_rate_ignores_every_other_account_for_the_snapshot_even_a_learned_one(world):
    """Das Konto der Momentaufnahme gilt immer: auch, wenn für den Satz 0 etwas „gelernt“ wäre oder der Nutzer ein anderes wählen wollte."""
    other = account(world.db, 4130)
    invoice_id = free_invoice(world)
    send(world, invoice_id)

    assignment = world.db.assign_invoice_payment(invoice_id, incoming(world, 100.0), revenue_coa_id=other)

    assert assignment.ok and assignment.revenue_coa_id == account(world.db, FREE)
    assert marked(world.db, 'F-100')[1][:2] == (4340, FREE)


def test_a_section_19_invoice_still_gets_its_standard_account_because_the_tax_free_invoice_with_its_own_account_teaches_nothing(world):
    """Ein ganz steuerfreier Beleg (Kopf 0) läge im Topf „0 und § 19“ und lehrte der nächsten Kleinunternehmer-Rechnung sein Konto – ``_learned_revenue_coa`` schließt ihn aus.
    Die § 19-Rechnung bucht auf das Standardkonto, das das Datum der Bankbewegung nennt (4185 bis 2025, 4184 ab 2026); der Satz 0 bleibt auf 4185."""
    invoice_id = free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)

    assert world.db._learned_revenue_coa(0.0) is None
    assert world.db.resolve_revenue_coa(-1, on='2025-12-31') == world.db.get_coa_id_by_account_number(4185)      # § 19: Kopf −1
    assert world.db.resolve_revenue_coa(-1, on='2026-01-01') == world.db.get_coa_id_by_account_number(4184)
    assert world.db.resolve_revenue_coa(0, on='2026-01-01') == world.db.get_coa_id_by_account_number(4185)       # Satz 0 mit Steuerausweis: fest 4185


def test_a_paid_reduced_rate_invoice_with_its_own_account_teaches_nothing_but_the_same_without_one_does(world):
    """Die Gegenprobe zum Lernen: Eine Rechnung mit 5,5 % ohne Erlöskonto der Stammdaten (wie heute) lehrt das gewählte Konto; die mit eigenem Konto lehrt nichts."""
    reduced_account = account(world.db, REDUCED)
    with_entry = entry_invoice(world, 'R-1', [entry('5.5', '100.00', reduced_account)])
    send(world, with_entry)
    pay_on(world, with_entry, 105.5)

    assert world.db._learned_revenue_coa(5.5) is None                                                   # nichts gelernt

    without = entry_invoice(world, 'R-2', [plain('5.5', '100.00')])
    send(world, without)
    booking = incoming(world, 105.5)
    assert world.db.assign_invoice_payment(without, booking, revenue_coa_id=reduced_account).ok         # die Wahl des Nutzers, wie heute

    assert world.db._learned_revenue_coa(5.5) == reduced_account                                        # das lernt genau diese Rechnung


def test_the_default_revenue_accounts_are_named_in_one_place_and_resolve_revenue_coa_uses_them(world, monkeypatch):
    import db.invoices

    assert DEFAULT_REVENUE_ACCOUNT_NUMBERS == {19: 4400, 7: 4300}
    assert world.db.resolve_revenue_coa(0.19) == world.db.get_coa_id_by_account_number(4400)
    assert world.db.resolve_revenue_coa(0.07) == world.db.get_coa_id_by_account_number(4300)
    assert world.db.resolve_revenue_coa(0.055) is None and world.db.resolve_revenue_coa(0.16) is None
    monkeypatch.setattr(db.invoices, 'DEFAULT_REVENUE_ACCOUNT_NUMBERS', {19: 4300})
    assert world.db.resolve_revenue_coa(0.19) == world.db.get_coa_id_by_account_number(4300)             # die Konstante wird gelesen, nichts steht doppelt im Code
    assert world.db.resolve_revenue_coa(0.07) is None


# ═════════════════════════════════════════════════════════════════════════════
# 19 % und steuerfrei (G): zwei Forderungen, Teil- und Schlusszahlung je Satz, Storno, Erstattung
# ═════════════════════════════════════════════════════════════════════════════

GROUP_RECEIVABLES = [(10000, 4405, 11900, 0.19, 1900, 'transfer', DAY, TEXT.format('G-100')),
                     (10000, 4340, 8000, None, None, 'transfer', DAY, TEXT.format('G-100'))]
# die Teilzahlung 100,00 (``tax_groups.split_payment``): 59,80 für 19 % (Steuer 9,55) und 40,20 für steuerfrei – der Satz 0 trägt keinen Satz und keine Steuer
FIRST_PAYMENT = [(1800, 10000, 10000, None, None, 'liquid_first'), (4405, 4400, 5980, 955, 0.19, 'transfer'), (4340, FREE, 4020, None, None, 'transfer')]
# die schließende Zahlung 99,00: jede Gruppe schließt mit ihrem Rest, 19 % mit dem Rest der Steuer (19,00 − 9,55)
SECOND_PAYMENT = [(1800, 10000, 9900, None, None, 'liquid_first'), (4405, 4400, 5920, 945, 0.19, 'transfer'), (4340, FREE, 3980, None, None, 'transfer')]


def test_sending_19_percent_and_tax_free_books_one_receivable_per_group_with_the_waiting_account_of_each(world):
    invoice_id = mixed_free_invoice(world)

    change = send(world, invoice_id)

    assert (change.payment_hint, columns(world.db, invoice_id)['MixedTaxRates']) == (None, 1)
    assert receivables(world.db, 'G-100') == GROUP_RECEIVABLES                       # 19 % mit Satz und Steuer, steuerfrei ohne beides – auf 4340
    assert standing(world.db, invoice_id) == ('sent', 19900)


def test_the_part_and_the_closing_payment_split_per_group_and_the_tax_free_group_carries_no_rate_and_no_tax(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)

    pay_on(world, invoice_id, 100.0)
    assert marked(world.db, 'G-100') == FIRST_PAYMENT and standing(world.db, invoice_id) == ('partial_payment', 9900)

    pay_on(world, invoice_id, 99.0, '2026-07-05')
    assert marked(world.db, 'G-100') == FIRST_PAYMENT + SECOND_PAYMENT and standing(world.db, invoice_id) == ('paid', 0)
    for number in (10000, 4405, 4340):
        assert balance(world.db, number) == 0, number                                 # Forderung und Zahlungen heben sich je Konto auf
    assert findings(world.db) == []


def test_the_steps_of_the_payment_always_add_up_to_the_receivables_per_group(world):
    """Je Gruppe ergeben die Teile den Betrag der Gruppe (119,00 und 80,00) und die Steuer die der Gruppe (19,00 und 0)."""
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    for amount, day in ((33.33, '2026-07-01'), (50.00, '2026-07-02'), (115.67, '2026-07-03')):
        pay_on(world, invoice_id, amount, day)

    rows = [row for row in marked(world.db, 'G-100') if row[5] == 'transfer']

    assert sum(row[2] for row in rows if row[0] == 4405) == 11900 and sum(row[3] for row in rows if row[0] == 4405) == 1900
    assert sum(row[2] for row in rows if row[0] == 4340) == 8000 and all(row[3] is None and row[4] is None for row in rows if row[0] == 4340)
    assert standing(world.db, invoice_id) == ('paid', 0)


def test_the_tax_free_group_is_read_as_0_percent_by_the_readers_and_has_no_booking_rate(world):
    """``booking_rate(0)`` ist NULL: Die Forderungszeile und die Zahlungssätze der steuerfreien Gruppe tragen keinen Satz – ``_receivable_sides_by_rate``, ``_mixed_booked`` und
    ``split_payment`` lesen NULL als 0 %. Nichts davon ist umgebaut; hier steht, dass es für die steuerfreie Gruppe so liest."""
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    conn = world.db._get_connection()
    try:
        debtor, waiting = world.db._receivable_sides_by_rate(conn, 'G-100')
        booked = world.db._mixed_booked(conn, invoice_id, 'G-100', {D(19), D(0)})
    finally:
        conn.close()

    assert booking_rate(D(0)) is None and booking_rate(D(19)) == 0.19
    assert debtor == world.db.get_coa_id_by_account_number(10000)
    assert waiting == {D(19): world.db.get_coa_id_by_account_number(4405), D(0): world.db.get_coa_id_by_account_number(4340)}
    assert booked == {D(19): (598000, 95500), D(0): (402000, 0)}                      # Minor Units: 59,80 mit Steuer 9,55 und 40,20 ohne Steuer


def test_cancelling_19_percent_and_tax_free_books_the_counter_receivable_per_group(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)

    storno = cancelled(world, invoice_id)

    text = f'Stornorechnung {storno.storno_number} Malermeister Pinsel'
    assert storno.counter_booked is True
    assert receivables(world.db, storno.storno_number) == [(10000, 4405, -11900, 0.19, -1900, 'transfer', STORNO_DAY, text),
                                                           (10000, 4340, -8000, None, None, 'transfer', STORNO_DAY, text)]
    assert [balance(world.db, number) for number in (10000, 4405, 4340)] == [0, 0, 0]
    stored = columns(world.db, storno.storno_id)
    assert (stored['MixedTaxRates'], stored['SumGross']) == (1, -1990000)
    assert query_rows(world.db, 'SELECT TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', storno.storno_id)[1] == {
        'TaxCategory': 'E', 'TaxRateBp': 0, 'TaxExemptReason': REASON, 'RevenueCoaId': account(world.db, FREE)}            # die Momentaufnahme geht unverändert mit


def test_the_refund_takes_back_each_group_on_the_account_its_payments_lie_on(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    storno = cancelled(world, invoice_id)

    assignment = refund_of(world, storno.storno_id, 100.0)

    assert assignment.ok and assignment.revenue_coa_id is None                        # kein einzelnes Erlöskonto: je Satz das der Zahlungen
    assert marked(world.db, storno.storno_number) == [(1800, 10000, -10000, None, None, 'liquid_first'), (4405, 4400, -5980, -955, 0.19, 'transfer'),
                                                      (4340, FREE, -4020, None, None, 'transfer')]
    assert world.db.get_storno_credit(invoice_id) == D('0.00') and findings(world.db) == []


def test_a_partial_refund_splits_like_a_payment_and_the_rest_closes_both_groups(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    storno = cancelled(world, invoice_id)

    assert refund_of(world, storno.storno_id, 40.0).ok
    assert refund_of(world, storno.storno_id, 60.0, '2026-07-11').ok

    rows = [row for row in marked(world.db, storno.storno_number) if row[5] == 'transfer']
    assert sum(row[2] for row in rows if row[0] == 4405) == -5980 and sum(row[3] for row in rows if row[0] == 4405) == -955    # genau das, was gezahlt wurde
    assert sum(row[2] for row in rows if row[0] == 4340) == -4020
    assert world.db.get_storno_credit(invoice_id) == D('0.00') and findings(world.db) == []


def test_the_text_of_a_refusal_names_a_tax_free_group_as_tax_free_services(world):
    """Eine steuerfreie Gruppe heißt in den Sätzen der Meldungen „steuerfreie Leistungen“, nicht „0 %“ und nicht „steuerfrei“ (``rate_sentence``; ``tax_groups.group_label`` für
    Listen und PDF bleibt „steuerfrei“)."""
    items = [plain(19, '100.00'), free('80.00', account(world.db, FREE)), free('-90.00', account(world.db, FREE))]
    invoice_id = entry_invoice(world, 'G-300', items)                                  # die steuerfreie Gruppe ergibt zusammen −10,00

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code) == (False, 409)
    assert change.message == rate_sentence(MIXED_GROUP_NOT_POSITIVE, D(0), True, amount='-10,00 €') and '(steuerfreie Leistungen: -10,00 €)' in change.message
    assert columns(world.db, invoice_id)['Status'] == 'draft' and receivables(world.db, 'G-300') == []


# ═════════════════════════════════════════════════════════════════════════════
# 19 % und 5,5 % (H): keine Forderung (kein Wartekonto für 5,5 %), der Hinweis, die Zahlungen je Satz
# ═════════════════════════════════════════════════════════════════════════════

def test_sending_19_and_5_5_percent_books_no_receivable_at_all_and_says_so(world):
    invoice_id = reduced_invoice(world)

    change = send(world, invoice_id)

    assert change.payment_hint == NO_WAITING_HINT.format(rate='5,5 %')
    assert change.payment_hint == 'Für 5,5 % gibt es kein Wartekonto – diese Rechnung bucht erst mit der Zahlung (keine Forderung).'
    assert receivables(world.db, 'H-100') == []                                       # auch für 19 % nicht: Ein Beleg bucht ganz oder gar nicht
    assert standing(world.db, invoice_id) == ('sent', 33000)


def test_the_hint_reaches_the_route_as_the_hint_of_the_answer(world):
    from tests.invoice_world import set_invoice_status
    invoice_id = reduced_invoice(world)
    # die Route erzeugt nach dem Versenden die Dokumente (E-Rechnung, PDF) – sie gehören zu einer anderen Etappe; hier genügt, was sie aus dem Statuswechsel macht
    code, body = set_invoice_status(invoice_id, 'sent')

    assert code == 200 and body['success'] is True and body['hint'] == NO_WAITING_HINT.format(rate='5,5 %'), body


def test_the_payments_book_one_row_per_group_on_the_account_of_the_snapshot_and_the_default_account(world):
    invoice_id = reduced_invoice(world)
    send(world, invoice_id)

    pay_on(world, invoice_id, 330.0)

    assert marked(world.db, 'H-100') == [(4400, 1800, 11900, 1900, 0.19, 'purpose_first'), (REDUCED, 1800, 21100, 1100, 0.055, 'purpose_first')]
    assert standing(world.db, invoice_id) == ('paid', 0) and findings(world.db) == []


def test_a_part_payment_splits_between_19_and_5_5_percent_and_the_closing_payment_takes_the_rest(world):
    invoice_id = reduced_invoice(world)
    send(world, invoice_id)

    pay_on(world, invoice_id, 100.0)
    first = marked(world.db, 'H-100')
    pay_on(world, invoice_id, 230.0, '2026-07-05')
    second = marked(world.db, 'H-100')[len(first):]

    assert first == [(4400, 1800, 3606, 576, 0.19, 'purpose_first'), (REDUCED, 1800, 6394, 333, 0.055, 'purpose_first')]       # 36,06 mit 5,76 und 63,94 mit 3,33
    assert second == [(4400, 1800, 8294, 1324, 0.19, 'purpose_first'), (REDUCED, 1800, 14706, 767, 0.055, 'purpose_first')]    # der Rest: 82,94 (13,24) und 147,06 (7,67)
    assert standing(world.db, invoice_id) == ('paid', 0)
    assert sum(row[3] for row in first + second) == 3000                              # die Steuer des Belegs: 19,00 + 11,00


def test_cancelling_19_and_5_5_percent_books_no_counter_receivable_and_the_refund_takes_back_each_rate(world):
    invoice_id = reduced_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)

    storno = cancelled(world, invoice_id)

    assert storno.counter_booked is False and receivables(world.db, storno.storno_number) == []
    assert refund_of(world, storno.storno_id, 100.0).ok
    assert marked(world.db, storno.storno_number) == [(4400, 1800, -3606, -576, 0.19, 'purpose_first'), (REDUCED, 1800, -6394, -333, 0.055, 'purpose_first')]
    assert world.db.get_storno_credit(invoice_id) == D('0.00') and findings(world.db) == []


def test_a_net_amount_that_two_step_rounding_would_get_wrong_is_booked_and_checked_without_a_mismatch(world):
    """0,09 € netto bei 5,5 %: zweistufig 0,01 € Steuer, einstufig (EN 16931) 0,00 €. Läse eine Stelle die Positionen ohne ``TaxRateBp``, passten sie nicht zu den
    gespeicherten Summen (``MIXED_GROUPS_MISMATCH``) – hier lässt sich der Beleg versenden, bezahlen, stornieren, und die Prüfliste schweigt."""
    items = [plain(19, '100.00'), entry('5.5', '0.09', account(world.db, REDUCED))]
    invoice_id = entry_invoice(world, 'H-200', items)
    assert [columns(world.db, invoice_id)[c] for c in ('SumNet', 'TaxAmount', 'SumGross')] == [1000900, 190000, 1190900]        # die Steuer: 19,00 und 0,00

    send(world, invoice_id)
    assert findings(world.db) == []
    pay_on(world, invoice_id, 119.09)

    assert marked(world.db, 'H-200') == [(4400, 1800, 11900, 1900, 0.19, 'purpose_first'), (REDUCED, 1800, 9, 0, 0.055, 'purpose_first')]
    assert standing(world.db, invoice_id) == ('paid', 0) and findings(world.db) == []
    storno = cancelled(world, invoice_id)

    assert [columns(world.db, storno.storno_id)[c] for c in ('SumNet', 'TaxAmount', 'SumGross')] == [-1000900, -190000, -1190900]      # exakt negiert


def test_the_same_groups_without_the_snapshot_are_rounded_in_two_steps_as_before(world):
    """Ohne Momentaufnahme gilt die Rundung von heute (zweistufig): Ein Bestand, der 5,5 % im Kopf trägt, ändert sich nicht."""
    invoice_id = entry_invoice(world, 'H-300', [plain('5.5', '0.09')])

    assert [columns(world.db, invoice_id)[c] for c in ('SumNet', 'TaxAmount', 'SumGross')] == [900, 100, 1000]               # 0,09 + 0,01


# ═════════════════════════════════════════════════════════════════════════════
# Das Wartekonto genau: 7,4 % mit Momentaufnahme hat keines, ohne wie heute das der 7 %
# ═════════════════════════════════════════════════════════════════════════════

def test_7_4_percent_with_a_snapshot_has_no_waiting_account_and_without_one_still_gets_the_one_of_7_percent(world):
    """``_tax_rate_key`` rundet auf ganze Prozent (7,4 → 7 → 4345); ein Beleg mit Momentaufnahme nimmt den genauen Satz (``_group_waiting_entry``), der Bestand bleibt, wie er war."""
    exact = entry_invoice(world, 'W-100', [entry('7.4', '100.00', account(world.db, REDUCED))])
    rounded = entry_invoice(world, 'W-200', [plain('7.4', '100.00')])

    exact_change, rounded_change = send(world, exact), send(world, rounded)

    assert exact_change.payment_hint == NO_WAITING_HINT.format(rate='7,4 %') and receivables(world.db, 'W-100') == []
    assert rounded_change.payment_hint is None and [row[:3] for row in receivables(world.db, 'W-200')] == [(10000, 4345, 10740)]        # der Bestand: das Wartekonto der 7 %


def test_the_waiting_account_of_a_single_rate_with_a_snapshot_is_the_one_of_exactly_that_rate(world):
    for pct, number in (('19', 4405), ('7', 4345)):
        invoice_id = entry_invoice(world, f'W-{pct}', [entry(pct, '100.00', None)])               # die Vorbelegung: ohne Erlöskonto, aber mit Momentaufnahme
        send(world, invoice_id)
        assert [row[:2] for row in receivables(world.db, f'W-{pct}')] == [(10000, number)]


def test_the_hint_names_every_rate_without_a_waiting_account(world):
    items = [plain(19, '100.00'), entry('5.5', '50.00', account(world.db, REDUCED)), entry('10', '40.00', account(world.db, 4160)),
             entry('12', '30.00', account(world.db, 4170))]
    invoice_id = entry_invoice(world, 'H-400', items)

    change = send(world, invoice_id)

    assert change.payment_hint == NO_WAITING_HINT.format(rate='12 %, 10 % und 5,5 %')            # nach Satz absteigend; 19 % hat eines und fehlt im Hinweis
    assert receivables(world.db, 'H-400') == []


def test_an_old_invoice_with_a_rate_without_a_waiting_account_gets_no_hint_as_before(world):
    """Ohne Momentaufnahme bleibt alles still wie heute: 5 % im Kopf ergibt keine Forderung und keinen Hinweis."""
    invoice_id = entry_invoice(world, 'W-300', [plain(5, '100.00')])

    change = send(world, invoice_id)

    assert change.payment_hint is None and receivables(world.db, 'W-300') == []


def test_reviving_a_cancelled_invoice_books_the_receivable_by_the_exact_rate_and_gives_the_hint(world):
    reduced = entry_invoice(world, 'W-400', [entry('5.5', '100.00', account(world.db, REDUCED))])
    free_one = free_invoice(world)
    for invoice_id in (reduced, free_one):
        run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)          # von Hand storniert (ohne Stornorechnung)

    revived = world.db.set_invoice_status(reduced, 'sent', USER)
    revived_free = world.db.set_invoice_status(free_one, 'overdue', USER)

    assert (revived.ok, revived.kind, revived.payment_hint) == (True, 'revive', NO_WAITING_HINT.format(rate='5,5 %')) and receivables(world.db, 'W-400') == []
    assert (revived_free.ok, revived_free.kind, revived_free.payment_hint) == (True, 'revive', None)
    assert [row[:3] for row in receivables(world.db, 'F-100')] == [(10000, 4340, 10000)]


def test_ensure_receivable_booking_uses_the_exact_waiting_account_for_a_snapshot_too(world):
    free_one, reduced = free_invoice(world), entry_invoice(world, 'W-500', [entry('5.5', '100.00', account(world.db, REDUCED))])

    first = world.db.ensure_receivable_booking(free_one)

    assert first is not None and world.db.ensure_receivable_booking(free_one) == first                      # idempotent
    assert [row[:3] for row in receivables(world.db, 'F-100')] == [(10000, 4340, 10000)]
    assert world.db.ensure_receivable_booking(reduced) is None and receivables(world.db, 'W-500') == []     # kein Wartekonto: keine Forderung


# ═════════════════════════════════════════════════════════════════════════════
# Versenden prüft das Erlöskonto der Stammdaten: 409 mit festem Klartext, vor dem ersten Schreiben
# ═════════════════════════════════════════════════════════════════════════════

def unsendable(world, make):
    """Ein Entwurf, dessen Erlöskonto der Stammdaten es nicht (mehr) gibt – ``make(coa)`` baut ihn mit dem Konto –, samt dem Stand der Tabellen vorher."""
    coa = account(world.db, FREE)
    invoice_id = make(coa)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)                  # nichts bucht darauf: Das Konto ist weg
    drop_accounts(world.db, 10000, 4340, 4405)                                           # und kein Weg darf eines von ihnen anlegen
    return invoice_id, tables(world.db)


def refused_with(world, invoice_id, before, message):
    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, message)
    assert tables(world.db) == before                                                    # nichts geschrieben: keine Forderung, kein Status, kein Konto, kein Protokoll
    assert columns(world.db, invoice_id)['Status'] == 'draft'


def test_a_tax_free_invoice_whose_revenue_account_is_gone_is_refused_with_the_fixed_sentence(world):
    invoice_id, before = unsendable(world, lambda coa: entry_invoice(world, 'F-100', [free('100.00', coa)]))

    refused_with(world, invoice_id, before, 'Für steuerfreie Leistungen hat die Rechnung kein gültiges Erlöskonto – bitte im Steuersatz der Stammdaten eines eintragen und die Rechnung danach '
                                            'neu speichern.')
    assert TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen') == world.db.set_invoice_status(invoice_id, 'sent', USER).message


def test_a_mixed_invoice_names_the_rate_whose_account_is_missing(world):
    invoice_id, before = unsendable(world, lambda coa: entry_invoice(world, 'H-100', [plain(19, '100.00'), entry('5.5', '200.00', coa)]))

    refused_with(world, invoice_id, before, TAX_SEND_NO_REVENUE.format(rate='5,5 %'))


def test_an_account_that_is_no_revenue_account_any_more_is_refused_too(world):
    """Das Konto gibt es, ist aber kein Erlöskonto (mehr): Es steht außerhalb von 4000–4999 (``_income_coas``)."""
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', coa)])
    run_sql(world.db, 'UPDATE ChartOfAccounts SET AccountNumber = 6120 WHERE ID = ?', coa)
    before = tables(world.db)

    refused_with(world, invoice_id, before, TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen'))


def test_a_waiting_account_is_no_revenue_account_either(world):
    """4340 ist ein Wartekonto, kein Erlöskonto (``is_income_account``): Ein Eintrag, der es als Erlöskonto trüge, ließe sich nicht versenden."""
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', world.db.get_coa_id_by_account_number(4340))])
    before = tables(world.db)

    refused_with(world, invoice_id, before, TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen'))


def test_the_check_runs_for_reviving_a_cancelled_invoice_too(world):
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', coa)])
    run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen')) and tables(world.db) == before


def test_the_route_answers_409_with_the_sentence_and_changes_nothing(world):
    from tests.invoice_world import set_invoice_status
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', coa)])
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)
    before = tables(world.db)

    code, body = set_invoice_status(invoice_id, 'sent')

    assert code == 409 and body == {'success': False, 'error': TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen')} and tables(world.db) == before


def test_a_snapshot_without_an_account_needs_a_default_account_for_its_rate(world):
    """Die 19 % der Vorbelegung (Momentaufnahme ohne Erlöskonto, per Schnittstelle mit einer ID gespeichert) brauchen heute ein Erlöskonto des Satzes (``resolve_revenue_coa``)."""
    invoice_id = entry_invoice(world, 'P-100', [entry('19', '100.00', None)])
    assert send(world, entry_invoice(world, 'P-101', [entry('19', '100.00', None)])).ok            # 4400 gibt es: in Ordnung
    drop_accounts(world.db, 4400)
    before = tables(world.db)

    refused_with(world, invoice_id, before, TAX_SEND_NO_REVENUE.format(rate='19 %'))


def test_a_snapshot_for_0_percent_without_an_account_is_refused_and_never_gets_4184_or_4185(world):
    invoice_id = entry_invoice(world, 'P-200', [Part(D(0), '100.00', {'tax_category': 'S', 'tax_rate_bp': 0, 'tax_exempt_reason': None, 'revenue_coa_id': None})])
    before = tables(world.db)

    refused_with(world, invoice_id, before, TAX_SEND_NO_REVENUE.format(rate='0 %'))


def test_an_invoice_without_a_snapshot_is_not_checked_as_before(world):
    """Ohne Momentaufnahme bucht die Rechnung ohne Erlöskonto und fragt erst bei der Zahlung – das bleibt."""
    invoice_id = entry_invoice(world, 'T-200', [plain(19, '100.00')])
    drop_accounts(world.db, 4400, 4300)

    assert world.db._entry_revenue_blocker(invoice_id, 'sent') is None
    assert send(world, invoice_id).ok and [row[:2] for row in receivables(world.db, 'T-200')] == [(10000, 4405)]


def test_the_blocker_is_found_without_creating_an_account(world):
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', coa)])
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)
    drop_accounts(world.db, 10000, 4340)
    chart = query_rows(world.db, 'SELECT * FROM ChartOfAccounts ORDER BY ID')

    assert world.db._entry_revenue_blocker(invoice_id, 'sent') == TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen')
    assert world.db._entry_revenue_blocker(invoice_id, 'finalized') is None and world.db._entry_revenue_blocker(invoice_id, 'draft') is None   # nur das Versenden fragt
    assert world.db._entry_revenue_blocker(invoice_id, 'overdue') is None                    # ein Entwurf wird nicht überfällig: kein Versenden, nichts zu prüfen
    run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)
    assert world.db._entry_revenue_blocker(invoice_id, 'overdue') == TAX_SEND_NO_REVENUE.format(rate='steuerfreie Leistungen')       # das Wiederbeleben ist ein Versenden
    assert query_rows(world.db, 'SELECT * FROM ChartOfAccounts ORDER BY ID') == chart


def full_dump(db):
    """Der Stand ALLER Tabellen der Datenbank, auch ``sqlite_sequence`` (die Zähler), die Nummernkreise und der Kontenrahmen – ``tables`` kennt nur die der Rechnungen."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(db.db_name)) as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: con.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for name in names}


def test_a_refused_send_of_a_replacement_invoice_creates_no_account_not_even_4184_or_4185(world, monkeypatch):
    """„Standard 0 %“ mit Konto versenden, stornieren, neu ausstellen, an beiden Belegen das Erlöskonto streichen und 4185 löschen: Das Versenden der Ersatzrechnung lehnt mit
    ``TAX_SEND_NO_REVENUE`` ab. Früher hatte der Umzugskontext (``_payment_move_context``: ``resolve_revenue_coa``) vorher 4185 angelegt – der Kontenrahmen hatte danach eine Zeile
    mehr. Jetzt läuft erst der Blocker, und der Umzugskontext nur ohne Ablehnung: Alle Tabellen sind danach, wie sie vorher waren."""
    original = entry_invoice(world, 'Z-100', [entry(0, '100.00', account(world.db, 4170))])
    send(world, original)
    cancelled(world, original)
    replacement = reissue(world.db, original, monkeypatch)
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = NULL WHERE InvoiceId IN (?, ?)', original, replacement)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    before = full_dump(world.db)
    assert world.db.get_coa_id_by_account_number(4184) is None and world.db.get_coa_id_by_account_number(4185) is None

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, TAX_SEND_NO_REVENUE.format(rate='0 %'))
    assert full_dump(world.db) == before
    assert world.db.get_coa_id_by_account_number(4184) is None and world.db.get_coa_id_by_account_number(4185) is None


def test_a_send_refused_for_a_contradiction_creates_no_account_either(world, monkeypatch):
    """Dieselbe Lage, aber das Versenden scheitert nicht am Erlöskonto, sondern an einem Widerspruch der Momentaufnahme (eine Position trägt einen Grund, die andere nicht):
    Der Umzugskontext löst dann kein Erlöskonto auf – auch hier entsteht weder 4184 noch 4185, alle Tabellen bleiben, wie sie waren."""
    coa = account(world.db, 4170)
    original = entry_invoice(world, 'Z-150', [entry(0, '60.00', coa), entry(0, '40.00', coa)])
    send(world, original)
    cancelled(world, original)
    replacement = reissue(world.db, original, monkeypatch)
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = NULL WHERE InvoiceId IN (?, ?)', original, replacement)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxExemptReason = 'Erfundener Grund' WHERE InvoiceId = ? AND Position = 2", replacement)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    before = full_dump(world.db)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert full_dump(world.db) == before
    assert world.db.get_coa_id_by_account_number(4184) is None and world.db.get_coa_id_by_account_number(4185) is None


def test_the_move_context_is_not_built_when_the_blocker_refuses_but_it_is_when_the_send_goes_through(world, monkeypatch):
    """Die Reihenfolge, auf die es ankommt: erst ``_entry_revenue_blocker``, dann – nur ohne Ablehnung – ``_payment_move_context`` (er löst das Erlöskonto auf und legt 4184/4185 an)."""
    original = entry_invoice(world, 'Z-200', [entry(0, '100.00', account(world.db, 4170))])
    send(world, original)
    cancelled(world, original)
    replacement = reissue(world.db, original, monkeypatch)
    calls = []
    real = type(world.db)._payment_move_context
    monkeypatch.setattr(type(world.db), '_payment_move_context', lambda self, invoice_id, new: calls.append(invoice_id) or real(self, invoice_id, new))
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = NULL WHERE InvoiceId = ?', replacement)

    refused = world.db.set_invoice_status(replacement, 'sent', USER)

    assert refused.ok is False and calls == []
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ?', account(world.db, 4170), replacement)

    assert world.db.set_invoice_status(replacement, 'sent', USER).ok is True and calls == [replacement]


@pytest.mark.parametrize('what', ['revive', 'plain_draft'])
def test_a_refused_send_of_any_other_kind_leaves_every_table_as_it_was_too(world, what):
    """Auch ohne Ersatzrechnung: Das abgelehnte Versenden (und Wiederbeleben) eines Belegs ohne Erlöskonto der Stammdaten schreibt nichts – kein Konto, keinen Zähler."""
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-800', [free('100.00', coa)])
    if what == 'revive':
        run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)
    drop_accounts(world.db, 10000, 4340, 4184, 4185)
    before = full_dump(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code) == (False, 409) and full_dump(world.db) == before


def test_the_key_of_the_preparation_carries_the_snapshots_so_a_changed_snapshot_is_caught_in_the_transaction(world, monkeypatch):
    """Ein gleichzeitig neu gespeicherter Entwurf: Die Gruppen sind dieselben, die Momentaufnahme nicht – „Die Rechnung wurde inzwischen geändert“, nichts geschrieben."""
    other = account(world.db, 4130)
    invoice_id = mixed_free_invoice(world)
    real = world.db._receivable_accounts_for

    def prepared_then_resaved(invoice, new):
        found = real(invoice, new)
        run_sql(world.db, "UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ? AND TaxCategory = 'E'", other, invoice)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts_for', prepared_then_resaved)
    run_sql(world.db, "UPDATE Invoices SET Status = Status WHERE ID = ?", invoice_id)
    before = {name: rows for name, rows in tables(world.db).items() if name != 'InvoiceItems'}

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE)
    assert {name: rows for name, rows in tables(world.db).items() if name != 'InvoiceItems'} == before and receivables(world.db, 'G-100') == []


def test_the_key_of_a_single_rate_carries_its_snapshot_too(world, monkeypatch):
    invoice_id = free_invoice(world)
    real = world.db._receivable_accounts_for

    def prepared_then_resaved(invoice, new):
        found = real(invoice, new)
        run_sql(world.db, "UPDATE InvoiceItems SET TaxExemptReason = 'ein anderer Grund' WHERE InvoiceId = ?", invoice)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts_for', prepared_then_resaved)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE) and receivables(world.db, 'F-100') == []
    assert columns(world.db, invoice_id)['Status'] == 'draft'


def test_ensure_receivable_booking_refuses_when_the_snapshot_changes_between_the_preparation_and_the_transaction(world, monkeypatch):
    """Dieselbe Vorprüfung für den Weg der Demodaten und Tests (``ensure_receivable_booking``): Das Wartekonto der Forderung hängt an der Momentaufnahme – ändert sie
    sich nach dem Auflösen der Konten, lehnt ``CHANGED_MESSAGE`` ab, bevor etwas geschrieben ist."""
    invoice_id = free_invoice(world)
    real = world.db._entry_receivable_accounts

    def prepared_then_resaved(pct):
        found = real(pct)
        run_sql(world.db, "UPDATE InvoiceItems SET TaxExemptReason = 'ein anderer Grund' WHERE InvoiceId = ?", invoice_id)
        return found

    monkeypatch.setattr(world.db, '_entry_receivable_accounts', prepared_then_resaved)

    with pytest.raises(ValueError) as refused:
        world.db.ensure_receivable_booking(invoice_id)

    assert str(refused.value) == CHANGED_MESSAGE and receivables(world.db, 'F-100') == []


def test_the_key_of_an_invoice_without_a_snapshot_is_the_rate_key_as_before(world):
    invoice_id = entry_invoice(world, 'T-300', [plain(19, '100.00')])

    accounts, key = world.db._receivable_accounts_for(invoice_id, 'sent')

    assert key == 19 and accounts == (world.db.get_coa_id_by_account_number(4405), world.db.get_coa_id_by_account_number(10000))


def test_an_invoice_with_contradictory_snapshots_is_refused_with_the_fixed_sentence_before_anything_is_written(world):
    """Zwei Positionen EINES Satzes mit verschiedenem Erlöskonto (nur per SQL herstellbar): Der Beleg lässt sich nicht buchen."""
    invoice_id = entry_invoice(world, 'H-500', [entry('5.5', '100.00', account(world.db, REDUCED)), entry('5.5', '50.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ? AND Position = 2', account(world.db, 4130), invoice_id)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION) and tables(world.db) == before


def test_a_mixed_invoice_with_contradictory_snapshots_is_refused_by_the_building_block_for_seeds_too(world):
    """Auch der Baustein für Demodaten und Tests (``ensure_receivable_booking``) bucht keinen Beleg, dessen Momentaufnahmen sich widersprechen – vor dem ersten Schreiben."""
    invoice_id = entry_invoice(world, 'G-500', [plain(19, '100.00'), free('40.00', account(world.db, FREE)), free('40.00', account(world.db, FREE))])
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ? AND Position = 3', account(world.db, 4130), invoice_id)
    before = tables(world.db)

    with pytest.raises(ValueError) as refused:
        world.db.ensure_receivable_booking(invoice_id)

    assert str(refused.value) == TAX_ENTRY_CONTRADICTION and tables(world.db) == before


CONTRADICTIONS = [
    "UPDATE InvoiceItems SET RevenueCoaId = NULL WHERE InvoiceId = ? AND Position = 2",                        # eine Position mit, eine ohne Erlöskonto
    "UPDATE InvoiceItems SET TaxRateBp = NULL WHERE InvoiceId = ? AND Position = 2",                           # eine Position mit, eine ohne Momentaufnahme (und mit Konto: die halbe)
    "UPDATE InvoiceItems SET TaxExemptReason = 'abweichend' WHERE InvoiceId = ? AND Position = 2",
    "UPDATE InvoiceItems SET TaxCategory = 'E' WHERE InvoiceId = ? AND Position = 2",                          # steuerfrei, aber mit Satz 5,5
    "UPDATE InvoiceItems SET TaxRateBp = 12345 WHERE InvoiceId = ? AND Position = 2",                          # kein Satz
    "UPDATE InvoiceItems SET TaxRateBp = 'x' WHERE InvoiceId = ? AND Position = 2",
    "UPDATE InvoiceItems SET TaxRateBp = -550 WHERE InvoiceId = ? AND Position = 2",                           # kein Satz: negativ
    "UPDATE InvoiceItems SET TaxRateBp = 550.5 WHERE InvoiceId = ? AND Position = 2",                          # kein Satz: keine ganze Zahl
    "UPDATE InvoiceItems SET TaxRateBp = 50, TaxRate = 0.005 WHERE InvoiceId = ? AND Position = 2",            # 0,5 %: ein Satz unter 1 % – passt zum Satz der Position und ist doch keiner
    "UPDATE InvoiceItems SET TaxRateBp = 1, TaxRate = 0.0001 WHERE InvoiceId = ? AND Position = 2",            # 0,01 %
    "UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2",                                # der Satz der Momentaufnahme (5,5 %) ist nicht der der Position
    "UPDATE InvoiceItems SET TaxRate = 'x' WHERE InvoiceId = ? AND Position = 2",                              # der Satz der Position ist keine Zahl
    "UPDATE InvoiceItems SET TaxRateBp = NULL, RevenueCoaId = NULL, TaxExemptReason = 'Grund' WHERE InvoiceId = ? AND Position = 2",     # die halbe Momentaufnahme: nur ein Grund
    "UPDATE InvoiceItems SET TaxRateBp = NULL, TaxExemptReason = 'Grund', RevenueCoaId = NULL, TaxRate = 7 WHERE InvoiceId = ? AND Position = 2",
]


@pytest.mark.parametrize('tamper', CONTRADICTIONS)
def test_every_contradiction_of_the_snapshots_makes_the_invoice_unbookable(world, tamper):
    """Jeder Widerspruch der Momentaufnahme hat EINEN Satz (``TAX_ENTRY_CONTRADICTION``) – der Baustein und das Versenden nennen ihn gleich, vor dem ersten Schreiben."""
    invoice_id = entry_invoice(world, 'H-500', [entry('5.5', '100.00', account(world.db, REDUCED)), entry('5.5', '50.00', account(world.db, REDUCED))])
    run_sql(world.db, tamper, invoice_id)
    before = tables(world.db)
    conn = world.db._get_connection()

    try:
        with pytest.raises(MixedNotBookable) as refused:
            world.db.group_snapshots(conn, invoice_id)
    finally:
        conn.close()

    assert str(refused.value) == TAX_ENTRY_CONTRADICTION
    change = world.db.set_invoice_status(invoice_id, 'sent', USER)
    assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert tables(world.db) == before and columns(world.db, invoice_id)['Status'] == 'draft'
    assert findings(world.db, invoice_id, 'tax_entry_mismatch') != []                                  # die Prüfliste nennt es auch


def test_the_fixed_sentence_of_a_contradiction_is_not_the_one_of_the_sums():
    assert TAX_ENTRY_CONTRADICTION == ('Die Angaben aus den Stammdaten an den Positionen dieser Rechnung widersprechen sich – sie lässt sich so nicht buchen (Prüfung unter Daten).')
    assert TAX_ENTRY_CONTRADICTION != MIXED_GROUPS_MISMATCH and 'Summen' not in TAX_ENTRY_CONTRADICTION


@pytest.mark.parametrize('way', ['send', 'pay', 'send_replacement'])
def test_two_positions_of_one_rate_whose_rate_was_changed_cannot_be_sent_paid_or_sent_again_as_a_replacement(world, monkeypatch, way):
    """Zwei Positionen à 5,5 % mit Momentaufnahme, dann bekommt eine den Steuersatz 7 (``UPDATE InvoiceItems SET TaxRate = 7 WHERE Position = 2``): Das Versenden, die Zahlung
    und das Versenden der Ersatzrechnung gelangen früher alle – jetzt lehnt jeder Weg mit dem Satz des Widerspruchs ab, vor dem ersten Schreiben (nichts geändert)."""
    coa = account(world.db, REDUCED)
    tamper = 'UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2'
    invoice_id = entry_invoice(world, 'H-600', [entry('5.5', '100.00', coa), entry('5.5', '50.00', coa)])
    if way == 'send':
        run_sql(world.db, tamper, invoice_id)
        before = tables(world.db)

        change = world.db.set_invoice_status(invoice_id, 'sent', USER)

        assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    elif way == 'pay':
        send(world, invoice_id)
        booking = incoming(world, 100.0)
        run_sql(world.db, tamper, invoice_id)
        before = tables(world.db)

        assignment = world.db.assign_invoice_payment(invoice_id, booking)

        assert assignment == PaymentAssignment(False, TAX_ENTRY_CONTRADICTION, None, None)
        assert world.db.payment_precheck(invoice_id, world.bank) == TAX_ENTRY_CONTRADICTION
    else:
        send(world, invoice_id)
        pay_on(world, invoice_id, 100.0)
        cancelled(world, invoice_id)
        replacement = reissue(world.db, invoice_id, monkeypatch)
        run_sql(world.db, tamper, replacement)
        before = tables(world.db)

        change = world.db.set_invoice_status(replacement, 'sent', USER)

        assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert tables(world.db) == before


@pytest.mark.parametrize('value', [1, 50, 99, -1, -550, 10001, 20000, 'x', 550.5])
def test_a_snapshot_rate_that_is_no_rate_is_a_contradiction_not_no_snapshot(world, value):
    """Im Dokument gilt so ein Wert als „keine Momentaufnahme“ (``export.einvoice_model.item_bp``); beim Buchen ist er ein Widerspruch – ein Beleg wird nie mit einem Satz
    gebucht, den die Positionen nicht tragen, und nie stillschweigend mit dem Satz aus ``TaxRate``."""
    invoice_id = entry_invoice(world, 'H-700', [entry('5.5', '100.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRateBp = ? WHERE InvoiceId = ?', value, invoice_id)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.message) == (False, TAX_ENTRY_CONTRADICTION) and tables(world.db) == before


def test_the_smallest_snapshot_rate_of_1_percent_books_like_any_other(world):
    """Die Grenze der Regel: 100 Hundertstel Prozent (1 %) ist ein Satz – er lässt sich versenden und bezahlen (kein Wartekonto, kein Erlöskonto geraten: das der Momentaufnahme)."""
    invoice_id = entry_invoice(world, 'U-100', [entry('1', '100.00', account(world.db, REDUCED))])

    change = send(world, invoice_id)
    pay_on(world, invoice_id, 101.0)

    assert change.payment_hint == NO_WAITING_HINT.format(rate='1 %')
    assert marked(world.db, 'U-100') == [(REDUCED, 1800, 10100, 100, 0.01, 'purpose_first')]
    assert standing(world.db, invoice_id) == ('paid', 0) and findings(world.db) == []


@pytest.mark.parametrize('value', [None, 0, 1, 50, 99, 100, 550, 1900, 10000, 10001, -1, -100, 20000, '1900', '', 'x', 550.0, 550.5, True, False, 1e30, b'550'])
def test_the_rule_for_a_snapshot_rate_of_the_bookings_equals_the_reading_of_the_document(value):
    """Dieselbe Regel steht in ``db.invoice_payments.valid_rate_bp`` und in ``export.einvoice_model.item_bp`` – ein Import in die andere Richtung gäbe einen Zyklus (``export``
    importiert ``db``). Hier werden beide gegeneinander gehalten: Was ``item_bp`` als Momentaufnahme liest, gilt auch beim Buchen, und umgekehrt."""
    from export.einvoice_model import _ITM, item_bp
    row = [None] * (max(_ITM.values()) + 1)
    row[_ITM['tax_rate_bp']] = value

    assert (item_bp(tuple(row)) is not None) is valid_rate_bp(value)
    assert item_bp(tuple(row)) == (value if valid_rate_bp(value) else None)


def test_valid_rate_bp_takes_0_and_1_to_100_percent_only():
    assert [valid_rate_bp(bp) for bp in (0, 1, 99, 100, 10000, 10001)] == [True, False, False, True, True, False]
    assert valid_rate_bp(True) is False and valid_rate_bp(False) is False and valid_rate_bp(None) is False and valid_rate_bp(100.0) is False


def test_the_sums_and_the_snapshots_have_their_own_sentences_on_a_flagged_invoice(world):
    """Ein Beleg mit Steuersatz je Position: Passt der Satz der Momentaufnahme nicht zu dem der Position, ist es ein Widerspruch der Momentaufnahme (``mixed_groups`` hätte
    es als „Positionen passen nicht zu den Summen“ gemeldet); passen die Positionen nicht zu den Summen, bleibt ``MIXED_GROUPS_MISMATCH`` – dann ist die Momentaufnahme in Ordnung."""
    changed = reduced_invoice(world, 'H-800')
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2', changed)
    sums = reduced_invoice(world, 'H-801')
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', sums)
    before = tables(world.db)

    contradiction = world.db.set_invoice_status(changed, 'sent', USER)
    mismatch = world.db.set_invoice_status(sums, 'sent', USER)

    assert (contradiction.ok, contradiction.code, contradiction.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert (mismatch.ok, mismatch.code, mismatch.message) == (False, 409, MIXED_GROUPS_MISMATCH)
    assert tables(world.db) == before


def test_a_flagged_invoice_without_a_snapshot_keeps_the_sentence_about_the_sums(world):
    """Ohne Momentaufnahme ändert sich nichts: Positionen, die nicht zu den Summen passen, heißen wie immer ``MIXED_GROUPS_MISMATCH``."""
    invoice_id = entry_invoice(world, 'T-500', [plain(19, '100.00'), plain(7, '50.00')])
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, MIXED_GROUPS_MISMATCH) and tables(world.db) == before


def test_group_snapshots_reads_nothing_into_an_invoice_without_a_snapshot_and_keys_the_groups_by_rate(world):
    plain_id, mixed_id = entry_invoice(world, 'T-400', [plain(19, '100.00'), plain(7, '50.00')]), mixed_free_invoice(world)
    conn = world.db._get_connection()
    try:
        none = world.db.group_snapshots(conn, plain_id)
        found = world.db.group_snapshots(conn, mixed_id)
        # die Art „steuerfrei“ allein ist keine Momentaufnahme (das Beispiel eines Belegs nach § 19 trägt sie ohne Satz in Hundertstel Prozent)
        run_sql(world.db, "UPDATE InvoiceItems SET TaxCategory = 'E' WHERE InvoiceId = ?", plain_id)
        still_none = world.db.group_snapshots(conn, plain_id)
    finally:
        conn.close()

    assert none == {} and still_none == {}
    assert found == {D(0): Snapshot(True, REASON, account(world.db, FREE))}                  # nur die Gruppe mit Momentaufnahme; 19 % fehlt: Für sie gelten die bisherigen Regeln


# ═════════════════════════════════════════════════════════════════════════════
# Die Zahlung auf einen Beleg, dessen Erlöskonto der Stammdaten keines (mehr) ist: fester Satz, keine Wahl
# ═════════════════════════════════════════════════════════════════════════════

def spoil(world, coa):
    """Das Konto der Momentaufnahme ist nach dem Versenden kein Erlöskonto mehr (die Kontenliste ließ sich ändern, solange nichts darauf verwies)."""
    run_sql(world.db, 'UPDATE ChartOfAccounts SET AccountNumber = 6120 WHERE ID = ?', coa)


def test_a_payment_on_an_invoice_whose_snapshot_account_is_no_revenue_account_is_refused_and_no_choice_is_offered(world):
    coa = account(world.db, FREE)
    invoice_id = entry_invoice(world, 'F-100', [free('100.00', coa)])
    send(world, invoice_id)
    spoil(world, coa)
    booking = incoming(world, 100.0)
    other = account(world.db, 4130)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking, revenue_coa_id=other)         # auch mit einer Wahl: Das Konto der Momentaufnahme gilt, immer

    assert assignment == PaymentAssignment(False, TAX_PAYMENT_NO_REVENUE.format(rate='steuerfreie Leistungen'), None, None)
    assert 'Erlöskonto, das diese Rechnung für steuerfreie Leistungen trägt, ist kein Erlöskonto (mehr)' in assignment.message
    assert world.db.payment_precheck(invoice_id, world.bank) == TAX_PAYMENT_NO_REVENUE.format(rate='steuerfreie Leistungen')
    assert tables(world.db) == before
    assert [f['category'] for f in findings(world.db, invoice_id)] == ['tax_entry_mismatch']          # und die Prüfliste nennt es


def test_a_payment_on_a_mixed_invoice_names_the_group_whose_snapshot_account_is_gone(world):
    coa = account(world.db, REDUCED)
    invoice_id = entry_invoice(world, 'H-100', [plain(19, '100.00'), entry('5.5', '200.00', coa)])
    send(world, invoice_id)
    run_sql(world.db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa)
    booking = incoming(world, 330.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, TAX_PAYMENT_NO_REVENUE.format(rate='5,5 %'), None, None) and tables(world.db) == before


def test_a_snapshot_without_an_account_for_0_percent_gets_no_default_for_the_payment_either(world):
    """Nie 4184/4185 raten: Ein Satz 0 ohne eigenes Erlöskonto in der Momentaufnahme lehnt die Zahlung ab, auch ohne dass das Versenden es zugesichert hätte (der ungeprüfte Baustein)."""
    invoice_id = entry_invoice(world, 'P-200', [Part(D(0), '100.00', {'tax_category': 'S', 'tax_rate_bp': 0, 'tax_exempt_reason': None, 'revenue_coa_id': None})])
    world.db.update_invoice_status(invoice_id, 'sent')

    assignment = world.db.assign_invoice_payment(invoice_id, incoming(world, 100.0))

    assert assignment == PaymentAssignment(False, TAX_PAYMENT_NO_REVENUE.format(rate='0 %'), None, None)


def test_the_revenue_account_of_a_document_follows_the_snapshot_then_todays_rules(world):
    free_id = free_invoice(world)
    reduced_id = entry_invoice(world, 'R-1', [entry('5.5', '100.00', account(world.db, REDUCED))])
    vorbelegung = entry_invoice(world, 'R-2', [entry('19', '100.00', None)])
    plain_19 = entry_invoice(world, 'T-100', [plain(19, '100.00')])
    section_19 = entry_invoice(world, 'K-100', [plain(0, '100.00')], tax_rate=-1)
    no_account = entry_invoice(world, 'R-3', [Part(D(0), '100.00', {'tax_category': 'S', 'tax_rate_bp': 0, 'tax_exempt_reason': None, 'revenue_coa_id': None})])

    def of(invoice_id, on=None):
        return world.db.document_revenue_coa(invoice_id, columns(world.db, invoice_id)['TaxRate'], on=on)

    assert of(free_id) == account(world.db, FREE) and of(reduced_id) == account(world.db, REDUCED)       # die Momentaufnahme gilt, immer
    assert of(vorbelegung) == world.db.get_coa_id_by_account_number(4400) and of(plain_19) == world.db.get_coa_id_by_account_number(4400)   # wie heute
    assert of(section_19, '2025-12-31') == world.db.get_coa_id_by_account_number(4185)                      # § 19: nach dem Datum der Bankbewegung – bis 2025 4185 …
    assert of(section_19, '2026-01-01') == world.db.get_coa_id_by_account_number(4184)                      # … ab 2026 4184
    assert of(no_account) is None                                                                           # Satz 0 mit Momentaufnahme ohne Konto: nie 4184/4185
    assert world.db._document_revenue(reduced_id, 0.055, account(world.db, 4130)) == (account(world.db, REDUCED), None)   # eine Wahl ändert daran nichts


def test_the_revenue_account_of_a_document_with_contradictory_snapshots_is_an_error_not_a_guess(world):
    invoice_id = entry_invoice(world, 'H-500', [entry('5.5', '100.00', account(world.db, REDUCED)), entry('5.5', '50.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ? AND Position = 2', account(world.db, 4130), invoice_id)

    with pytest.raises(MixedNotBookable):
        world.db.document_revenue_coa(invoice_id, 0.055)
    assert world.db._document_revenue(invoice_id, 0.055) == (None, TAX_ENTRY_CONTRADICTION)


def test_a_single_rate_whose_head_does_not_carry_the_rate_of_the_snapshot_is_unbookable(world):
    invoice_id = entry_invoice(world, 'R-1', [entry('5.5', '100.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE Invoices SET TaxRate = 0.19 WHERE ID = ?', invoice_id)                    # der Kopf sagt 19 %, die Momentaufnahme 5,5 %

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION) and receivables(world.db, 'R-1') == []


# ═════════════════════════════════════════════════════════════════════════════
# Die Erstattung ohne markierte Sätze: das Konto der Momentaufnahme, sonst wie heute
# ═════════════════════════════════════════════════════════════════════════════

def test_the_refund_of_an_imported_payment_books_on_the_account_of_the_snapshot_and_offers_no_choice(world):
    coa = account(world.db, REDUCED)
    invoice_id = entry_invoice(world, 'R-1', [entry('5.5', '100.00', coa)])
    send(world, invoice_id)
    add_payment(world, invoice_id, 105.5)                                                              # wie der Import zuordnet: ohne Buchungssätze
    storno = cancelled(world, invoice_id)
    assert world.db.get_storno_credit(invoice_id) == D('105.50') and 'name="revenue_coa_id"' not in PageInvoice(world.db, {}, storno.storno_id)

    assignment = refund_of(world, storno.storno_id, 105.5)

    assert assignment.ok and assignment.revenue_coa_id == coa
    assert marked(world.db, storno.storno_number) == [(REDUCED, 1800, -10550, -550, 0.055, 'purpose_first')]
    assert world.db.get_storno_credit(invoice_id) == D('0.00')


def test_the_same_refund_without_a_snapshot_still_asks_for_the_account(world):
    invoice_id = entry_invoice(world, 'R-2', [plain('5.5', '100.00')])
    send(world, invoice_id)
    add_payment(world, invoice_id, 105.5)
    storno = cancelled(world, invoice_id)
    booking = outgoing(world, -105.5, '2026-07-10')

    assert world.db.assign_storno_refund(storno.storno_id, booking) == PaymentAssignment(False, NO_REVENUE_COA, None, None)
    assert 'name="revenue_coa_id"' in PageInvoice(world.db, {}, storno.storno_id)                       # die Wahl bleibt, wo es kein Konto der Stammdaten gibt
    assert world.db.assign_storno_refund(storno.storno_id, booking, revenue_coa_id=account(world.db, REDUCED)).ok


def test_the_one_exception_a_refund_of_imported_payments_needs_the_account_of_the_snapshot_and_a_contradiction_refuses_it(world):
    """Die Erstattung folgt den Zahlungssätzen und prüft die Momentaufnahme nicht – außer dort, wo es keine Zahlungssätze gibt (Import ohne markierte Sätze): Das Konto kommt dann aus der
    Momentaufnahme (``_document_revenue``), und die darf sich dann nicht widersprechen. Abgelehnt wird mit ``TAX_ENTRY_CONTRADICTION``, nichts wird geschrieben."""
    coa = account(world.db, REDUCED)
    invoice_id = entry_invoice(world, 'R-720', [entry('5.5', '60.00', coa), entry('5.5', '40.00', coa)])
    send(world, invoice_id)
    add_payment(world, invoice_id, 105.5)                                                              # wie der Import zuordnet: ohne Buchungssätze
    storno = cancelled(world, invoice_id)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxExemptReason = 'anderer Grund' WHERE InvoiceId = ? AND Position = 2", invoice_id)
    booking = outgoing(world, -105.5, '2026-07-10')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, TAX_ENTRY_CONTRADICTION, None, None) and tables(world.db) == before


def test_the_refund_names_a_tax_free_group_whose_payments_lie_on_two_accounts(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    second = pay_on(world, invoice_id, 99.0, '2026-07-05')
    run_sql(world.db, "UPDATE Bookings SET CounterCOA_ID = ? WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate IS NULL", account(world.db, 4130), second)
    storno = cancelled(world, invoice_id)

    assignment = refund_of(world, storno.storno_id, 50.0)

    assert assignment.ok is False and 'für steuerfreie Leistungen auf verschiedenen Erlöskonten' in assignment.message
    assert assignment.message == rate_sentence(MIXED_REFUND_REVENUE_AMBIGUOUS, D(0), True)


REFUND_TAMPERS = {
    'reason': "UPDATE InvoiceItems SET TaxExemptReason = 'anderer Grund' WHERE InvoiceId = ? AND Position = {last}",                      # verschiedene Befreiungsgründe in einer Gruppe
    'account': "UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4130) WHERE InvoiceId = ? AND Position = {last}",   # ein anderes Erlöskonto
    'half': "UPDATE InvoiceItems SET TaxRateBp = NULL WHERE InvoiceId = ? AND Position = {last}",                                       # eine Position ohne Satz, aber mit Konto: die halbe Momentaufnahme
}


@pytest.mark.parametrize('tamper', list(REFUND_TAMPERS))
@pytest.mark.parametrize('kind', ['single', 'flagged'])
def test_the_refund_does_not_check_the_snapshot_it_follows_the_payments_even_from_a_contradictory_stand(world, kind, tamper):
    """Die Erstattung prüft die Momentaufnahme nicht (``group_snapshots``: Versenden, Zahlung, Stornieren und der Umzug prüfen): Ein Guthaben muss sich auch aus einem
    widersprüchlichen Stand erstatten lassen. Betrag, Steuer und Konten folgen den Sätzen der Zahlungen – auch das Erlöskonto, das die Momentaufnahme jetzt nennt, ändert daran
    nichts. Die Prüfliste nennt den Widerspruch trotzdem."""
    account(world.db, 4130)
    coa = account(world.db, FREE)
    parts, last = (([free('60.00', coa), free('40.00', coa)], 2) if kind == 'single' else ([plain(19, '100.00'), free('50.00', coa), free('30.00', coa)], 3))
    invoice_id = entry_invoice(world, 'F-700', parts)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    storno = cancelled(world, invoice_id)
    run_sql(world.db, REFUND_TAMPERS[tamper].format(last=last), invoice_id)
    assert findings(world.db, invoice_id, 'tax_entry_mismatch') != []                                    # der Widerspruch steht da – und wird genannt
    before_rows = marked(world.db, 'F-700')

    assignment = refund_of(world, storno.storno_id, 100.0)

    assert assignment.ok, assignment.message
    expected = ([(1800, 10000, -10000, None, None, 'liquid_first'), (4340, FREE, -10000, None, None, 'transfer')] if kind == 'single' else
                [(1800, 10000, -10000, None, None, 'liquid_first'), (4405, 4400, -5980, -955, 0.19, 'transfer'), (4340, FREE, -4020, None, None, 'transfer')])
    assert marked(world.db, storno.storno_number) == expected                                              # je Satz das, was die Zahlungen trugen, auf dem Konto der Zahlungen (4120)
    assert marked(world.db, 'F-700') == before_rows and world.db.get_storno_credit(invoice_id) == D('0.00')
    assert findings(world.db, invoice_id, 'tax_entry_mismatch') != []                                    # auch nach der Erstattung


def test_the_refund_of_a_flagged_invoice_whose_payment_account_is_no_revenue_account_names_the_account_of_the_invoice_when_it_carries_a_snapshot(world):
    """Die Zahlungen eines Satzes liegen auf EINEM Konto, das kein Erlöskonto (mehr) ist: Trägt der Beleg eine Momentaufnahme, nennt die Erstattung das Konto der Rechnung für den Satz
    (``TAX_PAYMENT_NO_REVENUE``) – „auf verschiedenen Erlöskonten“ (``MIXED_REFUND_REVENUE_AMBIGUOUS``) wäre dort falsch."""
    coa = account(world.db, FREE)
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    storno = cancelled(world, invoice_id)
    spoil(world, coa)
    booking = outgoing(world, -50.0, '2026-07-10')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, rate_sentence(TAX_PAYMENT_NO_REVENUE, D(0), True), None, None)
    assert assignment.message.startswith('Das Erlöskonto, das diese Rechnung für steuerfreie Leistungen trägt, ist kein Erlöskonto (mehr)')
    assert 'verschiedenen' not in assignment.message                                                      # nicht der Satz zu mehreren Konten
    assert tables(world.db) == before


def test_the_same_refund_for_a_rate_with_a_snapshot_names_the_rate_of_the_group(world):
    coa = account(world.db, REDUCED)
    invoice_id = reduced_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 330.0)
    storno = cancelled(world, invoice_id)
    spoil(world, coa)
    booking = outgoing(world, -50.0, '2026-07-10')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, TAX_PAYMENT_NO_REVENUE.format(rate='5,5 %'), None, None) and tables(world.db) == before


def test_the_refund_of_a_flagged_invoice_without_a_snapshot_keeps_the_sentence_about_different_accounts(world):
    """Ohne Momentaufnahme bleibt die Meldung, wie sie ist (``MIXED_REFUND_REVENUE_AMBIGUOUS``)."""
    invoice_id = entry_invoice(world, 'T-710', [plain(19, '100.00'), plain(7, '50.00')])
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    storno = cancelled(world, invoice_id)
    run_sql(world.db, 'UPDATE ChartOfAccounts SET AccountNumber = 6120 WHERE AccountNumber = 4400')       # das Konto der 19-%-Zahlungen ist kein Erlöskonto mehr
    booking = outgoing(world, -50.0, '2026-07-10')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, rate_sentence(MIXED_REFUND_REVENUE_AMBIGUOUS, D(19), False), None, None) and tables(world.db) == before
    assert 'für 19 % auf verschiedenen Erlöskonten' in assignment.message


@pytest.mark.parametrize('first, second, removed, rate, tax_free', [
    ('0.31', '0.11', 1, D(0), True),          # ohne die zweite Zahlung hätte die steuerfreie Gruppe mehr zurückbekommen, als sie trug
    ('0.31', '0.31', 0, D(19), False),        # dieselbe Zahlung zweimal: die erste zu entfernen trifft die 19 %
])
def test_removing_a_payment_after_a_refund_names_the_group_that_would_be_over_refunded(world, first, second, removed, rate, tax_free):
    """Die Summe geht auf (0,31 gezahlt, 0,31 erstattet), und doch hätte eine Gruppe nach dem Entfernen mehr zurückbekommen, als sie trug (``MIXED_REMOVAL_REFUSED``, je Satz):
    Eine steuerfreie Gruppe heißt dort „steuerfreie Leistungen“, nicht „0 %“ (``rate_sentence``, ``_exempt_pcts``)."""
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, D(first), '2026-07-01')
    pay_on(world, invoice_id, D(second), '2026-07-02')
    storno = cancelled(world, invoice_id, '2026-07-03')
    assert refund_of(world, storno.storno_id, D(first), '2026-07-04').ok
    payment_ids = [row['ID'] for row in query_rows(world.db, 'SELECT ID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)]
    before = tables(world.db)

    with pytest.raises(PaymentRemovalRefused) as refused:
        world.db.unassign_invoice_payment(payment_ids[removed])

    assert str(refused.value) == rate_sentence(MIXED_REMOVAL_REFUSED, rate, tax_free, number=storno.storno_number)
    assert (EXEMPT_SENTENCE_LABEL in str(refused.value)) is tax_free and ('0 %' in str(refused.value)) is False
    assert (f'für {EXEMPT_SENTENCE_LABEL} wäre danach mehr erstattet als gezahlt' in str(refused.value)) is tax_free
    assert tables(world.db) == before                                                                    # vor dem ersten Schreiben


# ═════════════════════════════════════════════════════════════════════════════
# Umzug auf die Ersatzrechnung: unterbleibt bei einem Erlöskonto der Stammdaten (MOVE_ENTRY_HINT)
# ═════════════════════════════════════════════════════════════════════════════

def paid_and_cancelled(world, make, number, amount):
    original = make(world, number)
    send(world, original)
    if amount:
        pay_on(world, original, amount)
    storno = cancelled(world, original)
    return original, storno


def test_the_payments_stay_on_the_cancelled_invoice_when_it_carries_an_account_of_the_masterdata(world, monkeypatch):
    original, storno = paid_and_cancelled(world, free_invoice, 'F-100', 100.0)
    replacement = reissue(world.db, original, monkeypatch)                                              # die Positionen kommen mit ihrer Momentaufnahme
    before = allocations(world.db, original)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.ok and change.moved == 0
    assert change.payment_hint == MOVE_ENTRY_HINT.format(number='F-100', amount='100,00 €')
    assert 'Eine der beiden Rechnungen trägt einen Steuersatz mit eigenem Erlöskonto aus den Stammdaten' in change.payment_hint
    assert allocations(world.db, original) == before and allocations(world.db, replacement) == []       # die Zahlung bleibt als Guthaben, mit allen Sätzen
    assert world.db.get_storno_credit(original) == D('100.00')
    number = columns(world.db, replacement)['InvoiceNumber']
    assert [row[:3] for row in receivables(world.db, number)] == [(10000, 4340, 10000)]               # die Forderung der Ersatzrechnung: wie jede


def test_it_is_enough_that_one_of_the_two_invoices_carries_the_account(world, monkeypatch):
    """Die stornierte Rechnung ohne Erlöskonto der Stammdaten, die Ersatzrechnung mit – und umgekehrt: Beide Male zieht nichts um."""
    for number, make, edit in (
            ('T-100', lambda w, n: entry_invoice(w, n, [plain(19, '100.00')]),
             "UPDATE InvoiceItems SET TaxRateBp = 1900, RevenueCoaId = {coa} WHERE InvoiceId = ?"),    # nur die neue Rechnung trägt eines (eine ganze Momentaufnahme, keine halbe)
            ('F-100', free_invoice,
             "UPDATE InvoiceItems SET TaxRateBp = NULL, TaxCategory = 'S', TaxExemptReason = NULL, RevenueCoaId = NULL WHERE InvoiceId = ?")):   # nur die alte
        original, _storno = paid_and_cancelled(world, make, number, 50.0 if number == 'T-100' else 100.0)
        replacement = reissue(world.db, original, monkeypatch)
        run_sql(world.db, edit.format(coa=account(world.db, 4130)), replacement)

        change = world.db.set_invoice_status(replacement, 'sent', USER)

        assert change.ok and change.moved == 0 and change.payment_hint.startswith(f'Die Zahlungen der stornierten Rechnung {number} ('), number
        assert 'mit eigenem Erlöskonto aus den Stammdaten' in change.payment_hint and allocations(world.db, replacement) == []


def test_after_a_part_refund_the_hint_names_what_is_refunded_and_after_a_full_one_it_says_nothing_stays(world, monkeypatch):
    original, storno = paid_and_cancelled(world, free_invoice, 'F-100', 100.0)
    assert refund_of(world, storno.storno_id, 40.0).ok
    replacement = reissue(world.db, original, monkeypatch)

    part = world.db.set_invoice_status(replacement, 'sent', USER)

    assert part.payment_hint == f"{MOVE_ENTRY_HINT.format(number='F-100', amount='60,00 €')} {MOVE_REFUNDED_HINT.format(amount='40,00 €')}"

    original2, storno2 = paid_and_cancelled(world, free_invoice, 'F-200', 100.0)
    assert refund_of(world, storno2.storno_id, 100.0, '2026-07-12').ok
    replacement2 = reissue(world.db, original2, monkeypatch)

    full = world.db.set_invoice_status(replacement2, 'sent', USER)

    assert full.payment_hint == f"{MOVE_NONE_HINT.format(number='F-200')} {MOVE_REFUNDED_HINT.format(amount='100,00 €')}"       # nichts mehr zu erstatten oder neu zuzuordnen
    assert MOVE_ENTRY_HINT.split(':')[0] not in full.payment_hint


def test_an_invoice_without_payments_gets_no_move_hint_and_the_waiting_hint_comes_first(world, monkeypatch):
    original, _storno = paid_and_cancelled(world, lambda w, n: entry_invoice(w, n, [entry('5.5', '100.00', account(w.db, REDUCED))]), 'R-1', 105.5)
    replacement = reissue(world.db, original, monkeypatch)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.payment_hint == f"{NO_WAITING_HINT.format(rate='5,5 %')} {MOVE_ENTRY_HINT.format(number='R-1', amount='105,50 €')}"    # beide, der Hinweis zum Wartekonto zuerst
    assert change.moved == 0 and allocations(world.db, replacement) == []

    none_paid, _storno = paid_and_cancelled(world, free_invoice, 'F-300', 0)
    quiet = world.db.set_invoice_status(reissue(world.db, none_paid, monkeypatch), 'sent', USER)

    assert quiet.ok and quiet.payment_hint is None


def test_the_move_context_resolves_no_revenue_account_for_an_invoice_with_an_account_of_the_masterdata(world, monkeypatch):
    original, _storno = paid_and_cancelled(world, free_invoice, 'F-100', 100.0)
    entry_replacement = reissue(world.db, original, monkeypatch)
    plain_original, _ = paid_and_cancelled(world, lambda w, n: entry_invoice(w, n, [plain(19, '100.00')]), 'T-100', 50.0)
    plain_replacement = reissue(world.db, plain_original, monkeypatch)

    assert world.db._payment_move_context(entry_replacement, 'sent')['revenue_coa'] is None
    assert world.db._payment_move_context(plain_replacement, 'sent')['revenue_coa'] == world.db.get_coa_id_by_account_number(4400)      # wie heute


# ═════════════════════════════════════════════════════════════════════════════
# Zuordnung neu aufbauen: nur der offene Betrag (Grund tax_entry)
# ═════════════════════════════════════════════════════════════════════════════

def test_the_rebuild_of_an_invoice_with_an_account_of_the_masterdata_only_recalculates_the_open_amount_and_writes_no_row(world):
    invoice_id = free_invoice(world)
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)               # ein Eingriff an der App vorbei
    before = tables(world.db)

    dry = world.db.rebuild_invoice_payments(invoice_id)

    assert (dry.ok, dry.code, dry.executed, dry.needs_revenue_coa) == (True, 200, False, False)
    plan = dry.plan
    assert [action.kind for action in plan.actions] == ['recalc'] and plan.rows == [] and plan.euer == []
    assert plan.skipped == [REBUILD_SKIP_REASONS['tax_entry']]
    assert REBUILD_SKIP_REASONS['tax_entry'] == ('Die Rechnung trägt einen Steuersatz mit eigenem Erlöskonto aus den Stammdaten – ihre Zuordnungen werden in dieser Version nicht '
                                                 'automatisch neu aufgebaut; nur der offene Betrag wird neu gerechnet.')
    assert (plan.due_before, plan.due_after) == (999999, 0) and tables(world.db) == before             # der Probelauf hinterlässt nichts

    done = world.db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=plan.check)

    assert (done.ok, done.executed) == (True, True) and standing(world.db, invoice_id) == ('paid', 0)
    assert tables(world.db)['Bookings'] == before['Bookings'] and findings(world.db, invoice_id) == []  # kein Satz geschrieben, keiner geändert


def test_an_allocation_without_rows_is_rebuilt_for_an_invoice_without_the_account_but_not_for_one_with_it(world):
    """Der Unterschied, auf den es ankommt: Dieselbe Zuordnung ohne ihre Sätze baut R1 bei einer gewöhnlichen Rechnung neu auf – bei einem Erlöskonto der Stammdaten nicht."""
    plain_id = sent(world, 'T-100')
    pay_on(world, plain_id, 119.0)
    free_id = free_invoice(world)
    send(world, free_id)
    pay_on(world, free_id, 100.0)
    run_sql(world.db, "DELETE FROM Bookings WHERE Status = 'invoice_payment'")                          # die Zuordnungen stehen ohne ihre Sätze da
    before = tables(world.db)

    plain_plan = world.db.rebuild_invoice_payments(plain_id).plan
    entry_plan = world.db.rebuild_invoice_payments(free_id).plan

    assert [action.kind for action in plain_plan.actions] == ['rebuild'] and len(plain_plan.rows) == 2 and plain_plan.skipped == []
    assert entry_plan.actions == [] and entry_plan.rows == [] and entry_plan.skipped == [REBUILD_SKIP_REASONS['tax_entry']]
    assert tables(world.db) == before


def test_the_rebuild_never_looks_for_or_asks_for_a_revenue_account_for_such_an_invoice(world):
    invoice_id = entry_invoice(world, 'R-1', [entry('5.5', '100.00', account(world.db, REDUCED))])
    send(world, invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)

    asked = world.db.rebuild_invoice_payments(invoice_id)
    chosen = world.db.rebuild_invoice_payments(invoice_id, revenue_coa_id=999999)                      # ein Konto, das es nicht gibt, wird nicht einmal gelesen

    assert asked.ok and not asked.needs_revenue_coa and asked.plan.revenue_coa is None and chosen.ok and chosen.plan.check == asked.plan.check


def test_a_flagged_invoice_with_an_account_of_the_masterdata_still_gives_the_reason_mixed(world):
    invoice_id = mixed_free_invoice(world)
    send(world, invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)

    plan = world.db.rebuild_invoice_payments(invoice_id).plan

    assert plan.skipped == [REBUILD_SKIP_REASONS['mixed']]                                              # der erste Grund gilt, nicht beide


# ═════════════════════════════════════════════════════════════════════════════
# Anzeigen: keine Wahl des Erlöskontos, wenn die Rechnung ihr Konto aus den Stammdaten trägt
# ═════════════════════════════════════════════════════════════════════════════

def test_the_payment_box_the_mask_and_the_rebuild_box_offer_no_choice_for_an_account_of_the_masterdata(world):
    with_entry = entry_invoice(world, 'R-1', [entry('5.5', '100.00', account(world.db, REDUCED))])
    without = entry_invoice(world, 'R-2', [plain('5.5', '100.00')])
    for invoice_id in (with_entry, without):
        send(world, invoice_id)
        run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)           # ein Befund: Die Seite zeigt den Neuaufbau
    before = tables(world.db)

    entry_page, entry_mask = PageInvoice(world.db, {}, with_entry), PageTransactions(world.db, from_invoice=with_entry)
    plain_page, plain_mask = PageInvoice(world.db, {}, without), PageTransactions(world.db, from_invoice=without)

    assert 'name="revenue_coa_id"' not in entry_page and 'id="rebuildRevenueCoa"' not in entry_page and 'kein Erlöskonto hinterlegt' not in entry_page
    assert 'name="revenue_coa_id"' not in entry_mask and 'R-1' in entry_mask
    assert 'rebuildPayments(%d, false)' % with_entry in entry_page                                       # der Neuaufbau-Knopf bleibt (nur der offene Betrag)
    assert 'name="revenue_coa_id"' in plain_page and 'id="rebuildRevenueCoa"' in plain_page and 'name="revenue_coa_id"' in plain_mask   # die Gegenprobe: ohne Konto der Stammdaten wie heute
    assert tables(world.db) == before                                                                   # Anzeigen ändert nichts


def test_the_choice_function_gets_the_flag_for_an_account_of_the_masterdata(world):
    assert revenue_coa_choice(world.db, 0.055, entry=True) is None and revenue_coa_choice(world.db, 0.055, teaches=False, entry=True) is None
    assert revenue_coa_choice(world.db, 0.055) is not None and revenue_coa_choice(world.db, 0.055, entry=False) is not None
    assert world.db.invoice_entry_accounts(free_invoice(world)) is True
    assert world.db.invoice_entry_accounts(entry_invoice(world, 'T-100', [plain(19, '100.00')])) is False
    assert world.db.invoice_entry_accounts(entry_invoice(world, 'T-101', [entry('19', '100.00', None)])) is False        # eine Momentaufnahme ohne Erlöskonto zählt nicht
    assert world.db.invoice_entry_accounts(999999) is False


# ═════════════════════════════════════════════════════════════════════════════
# Die festen Sätze: rate_sentence und group_label
# ═════════════════════════════════════════════════════════════════════════════

def sentence_templates():
    """Jede Vorlage, in die ``rate_sentence`` den Satz einer Gruppe einsetzt – mit den übrigen Platzhaltern, die sie braucht (Name, Vorlage, Platzhalter)."""
    from db.invoice_payments import (MIXED_GROUP_NOT_POSITIVE as NOT_POSITIVE, MIXED_NO_REVENUE_COA as NO_COA, MIXED_REFUND_REVENUE_AMBIGUOUS as AMBIGUOUS,
                                     MIXED_REMOVAL_REFUSED as REMOVAL)
    return [('MIXED_GROUP_NOT_POSITIVE', NOT_POSITIVE, {'amount': '-10,70 €'}), ('MIXED_NO_REVENUE_COA', NO_COA, {}), ('MIXED_REFUND_REVENUE_AMBIGUOUS', AMBIGUOUS, {}),
            ('MIXED_REMOVAL_REFUSED', REMOVAL, {'number': '26R001'}), ('TAX_PAYMENT_NO_REVENUE', TAX_PAYMENT_NO_REVENUE, {}), ('TAX_SEND_NO_REVENUE', TAX_SEND_NO_REVENUE, {}),
            ('differs', TAX_ENTRY_FINDINGS['differs'], {}), ('rate', TAX_ENTRY_FINDINGS['rate'], {'position': '7 %'}), ('exempt_rate', TAX_ENTRY_FINDINGS['exempt_rate'], {}),
            ('exempt_reason', TAX_ENTRY_FINDINGS['exempt_reason'], {}), ('revenue', TAX_ENTRY_FINDINGS['revenue'], {}),
            ('head', TAX_ENTRY_FINDINGS['head'], {'head': '19 %'}), ('half', TAX_ENTRY_FINDINGS['half'], {})]


@pytest.mark.parametrize('pct', ['19', '7', '5.5', '7.4', '0'])
def test_rate_sentence_is_character_for_character_the_old_sentence_for_every_rate_that_is_not_tax_free(pct):
    """Für 19, 7 und 5,5 % (und jeden anderen Satz, auch 0 % ohne die Art „steuerfrei“) bleibt jeder Satz Zeichen für Zeichen, wie er war: der Satz, ein Leerzeichen, das Prozentzeichen."""
    from tax_groups import pct_text
    rate = D(pct)

    for name, template, names in sentence_templates():
        assert rate_sentence(template, rate, False, **names) == template.replace('{rate} %', '{rate}').format(rate=f'{pct_text(rate)} %', **names), name
    assert rate_sentence(TAX_SEND_NO_REVENUE, rate, False) == f'Für {pct_text(rate)} % hat die Rechnung kein gültiges Erlöskonto – bitte im Steuersatz der Stammdaten eines eintragen und die Rechnung danach neu speichern.'


# Die fertigen Sätze mit einer steuerfreien Gruppe, wie sie ein Nutzer liest: „steuerfreie Leistungen“ – „für steuerfrei“ las sich holprig
TAX_FREE_SENTENCES = {
    'MIXED_GROUP_NOT_POSITIVE': ('Eine Rechnung mit mehreren Steuersätzen lässt sich nur buchen, wenn die Positionen jedes Steuersatzes zusammen einen positiven Betrag ergeben '
                                 '(steuerfreie Leistungen: -10,70 €).'),
    'MIXED_NO_REVENUE_COA': ('Für steuerfreie Leistungen ist kein Erlöskonto hinterlegt – bei einer Rechnung mit mehreren Steuersätzen lässt es sich nicht wählen. '
                             'Bitte das Erlöskonto für steuerfreie Leistungen im Kontenrahmen anlegen.'),
    'MIXED_REFUND_REVENUE_AMBIGUOUS': ('Die Zahlungen dieser Rechnung liegen für steuerfreie Leistungen auf verschiedenen Erlöskonten – bei einer Rechnung mit mehreren '
                                       'Steuersätzen lässt sich das Erlöskonto der Erstattung nicht wählen. Bitte die Buchungssätze der Zahlungen prüfen.'),
    'MIXED_REMOVAL_REFUSED': ('Zu dieser Rechnung wurde bereits erstattet – für steuerfreie Leistungen wäre danach mehr erstattet als gezahlt (oder Betrag und Steuer gingen '
                              'nicht auf). Erst die Erstattung an der Stornorechnung 26R001 entfernen.'),
    'TAX_PAYMENT_NO_REVENUE': ('Das Erlöskonto, das diese Rechnung für steuerfreie Leistungen trägt, ist kein Erlöskonto (mehr) – die Zahlung lässt sich nicht kontieren. '
                               'Bitte das Konto im Kontenrahmen prüfen (Prüfung unter Daten).'),
    'TAX_SEND_NO_REVENUE': ('Für steuerfreie Leistungen hat die Rechnung kein gültiges Erlöskonto – bitte im Steuersatz der Stammdaten eines eintragen und die Rechnung danach '
                            'neu speichern.'),
    'differs': ('Für steuerfreie Leistungen tragen die Positionen der Rechnung verschiedene Angaben aus den Stammdaten (Art, Befreiungsgrund oder Erlöskonto) oder teils keine – '
                'die Rechnung lässt sich so nicht buchen.'),
    'rate': 'Für steuerfreie Leistungen passt der Satz aus den Stammdaten nicht zum Steuersatz einer Position (7 %) – die Rechnung lässt sich so nicht buchen.',
    'revenue': 'Für steuerfreie Leistungen ist das Erlöskonto der Rechnung kein Erlöskonto (mehr) – die Zahlung lässt sich nicht kontieren.',
    'head': ('Der Steuersatz im Kopf der Rechnung (19 %) passt nicht zum Satz aus den Stammdaten (steuerfreie Leistungen) – die Rechnung lässt sich so nicht '
             'buchen.'),
}


@pytest.mark.parametrize('name, template, names', [entry_ for entry_ in sentence_templates() if entry_[0] in TAX_FREE_SENTENCES], ids=list(TAX_FREE_SENTENCES))
def test_every_sentence_with_a_tax_free_group_reads_as_german_with_tax_free_services(name, template, names):
    assert rate_sentence(template, D(0), True, **names) == TAX_FREE_SENTENCES[name]
    assert EXEMPT_SENTENCE_LABEL == 'steuerfreie Leistungen' and 'steuerfrei ' not in TAX_FREE_SENTENCES[name].replace('steuerfreie Leistungen', '')    # nirgends mehr das bloße „für steuerfrei“


def test_the_sentences_that_name_no_group_rate_do_not_change_with_the_flag():
    """„exempt_reason“ und „half“ haben keinen Platzhalter; „exempt_rate“ nennt den Satz der Position (nie „steuerfrei“, die Prüfliste ruft ihn mit ``exempt`` falsch auf)."""
    for kind in ('exempt_reason', 'half'):
        assert rate_sentence(TAX_ENTRY_FINDINGS[kind], D(0), True) == TAX_ENTRY_FINDINGS[kind] == rate_sentence(TAX_ENTRY_FINDINGS[kind], D(19), False)
    assert rate_sentence(TAX_ENTRY_FINDINGS['exempt_rate'], D('5.5'), False) == 'Eine Position der Art „steuerfrei“ trägt den Satz 5,5 % – eine steuerfreie Leistung hat den Satz 0.'


def test_the_group_label_of_the_lists_and_the_pdf_stays_tax_free():
    """Nur ``rate_sentence`` ändert sich: Die Listen der Prüfliste (``shown``) und das PDF nennen die Gruppe weiter „steuerfrei“ (``tax_groups.group_label``)."""
    assert group_label(D(19), False) == '19 %' and group_label(D(0), True) == 'steuerfrei' and group_label(D(0), False) == '0 %'


def test_the_fixed_sentences_of_this_stage_are_the_ones_of_the_plan():
    assert TAX_SEND_NO_REVENUE == ('Für {rate} hat die Rechnung kein gültiges Erlöskonto – bitte im Steuersatz der Stammdaten eines eintragen und die Rechnung danach '
                                   'neu speichern.')
    assert NO_WAITING_HINT == 'Für {rate} gibt es kein Wartekonto – diese Rechnung bucht erst mit der Zahlung (keine Forderung).'
    assert MOVE_ENTRY_HINT == ('Die Zahlungen der stornierten Rechnung {number} ({amount}) bleiben dort als Guthaben des Kunden: Eine der beiden '
                               'Rechnungen trägt einen Steuersatz mit eigenem Erlöskonto aus den Stammdaten, die Zahlungen ziehen in dieser Version nicht '
                               'automatisch um. Sie lassen sich dort erstatten oder entfernen und hier neu zuordnen.')
    assert MOVE_MIXED_HINT != MOVE_ENTRY_HINT and MOVE_ENTRY_HINT.split(':')[0] == MOVE_MIXED_HINT.split(':')[0]                # nach dem Muster von MOVE_MIXED_HINT


# ═════════════════════════════════════════════════════════════════════════════
# Prüfliste: tax_entry_mismatch
# ═════════════════════════════════════════════════════════════════════════════

def sentence(kind, pct, exempt=False, **names):
    return rate_sentence(TAX_ENTRY_FINDINGS[kind], D(pct), exempt, **names)


def test_the_fixed_sentences_of_the_findings_name_the_rate_and_no_amount():
    assert TAX_ENTRY_FINDINGS == {
        'differs': ('Für {rate} % tragen die Positionen der Rechnung verschiedene Angaben aus den Stammdaten (Art, Befreiungsgrund oder Erlöskonto) oder teils '
                    'keine – die Rechnung lässt sich so nicht buchen.'),
        'rate': 'Für {rate} % passt der Satz aus den Stammdaten nicht zum Steuersatz einer Position ({position}) – die Rechnung lässt sich so nicht buchen.',
        'exempt_rate': 'Eine Position der Art „steuerfrei“ trägt den Satz {rate} % – eine steuerfreie Leistung hat den Satz 0.',
        'exempt_reason': 'Die steuerfreien Positionen der Rechnung tragen keinen Grund der Steuerbefreiung.',
        'revenue': 'Für {rate} % ist das Erlöskonto der Rechnung kein Erlöskonto (mehr) – die Zahlung lässt sich nicht kontieren.',
        'head': ('Der Steuersatz im Kopf der Rechnung ({head}) passt nicht zum Satz aus den Stammdaten ({rate} %) – die Rechnung lässt sich so nicht '
                 'buchen.'),
        'half': 'Eine Position trägt Angaben aus den Stammdaten ohne den Satz dazu – die Rechnung lässt sich so nicht buchen.'}
    assert sentence('differs', 0, True) == ('Für steuerfreie Leistungen tragen die Positionen der Rechnung verschiedene Angaben aus den Stammdaten (Art, Befreiungsgrund oder Erlöskonto) oder '
                                            'teils keine – die Rechnung lässt sich so nicht buchen.')
    assert sentence('rate', '5.5', False, position='7 %') == 'Für 5,5 % passt der Satz aus den Stammdaten nicht zum Steuersatz einer Position (7 %) – die Rechnung lässt sich so nicht buchen.'


def test_a_clean_invoice_has_no_finding_in_any_of_its_states(world):
    """Kein Befund an sauberen Belegen: Entwurf, versendet, teilweise und ganz bezahlt, storniert und erstattet – für jede Art von Beleg mit Momentaufnahme."""
    ids = [free_invoice(world), mixed_free_invoice(world), reduced_invoice(world),
           entry_invoice(world, 'R-1', [entry('5.5', '100.00', account(world.db, REDUCED))]), entry_invoice(world, 'R-2', [entry('19', '100.00', None)]),
           entry_invoice(world, 'T-100', [plain(19, '100.00')])]
    assert findings(world.db) == []                                                                    # als Entwurf

    for invoice_id in ids:
        send(world, invoice_id)
    assert findings(world.db) == []                                                                    # versendet: Forderungen je Satz, soweit es Wartekonten gibt

    for invoice_id, amount in zip(ids, (50.0, 100.0, 100.0, 50.0, 50.0, 50.0)):
        pay_on(world, invoice_id, amount, '2026-07-01')
    assert findings(world.db) == []                                                                    # teilweise bezahlt

    for invoice_id, amount in zip(ids, (50.0, 99.0, 230.0, 55.5, 69.0, 69.0)):
        pay_on(world, invoice_id, amount, '2026-07-05')
    assert findings(world.db) == []                                                                    # ganz bezahlt
    assert [standing(world.db, invoice_id)[0] for invoice_id in ids] == ['paid'] * 6

    storno = cancelled(world, ids[1])
    assert refund_of(world, storno.storno_id, 199.0).ok
    assert findings(world.db) == []                                                                    # storniert und erstattet, je Satz


def test_a_quote_is_never_reported(world):
    invoice_id = entry_invoice(world, 'A-100', [free('100.00', account(world.db, FREE))], document_type='quote')
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxExemptReason = NULL WHERE InvoiceId = ?', invoice_id)

    assert findings(world.db) == []


@pytest.mark.parametrize('parts, tamper, expected', [
    # Positionen: 1 = 19 % wie heute, 2 = steuerfrei mit Momentaufnahme (Konto 4120)
    ('free', "UPDATE InvoiceItems SET TaxExemptReason = NULL WHERE InvoiceId = ? AND Position = 2", [sentence('exempt_reason', 0, True)]),
    ('free', "UPDATE InvoiceItems SET TaxExemptReason = '   ' WHERE InvoiceId = ? AND Position = 2", [sentence('exempt_reason', 0, True)]),
    ('free', "UPDATE InvoiceItems SET TaxRate = 5.5, TaxRateBp = 550 WHERE InvoiceId = ? AND Position = 2", [sentence('exempt_rate', '5.5')]),
    ('free', "UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2", [sentence('rate', 0, True, position='7 %')]),
    ('free', "UPDATE InvoiceItems SET RevenueCoaId = 999999 WHERE InvoiceId = ? AND Position = 2", [sentence('revenue', 0, True)]),
    ('free', "UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 1800) WHERE InvoiceId = ? AND Position = 2",
     [sentence('revenue', 0, True)]),
    ('free', "UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4340) WHERE InvoiceId = ? AND Position = 2",
     [sentence('revenue', 0, True)]),                                                                  # ein Wartekonto ist kein Erlöskonto
    ('free', "INSERT INTO InvoiceItems (InvoiceId, Position, Description, Quantity, PricePerUnit, TotalNet, TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, "
             "RevenueCoaId) VALUES (?, 3, 'dritte', 1, 0, 0, 'E', 0, 0, 'ein anderer Grund', (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4120))",
     [sentence('differs', 0, True)]),                                                                  # zwei Befreiungsgründe bei gleichem Satz
    ('free', "INSERT INTO InvoiceItems (InvoiceId, Position, Description, Quantity, PricePerUnit, TotalNet, TaxCategory, TaxRate) "
             "VALUES (?, 3, 'dritte', 1, 0, 0, 'S', 0)", [sentence('differs', 0, True)]),               # mit und ohne Momentaufnahme bei gleichem Satz
    ('reduced', "UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2", [sentence('rate', '5.5', False, position='7 %')]),
    ('reduced', "UPDATE InvoiceItems SET TaxRateBp = 12345 WHERE InvoiceId = ? AND Position = 2", [sentence('rate', '5.5', False, position='5,5 %')]),     # kein Satz
    ('reduced', "UPDATE InvoiceItems SET RevenueCoaId = 999999 WHERE InvoiceId = ? AND Position = 2", [sentence('revenue', '5.5')]),
    ('reduced', "UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4300) WHERE InvoiceId = ? AND Position = 2",
     []),                                                                                              # ein anderes Erlöskonto ist keine Unstimmigkeit
    ('reduced', "INSERT INTO InvoiceItems (InvoiceId, Position, Description, Quantity, PricePerUnit, TotalNet, TaxCategory, TaxRate, TaxRateBp) "
                "VALUES (?, 3, 'dritte', 1, 0, 0, 'S', 5.5, 550)", [sentence('differs', '5.5')]),      # Momentaufnahme ohne Konto neben einer mit Konto
    ('single', "UPDATE Invoices SET TaxRate = 0.19 WHERE ID = ?", [sentence('head', '5.5', False, head='19 %')]),
    ('single', "UPDATE Invoices SET TaxRate = -1 WHERE ID = ?", [sentence('head', '5.5', False, head='ohne Steuerausweis')]),
    ('single', "UPDATE Invoices SET TaxRate = 'x' WHERE ID = ?", [sentence('head', '5.5', False, head='ohne lesbaren Satz')]),
])
def test_every_contradiction_of_the_snapshots_is_named_with_its_fixed_sentence(world, parts, tamper, expected):
    coa = {'free': FREE, 'reduced': REDUCED, 'single': REDUCED}[parts]
    items = {'free': [plain(19, '100.00'), free('80.00', account(world.db, coa))],
             'reduced': [plain(19, '100.00'), entry('5.5', '200.00', account(world.db, coa))],
             'single': [entry('5.5', '100.00', account(world.db, coa))]}[parts]
    invoice_id = entry_invoice(world, 'X-100', items)
    assert findings(world.db, invoice_id, 'tax_entry_mismatch') == []                                  # vorher sauber
    run_sql(world.db, tamper, invoice_id)

    found = findings(world.db, invoice_id, 'tax_entry_mismatch')

    assert [f['text'] for f in found] == expected
    assert all((f['category'], f['invoice_id'], f['invoice_number']) == ('tax_entry_mismatch', invoice_id, 'X-100') for f in found)
    assert all('€' not in f['text'] for f in found)                                                    # keine Beträge
    assert [f for f in world.db.find_payment_inconsistencies() if f['category'] == 'tax_entry_mismatch'] == found    # die ganze Liste nennt dasselbe


def test_one_finding_per_kind_and_rate_however_many_positions_carry_it(world):
    items = [plain(19, '100.00'), free('40.00', account(world.db, FREE)), free('40.00', account(world.db, FREE))]
    invoice_id = entry_invoice(world, 'X-100', items)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxExemptReason = NULL WHERE InvoiceId = ? AND Position > 1', invoice_id)

    assert [f['text'] for f in findings(world.db, invoice_id, 'tax_entry_mismatch')] == [sentence('exempt_reason', 0, True)]


def test_an_invoice_with_a_snapshot_for_two_rates_in_an_invoice_without_the_flag_is_named_by_the_head_check_only_for_one_rate(world):
    """Ein Beleg mit EINEM Satz trägt EINE Satzgruppe: Mehr als eine mit Momentaufnahme heißt ``unflagged_rate_mix`` – der Kopfvergleich entfällt (er hätte keinen Satz)."""
    invoice_id = entry_invoice(world, 'X-200', [entry('5.5', '100.00', account(world.db, REDUCED)), entry('7', '50.00', account(world.db, REDUCED))],
                               mixed_tax_rates=None)

    categories = [f['category'] for f in findings(world.db, invoice_id)]

    assert 'unflagged_rate_mix' in categories and 'tax_entry_mismatch' not in categories


def test_the_rate_of_a_position_with_a_snapshot_is_the_one_of_the_snapshot_for_the_unflagged_mix(world):
    """Der Satz einer Position mit Momentaufnahme ist der der Momentaufnahme (``tax_groups.item_rate_of``), nicht der Wert ihrer Spalte ``TaxRate`` – so rechnen auch die Buchungen."""
    invoice_id = entry_invoice(world, 'X-300', [plain(19, '100.00'), entry('5.5', '100.00', account(world.db, REDUCED))], mixed_tax_rates=None)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND TaxRateBp IS NOT NULL', invoice_id)      # die Spalte sagt 19, die Momentaufnahme 5,5

    assert 'unflagged_rate_mix' in [f['category'] for f in findings(world.db, invoice_id)]


def test_the_section_19_sample_with_the_kind_tax_free_alone_is_no_finding(world):
    """Die Art ``'E'`` ohne Satz in Hundertstel Prozent, Grund und Konto steht im Beispiel eines Belegs nach § 19 (``tools/make_einvoice_samples.py``, Kopf −1): Das Merkmal einer
    Momentaufnahme ist ``TaxRateBp`` – ohne den ist der Beleg einer ohne, und die Prüfliste schweigt. Er lässt sich versenden und bezahlen wie immer."""
    invoice_id = entry_invoice(world, 'K-100', [plain(0, '100.00')], tax_rate=-1)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxCategory = 'E' WHERE InvoiceId = ?", invoice_id)

    assert findings(world.db, invoice_id) == [] and findings(world.db) == []
    send(world, invoice_id)
    pay_on(world, invoice_id, 100.0)
    assert findings(world.db) == [] and standing(world.db, invoice_id) == ('paid', 0)


def test_tax_free_alone_is_no_finding_whatever_the_rate_of_the_position(world):
    """Auch eine Position der Art ``'E'`` ohne den Rest trägt keinen Befund wegen ihres Satzes oder eines fehlenden Grundes: Sie ist keine Momentaufnahme, und das Dokument
    liest sie nicht als solche (``export.einvoice_model._exempt_item`` verlangt den Satz 0 in Hundertstel Prozent)."""
    invoice_id = entry_invoice(world, 'K-101', [plain(19, '100.00')])
    run_sql(world.db, "UPDATE InvoiceItems SET TaxCategory = 'E' WHERE InvoiceId = ?", invoice_id)

    assert findings(world.db, invoice_id) == []


@pytest.mark.parametrize('tamper', [
    "UPDATE InvoiceItems SET TaxExemptReason = 'Ein Grund' WHERE InvoiceId = ?",                                  # nur ein Grund
    "UPDATE InvoiceItems SET TaxCategory = 'E', TaxExemptReason = 'Ein Grund' WHERE InvoiceId = ?",              # steuerfrei mit Grund, aber ohne Satz
    "UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4400) WHERE InvoiceId = ?",       # nur ein Erlöskonto
    "UPDATE InvoiceItems SET TaxCategory = 'E', TaxExemptReason = 'Ein Grund', RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4400) WHERE InvoiceId = ?",
])
def test_the_half_written_snapshot_is_named_with_its_own_fixed_sentence_and_makes_the_invoice_unbookable(world, tamper):
    """Angaben aus den Stammdaten an einer Position OHNE den Satz dazu (``TaxRateBp`` NULL, aber Befreiungsgrund oder Erlöskonto): Die Prüfliste nennt es – ein Befund je Beleg –,
    und beim Buchen ist es ein Widerspruch (``TAX_ENTRY_CONTRADICTION``)."""
    invoice_id = entry_invoice(world, 'K-200', [plain(19, '100.00')])
    assert findings(world.db, invoice_id) == []                                                        # vorher sauber
    run_sql(world.db, tamper, invoice_id)
    half = 'Eine Position trägt Angaben aus den Stammdaten ohne den Satz dazu – die Rechnung lässt sich so nicht buchen.'

    found = findings(world.db, invoice_id, 'tax_entry_mismatch')

    assert [f['text'] for f in found] == [half] == [sentence('half', 0)]
    assert [(f['category'], f['invoice_id'], f['invoice_number']) for f in found] == [('tax_entry_mismatch', invoice_id, 'K-200')]
    assert [f for f in world.db.find_payment_inconsistencies() if f['category'] == 'tax_entry_mismatch'] == found
    before = tables(world.db)
    change = world.db.set_invoice_status(invoice_id, 'sent', USER)
    assert (change.ok, change.code, change.message) == (False, 409, TAX_ENTRY_CONTRADICTION) and tables(world.db) == before


def test_two_half_snapshots_in_one_invoice_are_one_finding(world):
    invoice_id = entry_invoice(world, 'K-300', [plain(19, '100.00'), plain(7, '50.00')])
    run_sql(world.db, "UPDATE InvoiceItems SET TaxExemptReason = 'Ein Grund' WHERE InvoiceId = ?", invoice_id)

    assert [f['text'] for f in findings(world.db, invoice_id, 'tax_entry_mismatch')] == [sentence('half', 0)]


def test_a_half_snapshot_next_to_a_whole_one_of_the_same_rate_is_named_twice_with_each_own_sentence(world):
    """Eine Position ohne Satz, aber mit Konto, neben einer mit Momentaufnahme desselben Satzes: Beides stimmt – die Angaben ohne Satz und die Mischung aus mit und ohne."""
    invoice_id = entry_invoice(world, 'K-400', [entry('5.5', '100.00', account(world.db, REDUCED)), entry('5.5', '50.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRateBp = NULL WHERE InvoiceId = ? AND Position = 2', invoice_id)

    assert [f['text'] for f in findings(world.db, invoice_id, 'tax_entry_mismatch')] == [sentence('half', 0), sentence('differs', '5.5')]


def test_a_snapshot_rate_below_1_percent_is_a_finding_even_when_it_fits_the_rate_of_the_position(world):
    """Ein Satz von 1 bis 99 Hundertstel Prozent ist beim Buchen ein Widerspruch – die Prüfliste nennt ihn ebenso, auch wenn er zum Satz der Position passt (0,005 = 0,5 %)."""
    invoice_id = entry_invoice(world, 'K-500', [entry('5.5', '100.00', account(world.db, REDUCED))])
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRateBp = 50, TaxRate = 0.005 WHERE InvoiceId = ?', invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET TaxRate = 0.005 WHERE ID = ?', invoice_id)             # der Kopf trägt denselben Satz: kein zweiter Befund

    assert [f['text'] for f in findings(world.db, invoice_id, 'tax_entry_mismatch')] == [sentence('rate', '0.5', False, position='0,5 %')]


def test_the_check_reads_one_statement_for_all_invoices_with_a_snapshot(world, monkeypatch):
    """Linear im Bestand: Die Abfrage über die Positionen läuft einmal, nicht je Beleg."""
    for number in range(8):
        entry_invoice(world, f'Z-{number}', [plain(19, '100.00'), free('80.00', account(world.db, FREE))])
    statements = []
    original = type(world.db)._get_connection

    def counting(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(type(world.db), '_get_connection', counting)

    assert world.db.find_payment_inconsistencies() == []

    assert sum('p.TaxRateBp, p.TaxExemptReason, p.RevenueCoaId' in statement for statement in statements) == 1
