# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Buchungssatz-Editor der Bankbewegung: Die Sätze einer Rechnungszahlung sind schreibgeschützt (2.9.2).

Ein Satz unter einer Bankbewegung mit der Marke ``Status = 'invoice_payment'`` (``PAYMENT_MARK``) gehört einer Rechnungszahlung oder Erstattung. Der Editor schreibt von ihm
Betrag, Konto, Gegenkonto, Steuersatz, Steuerbetrag, Text, Beleg-Nr. und Währung nie – gleich, was das Formular schickt: Maßgeblich ist die gespeicherte Marke, nie ein Feld, und ein
Unsinn in diesen Feldern lehnt nicht ab (sie werden nicht gelesen). Datum, Steuerdatum und Empfänger folgen wie bei jedem Satz der Bankbewegung, wenn er sie mit ihr geteilt hatte. Das ✕
entfernt weiter die ganze Zuordnung. Die Maske zeigt die Zeile gesperrt (``readonly``, das Konto ``disabled`` mit einem versteckten Feld ``split_coa``) und führt je Zeile alle Felder:
Gelesen wird über den Index.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``). Alle Namen und Werte sind erfunden.
"""
import json
import shutil
import subprocess
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from db.invoice_payments import PAYMENT_MARK
from money import to_minor
from server.handlers import (INVALID_REQUEST, PAYMENT_ROW_READONLY, handle_add_transaction, handle_delete_transaction, link_booking_to_invoice_capped)
from server.pages_transactions import PageTransactions
from tests.dom_helpers import Dom
from tests.entry_form_helpers import bank_account, coa, execute, query
from tests.entry_form_roundtrip import add_row, drop_row, editor_rest, open_form, row_fields, snapshot, submit

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – nur der Lauf des Skripts entfällt')

HINT = 'Rechnungszahlung – hier nicht änderbar'
REMOVE_TITLE = 'Zuordnung der Zahlung entfernen – alle Sätze dieser Zahlung gehen mit'
ROW_FIELDS = ['split_id', 'split_text', 'split_amount', 'split_coa', 'split_tax_rate', 'split_tax_amount', 'split_docnr', 'split_counter_coa']
NONSENSE = dict(split_amount='abc', split_coa='kein Konto', split_counter_coa='x', split_tax_rate='zehn', split_tax_amount='nan')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Sperre-Bank', 1800)
    return tmp_db


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Sperre-Bank')


def _invoice(db, number='RE-SPERRE', gross=119.00, **over):
    """Eine versendete Rechnung (19 %) samt Forderungsbuchung, wie beim Versenden."""
    net = round(gross / 1.19, 2)
    data = {'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31', 'seller_name': 'Vera Verkäufer',
            'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
            'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': 'sent'}
    data.update(over)
    invoice_id = db.insert_invoice(data)
    db.ensure_receivable_booking(invoice_id)
    return invoice_id


def _movement(db, amount, date='2026-02-01'):
    return db.insert_booking(date, Decimal(amount), account_id=_giro(db), booking_type='bank', recipient_client='Käufer AG')


def _ids(db, sql, *params):
    return [row[0] for row in query(db, sql, params)]


def _marked(db, bank):
    return _ids(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? ORDER BY ID', bank, PAYMENT_MARK)


def _children(db, bank):
    return _ids(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)


def _with_a_datev_key(db, row_ids):
    """Einen mitgebrachten BU-Schlüssel an die Sätze legen: Er darf sich nur ändern, wenn sich Konto, Gegenkonto oder Steuersatz ändern – und die schreibt der Editor hier nie."""
    for row_id in row_ids:
        execute(db, 'UPDATE Bookings SET DatevKey = 3 WHERE ID = ?', (row_id,))


def _paid_with_an_ordinary_row(db):
    """Eine Bankbewegung über 150,00 €: 119,00 € sind der Rechnung zugeordnet („Bank an Debitoren“ und „Wartekonto an Erlöskonto“, beide markiert), 31,00 € ein gewöhnlicher
    Satz aus dem Editor. Liefert (Rechnung, Bankbewegung, die markierten Sätze, der gewöhnliche Satz)."""
    invoice = _invoice(db)
    bank = _movement(db, '150.00')
    assert link_booking_to_invoice_capped(db, invoice, bank) == (True, None)
    post, _options = open_form(db, bank)
    submit(db, add_row(post, split_amount='31.00', split_coa=str(coa(db, 6815)), split_text='Versand', split_docnr='V-1'))
    marked = _marked(db, bank)
    (ordinary,) = [row_id for row_id in _children(db, bank) if row_id not in marked]
    assert len(marked) == 2
    return invoice, bank, marked, ordinary


def _paid_with_a_discount(db):
    """Eine Rechnung über 119,00 € mit Skonto, bezahlt mit 116,62 €: drei markierte Sätze („Bank an Debitoren“, „Wartekonto an Erlöskonto“, „Skonto-Konto an Bank“)."""
    invoice = _invoice(db, 'RE-SKONTO', skonto_days=14, skonto_percent=2.0)
    bank = _movement(db, '116.62')
    assert link_booking_to_invoice_capped(db, invoice, bank, skonto=True) == (True, None)
    marked = _marked(db, bank)
    assert len(marked) == 3 and _children(db, bank) == marked
    return invoice, bank, marked


def _edit(post, row_id, **values):
    """Die Felder einer Zeile des Formulars ändern, wie wenn der Browser (oder ein Angreifer) sie anders schickt."""
    result = {name: list(items) for name, items in post.items()}
    index = result['split_id'].index(str(row_id))
    for name, value in values.items():
        result[name][index] = str(value)
    return result


def _changed(before, after):
    return [row_id for row_id in sorted(set(before) | set(after)) if before.get(row_id) != after.get(row_id)]


class _EditorRows(HTMLParser):
    """Die Zeilen der Tabelle ``#splitTable``: Klassen, Felder (Tag und Attribute) und Texte der Hinweise ``splitHint`` sowie der Titel des ✕."""

    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._hint = [], None, None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'tr':
            self._row = {'class': (a.get('class') or '').split(), 'fields': [], 'hints': [], 'remove': None}
            self.rows.append(self._row)
        elif self._row is None:
            return
        elif tag in ('input', 'select'):
            self._row['fields'].append((tag, a))
        elif tag == 'div' and 'splitHint' in (a.get('class') or '').split():
            self._hint = ''
        elif tag == 'a' and 'delete-icon' in (a.get('class') or '').split():
            self._row['remove'] = a.get('title')

    def handle_data(self, data):
        if self._hint is not None:
            self._hint += data

    def handle_endtag(self, tag):
        if tag == 'div' and self._hint is not None:
            self._row['hints'].append(self._hint)
            self._hint = None
        elif tag == 'tr':
            self._row = None


def _editor_rows(page):
    """Die Zeilen des Editors (ohne Kopfzeile) – die Vorlage ``splitTemplate`` ist die letzte."""
    return [row for row in _EditorRows(page).rows if 'splitRow' in row['class'] or 'splitTemplate' in row['class']]


def _named(row, name):
    return [(tag, attrs) for tag, attrs in row['fields'] if attrs.get('name') == name]


def _locked_inputs(row):
    """Haben alle sichtbaren Eingaben der Zeile ``readonly`` und keine ein Skript (``oninput``)?"""
    return all('readonly' in _named(row, name)[0][1] and 'oninput' not in _named(row, name)[0][1]
               for name in ('split_text', 'split_amount', 'split_tax_rate', 'split_tax_amount', 'split_docnr'))


# ── Gespeichert wird nichts von einem Zahlungssatz ───────────────────────────

def test_saving_changed_values_in_the_rows_of_a_payment_changes_no_column_of_them_but_those_of_the_ordinary_row(db):
    invoice, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    _with_a_datev_key(db, marked)
    before = snapshot(db)
    payments = query(db, 'SELECT * FROM InvoicePayments ORDER BY ID')
    post, _options = open_form(db, bank)
    for row_id in marked:
        post = _edit(post, row_id, split_amount='1.00', split_coa=coa(db, 6300), split_counter_coa=coa(db, 6815), split_tax_rate='7', split_tax_amount='0.07',
                     split_text='von Hand', split_docnr='X-1')
    post = _edit(post, ordinary, split_amount='30.00', split_text='Porto')

    submit(db, post)                                                                # Erfolg: 303, keine Ablehnung

    after = snapshot(db)
    assert all(after[row_id] == before[row_id] for row_id in marked)                # jede Spalte beider Sätze, auch Form und BU-Schlüssel
    assert [after[row_id]['DatevKey'] for row_id in marked] == [3, 3]
    assert _changed(before, after) == [ordinary]                                    # der gewöhnliche Satz derselben Bewegung wurde im selben Speichern geändert
    assert (after[ordinary]['Amount'], after[ordinary]['Text']) == (to_minor(Decimal('30.00')), 'Porto')
    assert query(db, 'SELECT * FROM InvoicePayments ORDER BY ID') == payments


def test_the_stored_mark_decides_not_the_form(db):
    """Ein Satz ohne Marke (älterer Stand) bleibt im Editor änderbar; wer die Marke trägt, ist gesperrt – ein Formular kann daran nichts ändern."""
    _invoice_id, bank, marked, _ordinary = _paid_with_an_ordinary_row(db)
    execute(db, 'UPDATE Bookings SET Status = NULL WHERE ID = ?', (marked[0],))
    before = snapshot(db)
    post, _options = open_form(db, bank)
    post = _edit(_edit(post, marked[0], split_text='Anders'), marked[1], split_text='Anders')

    submit(db, post)

    after = snapshot(db)
    assert after[marked[0]]['Text'] == 'Anders' and after[marked[1]] == before[marked[1]]
    assert _changed(before, after) == [marked[0]]


def test_the_date_the_tax_date_and_the_recipient_follow_the_bank_movement_as_before(db):
    """Datum, Steuerdatum und Empfänger folgen der Bankbewegung nach der „geteilt“-Regel, je Feld: Ein Satz mit eigenem Wert behält ihn."""
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    execute(db, 'UPDATE Bookings SET RecipientClient = ? WHERE ID = ?', ('Eigener Empfänger', marked[1]))
    _with_a_datev_key(db, marked)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    post['recipient'], post['date_tax'] = ['Käufer AG neu'], ['2026-02-03']

    submit(db, post)

    after = snapshot(db)
    assert (after[marked[0]]['RecipientClient'], after[marked[0]]['DateTax']) == ('Käufer AG neu', '2026-02-03')                # geteilt: folgt
    assert (after[marked[1]]['RecipientClient'], after[marked[1]]['DateTax']) == ('Eigener Empfänger', '2026-02-03')            # eigener Empfänger bleibt, das Steuerdatum folgt
    assert (after[ordinary]['RecipientClient'], after[ordinary]['DateTax']) == ('Käufer AG neu', '2026-02-03')                  # wie jeder Satz
    for row_id in marked:                                                                                                       # alles andere bleibt, auch Form und BU-Schlüssel
        assert {c: v for c, v in after[row_id].items() if c not in ('RecipientClient', 'DateTax')} == \
               {c: v for c, v in before[row_id].items() if c not in ('RecipientClient', 'DateTax')}
    assert [after[row_id]['DatevKey'] for row_id in marked] == [3, 3]


def test_nonsense_in_the_fields_of_a_payment_row_does_not_refuse_and_is_not_even_read(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    for row_id in marked:
        post = _edit(post, row_id, **NONSENSE)

    submit(db, post)                                                                # keine Ablehnung, kein neuer Fehlertext

    assert snapshot(db) == before                                                   # gespeichert bleibt, was gespeichert ist
    # derselbe Unsinn in der Zeile eines gewöhnlichen Satzes lehnt weiter ab
    status, message = handle_add_transaction(db, _edit(open_form(db, bank)[0], ordinary, split_amount='abc'))
    assert (status, message) == (400, 'Zeile 3: Der Betrag ist keine gültige Zahl.') and snapshot(db) == before


def test_a_payment_row_never_triggers_the_same_account_refusal(db):
    """„Wartekonto an Erlöskonto“ mit dem Erlöskonto als Konto der Zeile ergäbe zweimal dasselbe Konto und lehnte bei einer gewöhnlichen Zeile ab – bei einem Zahlungssatz wird
    das Konto nicht einmal gelesen."""
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    (counter,) = _ids(db, 'SELECT CounterCOA_ID FROM Bookings WHERE ID = ?', marked[1])

    submit(db, _edit(post, marked[1], split_coa=counter))

    assert snapshot(db) == before
    status, message = handle_add_transaction(db, _edit(post, ordinary, split_coa=coa(db, 1800)))        # der gewöhnliche Satz mit dem Konto der Bank
    assert status == 400 and 'dasselbe Konto' in message


def test_the_rows_of_an_assignment_with_a_discount_are_locked_too(db):
    _invoice_id, bank, marked = _paid_with_a_discount(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    for row_id in marked:
        post = _edit(post, row_id, split_amount='1.00', split_coa=coa(db, 6300), split_tax_rate='7', split_tax_amount='0.07', split_text='von Hand', split_docnr='X-1')

    submit(db, post)

    assert snapshot(db) == before
    rows = _editor_rows(PageTransactions(db, edit_transaction_id=bank))
    assert [row['class'] for row in rows] == [['splitRow', 'splitLocked']] * 3 + [['splitTemplate']]
    assert all(_locked_inputs(row) for row in rows[:3])


def test_nonsense_in_the_rows_of_an_assignment_with_a_discount_does_not_refuse(db):
    _invoice_id, bank, marked = _paid_with_a_discount(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    for row_id in marked:
        post = _edit(post, row_id, **NONSENSE)
    submit(db, post)
    assert snapshot(db) == before


# ── Das ✕ bleibt ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('which', [0, 1], ids=['Bank-an-Debitoren', 'Wartekonto-an-Erloeskonto'])
def test_the_x_of_a_payment_row_still_removes_the_whole_assignment(db, which):
    invoice, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    post, _options = open_form(db, bank)

    submit(db, drop_row(post, marked[which]))

    assert _children(db, bank) == [ordinary]                                        # beide Sätze der Zahlung sind weg, der gewöhnliche bleibt
    assert query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]
    assert query(db, 'SELECT Status FROM Invoices WHERE ID = ?', (invoice,)) == [('sent',)]


def test_the_x_of_a_row_of_an_assignment_with_a_discount_removes_all_three_rows(db):
    _invoice_id, bank, marked = _paid_with_a_discount(db)
    post, _options = open_form(db, bank)

    submit(db, drop_row(post, marked[2]))

    assert _children(db, bank) == [] and query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]


# ── Die Maske ────────────────────────────────────────────────────────────────

def test_the_mask_shows_the_rows_of_a_payment_locked_and_carries_every_field_in_every_row(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    page = PageTransactions(db, edit_transaction_id=bank)
    rows = _editor_rows(page)
    assert [row['class'] for row in rows] == [['splitRow', 'splitLocked'], ['splitRow', 'splitLocked'], ['splitRow'], ['splitTemplate']]
    locked, free, template = rows[:2], rows[2], rows[3]

    for row in locked:
        assert _locked_inputs(row)                                                                    # schreibgeschützt, und kein Skript beschreibt die Felder
        ((_tag, hidden),) = _named(row, 'split_coa')                                                  # das Konto: genau EIN Feld dieses Namens, versteckt …
        assert hidden['type'] == 'hidden'
        selects = [attrs for tag, attrs in row['fields'] if tag == 'select']                         # … und eine gesperrte Auswahl daneben (nur zur Anzeige, ohne Namen)
        assert len(selects) == 1 and 'disabled' in selects[0] and 'name' not in selects[0]
        assert row['hints'].count(HINT) == 1 and row['remove'] == REMOVE_TITLE
    assert [attrs['value'] for _tag, attrs in _named(locked[0], 'split_id')] == [str(marked[0])]

    assert not any('readonly' in attrs or 'disabled' in attrs for _tag, attrs in free['fields'])      # der gewöhnliche Satz bleibt änderbar
    assert HINT not in free['hints'] and free['remove'] != REMOVE_TITLE and _named(free, 'split_coa')[0][0] == 'select'
    assert not any('readonly' in attrs or 'disabled' in attrs for _tag, attrs in template['fields'])  # die Vorlage für neue Zeilen auch
    assert page.count(HINT) == 2 and page.count(REMOVE_TITLE) == 2


def test_the_hidden_account_of_a_locked_row_is_the_one_the_editor_shows_for_every_row(db):
    """Das Konto des versteckten Feldes ist das Zweckkonto wie in jeder Zeile des Editors: bei „Bank an Debitoren“ (``liquid_first``) das Gegenkonto, bei „Wartekonto an
    Erlöskonto“ (``transfer``) das Konto."""
    _invoice_id, bank, marked, _ordinary = _paid_with_an_ordinary_row(db)
    rows = _editor_rows(PageTransactions(db, edit_transaction_id=bank))
    stored = {row_id: query(db, 'SELECT COA_ID, CounterCOA_ID FROM Bookings WHERE ID = ?', (row_id,))[0] for row_id in marked}
    assert [int(_named(row, 'split_coa')[0][1]['value']) for row in rows[:2]] == [stored[marked[0]][1], stored[marked[1]][0]]


def test_the_mark_is_read_by_its_own_method_and_the_tuples_of_the_children_stay_as_they_were(db):
    """Die Seite kennt die Marke über ``Database.payment_row_ids`` (rein lesend) – die Tupel von ``get_child_bookings_for_bank`` haben positionsbasierte Leser und behalten ihre Form."""
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    assert db.payment_row_ids([]) == set()
    assert db.payment_row_ids([*marked, ordinary, bank, 999999]) == set(marked)                  # nur die markierten Sätze, keine Bankbewegung, keine unbekannte ID
    assert db.payment_row_ids(str(row_id) for row_id in marked) == set(marked)
    assert snapshot(db) == before
    assert [len(row) for row in db.get_child_bookings_for_bank(bank)] == [10, 10, 10]


def test_every_field_arrives_once_per_row_so_that_the_index_stays_right(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    post, _options = open_form(db, bank)                                            # die Felder, wie ein Browser sie abschickt: nichts Gesperrtes (``disabled``) kommt mit
    assert sorted(row_fields(post)) == sorted(ROW_FIELDS)
    assert {name: len(post[name]) for name in ROW_FIELDS} == {name: 4 for name in ROW_FIELDS}                 # zwei Zahlungssätze, ein gewöhnlicher, die Vorlage
    assert post['split_id'][:3] == [str(row_id) for row_id in (*marked, ordinary)]


def test_the_summary_still_counts_the_locked_rows(db):
    _invoice_id, bank, _marked, _ordinary = _paid_with_an_ordinary_row(db)
    assert editor_rest(db, bank) == 0.0                                             # 119,00 € der Zahlung und 31,00 € des gewöhnlichen Satzes ergeben die 150,00 €


def test_opening_and_saving_the_mask_unchanged_changes_nothing(db):
    _invoice_id, bank, _marked, _ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    submit(db, post)
    assert snapshot(db) == before


# ── Das Skript des Vorschlags ────────────────────────────────────────────────

HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const table = {insertBefore: (row, before) => { rows.splice(rows.indexOf(before), 0, row); }};
function makeRow(name, classes) {
  const row = {name, classes, className: classes.join(' '), style: {}, dataset: {}, parentNode: table,
    fields: {'.splitAmount': {value: ''}, '[name=split_coa]': {value: ''}, '[name=split_counter_coa]': {value: ''}, '[name=split_tax_rate]': {value: ''},
             '[name=split_tax_amount]': {value: ''}, '[name=split_text]': {value: ''}, '[name=split_docnr]': {value: ''}},
    querySelector: selector => row.fields[selector], cloneNode: () => makeRow('new', ['splitTemplate']), remove: () => { rows.splice(rows.indexOf(row), 1); }};
  return row;
}
const template = makeRow('template', ['splitTemplate']);
const rows = [makeRow('locked', ['splitRow', 'splitLocked']), makeRow('free', ['splitRow']), template];
// querySelectorAll wie ein Browser für ``#splitTable tr.splitRow``, mit oder ohne ``:not(.klasse)``
function select(selector) {
  const excluded = (selector.match(/:not\(\.(\w+)\)/) || [])[1];
  return rows.filter(row => row.classes.includes('splitRow') && !(excluded && row.classes.includes(excluded)));
}
const sandbox = {
  document: {querySelectorAll: selector => select(selector), querySelector: selector => (selector.includes('splitTemplate') ? template : {value: 'B-1'})},
  SPLIT_SUFFIX: 'ABC', splitRecalc: () => {},
};
vm.createContext(sandbox);
vm.runInContext(input.source, sandbox);
vm.runInContext(`suggestFillSplit([{amount: '10.00', coa_id: 4400, text: 'Vorschlag'}])`, sandbox);
console.log(JSON.stringify(rows.map(row => row.name + ':' + row.className)));
"""


@NO_NODE
def test_the_suggestion_replaces_the_free_rows_but_never_removes_a_locked_row(db):
    """🪄 ersetzt die Kontierung der änderbaren Zeilen – eine gesperrte Zeile bliebe sonst beim Speichern aus dem Formular weg und entfernte die ganze Zuordnung."""
    _invoice_id, bank, _marked, _ordinary = _paid_with_an_ordinary_row(db)
    blocks = [block for block in Dom(PageTransactions(db, edit_transaction_id=bank)).scripts if 'function suggestFillSplit(' in block]
    assert len(blocks) == 1
    function = blocks[0][blocks[0].index('function suggestFillSplit('):]                           # die letzte Funktion ihres Blocks
    done = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'source': function}), capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ['locked:splitRow splitLocked', 'new:splitRow', 'template:splitTemplate']


# ── Die Einzelmaske eines Zahlungssatzes (2.9.2, Nacharbeit) ─────────────────

def test_the_sentence_for_the_single_mask_is_the_planned_one():
    assert PAYMENT_ROW_READONLY == ('Dieser Buchungssatz gehört zu einer Rechnungszahlung und lässt sich hier nicht ändern. '
                                    'Bitte die Zuordnung an der Rechnung entfernen und neu zuordnen.')


def test_the_single_mask_of_a_payment_row_refuses_every_change_and_writes_nothing(db):
    """Die Maske eines einzelnen Buchungssatzes (``/transactions/edit?id=<Satz>``, geöffnet über ✎ an einer Kindzeile der Liste) schrieb den Satz bisher an der Sperre des Editors vorbei."""
    invoice, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    _with_a_datev_key(db, marked)
    before = snapshot(db)
    payments = query(db, 'SELECT * FROM InvoicePayments ORDER BY ID')
    for row_id in marked:
        post, _options = open_form(db, row_id)
        assert handle_add_transaction(db, post) == (400, PAYMENT_ROW_READONLY)                         # auch unverändert abgeschickt
        changed = dict(post, amount=['1.00'], text=['von Hand'], coa_id=[str(coa(db, 6300))], tax_rate=['7'], tax_amount=['0.07'], document_nr=['X-1'], recipient=['Anderer'],
                       date=['2026-03-03'], currency=['USD'])
        assert handle_add_transaction(db, changed) == (400, PAYMENT_ROW_READONLY)
    assert snapshot(db) == before                                                                       # alle Spalten, auch Form und BU-Schlüssel
    assert query(db, 'SELECT * FROM InvoicePayments ORDER BY ID') == payments


def test_the_single_mask_refuses_before_it_reads_anything_else_of_the_form(db):
    """Auch ein Formular mit unbrauchbaren Feldern bekommt denselben festen Satz – und die Rechnung, die es mitbringt, wird nicht angefasst."""
    invoice, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, marked[0])
    assert handle_add_transaction(db, dict(post, invoice_id=[str(invoice)])) == (400, PAYMENT_ROW_READONLY)
    assert snapshot(db) == before


def test_the_single_mask_of_an_ordinary_row_still_saves(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, ordinary)
    assert handle_add_transaction(db, dict(post, text=['Porto'])) == (303, '/transactions')
    assert _changed(before, snapshot(db)) == [ordinary] and snapshot(db)[ordinary]['Text'] == 'Porto'


def test_the_single_mask_page_of_a_payment_row_shows_the_sentence_and_has_neither_the_update_button_nor_the_suggestion(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    for row_id in marked:
        page = PageTransactions(db, edit_transaction_id=row_id)
        assert f'<p class="warnColor">{PAYMENT_ROW_READONLY}</p>' in page
        assert 'value="Aktualisieren"' not in page and 'onclick="applySuggestion()"' not in page and 'name="suggest_range_id"' not in page
        assert 'Abbrechen' in page                                                                      # der Weg zurück bleibt
        assert not [attrs for tag, attrs in Dom(page).tags if tag == 'input' and attrs.get('type') == 'submit']
    page = PageTransactions(db, edit_transaction_id=ordinary)                                           # der gewöhnliche Satz: wie bisher
    assert 'value="Aktualisieren"' in page and 'onclick="applySuggestion()"' in page and PAYMENT_ROW_READONLY not in page
    page = PageTransactions(db, edit_transaction_id=bank)                                               # die Bankbewegung: wie bisher
    assert 'value="Aktualisieren"' in page and PAYMENT_ROW_READONLY not in page


def test_deleting_a_payment_row_from_the_list_stays_what_it_was(db):
    invoice, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    assert handle_delete_transaction(db, marked[0]) == (303, '/transactions')
    assert _children(db, bank) == [ordinary] and query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]       # die ganze Zuordnung geht mit


# ── Der Editor liest nur gleich lange Feldlisten ─────────────────────────────

LISTS = ['split_id', 'split_coa', 'split_text', 'split_docnr', 'split_counter_coa', 'split_tax_rate', 'split_tax_amount']


@pytest.mark.parametrize('name', LISTS)
@pytest.mark.parametrize('how', ['kuerzer', 'laenger'])
def test_a_list_of_another_length_than_the_amounts_refuses_the_whole_request(db, name, how):
    """Gelesen wird über den Index: Eine Liste mit anderer Länge ließe Felder zur falschen Zeile gehören – abgelehnt, nichts gespeichert."""
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    post = {key: list(values) for key, values in post.items()}
    post[name] = post[name][:-1] if how == 'kuerzer' else post[name] + ['']
    assert handle_add_transaction(db, post) == (400, INVALID_REQUEST)
    assert snapshot(db) == before


@pytest.mark.parametrize('how', ['fehlt', 'leer'])
@pytest.mark.parametrize('name', LISTS)
def test_a_missing_or_empty_list_is_not_refused_by_the_length_check(db, name, how):
    """Ältere Aufrufer schicken manche Felder gar nicht: Eine fehlende oder leere Liste lehnt die Längenprüfung nicht ab (was sie sonst bedeutet, regeln die übrigen Prüfungen)."""
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    post, _options = open_form(db, bank)
    post = {key: list(values) for key, values in post.items() if not (key == name and how == 'fehlt')}
    if how == 'leer':
        post[name] = []
    assert handle_add_transaction(db, post) != (400, INVALID_REQUEST)


@pytest.mark.parametrize('how', ['fehlt', 'leer'])
@pytest.mark.parametrize('name', ['split_tax_rate', 'split_tax_amount', 'split_counter_coa'])
def test_the_fields_that_older_forms_do_not_send_leave_everything_as_it_is(db, name, how):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    post = {key: list(values) for key, values in post.items() if not (key == name and how == 'fehlt')}
    if how == 'leer':
        post[name] = []
    submit(db, post)
    assert snapshot(db) == before


def test_lists_of_the_same_length_save_as_before(db):
    _invoice_id, bank, marked, ordinary = _paid_with_an_ordinary_row(db)
    before = snapshot(db)
    post, _options = open_form(db, bank)
    submit(db, post)
    assert snapshot(db) == before
    submit(db, _edit(post, ordinary, split_text='Porto'))
    assert _changed(before, snapshot(db)) == [ordinary]


def test_a_new_bank_movement_is_checked_the_same_way(db):
    post, _options = open_form(db, _movement(db, '10.00'))
    post = {key: list(values) for key, values in post.items()}
    post['transaction_id'] = ['0']
    post['split_text'] = post['split_text'] + ['zu viel']
    before = snapshot(db)
    assert handle_add_transaction(db, post) == (400, INVALID_REQUEST) and snapshot(db) == before


# ── Das ✕ einer gesperrten Zeile fragt nach ──────────────────────────────────

REMOVE_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = {confirm: [], removed: [], recalc: 0};
function makeRow(name, classes) {
  const row = {name, classes, dataset: {}, classList: {contains: name => classes.includes(name)}, remove: () => { calls.removed.push(name); },
               querySelector: () => ({value: ''}), closest: () => row};
  return row;
}
const rows = [makeRow('locked', ['splitRow', 'splitLocked']), makeRow('free', ['splitRow'])];
const summary = {textContent: '', className: ''};
const sandbox = {
  document: {querySelectorAll: () => rows, getElementById: () => summary, querySelector: () => null},
  parseFloat, Math,
  appConfirm: (text, onOk) => { calls.confirm.push(text); if (input.confirm) { onOk(); } },
};
vm.createContext(sandbox);
vm.runInContext(input.source, sandbox);                              // der ganze Block des Editors, er läuft bis zur Summenzeile am Ende
const recalc = sandbox.splitRecalc;
sandbox.splitRecalc = () => { calls.recalc += 1; return recalc(); };
const row = rows.find(r => r.name === input.row);
sandbox.splitRemove({closest: () => row});
console.log(JSON.stringify(calls));
"""


def run_remove(db, bank, row, confirm):
    blocks = [block for block in Dom(PageTransactions(db, edit_transaction_id=bank)).scripts if 'SPLIT_BANK_TEXT' in block]
    assert len(blocks) == 1
    done = subprocess.run([NODE, '-e', REMOVE_HARNESS], input=json.dumps({'source': blocks[0], 'row': row, 'confirm': confirm}), capture_output=True, text=True,
                          encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
def test_the_x_of_a_locked_row_asks_first_and_removes_the_row_only_after_the_answer(db):
    _invoice_id, bank, _marked, _ordinary = _paid_with_an_ordinary_row(db)
    asked = run_remove(db, bank, 'locked', confirm=False)
    assert asked['confirm'] == ['Beim Speichern wird die ganze Zuordnung dieser Zahlung entfernt.'] and asked['removed'] == []        # ohne Bestätigung bleibt die Zeile
    confirmed = run_remove(db, bank, 'locked', confirm=True)
    assert confirmed['confirm'] == asked['confirm'] and confirmed['removed'] == ['locked'] and confirmed['recalc'] == 1                  # danach verschwindet sie, die Summe wird neu gerechnet


@NO_NODE
def test_the_x_of_an_ordinary_row_does_not_ask(db):
    _invoice_id, bank, _marked, _ordinary = _paid_with_an_ordinary_row(db)
    removed = run_remove(db, bank, 'free', confirm=False)
    assert removed['confirm'] == [] and removed['removed'] == ['free'] and removed['recalc'] == 1
