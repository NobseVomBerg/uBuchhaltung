# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Forderung einer Rechnung in der Buchungsliste (Seite *Buchungen*).

Beim Versenden einer Rechnung bucht die App die Forderung selbst („Debitoren an Wartekonto“, ein eigenständiger Satz). Die Liste zeigte
sie wie eine vollständig gebuchte Zahlung – grüner Haken „Buchung vollständig gebucht“ und ein grün gefärbter, positiver Betrag –,
obwohl niemand etwas gebucht hat und kein Geld eingegangen ist. Jetzt trägt sie das graue Abzeichen „Forderung“ (``RECEIVABLE_HINT`` als
Hinweistext) und einen ungefärbten Betrag; jede andere Zeile bleibt, wie sie war.

``Database.receivable_entry_ids`` sagt, welche Buchungen eine Forderung sind: dieselbe Bedingung wie überall (``receivable_condition``),
keine eigene Lesart. Die Liste fragt damit EINMAL für alle ihre Einzelzeilen.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (Stichtage sind feste Tage).
"""
import datetime
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

import db.matching as matching
from db import Database
from server import pages_transactions
from server.handlers import link_booking_to_invoice_capped
from server.pages_transactions import RECEIVABLE_HINT, PageTransactions
from tests.dom_helpers import Dom, assert_same_structure

FIRST_DAY, LAST_DAY = '2026-01-01', '2026-12-31'          # der Zeitraum der Liste
CHECKED_TITLE = 'Buchung vollständig gebucht'
CHECK_MARK = '✓'

# Die Spalten der Buchungstabelle
DATE, RECIPIENT, TEXT, AMOUNT, CURRENCY, TAX, ACCOUNT, SKR, DOCNR, ACTIONS = range(10)


def storno_today():
    """Der Tag, nach dem kein Stornodatum liegen darf – ein fester Tag, nie die Uhr."""
    return datetime.date(2026, 11, 21)


# ── Bestände, über Wege der App geschrieben ─────────────────────────────────────────────────────────────────────────

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _bank_account(db):
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return next(account[0] for account in db.fetch_accounts() if account[1] == 'Probe-Giro')


def _sent_invoice(db, number, gross=119.00, day='2026-03-02'):
    """Eine Rechnung, die über den Statuswechsel der App versendet wird – er bucht die Forderung. Returns ``(Rechnung, Forderung)``."""
    net = round(gross / 1.19, 2)
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': 'finalized'})
    change = db.set_invoice_status(invoice_id, 'sent', 'tester')
    assert change.ok, change.message
    receivable = db.get_receivable_booking(invoice_id)
    assert receivable is not None
    return invoice_id, receivable


def _paid(db, invoice_id, account, amount, day='2026-03-20'):
    """Die Zahlung einer Rechnung, wie die App sie zuordnet: Bankbewegung mit „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ darunter."""
    bank = db.insert_booking(day, Decimal(amount), account_id=account, booking_type='bank', recipient_client='Käufer AG')
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank)
    assert ok, error
    return bank


def _expense(db, day='2026-03-05', amount='-35.70', number='VR-7', text='Büromaterial'):
    """Ein gewöhnlicher, kontierter Satz ohne Bankbewegung („Aufwand an Verrechnung“): eine eigene Zeile der Liste."""
    return db.insert_booking(day, Decimal(amount), coa_id=_coa(db, 6815), counter_coa_id=_coa(db, 1460), booking_type='entry',
                             document_number=number, recipient_client='Papier Peters', text=text)


def _income(db, day='2026-03-06', amount='42.00', number='VR-8', text='Bareinnahme'):
    return db.insert_booking(day, Decimal(amount), coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1460), booking_type='entry',
                             document_number=number, recipient_client='Tanja Tester', text=text)


def _loose(db, day='2026-03-08', amount='-5.00', text='Ohne Konto'):
    """Ein Satz ohne Konto: die Liste zeigt ihn ohne Abzeichen."""
    return db.insert_booking(day, Decimal(amount), booking_type='entry', recipient_client='Unbekannt', text=text)


def _group(db):
    """Zwei Sätze desselben Belegs am selben Tag ohne Bankbewegung: Die Liste fasst sie zu einer aufklappbaren Gruppe zusammen."""
    return [_expense(db, day='2026-03-09', amount=amount, number='VR-9', text=text)
            for amount, text in (('-10.00', 'Gruppe eins'), ('-20.00', 'Gruppe zwei'))]


def _plain_stock(db):
    """Ein kleiner Bestand ohne Rechnung: Einzelzeilen, eine Gruppe, eine Bankbewegung mit einem Satz darunter."""
    account = _bank_account(db)
    stock = SimpleNamespace(expense=_expense(db), income=_income(db), loose=_loose(db), group=_group(db))
    stock.bank = db.insert_booking('2026-03-12', Decimal('-25.00'), account_id=account, booking_type='bank', recipient_client='Papier Peters')
    stock.child = db.insert_booking('2026-03-12', Decimal('-25.00'), coa_id=_coa(db, 6815), counter_coa_id=_coa(db, 1800),
                                    booking_type='entry', parent_booking_id=stock.bank, document_number='VR-10', text='Porto')
    return stock


def _world(db):
    """Die Liste mit Forderungen: eine versendete, noch offene Rechnung; eine versendete und bezahlte (Forderung, Bankbewegung, zwei Sätze
    darunter); dazu gewöhnliche Einzelzeilen und eine Gruppe."""
    account = _bank_account(db)
    open_invoice, open_receivable = _sent_invoice(db, 'R26-0001')
    paid_invoice, paid_receivable = _sent_invoice(db, 'R26-0002', gross=238.00, day='2026-03-03')
    return SimpleNamespace(
        account=account, open_invoice=open_invoice, open_receivable=open_receivable,
        paid_invoice=paid_invoice, paid_receivable=paid_receivable, bank=_paid(db, paid_invoice, account, '238.00'),
        expense=_expense(db), income=_income(db), loose=_loose(db), group=_group(db))


# ── Die Tabelle, wie ein Browser sie liest ──────────────────────────────────────────────────────────────────────────

class _Badge:
    def __init__(self, attrs):
        self.classes, self.title, self.text = attrs.get('class'), attrs.get('title'), ''

    def key(self):
        return self.classes, self.title, self.text


class _Cell:
    def __init__(self, attrs):
        self.attrs, self.text, self.badges, self.anchors = attrs, '', [], []


class _Row:
    def __init__(self, attrs):
        self.attrs, self.cells = attrs, []

    @property
    def classes(self):
        return (self.attrs.get('class') or '').split()


class Table(HTMLParser):
    """Die Zeilen von ``#transactionsTable``: je Zeile ihre Attribute und Zellen, je Zelle Attribute, Text, Abzeichen (``span.badge``) und die
    Titel der Verweise – Entitäten aufgelöst, wie im Browser."""

    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._on = False
        self._row = self._cell = self._badge = None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self._on = attrs.get('id') == 'transactionsTable'
        elif not self._on:
            return
        elif tag == 'tr':
            self._row = _Row(attrs)
            self.rows.append(self._row)
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = _Cell(attrs)
            self._row.cells.append(self._cell)
        elif tag == 'span' and self._cell is not None and 'badge' in (attrs.get('class') or '').split():
            self._badge = _Badge(attrs)
            self._cell.badges.append(self._badge)
        elif tag == 'a' and self._cell is not None:
            self._cell.anchors.append(attrs.get('title'))

    def handle_endtag(self, tag):
        if tag == 'table':
            self._on = False
        elif tag == 'tr':
            self._row = self._cell = self._badge = None
        elif tag in ('td', 'th'):
            self._cell = self._badge = None
        elif tag == 'span':
            self._badge = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data
        if self._badge is not None:
            self._badge.text += data

    def data_rows(self):
        return [row for row in self.rows if len(row.cells) == 10 and row.cells[0].text != 'Datum']

    def row(self, text):
        """Die eine Zeile, deren Spalte „Text“ genau so lautet."""
        found = [row for row in self.data_rows() if row.cells[TEXT].text == text]
        assert len(found) == 1, (text, len(found))
        return found[0]

    def row_of_group(self, group_id):
        found = [row for row in self.data_rows() if row.attrs.get('data-group-id') == group_id]
        assert len(found) == 1, (group_id, len(found))
        return found[0]


def _page(db):
    return PageTransactions(db, date_from=FIRST_DAY, date_to=LAST_DAY)


def _table(db):
    return Table(_page(db))


def _badges(row):
    """Die Abzeichen der Zeile in der Spalte „Aktionen“ als ``(Klassen, Titel, Text)``."""
    return [badge.key() for badge in row.cells[ACTIONS].badges]


CHECKED = ('badge bg-green', CHECKED_TITLE, CHECK_MARK)


# ── Database.receivable_entry_ids ───────────────────────────────────────────────────────────────────────────────────

def test_receivable_of_a_sent_invoice_is_a_receivable(tmp_db):
    _invoice_id, receivable = _sent_invoice(tmp_db, 'R26-0001')

    assert tmp_db.receivable_entry_ids([receivable]) == {receivable}


def test_other_bookings_are_not_receivables(tmp_db):
    """Gewöhnlicher Aufwand und Einnahme, Bankbewegung, die Sätze der Zahlung (auch „Bank an Debitoren“: 10000 im Haben) und eine ID, die es
    nicht gibt, sind keine Forderung – die Forderung der bezahlten Rechnung bleibt eine."""
    account = _bank_account(tmp_db)
    invoice_id, receivable = _sent_invoice(tmp_db, 'R26-0001')
    bank = _paid(tmp_db, invoice_id, account, '119.00')
    children = {child[0]: child for child in tmp_db.get_child_bookings_for_bank(bank)}
    payment = next(child_id for child_id, child in children.items() if child[2] == _coa(tmp_db, 10000))
    assert children[payment][1] == _coa(tmp_db, 1800)                   # „Bank an Debitoren“: 10000 steht im Haben
    transfer = next(child_id for child_id in children if child_id != payment)       # „Wartekonto an Erlöskonto“
    expense, income, loose = _expense(tmp_db), _income(tmp_db), _loose(tmp_db)
    # eine Bankzeile mit den Konten einer Forderung (so schreibt sie nur ein Eingriff von Hand): nur ein eigenständiger Satz zählt
    odd_bank = tmp_db.insert_booking('2026-03-21', Decimal('50.00'), coa_id=_coa(tmp_db, 10000), counter_coa_id=_coa(tmp_db, 4405),
                                     booking_type='bank')

    everything = [receivable, expense, income, loose, bank, payment, transfer, odd_bank, 999_999]

    assert tmp_db.receivable_entry_ids(everything) == {receivable}


def test_counter_booking_of_a_storno_invoice_is_a_receivable(tmp_db):
    """Die Gegenbuchung der Stornorechnung („Debitoren an Wartekonto“ mit negativem Betrag) ist die Forderung einer Gutschrift."""
    invoice_id, receivable = _sent_invoice(tmp_db, 'R26-0001', gross=59.50, day='2026-04-01')
    storno = tmp_db.cancel_invoice(invoice_id, '2026-04-10', 'tester', today=storno_today())
    assert storno.ok and storno.counter_booked, storno.message
    counter = tmp_db.get_receivable_booking(storno.storno_id)
    assert counter is not None and counter != receivable
    assert tmp_db.get_booking_by_id(counter)[11] < 0

    assert tmp_db.receivable_entry_ids([receivable, counter]) == {receivable, counter}


def test_no_ids_no_receivables(tmp_db):
    _sent_invoice(tmp_db, 'R26-0001')

    assert tmp_db.receivable_entry_ids([]) == set()
    assert tmp_db.receivable_entry_ids(iter(())) == set()             # die Liste übergibt einen Generator


def _three_receivables(db):
    """Zwei Forderungen von Rechnungen und die Gegenbuchung einer Stornorechnung – dazu ein gewöhnlicher Aufwand."""
    _first_invoice, first = _sent_invoice(db, 'R26-0001')
    second_invoice, second = _sent_invoice(db, 'R26-0002', gross=59.50, day='2026-04-01')
    storno = db.cancel_invoice(second_invoice, '2026-04-10', 'tester', today=storno_today())
    assert storno.ok, storno.message
    return first, second, db.get_receivable_booking(storno.storno_id), _expense(db)


class _SpyConnection:
    """Eine Verbindung, die festhält, wie viele Werte jede Abfrage über ``execute`` bekommt (die Bedingung der Forderung und die IDs)."""

    def __init__(self, conn, sizes):
        self._conn, self._sizes = conn, sizes

    def cursor(self):
        return self._conn.cursor()

    def execute(self, sql, params=()):
        self._sizes.append(len(params))
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


def test_more_ids_than_one_chunk(tmp_db, monkeypatch):
    """Mehr IDs als ``_ID_CHUNK``: SQLite begrenzt die Zahl der Werte je Abfrage – gefunden wird in jedem Stück, an jeder Stelle, und keine
    Abfrage bekommt mehr als ein Stück an IDs (unabhängig davon, wie viele Werte die SQLite-Version des Rechners zulässt)."""
    first, second, third, expense = _three_receivables(tmp_db)
    chunk = matching._ID_CHUNK
    ids = list(range(100_000, 100_000 + 2 * chunk + 5))               # IDs, die es nicht gibt
    ids[0], ids[chunk - 1], ids[chunk], ids[-1] = first, expense, second, third      # Anfang, Ende des ersten, Anfang des zweiten, letztes Stück
    assert len(ids) > 2 * chunk
    sizes = []
    real_connection = Database._get_connection
    monkeypatch.setattr(Database, '_get_connection', lambda self: _SpyConnection(real_connection(self), sizes))

    assert tmp_db.receivable_entry_ids(ids) == {first, second, third}

    condition = sizes[-1] - 5                                         # die Werte der Bedingung selbst; im letzten Stück stehen fünf IDs
    assert sizes == [condition + chunk, condition + chunk, condition + 5]


@pytest.mark.parametrize('chunk', [1, 2, 3, 7])
def test_chunk_borders(tmp_db, monkeypatch, chunk):
    """Mit kleinem Stück liegen die Forderungen an wechselnden Grenzen: Keine geht beim Aufteilen verloren."""
    first, second, third, expense = _three_receivables(tmp_db)
    monkeypatch.setattr(matching, '_ID_CHUNK', chunk)
    ids = [first, expense, 900_001, second, 900_002, 900_003, third, first]          # eine ID doppelt: sie zählt einmal

    assert tmp_db.receivable_entry_ids(ids) == {first, second, third}


# ── Die Liste ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_hint_wording():
    """Der Text am Abzeichen steht fest."""
    assert RECEIVABLE_HINT == ('Forderung an einen Kunden: kein Geldeingang und noch keine Einnahme. Bei einer Rechnung entsteht sie mit dem Versenden; '
                               'der Zahlungseingang wird später an der Rechnung zugeordnet.')


def test_receivable_row_shows_badge_and_plain_amount(tmp_db):
    """Nach dem Versenden steht die Forderung mit dem Abzeichen „Forderung“ da – kein Haken „Buchung vollständig gebucht“, kein Grün am
    Betrag. Alles andere der Zeile bleibt."""
    _world(tmp_db)
    page = _page(tmp_db)
    table = Table(page)
    row = table.row('Rechnung R26-0001 Käufer AG')

    assert _badges(row) == [('badge bg-gray', RECEIVABLE_HINT, 'Forderung')]
    assert CHECK_MARK not in row.cells[ACTIONS].text
    assert row.cells[AMOUNT].attrs == {}                                # kein style: nicht grün, nicht rot
    assert row.cells[AMOUNT].text == '119,00'
    assert [row.cells[column].text for column in (DATE, RECIPIENT, CURRENCY, TAX, SKR, DOCNR)] == \
        ['2026-03-02', 'Käufer AG', 'EUR', '19%', '10000', 'R26-0001']
    assert row.cells[ACTIONS].anchors == ['Bearbeiten', 'Löschen']      # Bearbeiten und Löschen bleiben
    assert row.classes == ['transaction-row']
    assert "<td>119,00</td>" in page
    grey = [badge for other in table.data_rows() for cell in other.cells for badge in cell.badges if 'bg-gray' in badge.classes]
    assert len(grey) == 2                                               # die beiden Forderungen der Liste, sonst nichts


def test_hint_is_masked(tmp_db, monkeypatch):
    """Der Hinweistext wird wie jeder Wert maskiert ausgegeben: Er bleibt der Wert des Attributs, nichts darin wird Markup oder Attribut."""
    _world(tmp_db)
    benign = _page(tmp_db)
    hostile = "x' onmouseover='alert(1)' data-x=\"y\" <b>&amp;"
    monkeypatch.setattr(pages_transactions, 'RECEIVABLE_HINT', hostile)

    page = _page(tmp_db)

    assert hostile not in page
    assert_same_structure(page, benign)
    badges = [attrs for tag, attrs in Dom(page).tags if tag == 'span' and attrs.get('class') == 'badge bg-gray']
    assert [attrs['title'] for attrs in badges] == [hostile, hostile]
    assert all(set(attrs) == {'class', 'title'} for attrs in badges)


def test_receivable_stays_a_receivable_after_payment(tmp_db):
    """Auch nach der Zahlung ist die Forderung eine Forderung; die Zahlung (Bankbewegung und ihre Sätze) bleibt, wie sie war: Dort ist
    Geld geflossen, der Betrag bleibt gefärbt."""
    world = _world(tmp_db)
    table = _table(tmp_db)

    paid = table.row('Rechnung R26-0002 Käufer AG')
    assert _badges(paid) == [('badge bg-gray', RECEIVABLE_HINT, 'Forderung')]
    assert paid.cells[AMOUNT].attrs == {}

    bank = table.row_of_group(f'b{world.bank}')                           # die Bankbewegung, mit ihren zwei Sätzen zusammengefasst
    assert [badge.text for badge in bank.cells[ACTIONS].badges] == [CHECK_MARK]
    assert bank.cells[ACTIONS].badges[0].title == 'Bank + Buchungssätze vollständig'
    assert bank.cells[AMOUNT].attrs == {'style': 'color:green'}
    children = [row for row in table.data_rows() if row.attrs.get('data-parent-group') == f'b{world.bank}']
    assert len(children) == 2
    for child in children:                                                # die Sätze der Zahlung: ✓ und Farbe wie bisher
        assert _badges(child) == [CHECKED]
        assert child.cells[AMOUNT].attrs == {'style': 'color:green'}


def test_ordinary_rows_keep_check_mark_and_colour(tmp_db):
    """Eine gewöhnliche kontierte Zeile neben der Forderung behält Haken und Farbe, ein Satz ohne Konto bleibt ohne Abzeichen."""
    _world(tmp_db)
    page = _page(tmp_db)
    table = Table(page)

    for text, color in (('Büromaterial', 'red'), ('Bareinnahme', 'green')):
        row = table.row(text)
        assert _badges(row) == [CHECKED]
        assert row.cells[AMOUNT].attrs == {'style': f'color:{color}'}
    loose = table.row('Ohne Konto')
    assert _badges(loose) == []
    assert loose.cells[AMOUNT].attrs == {'style': 'color:red'}
    # und so steht es im HTML, Zeichen für Zeichen
    assert "<td style='color:red'>-35,70</td>" in page
    assert f"<span class='badge bg-green' title='{CHECKED_TITLE}'>{CHECK_MARK}</span>" in page


def test_group_and_child_rows_are_unchanged_next_to_receivables(tmp_db):
    """Gruppen- und Kindzeilen (Belege ohne Bankbewegung mit mehreren Sätzen) bleiben, wie sie waren – auch in einer Liste mit Forderungen."""
    _world(tmp_db)
    table = _table(tmp_db)

    headers = [row for row in table.data_rows() if 'group-row' in row.classes]
    assert len(headers) == 1
    header = headers[0]
    assert _badges(header) == [CHECKED]
    assert header.cells[AMOUNT].attrs == {'style': 'color:red'}
    assert header.cells[SKR].badges[0].text == 'Split 2×'
    children = [row for row in table.data_rows() if row.attrs.get('data-parent-group') == header.attrs['data-group-id']]
    assert len(children) == 2
    for child in children:
        assert _badges(child) == [CHECKED]
        assert child.cells[AMOUNT].attrs == {'style': 'color:red'}
    assert [badge.text for row in table.data_rows() for badge in row.cells[ACTIONS].badges].count('Forderung') == 2     # nur die beiden Einzelzeilen


def test_storno_counter_booking_row_is_a_receivable_too(tmp_db):
    """Die Gegenbuchung der Stornorechnung ist dieselbe Art Satz (Forderung einer Gutschrift): Abzeichen statt Haken, Betrag ohne Farbe."""
    invoice_id, _receivable = _sent_invoice(tmp_db, 'R26-0001', gross=59.50, day='2026-04-01')
    storno = tmp_db.cancel_invoice(invoice_id, '2026-04-10', 'tester', today=storno_today())
    assert storno.ok and storno.counter_booked, storno.message

    counter = Table(_page(tmp_db)).row(f'Stornorechnung {storno.storno_number} Käufer AG')

    assert _badges(counter) == [('badge bg-gray', RECEIVABLE_HINT, 'Forderung')]
    assert counter.cells[AMOUNT].attrs == {}
    assert counter.cells[AMOUNT].text == '-59,50'


def test_list_asks_once_for_all_single_rows(tmp_db, monkeypatch):
    """EINE Abfrage je Seite, nicht je Zeile – mit den IDs der Einzelzeilen: weder die Bankbewegung und ihre Sätze noch die Gruppe."""
    world = _world(tmp_db)
    calls = []
    original = Database.receivable_entry_ids

    def counting(self, ids):
        ids = list(ids)
        calls.append(ids)
        return original(self, ids)

    monkeypatch.setattr(Database, 'receivable_entry_ids', counting)

    _page(tmp_db)

    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([world.open_receivable, world.paid_receivable, world.expense, world.income, world.loose])


def test_list_without_receivables_is_unchanged(tmp_db):
    """Ein kleiner Bestand ohne Rechnung: Kein Abzeichen „Forderung“, kein graues Abzeichen überhaupt, und jede kontierte Zeile hat ihren Haken
    – Einzelzeilen, die Zeilen der Gruppe, die Bankbewegung mit ihrem Satz."""
    stock = _plain_stock(tmp_db)
    page = _page(tmp_db)
    table = Table(page)
    start = page.index("<table id='transactionsTable'>")
    html = page[start:page.index('</table>', start)]

    assert 'Forderung' not in html and 'bg-gray' not in html
    posted = [row for row in table.data_rows() if row.cells[SKR].text.strip().isdigit()]
    assert len(posted) == 6              # Aufwand, Einnahme, die zwei Sätze der Gruppe, die Bankbewegung (mit Satz zusammengefasst) und dieser Satz
    for row in posted:
        assert [badge.text for badge in row.cells[ACTIONS].badges] == [CHECK_MARK], row.cells[TEXT].text
        assert 'style' in row.cells[AMOUNT].attrs
    assert [badge.title for badge in table.row_of_group(f'b{stock.bank}').cells[ACTIONS].badges] == ['Bank + Buchungssätze vollständig']
    assert _badges(table.row('Ohne Konto')) == []
