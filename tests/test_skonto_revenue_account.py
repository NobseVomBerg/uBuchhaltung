# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ein Skonto-Konto als Erlöskonto einer Zahlung (2.9.0, zweite Prüfung): **Ein Skonto-Satz wird nie allein am Konto erkannt – maßgeblich ist ``InvoicePayments.SkontoAmount`` der Zuordnung.**

Die Konten 4730, 4731 und 4736 liegen im Bereich der Erlöskonten (4000–4999), und ein Satz darauf muss kein Skonto sein: Ein Steuersatz der Stammdaten kann eines als Erlöskonto tragen
(5,5 % mit 4736 – ohne Wartekonto bucht jede Zahlung „4736 an Bank“), ein Nutzer hat in einer früheren Version eines gewählt, und im Editor lässt sich ein Satz dorthin legen. Solche Sätze
tragen Erlös und Steuer wie jeder andere. Gezählt, gemeldet und zurückgenommen wird ein Skonto nur, wo die Zuordnung es trägt:

* **Die Steuer der Zuordnungen** (``_allocations_tax``): Die Sätze auf einem Skonto-Konto fallen nur für eine Zuordnung mit gültigem Skonto heraus (ihr Skonto-Satz nimmt von der Steuer der
  Zahlung selbst ab); für jede andere zählt jeder markierte Satz. Vorher bekam die schließende Zahlung einer 5,5-%-Rechnung über 211,00 € nach 100,00 € statt 5,79 € den ganzen Rest (11,00 €).
* **Die Prüfliste** (``skonto_mismatch``) vergleicht nur Zuordnungen, deren ``SkontoAmount`` nicht NULL ist (``tests/test_skonto_reports.py`` hält fest, dass ein Bestand mit geleerter
  Spalte über ``transfer_mismatch`` und ``amount_due_mismatch`` auffällt).
* **Das Stornieren** lehnt (``SKONTO_STORNO_EDITED``) nur ab, wenn unter der Bewegung einer Zuordnung ohne Skonto ein Satz mit NEGATIVEM Betrag auf einem Skonto-Konto steht und die Sätze dort
  sich nicht aufheben – so sieht ein Skonto aus, dessen Wert aus der Spalte genommen wurde. Ein Erlössatz (positiv) lehnt nie ab.
* **Umzug und Neuaufbau** (``_skonto_booked``): Skonto gebucht heißt eine Zuordnung mit gültigem Skonto oder ein Skonto-Satz mit negativem Betrag (er bleibt auch nach der Rücknahme beim
  Stornieren stehen); ein Erlössatz auf einem Skonto-Konto zählt nicht.

Dazu die Vorprüfung der Buchungsmaske: Bei einer NEUEN Bewegung rechnet sie mit dem genauen Betrag, den die Bewegung trägt (nicht auf Cent gerundet) – sonst entstünde die Bewegung, und die
Zuordnung lehnte danach mit ``SKONTO_NOT_CENTS`` ab. Ohne Skonto bleibt alles, wie es war.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
from contextlib import closing
from decimal import Decimal

import pytest

from db.invoice_payments import MOVE_SKIP_REASONS, SKONTO_NOT_CENTS, SKONTO_STORNO_EDITED
from db.invoice_repair import REBUILD_SKIP_REASONS
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped
from tests import installation
from tests import susa_helpers as helpers
from tests.susa_helpers import account_id
from tests.test_payment_suggestions import discount_world
from tests.test_skonto_guards import taxes_of_the_others, two_payments
from tests.test_skonto_pages import assigned, new_form, status_of
from tests.test_skonto_reports import check_every_probe, findings, invoice_of, minor, pay, raw
from tests.test_skonto_reports import db  # noqa: F401  (Fixture: Testdatenbank mit zwei Bankkonten)
from tests.test_skonto_storno import TODAY, USER, _reissue_and_send, cancel, kids, marked_tax, payments, refund, shape, state
from tests.test_tax_entries_bookings import account, entry
from tests.test_tax_entries_reports import invoice_of as entry_invoice_of

D = Decimal
ENTRY_NUMBER, OLD_NUMBER, DISCOUNT_NUMBER = 'E-55', 'R-16', 'SK-100'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def entry_world(db):
    """E (``E-55``, 03.03.2025): 200,00 € netto, 5,5 % = 11,00 €, 211,00 € brutto – eine Position mit der Momentaufnahme eines Steuersatzes der Stammdaten, dessen Erlöskonto 4736 ist.
    5,5 % hat kein Wartekonto: Es gibt keine Forderung, jede Zahlung bucht „4736 an Bank“. Liefert die Rechnungs-ID."""
    coa = account(db, 4736)
    db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=coa)
    return entry_invoice_of(db, ENTRY_NUMBER, [entry('5.5', '200.00', coa)], '2025-03-03')


def paid_entry_world(db):
    """E, bezahlt mit 100,00 € (02.04.) und 111,00 € (05.05.). Liefert die Rechnungs-ID."""
    invoice_id = entry_world(db)
    pay(db, invoice_id, '100.00', '2025-04-02')
    pay(db, invoice_id, '111.00', '2025-05-05')
    return invoice_id


def pay_on(db, invoice_id, amount, day, revenue_coa):
    """Eine Bankbewegung ``amount`` € am Tag ``day`` und ihre Zuordnung mit der Wahl des Erlöskontos (``revenue_coa``: COA-ID) – wie die Maske sie schickt. Liefert die Bewegung."""
    bank = db.insert_booking(day, D(amount), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, D(amount), revenue_coa)
    assert ok, error
    return bank


def old_choice_world(db, monkeypatch):
    """R (``R-16``, 03.03.2025): 116,00 € mit 16 % = 16,00 €, ohne Forderung (16 % hat kein Wartekonto). Die Teilzahlung A (50,00 €) hat der Nutzer in einer früheren Version auf 4736 als
    Erlöskonto gebucht – die Auswahl der Zahlung führte das Konto damals noch (``payment_revenue_choices`` gab es nicht); die schließende B (66,00 €) bucht heute auf 4400.
    Liefert ``(Rechnungs-ID, A, B)``."""
    invoice_id = invoice_of(db, OLD_NUMBER, 116.0, rate=0.16, receivable=False)
    with monkeypatch.context() as earlier:
        earlier.setattr(db, 'payment_revenue_choices', db.get_revenue_coas)                  # die Auswahl der früheren Version: alle Erlöskonten
        a = pay_on(db, invoice_id, '50.00', '2025-04-02', db.get_coa_id_by_account_number(4736))
    b = pay_on(db, invoice_id, '66.00', '2025-05-05', db.get_coa_id_by_account_number(4400))
    return invoice_id, a, b


def storno_view(db, invoice_id, number):
    """Die beiden Fragen, die ein Satz auf einem Skonto-Konto nicht anders beantworten darf als der Wert der Spalte: ``(Ablehnung des Stornierens oder None, Zahl der Rücknahmen,
    Skonto gebucht?)`` (``_skonto_reversals``, ``_skonto_booked``)."""
    with closing(db._get_connection()) as conn:
        error, reversals = db._skonto_reversals(conn, invoice_id, number)
        return error, None if reversals is None else len(reversals), db._skonto_booked(conn, invoice_id, number)


def refund_with_choice(db, storno_id, amount, revenue_account=4400):
    """Eine ausgehende Bankbewegung über ``amount`` € und ihre Zuordnung als Erstattung, mit der Wahl des Erlöskontos (die Zahlungen liegen auf mehr als einem); liefert die Bewegung."""
    bank = db.insert_booking('2025-07-01', -D(amount), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    result = db.assign_storno_refund(storno_id, bank, None, db.get_coa_id_by_account_number(revenue_account))
    assert result.ok, result.message
    return bank


# ═════════════════════════════════════════════════════════════════════════════
# Ein Steuersatz der Stammdaten mit dem Erlöskonto 4736
# ═════════════════════════════════════════════════════════════════════════════

def test_the_closing_payment_takes_only_the_rest_of_the_tax_when_the_revenue_account_is_a_discount_account(db):
    """Probe 1c: 100,00 € tragen 5,21 € Steuer auf 4736; die schließende Zahlung über 111,00 € bekommt den Rest 5,79 € – vorher 11,00 € (Σ 16,21 € statt 11,00 €)."""
    invoice_id = entry_world(db)

    first = pay(db, invoice_id, '100.00', '2025-04-02')
    assert shape(kids(db, first)) == [('purpose_first', 4736, 1800, D('100'), D('5.21'))]
    assert findings(db, invoice_id) == []                                          # ohne Skonto an der Zuordnung ist ein Satz auf 4736 ein Erlössatz
    second = pay(db, invoice_id, '111.00', '2025-05-05')

    assert shape(kids(db, second)) == [('purpose_first', 4736, 1800, D('111'), D('5.79'))]
    assert marked_tax(db) == D('11.00')
    assert state(db, invoice_id) == ('paid', 0) and findings(db) == []


def test_every_probe_of_the_reports_holds_with_a_revenue_account_that_is_a_discount_account(db):
    """EÜR, Summen- und Saldenliste (Proben a–c), Liquidität, Split-Summe und Prüfliste gehen in jedem Monat auf den Minor-Wert auf."""
    paid_entry_world(db)

    check_every_probe(db, (3, 4, 5, 6, 7))
    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {4736: minor('200.00'), 3806: minor('11.00')}     # der Erlös netto steht auf 4736 – das Konto trägt hier Erlös, nicht Skonto


def test_the_cancellation_is_not_refused_for_a_revenue_row_on_a_discount_account(db):
    """Probe 1c: Die Zuordnungen tragen kein Skonto, ihre Sätze auf 4736 (positiv) sind Erlös – das Stornieren lehnte vorher mit ``SKONTO_STORNO_EDITED`` ab, und das dauerhaft."""
    invoice_id = paid_entry_world(db)
    assert storno_view(db, invoice_id, ENTRY_NUMBER) == (None, 0, False)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert storno.counter_booked is False                                          # keine Forderung der App, keine Gegenbuchung
    assert payments(db, invoice_id) == [(1000000, None), (1110000, None)] and marked_tax(db) == D('11.00')       # nichts zurückgenommen: Es gab kein Skonto
    assert findings(db) == []


def test_the_refunds_take_the_tax_back_by_the_same_rule_as_the_payments_carried_it(db):
    """Zwei Erstattungen (100,00 € anteilig 5,21 €, 111,00 € den Rest 5,79 €): Die Steuer der Zahlungen und die der Erstattungen zählt jeden markierten Satz, auch die auf 4736."""
    invoice_id = paid_entry_world(db)
    storno = cancel(db, invoice_id, '2025-06-03')

    first = refund(db, storno.storno_id, '100.00', day='2025-07-01')
    second = refund(db, storno.storno_id, '111.00', day='2025-07-02')

    assert shape(kids(db, first)) == [('purpose_first', 4736, 1800, D('-100'), D('-5.21'))]
    assert shape(kids(db, second)) == [('purpose_first', 4736, 1800, D('-111'), D('-5.79'))]
    assert marked_tax(db) == 0 and db.get_storno_credits() == {storno.storno_id: 0}
    assert findings(db) == []
    check_every_probe(db, (5, 6, 7))


def test_one_refund_of_the_whole_credit_takes_the_whole_tax_back(db):
    invoice_id = paid_entry_world(db)
    storno = cancel(db, invoice_id, '2025-06-03')

    bank = refund(db, storno.storno_id, '211.00', day='2025-07-01')

    assert shape(kids(db, bank)) == [('purpose_first', 4736, 1800, D('-211'), D('-11'))]
    assert marked_tax(db) == 0 and findings(db) == []


# ═════════════════════════════════════════════════════════════════════════════
# Eine frühere Wahl des Nutzers: 4736 als Erlöskonto einer Zahlung
# ═════════════════════════════════════════════════════════════════════════════

def test_a_payment_the_user_booked_on_a_discount_account_earlier_counts_for_the_tax_of_the_closing_one(db, monkeypatch):
    """16 %: A auf 4736 (6,90 €), B auf 4400 schließt mit dem Rest (9,10 €) – nicht mit der ganzen Steuer (16,00 €)."""
    invoice_id, a, b = old_choice_world(db, monkeypatch)

    assert shape(kids(db, a)) == [('purpose_first', 4736, 1800, D('50'), D('6.90'))]
    assert shape(kids(db, b)) == [('purpose_first', 4400, 1800, D('66'), D('9.10'))]
    assert marked_tax(db) == D('16.00') and taxes_of_the_others(db, invoice_id, OLD_NUMBER) == (160000, 0)
    assert state(db, invoice_id) == ('paid', 0) and findings(db) == []
    check_every_probe(db, (4, 5, 6))


def test_such_an_invoice_cancels_and_is_refunded_with_the_whole_tax(db, monkeypatch):
    """Die Zahlungen liegen auf zwei Erlöskonten – der Nutzer wählt das der Erstattung (4400); sie nimmt die ganze Steuer zurück."""
    invoice_id, _a, _b = old_choice_world(db, monkeypatch)
    assert storno_view(db, invoice_id, OLD_NUMBER) == (None, 0, False)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert db.refund_revenue_coas_differ(storno.storno_id) is True
    bank = refund_with_choice(db, storno.storno_id, '116.00')
    assert shape(kids(db, bank)) == [('purpose_first', 4400, 1800, D('-116'), D('-16'))]
    assert marked_tax(db) == 0 and db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


# ═════════════════════════════════════════════════════════════════════════════
# Ein Erlössatz im Editor auf ein Skonto-Konto gelegt, die andere Zahlung trägt ein Skonto
# ═════════════════════════════════════════════════════════════════════════════

def test_a_revenue_row_moved_to_a_discount_account_does_not_hide_the_discount_of_the_other_payment(db):
    """A (50,00 €) und B (66,62 € mit Skonto), ohne Forderung; der Nutzer legt den Erlössatz von A im Editor auf 4736. A trägt kein Skonto: Sein Satz zählt als Steuer (7,98 €) und lehnt
    das Stornieren nicht ab; das Skonto von B wird zurückgenommen, und die Erstattung nimmt die Steuer des Geldes zurück (18,62 €)."""
    c = two_payments(db, receivable=False)
    raw(db, 'UPDATE Bookings SET COA_ID = ? WHERE ID = ?', db.get_coa_id_by_account_number(4736), c.kids_a[0])

    assert taxes_of_the_others(db, c.invoice) == (190000, 0)                       # A 7,98 (sein Satz auf 4736 zählt) und B 11,02 (sein Skonto-Satz nicht)
    assert storno_view(db, c.invoice, DISCOUNT_NUMBER) == (None, 1, True)          # nur B ist zurückzunehmen
    storno = cancel(db, c.invoice, '2025-06-03')

    assert payments(db, c.invoice) == [(500000, None), (666200, None)]             # das Skonto von B ist zurückgenommen, A unberührt
    assert marked_tax(db) == D('18.62') and findings(db) == []
    assert db.refund_revenue_coas_differ(storno.storno_id) is True                 # A liegt auf 4736, B auf 4400
    bank = refund_with_choice(db, storno.storno_id, '116.62')
    assert shape(kids(db, bank)) == [('purpose_first', 4400, 1800, D('-116.62'), D('-18.62'))]
    assert marked_tax(db) == 0 and db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_a_cancellation_still_refuses_an_emptied_column_next_to_a_revenue_row_on_a_discount_account(db):
    """Der Satz mit dem negativen Betrag auf dem Skonto-Konto (−2,38 €) bleibt das Zeichen eines Skontos, dessen Wert aus der Spalte genommen wurde – auch wenn eine andere Zahlung derselben
    Rechnung einen Erlössatz (positiv) auf einem Skonto-Konto trägt."""
    c = two_payments(db, receivable=False)
    raw(db, 'UPDATE Bookings SET COA_ID = ? WHERE ID = ?', db.get_coa_id_by_account_number(4736), c.kids_a[0])
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = NULL WHERE BookingID = ?', c.b)
    before = installation.dump(db.db_name)

    assert storno_view(db, c.invoice, DISCOUNT_NUMBER) == (SKONTO_STORNO_EDITED, None, True)
    storno = db.cancel_invoice(c.invoice, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert installation.dump(db.db_name) == before


# ═════════════════════════════════════════════════════════════════════════════
# Umzug und Neuaufbau: Ein Erlössatz auf einem Skonto-Konto ist kein Skonto
# ═════════════════════════════════════════════════════════════════════════════

def test_the_rebuild_does_not_name_a_discount_for_an_invoice_whose_payment_was_booked_on_a_discount_account(db, monkeypatch):
    """Der Neuaufbau fragt ``_skonto_booked``: Der Erlössatz auf 4736 (positiv) ist kein Skonto – für eine Rechnung ohne Skonto nannte der Plan sonst den Grund ``skonto`` und ließ die Zuordnungen
    liegen."""
    invoice_id, _a, _b = old_choice_world(db, monkeypatch)
    entry_id = paid_entry_world(db)

    assert storno_view(db, invoice_id, OLD_NUMBER)[2] is False and storno_view(db, entry_id, ENTRY_NUMBER)[2] is False
    plan = db.rebuild_invoice_payments(invoice_id, today='2025-12-31').plan

    assert REBUILD_SKIP_REASONS['skonto'] not in plan.skipped


def test_the_move_to_the_replacement_invoice_names_the_real_reason_for_a_revenue_row_on_a_discount_account(db):
    """A (50,00 €, sein Erlössatz im Editor auf 4736 gelegt) trägt kein Skonto: Der Grund, aus dem er an der stornierten Rechnung bleibt, ist das Erlöskonto, das nicht das hinterlegte ist.
    B (66,62 €, das Skonto ist zurückgenommen) bleibt mit dem Grund ``skonto`` – vorher stand für beide nur dieser eine."""
    c = two_payments(db, receivable=False)
    raw(db, 'UPDATE Bookings SET COA_ID = ? WHERE ID = ?', db.get_coa_id_by_account_number(4736), c.kids_a[0])
    cancel(db, c.invoice, '2025-06-03')

    new_id, change = _reissue_and_send(db, c.invoice)

    assert change.moved == 0 and payments(db, new_id) == []
    assert MOVE_SKIP_REASONS['skonto'] in change.payment_hint and MOVE_SKIP_REASONS['revenue_account'] in change.payment_hint


# ═════════════════════════════════════════════════════════════════════════════
# Was ein Skonto ist, sagt die Spalte (und der negative Skonto-Satz nach der Rücknahme)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_payment_with_a_discount_is_booked_while_it_stands_and_after_the_reversal(db):
    c = two_payments(db)

    assert storno_view(db, c.invoice, DISCOUNT_NUMBER) == (None, 1, True)
    cancel(db, c.invoice, '2025-06-03')

    assert payments(db, c.invoice) == [(500000, None), (666200, None)]             # die Spalte ist leer: Der Skonto-Satz der Zahlung (negativ) steht noch da
    assert storno_view(db, c.invoice, DISCOUNT_NUMBER) == (None, 0, True)


def test_a_discount_booked_by_the_column_alone_counts_even_without_a_row_on_the_discount_account(db):
    """Die Spalte allein genügt – auch wenn der Skonto-Satz gelöscht wurde (die Prüfliste nennt es: ``skonto_mismatch``)."""
    c = two_payments(db)
    raw(db, 'DELETE FROM Bookings WHERE ID = ?', c.kids_b[-1])

    assert storno_view(db, c.invoice, DISCOUNT_NUMBER)[2] is True
    assert [category for category, _text in findings(db, c.invoice)] == ['skonto_mismatch']


# ═════════════════════════════════════════════════════════════════════════════
# Die Vorprüfung der Buchungsmaske rechnet mit dem genauen Betrag
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('amount', ['116.625', '116.6249', '116.6251'])
def test_a_new_booking_whose_rest_is_no_whole_cent_amount_is_refused_before_it_is_created(tmp_db, amount):
    """Probe 3: 116,625 € lassen 2,375 € Rest – die Vorprüfung rundete den Betrag auf 116,63 € und ließ es durch, die Bewegung entstand, und die Zuordnung lehnte danach ab."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    before = installation.dump(tmp_db.db_name)

    assert handle_add_transaction(tmp_db, new_form(account, amount, invoice_id, skonto=['1'])) == (400, SKONTO_NOT_CENTS)

    assert installation.dump(tmp_db.db_name) == before                             # keine Bankbewegung, kein Satz, kein Konto
    assert raw(tmp_db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'bank'") == [(0,)] and assigned(tmp_db, invoice_id) == []


def test_the_precheck_says_what_the_assignment_says(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)

    assert tmp_db.payment_precheck(invoice_id, account, amount=D('116.625'), skonto=True) == SKONTO_NOT_CENTS
    assert tmp_db.payment_precheck(invoice_id, account, amount='116.625', skonto=True) == SKONTO_NOT_CENTS
    assert tmp_db.payment_precheck(invoice_id, account, amount=116.6249, skonto=True) == SKONTO_NOT_CENTS
    assert tmp_db.payment_precheck(invoice_id, account, amount=D('116.62'), skonto=True) is None
    assert tmp_db.payment_precheck(invoice_id, account, amount=D('116.625')) is None          # ohne Skonto zählt der Betrag für die Vorprüfung nicht


def test_without_the_tick_a_new_booking_with_a_fraction_of_a_cent_is_a_part_payment_as_before(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)

    assert handle_add_transaction(tmp_db, new_form(account, '116.625', invoice_id)) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(tmp_db, invoice_id) == [(1166250, None)] and status_of(tmp_db, invoice_id) == ('partial_payment', 23750)


def test_a_new_booking_with_a_whole_cent_rest_is_still_booked_with_the_discount(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)

    assert handle_add_transaction(tmp_db, new_form(account, '116.62', invoice_id, skonto=['1'])) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(tmp_db, invoice_id) == [(1166200, 23800)] and status_of(tmp_db, invoice_id) == ('paid', 0)
