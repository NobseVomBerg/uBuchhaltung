# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Auto-Abgleich: Ein Satz wird nur verknüpft, wenn seine Wirkung auf das Konto der Bankbewegung – mit
Vorzeichen – deren Betrag trifft (``link_effect``), bei einer Gruppe die Summe der Wirkungen.

Früher verglichen die Stufen 1–6 die Beträge ohne Vorzeichen (``ABS``) und nahmen auch Sätze, deren Geldseite
ein anderes Konto ist: Eine Lastschrift über −119 € wurde mit einem Barverkauf über +119 € verknüpft, und die
Split-Summe ging danach nicht auf. Hier steht je Stufe ein Aufbau in beiden Richtungen – die richtige wird
verknüpft, die falsche nie –, dazu die Geldseiten, die nie zur Bewegung gehören (Kasse, 1460, zweites eigenes
Bankkonto), Umbuchungen (zählen bei der Eindeutigkeit mit, werden nie allein verknüpft), Spiegel, Gruppen mit
einem Mitglied, das nicht dazugehört, und der Text der Bankzeile (Stufe 6 hat früher die Konto-ID statt des
Textes des Satzes übernommen).

Alle Namen und Beträge sind erfunden.
"""
from collections import namedtuple
from decimal import Decimal

import pytest

from db.matching import FORM_MIRROR_COA, FORM_PURPOSE_FIRST, FORM_TRANSFER
from money import from_minor
from tests.entry_form_helpers import bank_account, cash_account_id, coa, execute, query

DAY = '2026-03-05'


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Giro', 1800)
    bank_account(tmp_db, 'Giro Zwei', 1810)
    return tmp_db


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Giro')


def _bank(db, amount, text='', recipient='', docnr=None):
    """Bankbewegung auf dem Girokonto (SKR 1800)."""
    return db.insert_booking(DAY, Decimal(amount), account_id=_giro(db), recipient_client=recipient,
                             text=text, document_number=docnr, booking_type='bank')


def _entry(db, konto, gegen, amount, text='', docnr=None, recipient='', form=None, account_id=None):
    """Freier Buchungssatz; ``konto``/``gegen`` sind Kontonummern (``None`` = leer). Die Form leitet
    ``insert_booking`` aus den Konten ab, wo sie nicht genannt ist."""
    return db.insert_booking(DAY, Decimal(amount), account_id=account_id, coa_id=coa(db, konto),
                             counter_coa_id=coa(db, gegen), recipient_client=recipient, text=text,
                             document_number=docnr, booking_type='entry', entry_form=form)


def _kids(db, bank_id):
    return [row[0] for row in query(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', (bank_id,))]


def _parent(db, booking_id):
    return query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (booking_id,))[0][0]


def _text(db, booking_id):
    return query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (booking_id,))[0][0]


# ── Je Stufe ein Aufbau in beiden Richtungen ─────────────────────────────────
# ``right``: Die Bankbewegung geht in dieselbe Richtung wie die Sätze. Eine Szene sagt, welche Sätze beim
# richtigen Aufbau verknüpft werden (``linked``) und welche frei bleiben (``rest``). Jeder Aufbau ist so
# gebaut, dass vor der Richtungsregel nur die genannte Stufe verknüpfen konnte.

Scene = namedtuple('Scene', 'bank linked rest')


def _sign(right):
    return 1 if right else -1


def _stage_1(db, right):
    """Zwei gleiche Einnahmen, nur eine trägt den Empfänger der Bewegung (Stufe 2 sähe zwei Kandidaten)."""
    bank = _bank(db, str(_sign(right) * 119), text='Zahlung Rechnung', recipient='Muster GmbH')
    mine = _entry(db, 1800, 4400, '119', text='Erlös Muster', recipient='Muster GmbH')
    other = _entry(db, 1800, 4400, '119', text='Erlös Beispiel', recipient='Beispiel AG')
    return Scene(bank, [mine], [other])


def _stage_2(db, right):
    bank = _bank(db, str(_sign(right) * 119), text='Zahlung Rechnung')
    mine = _entry(db, 1800, 4400, '119', text='Erlös Muster')
    return Scene(bank, [mine], [])


def _stage_4(db, right):
    """Zwei gleiche Einnahmen mit verschiedenen Belegnummern; die Bewegung trägt die erste."""
    bank = _bank(db, str(_sign(right) * 119), text='Zahlung', docnr='B-1')
    first = _entry(db, 1800, 4400, '119', text='Erlös B-1', docnr='B-1')
    second = _entry(db, 1800, 4400, '119', text='Erlös B-2', docnr='B-2')
    return Scene(bank, [first], [second])


def _stage_3(db, right):
    """Eine Gruppe aus zwei Teilbeträgen desselben Belegs."""
    bank = _bank(db, str(_sign(right) * 100), text='Zahlung Sammelbeleg')
    part_a = _entry(db, 1800, 4400, '60', docnr='G-1')
    part_b = _entry(db, 1800, 4400, '40', docnr='G-1')
    return Scene(bank, [part_a, part_b], [])


def _stage_3b(db, right):
    """Rechnungs-Split: „Bank an Debitoren“ und die Umbuchung „Wartekonto an Erlöskonto“."""
    bank = _bank(db, str(_sign(right) * 119), text='Zahlung Rechnung')
    paid = _entry(db, 1800, 10000, '119', docnr='R-3B', text='Zahlung R-3B')
    clearing = _entry(db, 4405, 4400, '119', docnr='R-3B', text='Umbuchung R-3B')
    return Scene(bank, [paid, clearing], [])


def _stage_3c(db, right):
    """Privatanteil: zwei Ausgaben über 1800 und die Gegenbuchung des Privatanteils (positiv, Konto 2100)."""
    bank = _bank(db, str(-_sign(right) * 100), text='Abschlag Strom')
    business = _entry(db, 6325, 1800, '-60', docnr='P-1')
    other = _entry(db, 6815, 1800, '-40', docnr='P-1')
    private = _entry(db, 2100, 6325, '15', docnr='P-1')
    return Scene(bank, [business, other, private], [])


def _stage_3d(db, right):
    """Sammelzahlung: zwei Rechnungsnummern im Text der Bewegung, die Sätze liegen in verschiedenen Belegen."""
    bank = _bank(db, str(_sign(right) * 300), text='Rechnungen 20270001, 20270002')
    first = _entry(db, 1800, 4400, '100', docnr='20270001')
    second = _entry(db, 1800, 4400, '200', docnr='20270002')
    return Scene(bank, [first, second], [])


def _stage_5(db, right):
    """Zwei gleiche Einnahmen; nur einer trägt die lange Ziffernfolge aus dem Text der Bewegung."""
    bank = _bank(db, str(_sign(right) * 119), text='Beleg 1040749116593')
    mine = _entry(db, 1800, 4400, '119', text='Rechnung1040749116593 Büro')
    other = _entry(db, 1800, 4400, '119', text='Sonstiges')
    return Scene(bank, [mine], [other])


def _stage_6(db, right):
    """Zwei gleiche Privatentnahmen ohne Empfänger, Belegnummer und Ziffernfolge; einer mit ähnlichem Text."""
    bank = _bank(db, str(-_sign(right) * 50), text='Mitgliedsbeitrag Verein')
    mine = _entry(db, 2100, 1800, '-50', text='Mitgliedsbeitrag Verein Jahr')
    other = _entry(db, 2100, 1800, '-50', text='Strom Abschlag')
    return Scene(bank, [mine], [other])


STAGES = {'stufe-1': _stage_1, 'stufe-2': _stage_2, 'stufe-4': _stage_4, 'stufe-3': _stage_3,
          'stufe-3b': _stage_3b, 'stufe-3c': _stage_3c, 'stufe-3d': _stage_3d, 'stufe-5': _stage_5,
          'stufe-6': _stage_6}


@pytest.mark.parametrize('stage', STAGES)
def test_a_movement_in_the_right_direction_is_linked_on_every_stage(db, stage):
    scene = STAGES[stage](db, right=True)

    result = db.link_bank_to_entries()

    assert result['linked'] == 1
    assert _kids(db, scene.bank) == sorted(scene.linked)
    assert all(_parent(db, entry) is None for entry in scene.rest)


@pytest.mark.parametrize('stage', STAGES)
def test_a_movement_in_the_wrong_direction_is_never_linked_on_any_stage(db, stage):
    """Dieselben Sätze, die Bewegung geht in die andere Richtung: nichts wird verknüpft, der Text der Bankzeile
    bleibt, die Bewegung bleibt offen."""
    scene = STAGES[stage](db, right=False)
    text = _text(db, scene.bank)

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert result['skipped'] == 1
    assert _kids(db, scene.bank) == []
    assert _text(db, scene.bank) == text
    assert all(_parent(db, entry) is None for entry in scene.linked + scene.rest)


# ── Die Fälle der Analyse ────────────────────────────────────────────────────

def test_a_direct_debit_is_not_linked_to_a_cash_sale(db):
    """Lastschrift −119 gegen einen Barverkauf „1600 an 4400“ über +119: Die Kasse ist eine andere Geldseite."""
    bank = _bank(db, '-119', text='Lastschrift Muster')
    sale = _entry(db, 1600, 4400, '119', text='Barverkauf')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert _kids(db, bank) == []
    assert _parent(db, sale) is None
    assert _text(db, bank) == 'Lastschrift Muster'


def test_a_receipt_is_linked_to_its_booking_and_takes_over_the_text(db):
    bank = _bank(db, '119', text='Zahlung Rechnung')
    entry = _entry(db, 1800, 4400, '119', text='Erlös Muster')

    result = db.link_bank_to_entries()

    assert result['linked'] == 1
    assert _kids(db, bank) == [entry]
    assert _text(db, bank) == 'Erlös Muster'


@pytest.mark.parametrize('counter, linked', [
    pytest.param(1800, True, id='konto-der-bewegung'),
    pytest.param(1600, False, id='kasse'),
    pytest.param(1460, False, id='verrechnungskonto'),
    pytest.param(1810, False, id='zweites-bankkonto'),
])
def test_an_expense_is_linked_only_when_the_money_side_is_the_account_of_the_movement(db, counter, linked):
    """„6815 an <Geldseite>“ über −119 gegen eine Lastschrift über −119: gleiches Vorzeichen, gleicher Betrag –
    entscheidend ist allein, ob die Geldseite das Konto der Bewegung ist."""
    bank = _bank(db, '-119', text='Lastschrift Muster')
    entry = _entry(db, 6815, counter, '-119', text='Einkauf Büro')

    result = db.link_bank_to_entries()

    assert result['linked'] == (1 if linked else 0)
    assert _kids(db, bank) == ([entry] if linked else [])
    assert _text(db, bank) == ('Einkauf Büro' if linked else 'Lastschrift Muster')


def test_a_transfer_without_a_money_side_is_never_linked_alone(db):
    """„4405 an 4400“ über +119 gegen eine Einnahme über +119: Die Umbuchung bewegt kein Geld."""
    bank = _bank(db, '119', text='Zahlung')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert _parent(db, transfer) is None


def test_a_depreciation_booking_is_not_linked_to_a_movement_of_the_same_amount_and_day(db):
    """„Aufwand an Anlagekonto“ (AfA, 31.12.) bewegt kein Geld: Eine gleich hohe Lastschrift am selben Tag nimmt
    sie nicht auf (früher hängte der Abgleich sie dort an)."""
    asset = db.insert_asset('Laptop', '2026-01-10', 1800.0, 3, coa_id=coa(db, 520))
    depreciation = db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
    date, amount = query(db, 'SELECT DateBooking, Amount FROM Bookings WHERE ID = ?', (depreciation,))[0]
    bank = db.insert_booking(date, from_minor(-amount), account_id=_giro(db), text='Lastschrift',
                             booking_type='bank')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []
    assert _parent(db, depreciation) is None


def test_stage_2_does_not_link_a_payment_alone_when_a_transfer_of_the_same_amount_stands_beside_it(db):
    """Ohne Belegnummer lässt sich nicht erkennen, dass die Umbuchung zur Zahlung gehört: Stufe 2 sieht zwei
    Kandidaten und verknüpft keinen (die Zahlung allein ließe die Umbuchung frei). Die Bewegung hat keinen
    Text, so greifen auch die Stufen 5 und 6 nicht."""
    bank = _bank(db, '119')
    paid = _entry(db, 1800, 10000, '119', text='Zahlung')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert _kids(db, bank) == []
    assert _parent(db, paid) is None and _parent(db, transfer) is None


def test_a_wrong_way_twin_does_not_keep_the_right_one_from_being_linked(db):
    """Eine Gutschrift in der Gegenrichtung (−119) ist kein Kandidat der Einnahme über +119 – der eindeutige
    Treffer wird verknüpft (früher standen zwei Kandidaten da, und nichts wurde verknüpft)."""
    bank = _bank(db, '119', text='Zahlung')
    right = _entry(db, 1800, 4400, '119', text='Erlös Muster')
    wrong = _entry(db, 1800, 4400, '-119', text='Erlösminderung')

    result = db.link_bank_to_entries()

    assert result['linked'] == 1
    assert _kids(db, bank) == [right]
    assert _parent(db, wrong) is None


def test_a_set_on_another_money_side_does_not_keep_the_right_one_from_being_linked(db):
    """Ein Barverkauf („1600 an 4400“) desselben Betrags und Tags gehört nie zu dieser Bewegung: Er zählt bei der
    Eindeutigkeit nicht mit, die Einnahme über das Konto der Bewegung ist der eindeutige Treffer."""
    bank = _bank(db, '119', text='Zahlung')
    right = _entry(db, 1800, 4400, '119', text='Erlös Muster')
    cash = _entry(db, 1600, 4400, '119', text='Barverkauf')

    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == [right]
    assert _parent(db, cash) is None


def test_a_mirror_is_linked_by_its_position_on_the_account_of_the_movement(db):
    """„1460 an 1800“ über +100 (``mirror_coa``): 1800 steht im Haben, also −100 auf dem Konto der Bewegung."""
    bank = _bank(db, '-100', text='Übertrag')
    mirror = _entry(db, 1460, 1800, '100', text='Übertrag', form=FORM_MIRROR_COA)

    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == [mirror]


def test_a_mirror_in_the_wrong_direction_is_not_linked(db):
    bank = _bank(db, '100', text='Übertrag')
    _entry(db, 1460, 1800, '100', text='Übertrag', form=FORM_MIRROR_COA)

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []


def test_a_legacy_mirror_is_never_linked_automatically(db):
    """Die Lesart des Betrags ist unbekannt – dieselbe Zeile wie oben, nur als ``mirror_legacy``."""
    bank = _bank(db, '-100', text='Übertrag')
    mirror = _entry(db, 1460, 1800, '100', text='Übertrag', form=FORM_MIRROR_COA)
    execute(db, "UPDATE Bookings SET EntryForm = 'mirror_legacy' WHERE ID = ?", (mirror,))

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []


# ── Gruppen ──────────────────────────────────────────────────────────────────

def test_a_group_with_a_member_on_another_money_side_is_not_linked(db):
    """Teilbeträge desselben Belegs, einer davon aus der Kasse: Die Summe der Beträge träfe die Bewegung, die
    Summe der Wirkungen nicht – und der Satz der Kasse gehört nie zu dieser Bewegung."""
    bank = _bank(db, '-100', text='Zahlung Sammelbeleg')
    on_bank = _entry(db, 6815, 1800, '-60', docnr='G-2')
    in_cash = _entry(db, 6815, 1600, '-40', docnr='G-2')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, on_bank) is None and _parent(db, in_cash) is None


def test_an_invoice_split_with_a_member_on_another_money_side_is_not_linked(db):
    """Stufe 3b: Ein Satz des Belegs liegt auf der Kasse („1600 an 4400“). Er gehört nie zu dieser Bewegung, und
    die Gruppe wird nicht ohne ihn verknüpft – selbst wenn die übrigen Mitglieder die Bewegung träfen."""
    bank = _bank(db, '119')
    paid = _entry(db, 1800, 10000, '119', docnr='R-8', text='Zahlung R-8')
    transfer = _entry(db, 4405, 4400, '119', docnr='R-8', text='Umbuchung R-8')
    in_cash = _entry(db, 1600, 4400, '119', docnr='R-8', text='Barverkauf R-8')

    assert db.link_bank_to_entries()['linked'] == 0
    assert all(_parent(db, entry) is None for entry in (paid, transfer, in_cash))


def test_a_group_of_transfers_only_is_never_linked(db):
    """Zwei Umbuchungen, deren Beträge zusammen die Bewegung treffen: Nichts davon bewegt Geld."""
    bank = _bank(db, '119', text='Zahlung')
    first = _entry(db, 4405, 4400, '60', docnr='U-1')
    second = _entry(db, 4405, 4400, '59', docnr='U-1')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, first) is None and _parent(db, second) is None


def test_a_group_of_transfers_is_not_linked_even_to_a_movement_below_the_tolerance(db):
    """Die Summe der Wirkungen (0) liegt hier innerhalb der Toleranz des Abgleichs (50 Minor Units, ein halber
    Cent) um den Betrag der Bewegung – trotzdem bewegt keines der Mitglieder Geld."""
    bank = _bank(db, '0.0030', text='Zinsen')
    first = _entry(db, 4405, 4400, '0.0020', docnr='U-2')
    second = _entry(db, 4405, 4400, '0.0010', docnr='U-2')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []
    assert _parent(db, first) is None and _parent(db, second) is None


def test_a_collective_payment_of_expenses_is_linked(db):
    """Stufe 3d für Ausgaben (Zweckkonto an Bank): keine Bank-Seite im Satz, die Summe aller Belege gilt."""
    bank = _bank(db, '-300', text='Rechnungen 20270001, 20270002')
    first = _entry(db, 6815, 1800, '-100', docnr='20270001')
    second = _entry(db, 6815, 1800, '-200', docnr='20270002')

    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == sorted([first, second])


def test_the_invoice_split_of_a_receipt_links_both_sets(db):
    """Stufe 3b: „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ gehören zusammen zur Einnahme."""
    bank = _bank(db, '119', text='Zahlung Rechnung')
    paid = _entry(db, 1800, 10000, '119', docnr='R-9', text='Zahlung R-9')
    transfer = _entry(db, 4405, 4400, '119', docnr='R-9', text='Umbuchung R-9')

    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == sorted([paid, transfer])


# ── Stufe 1: Token-Treffer, Reihenfolge, Umbuchung ───────────────────────────

def test_stage_1_takes_no_other_set_when_the_token_hit_is_a_transfer(db):
    """Zwei Treffer desselben Empfängers: Der mit der Kennung aus dem Text ist eine Umbuchung. Stufe 1 nimmt
    dann nicht einfach den anderen Satz des Empfängers – er kann zu einem anderen Beleg gehören."""
    bank = _bank(db, '119', text='Rechnung 1040749116593', recipient='Muster GmbH')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung 1040749116593', recipient='Muster GmbH')
    paid = _entry(db, 1800, 4400, '119', text='Zahlung', recipient='Muster GmbH')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []
    assert _parent(db, transfer) is None and _parent(db, paid) is None


def test_stage_1_prefers_the_token_hit_when_it_fits(db):
    bank = _bank(db, '119', text='Rechnung 1040749116593', recipient='Muster GmbH')
    _entry(db, 1800, 4400, '119', text='Zahlung', recipient='Muster GmbH')
    marked = _entry(db, 1800, 4400, '119', text='Zahlung 1040749116593', recipient='Muster GmbH')

    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == [marked]


def test_stage_1_with_only_a_transfer_links_nothing_and_goes_on(db):
    """Der einzige Treffer des Empfängers ist eine Umbuchung: Stufe 1 verknüpft ihn nicht, die weiteren Stufen
    sehen ihn weiter als Kandidaten – und verknüpfen ihn ebenso wenig."""
    bank = _bank(db, '119', text='Zahlung', recipient='Muster GmbH')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung', recipient='Muster GmbH')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == [] and _parent(db, transfer) is None


# ── Umbuchungen: auch die Stufen 4, 5 und 6 verknüpfen sie nie allein ───────

def test_stage_4_does_not_link_a_transfer_alone_when_the_document_number_hits_it(db):
    bank = _bank(db, '119', docnr='B-1')
    paid = _entry(db, 1800, 4400, '119', docnr='B-2', text='Erlös B-2')
    transfer = _entry(db, 4405, 4400, '119', docnr='B-1', text='Umbuchung B-1')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, paid) is None and _parent(db, transfer) is None


def test_stage_5_does_not_link_a_transfer_alone_when_the_token_hits_it(db):
    bank = _bank(db, '119', text='Beleg 1040749116593')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung 1040749116593')
    paid = _entry(db, 1800, 4400, '119', text='Sonstiges')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, paid) is None and _parent(db, transfer) is None


def test_stage_6_does_not_link_a_transfer_alone_when_its_text_is_the_most_similar(db):
    bank = _bank(db, '119', text='Umbuchung Rechnung')
    transfer = _entry(db, 4405, 4400, '119', text='Umbuchung Rechnung Muster')
    paid = _entry(db, 1800, 4400, '119', text='Strom Abschlag')

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, paid) is None and _parent(db, transfer) is None


# ── Eigenes Konto am Satz ────────────────────────────────────────────────────

def test_a_set_that_belongs_to_another_account_is_never_linked(db):
    """Ein eigenständiger Satz auf der Kasse („6815“, Konto der Kasse) trägt ein Account_ID – er gehört nur zu
    einer Bewegung desselben Kontos. Ohne Account_ID wäre er ein unvollständig kontierter Satz und passte."""
    bank = _bank(db, '-119', text='Lastschrift Muster')
    on_cash = _entry(db, 6815, None, '-119', text='Barausgabe', account_id=cash_account_id(db))

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, on_cash) is None

    execute(db, 'UPDATE Bookings SET Account_ID = NULL WHERE ID = ?', (on_cash,))
    assert db.link_bank_to_entries()['linked'] == 1
    assert _kids(db, bank) == [on_cash]


# ── Text der Bankzeile (Stufe 6) ─────────────────────────────────────────────

def test_stage_6_takes_over_the_text_of_the_set_and_not_its_account_id(db):
    scene = _stage_6(db, right=True)

    assert db.link_bank_to_entries()['linked'] == 1

    text = _text(db, scene.bank)
    assert text == 'Mitgliedsbeitrag Verein Jahr'
    assert not text.isdigit()


# ── Was schon verknüpft ist, bleibt ──────────────────────────────────────────

def test_a_link_set_by_hand_and_the_bank_text_survive_a_rerun(db):
    """Auch eine falsch gerichtete, von Hand gesetzte Verknüpfung: Der Abgleich fasst nur Bewegungen ohne Kinder
    und Sätze ohne Eltern an."""
    bank = _bank(db, '-119', text='Lastschrift Muster')
    entry = _entry(db, 1800, 4400, '119', text='Barverkauf')
    execute(db, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE ID = ?', (bank, entry))

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert _kids(db, bank) == [entry]
    assert _text(db, bank) == 'Lastschrift Muster'


def test_a_second_run_changes_nothing(db):
    scene = _stage_2(db, right=True)
    assert db.link_bank_to_entries()['linked'] == 1
    state = query(db, 'SELECT ID, ParentBooking_ID, Text FROM Bookings ORDER BY ID')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert query(db, 'SELECT ID, ParentBooking_ID, Text FROM Bookings ORDER BY ID') == state
    assert _kids(db, scene.bank) == scene.linked


# ── Das Konto der Bewegung auf der Seite, die die Form nicht als Geldseite führt ─────────────────
# Der Betrag sagt dort nicht die Richtung der Geldbewegung: ``bank_effect`` zählt ihn wie gespeichert, die
# wirkliche Wirkung wäre das Gegenteil. Solche Sätze gehören zu keiner Bewegung – in keiner Richtung.

@pytest.mark.parametrize('konto, gegen, form', [
    (6815, 1800, FORM_TRANSFER),          # importiert, bevor 1800 ein Bankkonto war: „Konto im Soll“, +119
    (6815, None, FORM_TRANSFER),          # Gegenkonto leer oder unbekannt
    (1800, None, FORM_PURPOSE_FIRST),     # das Konto der Bewegung steht vorn, die Geldseite (hinten) ist leer
    (1800, 4400, FORM_PURPOSE_FIRST),
], ids=['umbuchung-an-bank', 'umbuchung-an-leer', 'bank-vorn-geldseite-leer', 'bank-vorn-geldseite-zweck'])
@pytest.mark.parametrize('movements', [('-119',), ('119',), ('-119', '119')], ids=['lastschrift', 'gutschrift', 'beide'])
def test_a_row_with_the_account_on_the_wrong_side_of_its_form_is_linked_in_no_direction(db, konto, gegen, form,
                                                                                         movements):
    banks = [_bank(db, amount, text='Bewegung') for amount in movements]
    entry = _entry(db, konto, gegen, '119', text='Satz', form=form)

    assert db.link_bank_to_entries()['linked'] == 0
    assert _parent(db, entry) is None
    assert all(_kids(db, bank) == [] for bank in banks)


# ── Ein Betrag, der nicht als Zahl gespeichert ist ───────────────────────────

def test_a_row_whose_amount_is_stored_as_text_is_never_a_candidate_and_stops_nothing(db):
    """Ein Eingriff von Hand (SQL-Konsole, zurückgespielte Sicherung) kann Text in ``Amount`` hinterlassen. Der
    Abgleich läuft nach jedem Import: Er rechnet mit so einer Zeile nicht – und bricht an ihr nicht ab."""
    bank = _bank(db, '119', text='Zahlung')
    odd = _entry(db, 1800, 4400, '119', text='Erlös')
    execute(db, "UPDATE Bookings SET Amount = '1190000x' WHERE ID = ?", (odd,))
    other_bank = _bank(db, '50', text='Zahlung Zwei')
    good = _entry(db, 1800, 4400, '50', text='Erlös Zwei')

    result = db.link_bank_to_entries()

    assert _parent(db, odd) is None and _kids(db, bank) == []
    assert _kids(db, other_bank) == [good]
    assert result['linked'] == 1


def test_a_group_with_a_member_whose_amount_is_stored_as_text_is_not_linked(db):
    bank = _bank(db, '140', text='Zahlung Sammelbeleg')
    members = [_entry(db, 1800, 4400, amount, docnr='G-9') for amount in ('60', '40', '40')]
    execute(db, "UPDATE Bookings SET Amount = '40,00' WHERE ID = ?", (members[2],))

    assert db.link_bank_to_entries()['linked'] == 0
    assert _kids(db, bank) == []


# ── Stufe 1: Die Kennung zeigt auf die Umbuchung eines Belegs ────────────────

def test_stage_1_leaves_the_document_of_the_token_hit_to_the_group_stage(db):
    """Bewegung +119 mit der Kennung von Beleg 011. Desselben Empfängers: die Zahlung von Beleg 022 (ältere ID),
    die Umbuchung und die Zahlung von Beleg 011. Verknüpft wird der Beleg 011 als Gruppe – nicht die Zahlung
    eines anderen Belegs, nur weil sie zuerst kommt."""
    bank = _bank(db, '119', text='RE 202600011', recipient='Muster GmbH')
    other_paid = _entry(db, 1800, 10000, '119', docnr='202600022', text='Zahlung', recipient='Muster GmbH')
    clearing = _entry(db, 4405, 4400, '119', docnr='202600011', text='Umbuchung 202600011',
                      recipient='Muster GmbH')
    paid = _entry(db, 1800, 10000, '119', docnr='202600011', text='Zahlung', recipient='Muster GmbH')

    db.link_bank_to_entries()

    assert _parent(db, other_paid) is None
    assert sorted(_kids(db, bank)) == sorted([clearing, paid])


# ── Die Zusage: Jede automatische Verknüpfung ergibt eine ausgeglichene Split-Summe ─────────────

@pytest.mark.parametrize('stage', STAGES)
def test_every_link_of_the_matching_leaves_a_balanced_split_sum(db, stage):
    scene = STAGES[stage](db, right=True)

    assert db.link_bank_to_entries()['linked'] >= 1
    assert _kids(db, scene.bank) != []
    assert db.find_unbalanced_splits(DAY, DAY) == []
