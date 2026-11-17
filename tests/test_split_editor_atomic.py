# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Speichern des Bank-Editors ist EINE Transaktion (T80): ganz oder gar nicht.

Früher öffnete jeder Schritt – die Bankzeile, jede Zeile des Editors, das Abhängen oder Löschen einer entfernten Zeile – seine
eigene Verbindung und committete selbst: Ein Fehler in einer späteren Zeile ließ einen halben Stand zurück, und die früheren
Schritte standen auch im Replay-Log. Jetzt holt ``Database.save_bank_with_rows`` die Schreibsperre (``BEGIN IMMEDIATE``), plant
unter ihr, schreibt Bankzeile und Sätze über dieselbe Verbindung (``update_booking_fields``/``insert_booking`` mit ``tx=``,
``_remove_split_row_in``) und committet einmal; das Replay-Log folgt erst danach, in der Reihenfolge Bankzeile → Zeilen → Abhängen.
Jeder Fehler nimmt alles zurück – einer, den die Anwendung nicht kennt (ein Trigger, ein Fremdschlüssel), wie eine Ablehnung.

Ebenfalls hier: ``plan`` läuft schon unter der Sperre (ein anderer Schreiber kommt in dieser Zeit nicht durch), und die ID der
Bankbewegung im Formular (``transaction_id``) wird streng gelesen – leer oder ``0`` heißt „neu“, sonst zählt nur eine positive
Ganzzahl aus ASCII-Ziffern.

Ein SQLite-Trigger im Test löst den Fehler verlässlich an einer bestimmten Zeile aus. Alle Namen und Werte sind erfunden.
"""
import sqlite3
from decimal import Decimal

import pytest

import server.handlers as handlers
from db import Database
from server.handlers import _plan_split_rows, _save_split_rows, handle_add_transaction, link_booking_to_invoice_capped
from tests.document_helpers import state
from tests.entry_form_helpers import bank_account, coa, execute, query
from tests.entry_form_roundtrip import add_row, differences, drop_row, open_form, snapshot

STATE_TABLES = ('Bookings', 'InvoicePayments', 'Invoices')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Atom-Bank', 1800)
    return tmp_db


@pytest.fixture
def logged(monkeypatch):
    """Die Beschreibungen der Einträge, die ins Replay-Log gehen (``Database._log_sql``), in ihrer Reihenfolge."""
    entries = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: entries.append(description))
    return entries


def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Atom-Bank')


def _movement(db, amount='-100.00', date='2026-08-01'):
    return db.insert_booking(date, Decimal(amount), account_id=_giro(db), booking_type='bank',
                             recipient_client='Bank-Empfänger', text='Zuordnungsziel')


def _child(db, bank, amount, text, **over):
    values = dict(coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry', parent_booking_id=bank,
                  text=text, document_number=f'ER-{text}')
    values.update(over)
    return db.insert_booking('2026-08-01', Decimal(amount), **values)


def _invoice(db):
    return db.insert_invoice({
        'invoice_number': 'RE-ATOM', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'sent'})


def _everything(db):
    return state(db, *STATE_TABLES)


def _trap(db, event):
    """Der Auslöser: Jedes ``INSERT`` bzw. ``UPDATE`` auf ``Bookings`` mit dem Text ``BOOM`` scheitert."""
    execute(db, f"CREATE TRIGGER boom BEFORE {event} ON Bookings WHEN NEW.Text = 'BOOM' "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")


def _two_rows(db, bank):
    return _child(db, bank, '-60.00', 'Teil A'), _child(db, bank, '-40.00', 'Teil B')


def _new_post(db, rows):
    """Eine neue Bankbewegung mit Editor-Zeilen (von Hand gebaut: Die Maske für eine neue Buchung hat keinen Editor)."""
    names = ('split_id', 'split_text', 'split_counter_coa', 'split_amount', 'split_coa', 'split_tax_rate',
             'split_tax_amount', 'split_docnr')
    post_data = {'transaction_id': ['0'], 'date': ['2026-08-05'], 'amount': ['-100.00'], 'account': [str(_giro(db))],
                 'currency': ['EUR'], 'recipient': ['Neue Bewegung'], 'text': ['Neuer Verwendungszweck'],
                 **{name: [] for name in names}}
    for row in rows:
        for name in names:
            post_data[name].append(row.get(name, ''))
    return post_data


# ── ein Fehler in einer späteren Zeile: nichts bleibt zurück ───────────────────────────────────────────────

def test_a_failing_new_row_rolls_back_the_bank_row_and_the_rows_before_it(db, logged):
    bank = _movement(db)
    _two_rows(db, bank)
    _trap(db, 'INSERT')
    post_data, _options = open_form(db, bank)
    post_data['text'] = ['Geänderter Verwendungszweck']                           # 1. die Bankzeile
    post_data['split_amount'][0], post_data['split_text'][0] = '-55.00', 'Geändert'  # 2. die erste Zeile
    post_data = add_row(post_data, split_amount='-5.00', split_coa=str(coa(db, 6850)), split_text='BOOM')   # 3. schlägt fehl
    before = _everything(db)
    logged.clear()

    status, text = handle_add_transaction(db, post_data)

    assert status == 500 and 'boom' in text
    assert _everything(db) == before                    # Bankzeile, Zeile 1 und Zeile 2: wie vorher
    assert logged == []                                 # und nichts davon im Replay-Log


def test_a_failing_update_of_a_later_existing_row_rolls_back_everything(db, logged):
    bank = _movement(db)
    _two_rows(db, bank)
    _trap(db, 'UPDATE')
    post_data, _options = open_form(db, bank)
    post_data['text'] = ['Geänderter Verwendungszweck']
    post_data['split_text'][0] = 'Geändert'
    post_data['split_text'][1] = 'BOOM'                                           # die zweite, schon vorhandene Zeile
    before = _everything(db)
    logged.clear()

    status, _text = handle_add_transaction(db, post_data)

    assert status != 303
    assert _everything(db) == before
    assert logged == []


def test_a_new_row_with_an_account_that_does_not_exist_rolls_back_everything(db, logged):
    """Ohne Trigger: Der Fremdschlüssel auf das Konto scheitert beim Einfügen der neuen Zeile."""
    bank = _movement(db)
    _two_rows(db, bank)
    post_data, _options = open_form(db, bank)
    post_data['text'] = ['Geänderter Verwendungszweck']
    post_data['split_text'][0] = 'Geändert'
    post_data = add_row(post_data, split_amount='-5.00', split_coa='999999', split_text='Neu')
    before = _everything(db)
    logged.clear()

    status, text = handle_add_transaction(db, post_data)

    assert status == 500 and 'FOREIGN KEY' in text
    assert _everything(db) == before
    assert logged == []


def test_a_failing_row_of_a_new_bank_movement_creates_no_bank_row(db, logged):
    _trap(db, 'INSERT')
    post_data = _new_post(db, [
        {'split_amount': '-60.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil A'},
        {'split_amount': '-40.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'BOOM'}])
    before = _everything(db)
    logged.clear()

    status, _text = handle_add_transaction(db, post_data)

    assert status != 303
    assert _everything(db) == before                    # keine Bankzeile, auch nicht die erste Zeile
    assert logged == []


def test_a_new_bank_movement_with_valid_rows_is_logged_bank_first(db, logged):
    post_data = _new_post(db, [
        {'split_amount': '-60.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil A'},
        {'split_amount': '-40.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil B'}])
    logged.clear()

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert logged == ['Manual booking entry', 'Split row insert', 'Split row insert']
    bank = query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")[0][0]
    assert [row[0] for row in query(db, 'SELECT Text FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                                    (bank,))] == ['Teil A', 'Teil B']


# ── die ID der Bankbewegung im Formular ──────────────────────────────────────────────

INVALID_ID = 'Ungültige Buchung.'
ARABIC_INDIC = str.maketrans('0123456789', ''.join(chr(0x0660 + n) for n in range(10)))


@pytest.mark.parametrize('text', ['-1', 'abc', '1.0', '1e3', '٣', '+1', '9223372036854775808'])
def test_a_transaction_id_that_is_no_positive_integer_is_refused_and_nothing_is_created(db, text):
    """``int()`` nähme ``'-1'`` (legte eine neue Bewegung an), ``'+1'`` und Ziffern anderer Schriften und liefe bei ``'1.0'`` in
    einen 500: Der Server liest die ID streng (``_form_id``) und lehnt alles andere mit Klartext ab."""
    post_data = _new_post(db, [{'split_amount': '-60.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil A'}])
    post_data['transaction_id'] = [text]
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, INVALID_ID)

    assert _everything(db) == before                    # weder eine Bankzeile noch ein Satz


@pytest.mark.parametrize('spell', [lambda i: f'+{i}', lambda i: f'-{i}', lambda i: f'{i}.0', lambda i: f'{i}e0',
                                   lambda i: i.translate(ARABIC_INDIC)],
                         ids=['plus', 'minus', 'dezimalpunkt', 'exponent', 'arabisch-indische-ziffern'])
def test_another_spelling_of_the_id_of_an_existing_movement_changes_nothing(db, spell):
    """Dieselbe Bewegung, die ID anders geschrieben, dazu ein geänderter Verwendungszweck: abgelehnt, die Bewegung bleibt."""
    bank = _movement(db)
    _two_rows(db, bank)
    post_data, _options = open_form(db, bank)
    post_data['transaction_id'] = [spell(str(bank))]
    post_data['text'] = ['Geänderter Verwendungszweck']
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, INVALID_ID)

    assert _everything(db) == before


@pytest.mark.parametrize('text', ['', '0', None], ids=['leer', 'null', 'fehlt'])
def test_an_empty_zero_or_missing_transaction_id_creates_a_new_movement(db, text):
    post_data = _new_post(db, [{'split_amount': '-60.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil A'}])
    if text is None:
        del post_data['transaction_id']
    else:
        post_data['transaction_id'] = [text]

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    (bank,) = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")]
    assert query(db, 'SELECT Text FROM Bookings WHERE ParentBooking_ID = ?', (bank,)) == [('Teil A',)]


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_a_movement_that_no_longer_exists_is_refused_instead_of_silently_dropping_the_input(db, with_editor):
    """Die Bewegung wurde in einem anderen Fenster gelöscht: Das Formular meldet es, statt mit „gespeichert“ zu antworten und
    die Eingabe zu verlieren."""
    post_data = _new_post(db, [{'split_amount': '-60.00', 'split_coa': str(coa(db, 6815)), 'split_text': 'Teil A'}])
    if not with_editor:
        post_data = {name: values for name, values in post_data.items() if not name.startswith('split_')}
    post_data['transaction_id'] = ['999999']
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (
        400, 'Diese Buchung gibt es nicht (mehr). Bitte die Seite neu laden.')

    assert _everything(db) == before


# ── alles auf einmal ──────────────────────────────────────────────────────────────

def _bank_with_every_kind_of_row(db):
    """Eine Bankbewegung über 119,00 mit einer zugeordneten Rechnungszahlung (zwei markierte Sätze), einer Zeile zum Ändern,
    einem Satz von Hand (wird gelöscht) und einem importierten Satz (wird abgehängt)."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    bank = db.insert_booking('2026-08-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                             recipient_client='Käufer AG', text='Zuordnungsziel')
    assert link_booking_to_invoice_capped(db, invoice, bank) == (True, None)
    settlement, transfer = [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                                                     (bank,))]
    kept = _child(db, bank, '-10.00', 'Bleibt')
    by_hand = _child(db, bank, '-20.00', 'Handsatz')
    imported = _child(db, bank, '-30.00', 'Importsatz')
    execute(db, "UPDATE Bookings SET SourceGroup = 'WISO-ATOM-1' WHERE ID = ?", (imported,))
    return invoice, bank, settlement, transfer, kept, by_hand, imported


def test_new_changed_deleted_detached_and_removed_payment_rows_in_one_save(db, logged):
    invoice, bank, settlement, transfer, kept, by_hand, imported = _bank_with_every_kind_of_row(db)
    post_data, _options = open_form(db, bank)
    post_data['recipient'] = ['Neuer Empfänger']                                 # die Bankzeile
    post_data['split_text'][post_data['split_id'].index(str(kept))] = 'Geändert'  # eine geänderte Zeile
    post_data = add_row(post_data, split_amount='5.00', split_coa=str(coa(db, 6815)), split_text='neue Zeile')
    for row_id in (settlement, by_hand, imported):                               # ✕ in drei Zeilen
        post_data = drop_row(post_data, row_id)
    logged.clear()

    assert handle_add_transaction(db, post_data) == (303, '/transactions')       # nichts hat sich selbst blockiert

    assert query(db, 'SELECT RecipientClient FROM Bookings WHERE ID = ?', (bank,)) == [('Neuer Empfänger',)]
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (kept,)) == [('Geändert',)]
    new = [row for row in query(db, 'SELECT ID, ParentBooking_ID FROM Bookings WHERE Text = ?', ('neue Zeile',))]
    assert [parent for _id, parent in new] == [bank]                             # die neue Zeile hängt an der Bankbewegung
    assert query(db, 'SELECT 1 FROM Bookings WHERE ID IN (?, ?, ?)', (settlement, transfer, by_hand)) == []   # gelöscht
    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (imported,)) == [(None,)]         # abgehängt
    assert query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]           # die Zuordnung ist weg …
    assert query(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', (invoice,)) == [('sent', 1190000)]   # … und neu gerechnet
    # Bankzeile → Zeilen (geändert, neu) → Abhängen; Löschungen gehören nicht ins Log
    assert logged == ['Manual booking update', 'Split row update', 'Split row update', 'Split row insert',
                      'Split row detach']


def test_a_refusal_inside_the_transaction_rolls_back_the_bank_row_and_the_rows_before_it(db, logged, monkeypatch):
    """Die Prüfung im Plan (``split_removal_blocker``) sieht nichts; das Entfernen der Zahlung in der Transaktion lehnt ab –
    erst nachdem die Bankzeile und die Zeilen davor schon geschrieben sind. Alles geht zurück, und die Antwort ist 400."""
    invoice, bank, settlement, transfer, kept, by_hand, imported = _bank_with_every_kind_of_row(db)
    monkeypatch.setattr(Database, 'split_removal_blocker', lambda self, ids: None)
    monkeypatch.setattr(Database, '_refund_removal_blocker',
                        staticmethod(lambda conn, payment_ids: 'Die Zahlung bleibt.' if payment_ids else None))
    post_data, _options = open_form(db, bank)
    post_data['recipient'] = ['Neuer Empfänger']
    post_data = add_row(post_data, split_amount='5.00', split_coa=str(coa(db, 6815)), split_text='neue Zeile')
    post_data = drop_row(post_data, settlement)
    before = _everything(db)
    logged.clear()

    assert handle_add_transaction(db, post_data) == (400, 'Die Zahlung bleibt.')

    assert _everything(db) == before
    assert logged == []


def test_the_rows_of_the_editor_without_the_bank_row_are_one_transaction_too(db, logged):
    """``_save_split_rows``: die Bankzeile bleibt unberührt, die Sätze gelingen gemeinsam oder gar nicht."""
    bank = _movement(db)
    first, second = _two_rows(db, bank)
    post_data, _options = open_form(db, bank)
    post_data['split_text'][0] = 'Geändert'
    post_data = add_row(post_data, split_amount='-5.00', split_coa=str(coa(db, 6850)), split_text='BOOM')
    _trap(db, 'INSERT')
    before = _everything(db)
    logged.clear()
    args = (db, bank, post_data, coa(db, 1800), '2026-08-01', None, 'Bank-Empfänger', 'EUR')

    with pytest.raises(Exception, match='boom'):
        _save_split_rows(*args)

    assert _everything(db) == before
    assert logged == []
    # Gegenprobe: Das Formular selbst ist gültig – es scheitert allein am Auslöser der neuen Zeile
    assert [rid for rid, _fields, _adopt in _plan_split_rows(*args)] == [first, second, None]


# ── geplant wird unter der Schreibsperre ────────────────────────────────────────────

def test_the_plan_runs_under_the_write_lock(db, monkeypatch):
    """``save_bank_with_rows`` holt die Schreibsperre, bevor es plant: Während ``_plan_split_rows`` läuft, liest eine zweite
    Verbindung noch, ein zweiter Schreiber aber kommt nicht durch („database is locked“, ohne Wartezeit). So ändert sich der Stand
    zwischen Planen und Schreiben nicht mehr – die Prüfung im Plan ist die maßgebliche. Danach läuft das Speichern normal weiter."""
    bank = _movement(db)
    _two_rows(db, bank)
    seen = {}
    plan = handlers._plan_split_rows

    def plan_with_a_second_connection(*args):
        other = sqlite3.connect(db.db_name, timeout=0)
        try:
            seen['read'] = other.execute('SELECT COUNT(*) FROM Bookings').fetchone()[0]
            try:
                other.execute('BEGIN IMMEDIATE')
                seen['write'] = 'durchgekommen'
            except sqlite3.OperationalError as e:
                seen['write'] = str(e)
        finally:
            other.close()
        return plan(*args)

    monkeypatch.setattr(handlers, '_plan_split_rows', plan_with_a_second_connection)
    post_data, _options = open_form(db, bank)
    post_data['split_text'][0] = 'Geändert'
    post_data = add_row(post_data, split_amount='-5.00', split_coa=str(coa(db, 6850)), split_text='Neu')

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert seen == {'read': 3, 'write': 'database is locked'}      # die Bewegung und ihre zwei Sätze
    assert query(db, 'SELECT Text FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', (bank,)) == [
        ('Geändert',), ('Teil B',), ('Neu',)]


# ── die Bausteine mit ``tx=`` ──────────────────────────────────────────────────────

def test_update_booking_fields_with_a_transaction_commits_and_logs_only_with_it(db, logged):
    bank = _movement(db)

    with db.transaction() as tx:
        db.update_booking_fields(bank, log_description='im Test', tx=tx, text='Neu')
        assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Zuordnungsziel',)]   # noch nicht fest
        assert logged == []

    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Neu',)]
    assert logged == ['im Test']


def test_update_booking_fields_with_a_transaction_that_fails_leaves_the_row_and_the_log(db, logged):
    bank = _movement(db)

    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            db.update_booking_fields(bank, log_description='im Test', tx=tx, text='Neu')
            raise RuntimeError('danach scheitert etwas')

    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Zuordnungsziel',)]
    assert logged == []


def test_update_booking_fields_without_a_transaction_commits_and_logs_as_before(db, logged):
    bank = _movement(db)

    db.update_booking_fields(bank, log_description='allein', text='Neu')

    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Neu',)]
    assert logged == ['allein']


def test_remove_split_row_stays_one_transaction_and_logs_a_detachment_after_the_commit(db, logged):
    bank = _movement(db)
    imported = _child(db, bank, '-30.00', 'Importsatz')
    by_hand = _child(db, bank, '-20.00', 'Handsatz')
    execute(db, "UPDATE Bookings SET SourceGroup = 'WISO-ATOM-2' WHERE ID = ?", (imported,))

    db.remove_split_row(imported)
    db.remove_split_row(by_hand)

    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (imported,)) == [(None,)]
    assert query(db, 'SELECT 1 FROM Bookings WHERE ID = ?', (by_hand,)) == []
    assert logged == ['Split row detach']


def test_dropped_split_rows_leave_the_mirrors_alone(db):
    """Die Regel steht an einer Stelle (``Database.dropped_split_rows``): Was der Nutzer entfernt hat, ohne die Doppik-Spiegel,
    die im Editor nicht stehen."""
    bank = _movement(db)
    first, second = _two_rows(db, bank)
    db.insert_booking('2026-08-01', Decimal('-100.00'), coa_id=coa(db, 1800), counter_coa_id=coa(db, 1600),
                      booking_type='entry', parent_booking_id=bank, text='Spiegel')
    before = snapshot(db)

    assert db.dropped_split_rows(bank, {first}) == [second]            # der Spiegel (zwei Geldkonten) nie
    assert db.dropped_split_rows(bank, {first, second}) == []
    assert db.dropped_split_rows(bank, set()) == [first, second]
    assert differences(before, snapshot(db)) == []                     # rein lesend
