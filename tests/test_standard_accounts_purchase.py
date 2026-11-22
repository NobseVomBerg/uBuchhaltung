# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Fünf Standardkonten mehr im Kontenrahmen und die Anzeige eines Steuersatzes mit Nachkommastellen in der Buchungsliste.

Der Standard-Kontenrahmen (``seed_data/chart_of_accounts_skr04.json``) führt fünf Konten mehr:

* 1400 „Abziehbare Vorsteuer“ – ohne Satz im Namen; die Auswertungen kennen den Bereich 1400–1439 schon als „gezahlte Vorsteuer“;
* 4100 „Steuerfreie Umsätze § 4 Nr. 8 ff. UStG“ und 4180 „Erlöse, die mit den Durchschnittssätzen des § 24 UStG versteuert werden“ – Erlöskonten, in
  ``get_revenue_coas()`` wählbar;
* 5170 und 5505, die Einkaufskonten mit 5,5 % Vorsteuer.

Ein frischer Bestand führt sie (ID = ``coa_id(4, Nummer)``); ein bestehender bekommt sie nur über „fehlende Standardkonten“
(``add_missing_standard_accounts``) – der Start legt nie Konten nach (``tests/test_seed_standard_accounts.py``). **Keine Auswertung hat eine eigene Regel für
diese Nummern** (eingeordnet wird allein nach dem Nummernbereich): EÜR, BWA, Dashboard, Summen- und Saldenliste, Liquidität und der Stapel für das
Steuerbüro der vorhandenen Welten sind mit und ohne die Konten im Rahmen Zeichen für Zeichen gleich. Eine Ausgabe mit 5,5 % (Maske) oder 7,8 % (Editor) wird
gespeichert, wie sie gesendet wird; der Stapel trägt dafür keinen BU-Schlüssel (die Ableitung kennt nur 19, 7, 16 und 5 %) und in Spalte 40 keinen Satz. Wie die
Auswertungen die Vorsteuer eines solchen Satzes ausweisen, hält diese Datei nicht fest.

Die Buchungsliste zeigt einen Satz mit Nachkommastellen mit Komma („5,5%“) statt wie bisher auf ganze Prozent gerundet („6%“); ganze Prozent bleiben, wie sie
waren (die frühere Formel steht als Vergleich im Test).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (Stichtage sind feste Tage).
"""
import csv
import html
import json
import os
import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from db import bwa, coa_id, datev_keys, susa
from db.matching import FORM_PURPOSE_FIRST
from db.reporting import euer_section, is_income_account
from money import to_minor
from server.app import parse_form
from server.handlers import handle_add_transaction
from server.pages_masterdata import PageSkr
from server.pages_transactions import PageTransactions
from tests import susa_helpers as helpers
from tests.test_bwa import demo_db, world  # noqa: F401  (Fixtures: die Testwelt und der Demobestand)
from tests.test_seed_standard_accounts import box_text, paragraph, reopen
from tests.test_susa import chart_rows, export_of, susa_world, without_accounts  # noqa: F401  (susa_world: die Fixture der SuSa-Welt)

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'chart_of_accounts_skr04.json')
FIRST_DAY, LAST_DAY = '2026-01-01', '2026-12-31'          # der Zeitraum der Liste und des Stapels

#: Die fünf Standardkonten: (Nummer, Name, Gruppe) – eine eigene Abschrift neben der Seed-Datei, damit ein Tippfehler dort auffällt
NEW_ACCOUNTS = [
    (1400, 'Abziehbare Vorsteuer', 'Umlaufvermögen'),
    (4100, 'Steuerfreie Umsätze § 4 Nr. 8 ff. UStG', 'Betriebliche Erträge'),
    (4180, 'Erlöse, die mit den Durchschnittssätzen des § 24 UStG versteuert werden', 'Betriebliche Erträge'),
    (5170, 'Einkauf Roh-, Hilfs- und Betriebsstoffe 5,5% Vorsteuer', 'Betriebliche Aufwendungen'),
    (5505, 'Wareneingang 5,5% Vorsteuer', 'Betriebliche Aufwendungen'),
]
NUMBERS = [number for number, _name, _group in NEW_ACCOUNTS]
IDS = [401400, 404100, 404180, 405170, 405505]            # Rahmen 4: 400000 + Nummer
BANKS = {1600, 1800, 1810}                                # die Geldkonten einer Testwelt: Als Zweckkonto zeigt die EÜR sie nie

# Die Spalten der Buchungstabelle
DATE, RECIPIENT, TEXT, AMOUNT, CURRENCY, RATE, ACCOUNT, SKR, DOCNR, ACTIONS = range(10)


# ── Hilfen ───────────────────────────────────────────────────────────────────

def seed_accounts():
    with open(SEED, encoding='utf-8') as handle:
        return json.load(handle)['accounts']


def all_rows(db):
    """Der ganze Kontenrahmen der Datenbank, nach ID – jede Spalte."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT ID, Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent, ShowInMenu '
                           'FROM ChartOfAccounts ORDER BY ID').fetchall()


def ids_of(db, *numbers):
    marks = ', '.join('?' * len(numbers))
    with closing(sqlite3.connect(db.db_name)) as con:
        return [row[0] for row in con.execute(f'SELECT ID FROM ChartOfAccounts WHERE Framework = 4 AND AccountNumber IN ({marks}) '
                                              f'ORDER BY AccountNumber', numbers)]


def with_a_bank_account(db):
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return helpers.account_id(db, 'Probe-Giro')


def entries_on(db, number):
    """Die IDs der Buchungssätze (nicht der Bankbewegungen), deren Zweckkonto ``number`` ist."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return [row[0] for row in con.execute("SELECT ID FROM Bookings WHERE BookingType = 'entry' AND COA_ID = ? ORDER BY ID",
                                              (helpers.coa(db, number),))]


def stored(db, booking_id):
    """``(TaxRate, TaxAmount)`` des Satzes, wie die Datenbank sie trägt."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT TaxRate, TaxAmount FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()


def datev_fields(db, monkeypatch, booking_id):
    """Die Felder der Zeile des Stapels (alle Sätze des Jahres 2026), die zu dieser Buchung gehört."""
    csv_bytes, ids, _rows = export_of(db, FIRST_DAY, LAST_DAY, monkeypatch)
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]          # ohne Vorlauf und Kopfzeile
    assert len(lines) == len(ids) and booking_id in ids, (len(lines), len(ids), booking_id)
    return next(csv.reader([lines[ids.index(booking_id)]], delimiter=';', quotechar='"'))


def list_page(db):
    return PageTransactions(db, date_from=FIRST_DAY, date_to=LAST_DAY)


def data_rows(page):
    """Die Zellen jeder Datenzeile der Buchungstabelle (ohne Markup, Entitäten aufgelöst), in der Reihenfolge der Seite."""
    start = page.index("<table id='transactionsTable'>")
    rows = []
    for row in re.findall(r'<tr.*?</tr>', page[start:page.index('</table>', start)], re.S):
        cells = [html.unescape(re.sub(r'<[^>]+>', '', cell)) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) == 10:
            rows.append(cells)
    return rows


def rate_by_text(db):
    """Die Spalte „St.%“ der Liste je Text der Zeile (jeder Text steht nur einmal da)."""
    shown = {}
    for cells in data_rows(list_page(db)):
        assert cells[TEXT] not in shown, cells[TEXT]
        shown[cells[TEXT]] = cells[RATE]
    return shown


def entry(db, rate, text, number):
    """Ein eigenständiger Satz „Aufwand an Verrechnung“ mit diesem Satz (der Anteil, wie die Datenbank ihn trägt): eine eigene Zeile der Liste.
    Jeder Satz trägt eine eigene Beleg-Nr., sonst fasste die Liste gleiche Belege zu einer Gruppe zusammen."""
    return db.insert_booking('2026-03-11', Decimal('-1.00'), coa_id=helpers.coa(db, 6815), counter_coa_id=helpers.coa(db, 1460), booking_type='entry',
                             tax_rate=rate, document_number=f'E{number}', text=text)


def post_editor(db, bank, account, amount, rows):
    """Der Buchungssatz-Editor einer Bankbewegung, wie der Browser ihn schickt (urlencode, dann ``parse_form``): ``rows`` = ``[(Konto, Betrag, Satz, Steuer)]``."""
    fields = {'transaction_id': [str(bank)], 'date': ['2026-03-12'], 'amount': [amount], 'account': [str(account)], 'currency': ['EUR'],
              'split_id': [''] * len(rows), 'split_coa': [str(helpers.coa(db, row[0])) for row in rows], 'split_amount': [row[1] for row in rows],
              'split_tax_rate': [row[2] for row in rows], 'split_tax_amount': [row[3] for row in rows],
              'split_docnr': [''] * len(rows), 'split_text': [''] * len(rows)}
    status, location = handle_add_transaction(db, parse_form(urlencode([(key, value) for key, values in fields.items() for value in values])))
    assert status == 303, (status, location)


# ═══ Die Seed-Datei ═══════════════════════════════════════════════════════════

def test_the_seed_file_stays_ascending_unique_and_every_account_carries_exactly_the_four_fields():
    accounts = seed_accounts()
    numbers = [account['account_number'] for account in accounts]
    assert numbers == sorted(set(numbers))                                       # aufsteigend und ohne Dublette
    for account in accounts:
        assert set(account) == {'account_number', 'name', 'description', 'is_standard'}, account
        assert type(account['account_number']) is int and type(account['is_standard']) is bool, account
        assert isinstance(account['name'], str) and account['name'] == account['name'].strip() != '', account
        assert isinstance(account['description'], str) and account['description'], account


def test_the_seed_file_names_the_five_accounts_with_their_group_and_the_standard_flag():
    by_number = {account['account_number']: account for account in seed_accounts()}
    for number, name, group in NEW_ACCOUNTS:
        assert by_number[number] == {'account_number': number, 'name': name, 'description': group, 'is_standard': True}


# ═══ Ein frischer Bestand und ein Bestand von vorher ══════════════════════════

def test_a_fresh_book_leads_the_five_accounts_with_id_name_group_and_standard_flag_of_the_seed_file(tmp_db):
    assert [coa_id(4, number) for number in NUMBERS] == IDS
    assert ids_of(tmp_db, *NUMBERS) == IDS
    assert chart_rows(tmp_db, *NUMBERS) == [(number, name, group, 1) for number, name, group in NEW_ACCOUNTS]
    assert tmp_db.missing_standard_accounts() == []                              # sie fehlen dem frischen Bestand nicht


def test_a_book_without_the_five_accounts_names_exactly_them_as_missing_and_creates_them_on_request(tmp_db, tmp_path):
    """So sieht ein Bestand von vor dieser Version aus: Der Start legt die Konten nicht nach, ``missing_standard_accounts`` nennt sie (aufsteigend, mit dem
    Namen der Seed-Datei), ``add_missing_standard_accounts`` legt genau diese an – danach ist der Kontenrahmen Zeile für Zeile der eines frischen Bestands."""
    older = without_accounts(tmp_db, tmp_path / 'alt.db', *NUMBERS)
    assert all_rows(older) == [row for row in all_rows(tmp_db) if row[2] not in NUMBERS]
    assert chart_rows(reopen(older.db_name), *NUMBERS) == []                     # ein Neustart legt nichts nach
    assert older.missing_standard_accounts() == [(number, name) for number, name, _group in NEW_ACCOUNTS]
    assert older.add_missing_standard_accounts() == 5
    assert older.missing_standard_accounts() == []
    assert all_rows(older) == all_rows(tmp_db)
    assert older.add_missing_standard_accounts() == 0                            # ein zweiter Klick legt nichts mehr an


def test_the_chart_page_of_a_book_without_the_five_accounts_names_them_in_its_hint(tmp_db, tmp_path):
    older = without_accounts(tmp_db, tmp_path / 'alt.db', *NUMBERS)
    assert box_text(PageSkr(older)) == paragraph([(number, name) for number, name, _group in NEW_ACCOUNTS])
    assert box_text(PageSkr(tmp_db)) is None                                     # der frische Bestand führt sie: kein Hinweis


# ═══ Einordnung je Konto ══════════════════════════════════════════════════════

@pytest.mark.parametrize('number', [4100, 4180])
def test_the_vat_free_and_the_flat_rate_revenue_accounts_are_revenue_accounts_everywhere(number, tmp_db):
    assert euer_section(number, BANKS) == 'income' and is_income_account(number)
    assert bwa.group_of(number) == 'revenue'
    assert susa.group_key(number) == 'revenue' and not susa.is_carried(number)
    for inflow in (True, False):                                                 # Einnahme und Erstattung
        assert datev_keys.booking_kind(FORM_PURPOSE_FIRST, number, 1800, inflow) == datev_keys.KIND_VAT
    assert (coa_id(4, number), number) in [row[:2] for row in tmp_db.get_revenue_coas()]       # als Erlöskonto einer Zahlung wählbar


def test_the_revenue_choice_of_a_book_without_the_two_revenue_accounts_lacks_exactly_them(tmp_db, tmp_path):
    older = without_accounts(tmp_db, tmp_path / 'alt.db', *NUMBERS)
    fresh_numbers = [row[1] for row in tmp_db.get_revenue_coas()]
    assert fresh_numbers == sorted(fresh_numbers) and {4100, 4180} <= set(fresh_numbers)
    assert [row for row in tmp_db.get_revenue_coas() if row[1] not in (4100, 4180)] == older.get_revenue_coas()
    older.add_missing_standard_accounts()
    assert older.get_revenue_coas() == tmp_db.get_revenue_coas()


def test_the_input_tax_account_1400_is_a_paid_input_tax_line_of_the_euer_and_a_balance_account():
    assert euer_section(1400, BANKS) == 'expense' and not is_income_account(1400)
    assert bwa.group_of(1400) == 'input_vat'                                     # „Gezahlte Vorsteuer“
    assert susa.group_key(1400) == 'current_assets' and susa.is_carried(1400)    # ein Bestandskonto: sein Saldo wird vorgetragen


@pytest.mark.parametrize('number', [5170, 5505])
def test_a_purchase_account_with_5_5_percent_input_tax_is_a_material_expense_without_a_datev_key(number):
    assert euer_section(number, BANKS) == 'expense' and not is_income_account(number)
    assert bwa.group_of(number) == 'material'
    assert susa.group_key(number) == 'expenses' and not susa.is_carried(number)
    for inflow in (False, True):                                                 # Ausgabe und Erstattung: Vorsteuer, nie Umsatzsteuer
        assert datev_keys.booking_kind(FORM_PURPOSE_FIRST, number, 1800, inflow) == datev_keys.KIND_INPUT
    for rate in (0.055, 0.078, 5.5):
        for day in ('2026-03-10', '2020-09-01'):                                 # auch in den befristeten Sätzen von Juli bis Dezember 2020
            assert datev_keys.derive_key(datev_keys.KIND_INPUT, rate, day) == 0
        assert not datev_keys.is_vat_row(0, datev_keys.KIND_INPUT, rate)


# ═══ Die Konten im Rahmen ändern keine Zahl ═══════════════════════════════════

def assert_the_five_accounts_change_no_number(db, tmp_path, monkeypatch, spans):
    """Derselbe Bestand mit und ohne die fünf Konten im Kontenrahmen: EÜR (``get_euer_data``), BWA und Liquidität in beiden Darstellungen, Dashboard,
    Summen- und Saldenliste und der Stapel für das Steuerbüro (Byte für Byte) sind gleich. Spannen: ``(von, bis, Jahr, Monat)`` – ``von`` und ``bis`` für
    EÜR, Dashboard und Stapel, Jahr und Monat für die Berichte eines Monats."""
    older = without_accounts(db, tmp_path / 'ohne.db', *NUMBERS)
    for date_from, date_to, year, month in spans:
        where = (date_from, date_to)
        assert older.get_euer_data(date_from, date_to) == db.get_euer_data(date_from, date_to) != [], where
        for variant in ('vorjahr', 'jahr'):
            assert older.get_bwa(year, month, variant) == db.get_bwa(year, month, variant), (where, variant)
            assert older.get_liquidity(year, month, variant) == db.get_liquidity(year, month, variant), (where, variant)
        assert older.get_dashboard(date_from, date_to) == db.get_dashboard(date_from, date_to), where
        assert older.get_susa(year, month) == db.get_susa(year, month), where
        assert export_of(older, date_from, date_to, monkeypatch)[0] == export_of(db, date_from, date_to, monkeypatch)[0], where


def test_the_five_accounts_in_the_chart_change_no_number_of_the_susa_world(susa_world, tmp_path, monkeypatch):
    assert_the_five_accounts_change_no_number(susa_world.db, tmp_path, monkeypatch, [
        ('2025-01-01', '2025-12-31', 2025, 12), ('2026-01-01', '2026-12-31', 2026, 12), ('2026-03-01', '2026-03-31', 2026, 3)])


def test_the_five_accounts_in_the_chart_change_no_number_of_the_test_world(world, tmp_path, monkeypatch):
    assert_the_five_accounts_change_no_number(world, tmp_path, monkeypatch, [
        ('2026-01-01', '2026-12-31', 2026, 12), ('2026-03-01', '2026-03-31', 2026, 3)])


def test_the_five_accounts_in_the_chart_change_no_number_of_the_demo_stock(demo_db, tmp_path, monkeypatch):
    assert_the_five_accounts_change_no_number(demo_db, tmp_path, monkeypatch, [
        ('2025-01-01', '2025-12-31', 2025, 12), ('2026-01-01', '2026-10-31', 2026, 10)])


# ═══ Eine Ausgabe auf den Einkaufskonten: gespeichert, wie sie gesendet wird ═══

def test_an_expense_with_5_5_percent_on_5505_through_the_mask_is_saved_as_sent_and_exported_without_a_key(tmp_db, monkeypatch):
    with_a_bank_account(tmp_db)
    helpers.mask(tmp_db, 'Probe-Giro', 5505, '-105.50', '2026-03-10', '5.5', '-5.50', document='ER-55')
    (booking,) = entries_on(tmp_db, 5505)
    assert stored(tmp_db, booking) == (pytest.approx(0.055), to_minor(Decimal('-5.50')))
    fields = datev_fields(tmp_db, monkeypatch, booking)
    assert (fields[6], fields[7], fields[8], fields[40]) == ('5505', '1800', '0', '')       # Konto, Gegenkonto, BU-Schlüssel, Satz in Spalte 40
    shown = [cells[RATE] for cells in data_rows(list_page(tmp_db)) if cells[SKR] == '5505']
    assert shown == ['5,5%', '5,5%']                                             # die Zeile der Bankbewegung und ihr Satz darunter


def test_an_expense_with_7_8_percent_on_5170_through_the_editor_is_saved_as_sent_and_exported_without_a_key(tmp_db, monkeypatch):
    giro = with_a_bank_account(tmp_db)
    bank = tmp_db.insert_booking('2026-03-12', Decimal('-213.30'), account_id=giro, booking_type='bank', recipient_client='Probe-Händler')
    post_editor(tmp_db, bank, giro, '-213.30', [(5170, '-107.80', '7.8', '-7.80'), (5505, '-105.50', '5.5', '-5.50')])
    (first,), (second,) = entries_on(tmp_db, 5170), entries_on(tmp_db, 5505)
    assert stored(tmp_db, first) == (pytest.approx(0.078), to_minor(Decimal('-7.80')))
    assert stored(tmp_db, second) == (pytest.approx(0.055), to_minor(Decimal('-5.50')))
    for booking, number in ((first, '5170'), (second, '5505')):
        fields = datev_fields(tmp_db, monkeypatch, booking)
        assert (fields[6], fields[7], fields[8], fields[40]) == (number, '1800', '0', ''), number
    rows = data_rows(list_page(tmp_db))
    assert [cells[RATE] for cells in rows if cells[SKR] == 'Split 2×'] == ['']               # die Kopfzeile der Bankbewegung nennt keinen Satz
    assert {cells[SKR]: cells[RATE] for cells in rows if cells[SKR] in ('5170', '5505')} == {'5170': '7,8%', '5505': '5,5%'}


# ═══ Die Spalte „St.%“ der Buchungsliste ═════════════════════════════════════

def test_the_list_shows_every_whole_percent_rate_as_before(tmp_db):
    """Jeder ganzzahlige Satz von 0 bis 100 als Anteil (``p / 100``) steht wie bisher da: die frühere Formel – auf ganze Prozent gerundet – ist hier abgeschrieben."""
    for percent in range(101):
        entry(tmp_db, percent / 100, f'Satz {percent}', percent)
    shown = rate_by_text(tmp_db)
    assert {text: shown[text] for text in shown if text.startswith('Satz ')} == {f'Satz {p}': f'{round(p / 100 * 100)}%' for p in range(101)}
    assert (shown['Satz 19'], shown['Satz 7'], shown['Satz 0'], shown['Satz 100']) == ('19%', '7%', '0%', '100%')


@pytest.mark.parametrize('rate, text', [(0.055, '5,5%'), (0.107, '10,7%'), (0.078, '7,8%'), (0.0725, '7,25%'), (0.1234, '12,34%'),
                                        (0.0549999, '5,5%'), (0.19004, '19%'), (0.195, '19,5%')])
def test_the_list_shows_a_rate_with_decimals_with_up_to_two_places_and_a_comma(tmp_db, rate, text):
    entry(tmp_db, rate, 'Satz', 1)
    assert rate_by_text(tmp_db)['Satz'] == text


def test_the_list_shows_no_rate_where_there_is_none_or_it_is_unreadable(tmp_db):
    """Kein Satz, ein leerer Text und ein Text, der keine Zahl ist (so etwas schreibt nur die SQL-Konsole): die Zelle bleibt leer, die Seite steht."""
    entry(tmp_db, None, 'Ohne Satz', 1)
    helpers.raw(tmp_db, "UPDATE Bookings SET TaxRate = ? WHERE ID = ?", '', entry(tmp_db, 0.19, 'Leerer Satz', 2))
    helpers.raw(tmp_db, "UPDATE Bookings SET TaxRate = ? WHERE ID = ?", 'x', entry(tmp_db, 0.19, 'Kein Satz', 3))
    assert rate_by_text(tmp_db) == {'Ohne Satz': '', 'Leerer Satz': '', 'Kein Satz': ''}
