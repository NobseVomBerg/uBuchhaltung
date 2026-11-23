# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Konto im Kontenrahmen eines Bankkontos: Auswahl statt freier Zahl (2.8.1).

Anlass: Das Formular Stammdaten → Bankkonten nahm als „SKR-Gegenkonto“ eine freie Zahl. Wer 1200 eintrug (die Banknummer eines anderen
Kontenrahmens), merkte es erst beim Zuordnen einer Zahlung: Das Konto steht nicht im Kontenrahmen. Jetzt gilt:

* Das Formular wählt aus den Konten des Kontenrahmens (Bank 1700–1899, Kasse 1600–1699); ein neues Bankkonto bekommt die kleinste freie
  Nummer vorgewählt (1800, dann 1810 …), ein vorhandener Wert, den die Auswahl nicht kennt (1200, 1460), steht als eigene gewählte
  Option da – nichts wird still umgestellt. Die Liste nennt in Warnfarbe, wo das Konto fehlt oder der Kontenrahmen die Nummer nicht kennt.
* Der Handler (``handle_add_bankaccount``, ``handle_update_bankaccount``, EINE Prüfung) lehnt eine neue oder geänderte Nummer ab, die nicht
  im Kontenrahmen steht, außerhalb des Bereichs liegt oder schon ein anderes eigenes Konto trägt – mit festen Sätzen, nichts gespeichert.
  Ein unverändert zurückgeschickter Wert ist nie ein Verstoß: Der Bestand bleibt speicherbar. Die Sperre „SKR-Konto nicht geändert: n
  Buchungssätze …“ bleibt, wie sie war, und kommt nach den neuen Prüfungen.
* Beim Zuordnen einer Zahlung sagt die Meldung, was zu tun ist (``Database._bank_coa_of``).
* Prüfen und Anlegen (bzw. Ändern) gehören zusammen: Die Handler (auch die Ersteinrichtung) laufen unter EINER Sperre
  (``handlers._BANK_ACCOUNT_LOCK``) – zwei Anfragen zugleich bekommen nie dieselbe freie Nummer. Als vergeben gilt auch eine Nummer, die als
  Text in der Spalte steht (``handlers._carries_skr_number``; SQLite wandelt sie nicht in eine Zahl um, wenn etwa ein geschütztes
  Leerzeichen daran hängt) – bei der Prüfung wie bei der Vorauswahl (``free_bank_skr_number``).

Die Datenbankschicht (``insert_account``, ``update_account``) prüft weiter nichts: Importe, Demodaten und ältere Bestände tragen auch andere
Nummern – wo ein Test hier einen solchen Bestand braucht, legt er ihn über sie an. Namen, Nummern und IBANs sind erfundene Testwerte.
"""
import json
import re
import threading
import time
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from db import Database
from server import handlers
from server.pages_masterdata import PageBankAccounts, SKR_ACCOUNT_HINT
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, post

IBAN_A = 'DE89370400440532013000'
IBAN_B = 'DE02120300000000202051'
IBAN_C = 'DE02200505501015871393'
IMG = '<img src=x onerror=alert(1)>'

# Die Sätze stehen hier ausgeschrieben, nicht aus ``handlers`` gelesen: Ändert sich einer, schlägt der Test an.
NOT_IN_CHART = ('Das Konto {n} steht nicht im Kontenrahmen – bitte eines aus der Liste wählen oder es zuerst unter '
                'Stammdaten → Kontenrahmen anlegen.')
OUT_OF_RANGE = 'Für ein Bankkonto bitte ein Konto von 1700 bis 1899 wählen (für die Kasse 1600 bis 1699).'
IN_USE = 'Das Konto {n} ist schon einem anderen Bank- oder Kassenkonto zugeordnet.'
HINT = 'Ohne dieses Konto lassen sich Zahlungen keiner Rechnung zuordnen.'
LOCKED = 'SKR-Konto nicht geändert: {n} Buchungssätze führen das bisherige oder das neue Konto.'
NO_NUMBER_TEXT = ('Das Bankkonto hat kein Konto im Kontenrahmen – bitte unter Stammdaten → Bankkonten eines wählen (üblich: 1800), '
                  'sonst lässt sich die {what} nicht kontieren.')
UNKNOWN_NUMBER_TEXT = ('Das Bankkonto trägt die Nummer {n}, die nicht im Kontenrahmen steht – bitte unter Stammdaten → Bankkonten '
                       'ein Konto aus dem Kontenrahmen wählen (üblich: 1800), sonst lässt sich die {what} nicht kontieren.')

NONE_LABEL = '-- keines --'
BANK_OPTIONS = [('1800', '1800 – Bank 0'), ('1810', '1810 – Bank 1'), ('1820', '1820 – Bank 2')]   # der Kontenrahmen einer frischen Datenbank
CASH_OPTIONS = [('1600', '1600 – Kasse'), ('1610', '1610 – Nebenkasse 1')]


# ── Hilfen ───────────────────────────────────────────────────────────────────

class _Page(HTMLParser):
    """Aus der Seite der Bankkonten: die Optionen der Auswahl ``skr_account`` als ``(Wert, Text, gewählt)``, die Texte der Randbemerkungen
    (``<small class="hint">``) und der Warnhinweise (``<span class="warnColor">``) und alle Starttags – wie ein Browser sie liest (Entitäten
    aufgelöst)."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.options, self.hints, self.warnings, self.tags = [], [], [], []
        self._in_select, self._option, self._note = False, None, None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == 'select':
            self._in_select = attrs.get('name') == 'skr_account'
        elif tag == 'option' and self._in_select:
            self._option = [attrs.get('value'), '', 'selected' in attrs]
        elif (tag, attrs.get('class')) in (('small', 'hint'), ('span', 'warnColor')):
            self._note = [tag, attrs['class'], '']

    def handle_endtag(self, tag):
        if tag == 'select':
            self._in_select = False
        elif tag == 'option' and self._option is not None:
            self.options.append(tuple(self._option))
            self._option = None
        elif self._note is not None and tag == self._note[0]:
            (self.hints if self._note[1] == 'hint' else self.warnings).append(self._note[2])
            self._note = None

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data
        if self._note is not None:
            self._note[2] += data


def _page(db, edit=None):
    return _Page(PageBankAccounts(db, edit_id=edit))


def _selected(page):
    return [option for option in page.options if option[2]]


def _choices(selected='', extra=(), base=BANK_OPTIONS):
    """Die erwartete Auswahl einer frischen Datenbank: „-- keines --“, dann die Konten des Kontenrahmens (``base``) und ``extra``
    (``(Wert, Text)``), nach Nummer; gewählt ist der Eintrag mit dem Wert ``selected`` (``''``: „-- keines --“)."""
    entries = sorted([*base, *extra])
    return [('', NONE_LABEL, selected == '')] + [(value, label, value == selected) for value, label in entries]


def _rows(db, table='Accounts'):
    with closing(db._get_connection()) as con:
        return con.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()


def _untouched(db):
    """Alles, was die Verwaltung der Bankkonten nicht anfassen darf, wenn sie ablehnt."""
    return [_rows(db, table) for table in ('Accounts', 'ChartOfAccounts', 'Bookings')]


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as con:
        con.execute(sql, params)
        con.commit()


def _account(db, name):
    return next(row for row in db.fetch_accounts() if row[1] == name)


def _skr(db, name):
    return _account(db, name)[7]


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _chart_name(db, number):
    return next(row[3] for row in db.fetch_chart_of_accounts() if row[2] == number)


def _cash_id(db):
    return next(row[0] for row in db.fetch_accounts() if row[6] == 1)


def _as_old_cash(db, number):
    """Die Kasse auf ``number`` (``None``: leer) – an der Verwaltung vorbei, wie eine ältere Datenbank sie trägt."""
    row = _account(db, 'Kasse')
    db.update_account(row[0], row[1], row[2], row[3], row[4], row[5], skr_account=number)
    return row[0]


def _add(db, name='Giro Neu', skr='', **extra):
    form = {'name': name, 'holder': 'Erika Probe', 'iban': IBAN_C, 'bic': 'TESTDEFF', 'bank_name': 'Testbank', 'skr_account': skr}
    form.update(extra)
    return handlers.handle_add_bankaccount(db, {key: [value] for key, value in form.items()})


def _update(db, account_id, name, skr, **extra):
    form = {'id': str(account_id), 'name': name, 'holder': 'Erika Probe', 'iban': IBAN_C, 'bic': 'TESTDEFF', 'bank_name': 'Testbank',
            'skr_account': '' if skr is None else str(skr)}
    form.update(extra)
    return handlers.handle_update_bankaccount(db, {key: [value] for key, value in form.items()})


def _error(location):
    """Der Satz einer Ablehnung (``error`` in der Adresse); sonst ``None``."""
    query = parse_qs(urlsplit(location).query)
    return query['error'][0] if 'error' in query else None


def _shown_error(html):
    """Die Meldung des ``appMsg``-Aufrufs der Seite (JSON-Text, Art ``'error'``); ohne Aufruf ``None``."""
    for script in Dom(html).scripts:
        found = re.search(r'function\(\)\{appMsg\(', script)
        if found:
            value, position = json.JSONDecoder().raw_decode(script, found.end())
            assert script[position:].startswith(",'error');})"), script[position:position + 30]
            return value
    return None


@pytest.fixture
def two(tmp_db):
    """Zwei Bankkonten auf 1800 und 1810; die Kasse trägt 1600."""
    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1800)
    tmp_db.insert_account('Giro Beta', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank', skr_account=1810)
    return tmp_db


# ── Das Formular eines neuen Kontos ──────────────────────────────────────────

def test_the_new_account_form_is_a_choice_from_the_chart_and_starts_with_1800(tmp_db):
    html = PageBankAccounts(tmp_db)
    page = _Page(html)

    assert page.options == _choices(selected='1800')                                # nur der Bereich 1700–1899, 1800 vorgewählt
    assert [a for tag, a in page.tags if tag == 'input' and a.get('name') == 'skr_account'] == []      # keine freie Zahl mehr
    assert 'Konto im Kontenrahmen:' in html and 'SKR-Gegenkonto' not in html
    assert page.hints.count(HINT) == 1 and page.warnings == []


def test_the_hint_under_the_field_is_one_short_sentence_without_the_pointer_to_the_chart():
    assert SKR_ACCOUNT_HINT == HINT
    assert 'Kontenrahmen' not in HINT                                               # der Verweis steht in der Ablehnung, nicht hier


def test_each_assignment_moves_the_preselection_to_the_next_free_number(tmp_db):
    selected_after = []
    for name, number in (('Giro Alpha', 1800), ('Giro Beta', 1810), ('Giro Gamma', 1820)):
        tmp_db.insert_account(name, 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=number)
        selected_after.append(_selected(_page(tmp_db)))

    assert selected_after == [[('1810', '1810 – Bank 1', True)], [('1820', '1820 – Bank 2', True)], [('', NONE_LABEL, True)]]
    assert _page(tmp_db).options == _choices()                                      # alle vergeben: „-- keines --“, die Liste bleibt


def test_the_preselection_is_the_smallest_free_number_not_the_one_after_the_last(tmp_db):
    tmp_db.insert_account('Giro Beta', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank', skr_account=1810)
    assert _selected(_page(tmp_db)) == [('1800', '1800 – Bank 0', True)]            # 1800 ist frei, 1810 vergeben

    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1800)
    tmp_db.insert_account('Giro Gamma', 'Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank', skr_account=1830)
    assert _selected(_page(tmp_db)) == [('1820', '1820 – Bank 2', True)]            # 1830 steht nicht im Kontenrahmen: zählt nicht


def test_numbers_outside_the_range_and_the_cash_account_never_block_the_preselection(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    tmp_db.insert_account('Giro Hoch', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank', skr_account=1460)
    assert _selected(_page(tmp_db)) == [('1800', '1800 – Bank 0', True)]            # die Kasse trägt 1600 – ebenfalls kein Hindernis


def test_a_chart_account_added_by_the_user_is_offered_and_becomes_the_next_preselection(tmp_db):
    assert tmp_db.insert_chart_of_accounts(4, 1830, 'Bank 3', 'Umlaufvermögenskonten')
    for name, number in (('Giro Alpha', 1800), ('Giro Beta', 1810), ('Giro Gamma', 1820)):
        tmp_db.insert_account(name, 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=number)

    assert _page(tmp_db).options == _choices(selected='1830', extra=[('1830', '1830 – Bank 3')])


def test_the_range_limits_are_inclusive_and_the_two_selections_do_not_overlap(tmp_db):
    for number in (1599, 1699, 1700, 1899, 1900):
        assert tmp_db.insert_chart_of_accounts(4, number, f'Grenze {number}', 'Test')

    bank = [value for value, _label, _chosen in _page(tmp_db).options]
    cash = [value for value, _label, _chosen in _page(tmp_db, _cash_id(tmp_db)).options]

    assert bank == ['', '1700', '1800', '1810', '1820', '1899']                     # 1699 und 1900 nicht, 1700 und 1899 ja
    assert cash == ['', '1600', '1610', '1699']                                     # 1599 und 1700 nicht, 1600 und 1699 ja


def test_a_number_in_two_frameworks_is_one_option_named_like_the_account_a_payment_uses(tmp_db):
    """``get_coa_id_by_account_number`` – so findet die Zahlung das Konto des Bankkontos – nimmt das erste Konto der Nummer."""
    assert tmp_db.insert_chart_of_accounts(3, 1800, 'Privatentnahmen allgemein', 'Test')
    name = next(row[3] for row in tmp_db.fetch_chart_of_accounts() if row[0] == _coa(tmp_db, 1800))

    assert [option for option in _page(tmp_db).options if option[0] == '1800'] == [('1800', f'1800 – {name}', True)]


def test_a_chart_name_is_masked_in_the_option(tmp_db):
    tmp_db.insert_chart_of_accounts(4, 1830, IMG, 'Test')
    html = PageBankAccounts(tmp_db)

    assert IMG not in html and '1830 – &lt;img src=x onerror=alert(1)&gt;' in html
    assert Dom(html).all('img') == []
    assert ('1830', f'1830 – {IMG}', False) in _Page(html).options                  # als Text, so wie ein Browser ihn liest


# ── Das Formular eines vorhandenen Kontos: der gespeicherte Wert bleibt ──────

def test_an_existing_account_shows_its_stored_number_chosen(two):
    assert _page(two, _account(two, 'Giro Beta')[0]).options == _choices(selected='1810')


def test_a_number_from_another_chart_is_shown_as_its_own_chosen_option_and_the_list_warns(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    account = _account(tmp_db, 'Giro Alt')[0]

    page = _page(tmp_db, account)

    assert page.options == _choices(selected='1200', extra=[('1200', '1200 – nicht im Kontenrahmen')])
    assert page.warnings == ['⚠ 1200 nicht im Kontenrahmen']                        # die Liste, in Warnfarbe (vorhandene Klasse)
    assert _skr(tmp_db, 'Giro Alt') == 1200                                         # das Öffnen ändert nichts


def test_a_number_in_the_range_that_the_chart_does_not_know_is_shown_the_same_way(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1830)

    page = _page(tmp_db, _account(tmp_db, 'Giro Alt')[0])

    assert page.options == _choices(selected='1830', extra=[('1830', '1830 – nicht im Kontenrahmen')])
    assert page.warnings == ['⚠ 1830 nicht im Kontenrahmen']


def test_a_number_outside_the_range_that_the_chart_knows_shows_the_chart_name_and_no_warning(tmp_db):
    tmp_db.insert_account('Giro Hoch', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1460)

    page = _page(tmp_db, _account(tmp_db, 'Giro Hoch')[0])

    assert _selected(page) == [('1460', f'1460 – {_chart_name(tmp_db, 1460)}', True)]
    assert page.warnings == []                                                      # im Kontenrahmen: kein Hinweis in der Liste


def test_an_account_without_a_number_shows_no_choice_and_the_list_warns(tmp_db):
    tmp_db.insert_account('Giro Leer', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank')

    page = _page(tmp_db, _account(tmp_db, 'Giro Leer')[0])

    assert page.options == _choices()                                               # „-- keines --“ gewählt: nichts wird vorgeschlagen
    assert page.warnings == ['⚠ kein Konto im Kontenrahmen']


def test_the_list_warns_for_each_account_that_cannot_be_booked_and_only_for_those(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    tmp_db.insert_account('Giro Leer', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank')
    tmp_db.insert_account('Giro Gut', 'Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank', skr_account=1800)

    assert _page(tmp_db).warnings == ['⚠ 1200 nicht im Kontenrahmen', '⚠ kein Konto im Kontenrahmen']      # nicht die Kasse, nicht 1800


def test_a_stored_value_that_is_no_number_is_masked_and_refused_by_the_strict_reader(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    _execute(tmp_db, 'UPDATE Accounts SET SKRAccount = ? WHERE Name = ?', (IMG, 'Giro Alt'))
    account = _account(tmp_db, 'Giro Alt')[0]
    html = PageBankAccounts(tmp_db, edit_id=account)
    page = _Page(html)

    assert IMG not in html and Dom(html).all('img') == []
    assert _selected(page) == [(IMG, f'{IMG} – nicht im Kontenrahmen', True)]
    assert page.warnings == [f'⚠ {IMG} nicht im Kontenrahmen']
    before = _rows(tmp_db)
    assert _update(tmp_db, account, 'Giro Alt', IMG) == (303, f"/masterdata/bankaccounts/edit?id={account}&error="
                                                                f"{quote(handlers.SKR_ACCOUNT_ERROR, safe='')}")
    assert _rows(tmp_db) == before                                                  # wie bisher: keine Zahl, nichts gespeichert


# ── Die Kasse ────────────────────────────────────────────────────────────────

def test_the_cash_form_offers_1600_to_1699_and_has_the_same_hint(tmp_db):
    page = _page(tmp_db, _cash_id(tmp_db))

    assert page.options == _choices(selected='1600', base=CASH_OPTIONS)
    assert page.hints == [HINT] and page.warnings == []
    assert 'Konto im Kontenrahmen:' in PageBankAccounts(tmp_db, edit_id=_cash_id(tmp_db))


def test_an_old_cash_account_on_1460_keeps_its_number_as_its_own_option(tmp_db):
    kasse = _as_old_cash(tmp_db, 1460)

    page = _page(tmp_db, kasse)

    assert page.options == _choices(selected='1460', extra=[('1460', f'1460 – {_chart_name(tmp_db, 1460)}')], base=CASH_OPTIONS)
    assert page.warnings == []                                                      # 1460 steht im Kontenrahmen


def test_an_old_cash_account_without_a_number_is_listed_with_the_warning(tmp_db):
    kasse = _as_old_cash(tmp_db, None)

    page = _page(tmp_db, kasse)

    assert page.options == _choices(base=CASH_OPTIONS)
    assert page.warnings == ['⚠ kein Konto im Kontenrahmen']                        # „die Kasse ohne Nummer ebenso“


def test_in_an_old_database_without_1600_the_cash_form_keeps_the_stored_number_as_its_only_choice(tmp_db):
    _execute(tmp_db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610)')

    page = _page(tmp_db, _cash_id(tmp_db))

    assert page.options == [('', NONE_LABEL, False), ('1600', '1600 – nicht im Kontenrahmen', True)]
    assert page.warnings == ['⚠ 1600 nicht im Kontenrahmen']


# ── Speichern: jede Ablehnung ────────────────────────────────────────────────

ADD_REFUSALS = [
    pytest.param('1200', NOT_IN_CHART.format(n=1200), id='SKR03-Bank-1200'),
    pytest.param('1830', NOT_IN_CHART.format(n=1830), id='im-Bereich-aber-nicht-im-Kontenrahmen'),
    pytest.param('99999', NOT_IN_CHART.format(n=99999), id='groesste-Zahl-des-Feldes'),
    pytest.param('1460', OUT_OF_RANGE, id='Verrechnungskonto-1460'),
    pytest.param('1600', OUT_OF_RANGE, id='Kassenkonto-1600-ausserhalb-und-vergeben'),
    pytest.param('1610', OUT_OF_RANGE, id='Kassenbereich-1610'),
    pytest.param('1800', IN_USE.format(n=1800), id='schon-vergeben-1800'),
    pytest.param('1810', IN_USE.format(n=1810), id='schon-vergeben-1810'),
]


@pytest.mark.parametrize('number, sentence', ADD_REFUSALS)
def test_a_new_account_with_a_wrong_number_is_refused_and_nothing_is_stored(two, number, sentence):
    before = _untouched(two)

    status, location = _add(two, 'Giro Neu', number)

    assert (status, location) == (303, f"/masterdata/bankaccounts?error={quote(sentence, safe='')}")
    assert _error(location) == sentence
    assert _untouched(two) == before                                                # Accounts (und alles andere) vorher = nachher
    assert [row[1] for row in two.fetch_accounts()] == ['Kasse', 'Giro Alpha', 'Giro Beta']


@pytest.mark.parametrize('number, sentence', [
    pytest.param('1200', NOT_IN_CHART.format(n=1200), id='SKR03-Bank-1200'),
    pytest.param('1830', NOT_IN_CHART.format(n=1830), id='im-Bereich-aber-nicht-im-Kontenrahmen'),
    pytest.param('1460', OUT_OF_RANGE, id='Verrechnungskonto-1460'),
    pytest.param('1600', OUT_OF_RANGE, id='Kassenbereich-1600'),
    pytest.param('1800', IN_USE.format(n=1800), id='schon-von-Giro-Alpha-getragen'),
])
def test_changing_to_a_wrong_number_is_refused_with_the_sentence_and_nothing_is_stored(two, number, sentence):
    beta = _account(two, 'Giro Beta')[0]
    before = _untouched(two)

    status, location = _update(two, beta, 'Giro Beta neu', number)

    assert (status, location) == (303, f"/masterdata/bankaccounts/edit?id={beta}&error={quote(sentence, safe='')}")
    assert _untouched(two) == before                                                # auch der neue Name ist nicht angekommen
    assert _skr(two, 'Giro Beta') == 1810


@pytest.mark.parametrize('number, sentence', [
    pytest.param('1200', NOT_IN_CHART.format(n=1200), id='1200'),
    pytest.param('1800', OUT_OF_RANGE, id='Bankbereich-1800'),
    pytest.param('1460', OUT_OF_RANGE, id='Verrechnungskonto-1460'),
    pytest.param('1650', NOT_IN_CHART.format(n=1650), id='im-Kassenbereich-aber-nicht-im-Kontenrahmen'),
])
def test_the_cash_account_takes_only_chart_accounts_of_the_cash_range(tmp_db, number, sentence):
    before = _untouched(tmp_db)

    status, location = _update(tmp_db, _cash_id(tmp_db), 'Kasse', number)

    assert status == 303 and _error(location) == sentence
    assert _untouched(tmp_db) == before


def test_the_checks_run_in_the_order_chart_range_use_and_a_bad_input_keeps_its_old_sentence(two):
    """1200: nicht im Kontenrahmen (nicht „außerhalb des Bereichs“); 1600: außerhalb des Bereichs (nicht „vergeben“, die Kasse trägt es);
    eine Eingabe, die keine Zahl ist, bleibt bei ihrem Satz – die Prüfung des Feldes kommt davor."""
    assert _error(_add(two, 'X', '1200')[1]) == NOT_IN_CHART.format(n=1200)
    assert _error(_add(two, 'X', '1600')[1]) == OUT_OF_RANGE
    assert _error(_add(two, 'X', '1800')[1]) == IN_USE.format(n=1800)
    for bad in ('abc', '0', '100000', '1e3', '1.5', '٣'):
        assert _error(_add(two, 'X', bad)[1]) == handlers.SKR_ACCOUNT_ERROR, bad
    assert [row[1] for row in two.fetch_accounts()] == ['Kasse', 'Giro Alpha', 'Giro Beta']


def test_the_sentences_and_ranges_are_the_agreed_ones():
    assert handlers.SKR_NOT_IN_CHART == NOT_IN_CHART and handlers.SKR_OUT_OF_RANGE == OUT_OF_RANGE and handlers.SKR_IN_USE == IN_USE
    assert (handlers.BANK_SKR_RANGE, handlers.CASH_SKR_RANGE) == ((1700, 1899), (1600, 1699))


def test_a_refusal_is_shown_on_the_form_page_as_text(two):
    assert _shown_error(PageBankAccounts(two, edit_id=_account(two, 'Giro Beta')[0], error=OUT_OF_RANGE)) == OUT_OF_RANGE


# ── Speichern: was durchgeht ─────────────────────────────────────────────────

def test_a_valid_choice_is_stored_on_a_new_account_with_everything_else_as_before(two):
    assert _add(two, 'Giro Neu', '1820') == (303, '/masterdata/bankaccounts')

    row = _account(two, 'Giro Neu')
    assert row[2:] == ('Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank', 0, 1820, 1)


def test_an_empty_choice_still_means_no_account_on_add_and_update(two):
    assert _add(two, 'Giro Ohne', '') == (303, '/masterdata/bankaccounts')
    assert _skr(two, 'Giro Ohne') is None
    assert _update(two, _account(two, 'Giro Beta')[0], 'Giro Beta', None) == (303, '/masterdata/bankaccounts')
    assert _skr(two, 'Giro Beta') is None


def test_a_valid_change_is_stored_for_a_bank_account_and_for_the_cash_account(two):
    assert _update(two, _account(two, 'Giro Beta')[0], 'Giro Beta', 1820) == (303, '/masterdata/bankaccounts')
    assert _skr(two, 'Giro Beta') == 1820
    assert _update(two, _cash_id(two), 'Kasse', 1610) == (303, '/masterdata/bankaccounts')
    assert _skr(two, 'Kasse') == 1610


def test_a_number_that_a_move_just_freed_can_be_taken_by_another_account(two):
    alpha, beta = _account(two, 'Giro Alpha')[0], _account(two, 'Giro Beta')[0]
    assert _update(two, alpha, 'Giro Alpha', 1820) == (303, '/masterdata/bankaccounts')
    assert _update(two, beta, 'Giro Beta', 1800) == (303, '/masterdata/bankaccounts')
    assert (_skr(two, 'Giro Alpha'), _skr(two, 'Giro Beta')) == (1820, 1800)


def test_an_account_may_be_saved_with_the_number_it_has_even_when_everything_else_changes(two):
    result = _update(two, _account(two, 'Giro Beta')[0], 'Giro Beta umbenannt', 1810, holder='Max Inhaber', iban=IBAN_A)

    assert result == (303, '/masterdata/bankaccounts')
    row = _account(two, 'Giro Beta umbenannt')
    assert (row[2], row[3], row[7]) == ('Max Inhaber', IBAN_A, 1810)


def test_the_cash_account_on_1600_creates_the_chart_account_an_older_database_lacks(tmp_db):
    """Die Umstellung der Kasse auf 1600 legt das Konto selbst an (``ensure_cash_coa``) – es gilt dort als im Kontenrahmen. 1610 legt
    nichts an: abgelehnt."""
    _execute(tmp_db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610)')
    kasse = _cash_id(tmp_db)
    assert _update(tmp_db, kasse, 'Kasse', None) == (303, '/masterdata/bankaccounts')
    assert _coa(tmp_db, 1600) is None

    assert _update(tmp_db, kasse, 'Kasse', 1600) == (303, '/masterdata/bankaccounts')

    assert _skr(tmp_db, 'Kasse') == 1600 and _coa(tmp_db, 1600) is not None
    assert _error(_update(tmp_db, kasse, 'Kasse', 1610)[1]) == NOT_IN_CHART.format(n=1610)
    assert _coa(tmp_db, 1610) is None


# ── Der Bestand bleibt speicherbar: ein unverändert zurückgeschickter Wert ist nie ein Verstoß ──

def _legacy(db):
    """Konten, wie ältere Bestände sie tragen: 1200 (anderer Kontenrahmen), 1830 (im Bereich, nicht im Kontenrahmen), 1460 an einem
    Bankkonto, keine Nummer, zwei Konten auf derselben Nummer und die Kasse auf 1460 – alles an der Verwaltung vorbei angelegt."""
    names = []
    for name, iban, number in (('Alt 1200', IBAN_A, 1200), ('Alt 1830', IBAN_B, 1830), ('Alt 1460', IBAN_C, 1460),
                               ('Alt ohne', IBAN_A, None), ('Alt Doppel 1', IBAN_B, 1800), ('Alt Doppel 2', IBAN_C, 1800)):
        db.insert_account(name, 'Erika Probe', iban, 'TESTDEFF', 'Testbank', skr_account=number)
        names.append(name)
    _as_old_cash(db, 1460)
    return [*names, 'Kasse']


def test_every_legacy_value_can_be_saved_again_unchanged_and_the_form_posts_it_back_as_it_shows_it(tmp_db):
    for name in _legacy(tmp_db):
        row = _account(tmp_db, name)
        (chosen,) = _selected(_page(tmp_db, row[0]))                                 # genau eine Option ist gewählt
        before = _rows(tmp_db)

        status, location = _update(tmp_db, row[0], name, chosen[0], holder=row[2], iban=row[3], bic=row[4], bank_name=row[5])

        assert (status, location) == (303, '/masterdata/bankaccounts'), name
        assert _rows(tmp_db) == before, name                                         # Öffnen und unverändert speichern ändert nichts


def test_an_old_cash_account_on_1460_may_move_into_the_range_but_not_out_of_it_again(tmp_db):
    kasse = _as_old_cash(tmp_db, 1460)

    assert _error(_update(tmp_db, kasse, 'Kasse', 1650)[1]) == NOT_IN_CHART.format(n=1650)
    assert _skr(tmp_db, 'Kasse') == 1460
    assert _update(tmp_db, kasse, 'Kasse', 1600) == (303, '/masterdata/bankaccounts')             # in den Bereich: erlaubt
    assert _skr(tmp_db, 'Kasse') == 1600
    assert _error(_update(tmp_db, kasse, 'Kasse', 1460)[1]) == OUT_OF_RANGE                        # wieder hinaus: nicht


def test_two_accounts_on_the_same_number_keep_it_and_a_third_cannot_take_it(tmp_db):
    _legacy(tmp_db)
    third = _account(tmp_db, 'Alt ohne')[0]

    assert _error(_update(tmp_db, third, 'Alt ohne', 1800)[1]) == IN_USE.format(n=1800)
    assert _skr(tmp_db, 'Alt ohne') is None


# ── Von 1200 auf 1800 – der Weg, den der betroffene Nutzer geht ──────────────

def _giro_on_1200(db, children=False):
    """Ein Bankkonto auf 1200 (der Kontenrahmen kennt die Nummer nicht) mit einer Bankbewegung – auf Wunsch mit einem Buchungssatz
    darunter (Konto 6815, Gegenkonto leer: die Bank ließ sich nicht eintragen)."""
    db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    account = _account(db, 'Giro Alt')[0]
    bank = db.insert_booking('2026-03-05', -119.0, account_id=account, booking_type='bank', text='Bürobedarf')
    if children:
        db.insert_booking('2026-03-05', -119.0, coa_id=_coa(db, 6815), tax_rate=0.19, tax_amount=-19.0, document_number='ER-1',
                          booking_type='entry', parent_booking_id=bank)
    return account


def test_1200_to_1800_succeeds_without_booking_entries(tmp_db):
    account = _giro_on_1200(tmp_db)

    assert _update(tmp_db, account, 'Giro Alt', 1800) == (303, '/masterdata/bankaccounts')
    assert _skr(tmp_db, 'Giro Alt') == 1800


def test_1200_to_1800_succeeds_with_entries_under_its_bank_movements_and_rewrites_nothing(tmp_db):
    """Die Sperre zählt Buchungssätze, die das bisherige oder das neue Konto führen. 1200 steht nicht im Kontenrahmen – kein Satz kann es
    führen –, und die Sätze unter den Bankbewegungen mit leerem Gegenkonto führen 1800 nicht: Die Sperre greift nicht, die Umstellung
    gelingt. Kein Satz wird dabei angefasst (so wie es heute ist: das Gegenkonto bleibt leer)."""
    account = _giro_on_1200(tmp_db, children=True)
    assert tmp_db.count_entries_on_account_numbers([1200, 1800]) == 0
    before = _rows(tmp_db, 'Bookings')
    assert len(before) == 2                                                          # die Bankbewegung und ihr Satz

    assert _update(tmp_db, account, 'Giro Alt', 1800) == (303, '/masterdata/bankaccounts')

    assert _skr(tmp_db, 'Giro Alt') == 1800
    assert _rows(tmp_db, 'Bookings') == before


def test_1200_to_1800_is_locked_once_an_entry_carries_1800(tmp_db):
    account = _giro_on_1200(tmp_db, children=True)
    tmp_db.insert_booking('2026-03-06', -10.0, coa_id=_coa(tmp_db, 6815), counter_coa_id=_coa(tmp_db, 1800), booking_type='entry')
    before = _untouched(tmp_db)

    status, location = _update(tmp_db, account, 'Giro Alt', 1800)

    assert status == 303 and location.startswith(f'/masterdata/bankaccounts/edit?id={account}&error=')
    assert _error(location).startswith(LOCKED.format(n=1))                          # die Sperre, unverändert, nach den neuen Prüfungen
    assert _skr(tmp_db, 'Giro Alt') == 1200 and _untouched(tmp_db) == before


def test_the_new_checks_come_before_the_lock(tmp_db):
    """Ein Wert, den schon die Auswahl nicht zuließe, wird mit seinem Satz abgelehnt – nicht mit der Sperre."""
    account = _giro_on_1200(tmp_db, children=True)
    tmp_db.insert_booking('2026-03-06', -10.0, coa_id=_coa(tmp_db, 6815), counter_coa_id=_coa(tmp_db, 1460), booking_type='entry')

    assert _error(_update(tmp_db, account, 'Giro Alt', 1460)[1]) == OUT_OF_RANGE
    assert _error(_update(tmp_db, account, 'Giro Alt', 1830)[1]) == NOT_IN_CHART.format(n=1830)
    assert _skr(tmp_db, 'Giro Alt') == 1200


# ── Meldungen beim Zuordnen einer Zahlung ────────────────────────────────────

def _sent_invoice(db, number='R-100', gross=119.0):
    """Eine versendete Rechnung samt Forderungsbuchung, wie das Versenden sie hinterlässt."""
    net = round(gross / 1.19, 2)
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 0.19, 'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross,
        'status': 'sent'})
    db.ensure_receivable_booking(invoice_id)
    return invoice_id


def _payment(db, account_id, amount=119.0):
    return db.insert_booking('2026-02-01', amount, account_id=account_id, booking_type='bank', recipient_client='Käufer AG')


def test_the_message_without_a_number_says_what_to_do(tmp_db):
    tmp_db.insert_account('Giro Leer', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank')
    account = _account(tmp_db, 'Giro Leer')[0]
    invoice, booking = _sent_invoice(tmp_db), _payment(tmp_db, account)

    result = tmp_db.assign_invoice_payment(invoice, booking)

    assert (result.ok, result.message) == (False, NO_NUMBER_TEXT.format(what='Zahlung'))
    assert result.message == ('Das Bankkonto hat kein Konto im Kontenrahmen – bitte unter Stammdaten → Bankkonten eines wählen '
                              '(üblich: 1800), sonst lässt sich die Zahlung nicht kontieren.')
    assert tmp_db.payment_precheck(invoice, account) == result.message               # dieselbe Prüfung, bevor eine Bewegung entsteht


def test_the_message_for_a_number_the_chart_does_not_know_names_the_number(tmp_db):
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    account = _account(tmp_db, 'Giro Alt')[0]
    invoice, booking = _sent_invoice(tmp_db), _payment(tmp_db, account)

    result = tmp_db.assign_invoice_payment(invoice, booking)

    assert (result.ok, result.message) == (False, UNKNOWN_NUMBER_TEXT.format(n=1200, what='Zahlung'))
    assert result.message == ('Das Bankkonto trägt die Nummer 1200, die nicht im Kontenrahmen steht – bitte unter Stammdaten → '
                              'Bankkonten ein Konto aus dem Kontenrahmen wählen (üblich: 1800), sonst lässt sich die Zahlung nicht '
                              'kontieren.')
    assert tmp_db.payment_precheck(invoice, account) == result.message


@pytest.mark.parametrize('what', ['Zahlung', 'Erstattung'])
def test_both_messages_carry_the_word_of_the_booking_and_the_other_sentences_stay(tmp_db, what):
    tmp_db.insert_account('Giro Leer', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank')
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank', skr_account=1200)
    tmp_db.insert_account('Giro Gut', 'Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank', skr_account=1800)
    leer, alt, gut = (_account(tmp_db, name)[0] for name in ('Giro Leer', 'Giro Alt', 'Giro Gut'))

    assert tmp_db._bank_coa_of(leer, what) == (NO_NUMBER_TEXT.format(what=what), None)
    assert tmp_db._bank_coa_of(alt, what) == (UNKNOWN_NUMBER_TEXT.format(n=1200, what=what), None)
    assert tmp_db._bank_coa_of(gut, what) == (None, _coa(tmp_db, 1800))
    assert tmp_db._bank_coa_of(None, what) == (f'Für die {what} fehlt das Bankkonto.', None)        # dieser Satz bleibt, wie er war


def test_after_the_user_picks_1800_the_same_payment_can_be_assigned(tmp_db):
    """Der ganze Weg des betroffenen Nutzers: 1200 → die Zuordnung sagt, was zu tun ist → Auswahl 1800 im Formular → die Zahlung geht."""
    tmp_db.insert_account('Giro Alt', 'Erika Probe', IBAN_A, 'TESTDEFF', 'Testbank', skr_account=1200)
    account = _account(tmp_db, 'Giro Alt')[0]
    invoice, booking = _sent_invoice(tmp_db), _payment(tmp_db, account)
    assert tmp_db.assign_invoice_payment(invoice, booking).ok is False

    assert _update(tmp_db, account, 'Giro Alt', 1800) == (303, '/masterdata/bankaccounts')

    result = tmp_db.assign_invoice_payment(invoice, booking)
    assert result.ok is True and result.payment_id is not None


# ── Durch den echten Server ──────────────────────────────────────────────────

def _headers(server_port):
    return {'Origin': own_origin(server_port)}


def test_a_refusal_travels_through_the_server_to_the_page_and_a_valid_choice_is_stored(auto_login, server_port):
    db = auto_login.db
    form = {'name': 'Giro Neu', 'holder': 'Erika Probe', 'iban': IBAN_A, 'bic': 'TESTDEFF', 'bank_name': 'Testbank',
            'business_only_field': '1', 'business_only': '1'}

    refused = post(server_port, '/masterdata/bankaccounts/add', headers=_headers(server_port), csrf=auto_login.csrf,
                   form={**form, 'skr_account': '1200'})
    assert refused.status == 303
    assert _error(refused.headers['Location']) == NOT_IN_CHART.format(n=1200)
    assert [row[1] for row in db.fetch_accounts()] == ['Kasse']                      # nichts gespeichert
    page = get(server_port, refused.headers['Location'])
    assert page.status == 200 and _shown_error(page.text) == NOT_IN_CHART.format(n=1200)

    saved = post(server_port, '/masterdata/bankaccounts/add', headers=_headers(server_port), csrf=auto_login.csrf,
                 form={**form, 'skr_account': '1800'})
    assert (saved.status, saved.headers['Location']) == (303, '/masterdata/bankaccounts')
    assert _skr(db, 'Giro Neu') == 1800
    listing = _Page(get(server_port, '/masterdata/bankaccounts').text)
    assert listing.options == _choices(selected='1810') and listing.warnings == []  # 1800 ist jetzt vergeben


def test_the_edit_page_of_an_old_account_shows_its_number_chosen_and_an_update_to_a_wrong_one_is_refused(auto_login, server_port):
    db = auto_login.db
    db.insert_account('Giro Alt', 'Erika Probe', IBAN_B, 'TESTDEFF', 'Testbank', skr_account=1200)
    account = _account(db, 'Giro Alt')[0]

    edit = _Page(get(server_port, f'/masterdata/bankaccounts/edit?id={account}').text)
    assert _selected(edit) == [('1200', '1200 – nicht im Kontenrahmen', True)]

    refused = post(server_port, '/masterdata/bankaccounts/update', headers=_headers(server_port), csrf=auto_login.csrf,
                   form={'id': account, 'name': 'Giro Alt', 'skr_account': '1460'})
    assert refused.status == 303 and _error(refused.headers['Location']) == OUT_OF_RANGE
    assert refused.headers['Location'].startswith(f'/masterdata/bankaccounts/edit?id={account}&error=')
    assert _shown_error(get(server_port, refused.headers['Location']).text) == OUT_OF_RANGE
    assert _skr(db, 'Giro Alt') == 1200

    saved = post(server_port, '/masterdata/bankaccounts/update', headers=_headers(server_port), csrf=auto_login.csrf,
                 form={'id': account, 'name': 'Giro Alt', 'skr_account': '1800'})
    assert (saved.status, saved.headers['Location']) == (303, '/masterdata/bankaccounts')
    assert _skr(db, 'Giro Alt') == 1800


# ── Ersteinrichtung: das Bankkonto bekommt ein Konto des Kontenrahmens ───────
def _setup_form(**values):
    return {key: [value] for key, value in values.items()}


def test_the_first_setup_gives_the_bank_account_the_first_free_account_of_the_chart(tmp_db):
    """Bis 2.8.0 trug das Bankkonto der Ersteinrichtung fest 1200 – die Banknummer eines anderen Kontenrahmens, die es im Kontenrahmen der
    App nicht gibt: Keine Zahlung ließ sich danach einer Rechnung zuordnen."""
    status, _target = handlers.handle_setup_save(tmp_db, _setup_form(company_name='Muster GmbH', iban=IBAN_A))
    assert status == 303
    bank = [row for row in tmp_db.fetch_accounts() if row[6] != 1]
    assert [row[7] for row in bank] == [1800]
    assert tmp_db.get_coa_id_by_account_number(1800) is not None
    assert handlers.free_bank_skr_number(tmp_db) == 1810                        # die nächste freie für das zweite Konto


def test_the_first_setup_leaves_the_account_empty_when_no_account_of_the_range_is_free(tmp_db):
    for number, iban in ((1800, IBAN_A), (1810, IBAN_B)):
        tmp_db.insert_account(f'Konto {number}', 'Erika Probe', iban, 'TESTDEFF', 'Testbank', skr_account=number)
    tmp_db.insert_account('Konto 1820', 'Erika Probe', 'DE00000000000000000003', 'TESTDEFF', 'Testbank', skr_account=1820)
    assert handlers.free_bank_skr_number(tmp_db) is None


# ── Eine als Text gespeicherte Nummer gilt als vergeben ──────────────────────

NBSP = '\xa0'


def _typeof(db, name):
    with closing(db._get_connection()) as con:
        return con.execute('SELECT typeof(SKRAccount) FROM Accounts WHERE Name = ?', (name,)).fetchone()[0]


def _text_account(db, name, stored):
    """Ein Konto, dessen Nummer als TEXT in der Spalte steht: SQLite wandelt ``'1820'`` und ``' 1820 '`` in eine Zahl um, ein geschütztes Leerzeichen
    (so kommt es aus einer Kopie) verhindert es."""
    db.insert_account(name, 'Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank', skr_account=stored)
    assert _typeof(db, name) == 'text'
    return _account(db, name)[0]


@pytest.mark.parametrize('stored', [NBSP + '1820', '1820' + NBSP], ids=['geschuetztes-Leerzeichen-davor', 'geschuetztes-Leerzeichen-dahinter'])
def test_a_number_stored_as_text_counts_as_taken_for_a_new_and_a_changed_account(two, stored):
    _text_account(two, 'Giro Text', stored)
    beta = _account(two, 'Giro Beta')[0]
    before = _untouched(two)

    assert _error(_add(two, 'Giro Neu', '1820')[1]) == IN_USE.format(n=1820)
    assert _error(_update(two, beta, 'Giro Beta', 1820)[1]) == IN_USE.format(n=1820)

    assert _untouched(two) == before
    assert handlers.free_bank_skr_number(two) is None                      # 1800 und 1810 als Zahl, 1820 als Text: alle drei sind vergeben


def test_the_account_that_carries_the_text_may_be_saved_with_the_clean_number(two):
    account = _text_account(two, 'Giro Text', NBSP + '1820')

    assert _update(two, account, 'Giro Text', 1820) == (303, '/masterdata/bankaccounts')      # sich selbst zählt es nie als vergeben

    assert _skr(two, 'Giro Text') == 1820 and _typeof(two, 'Giro Text') == 'integer'


def test_the_preselection_and_the_first_setup_skip_a_number_stored_as_text(tmp_db):
    _text_account(tmp_db, 'Giro Text', NBSP + '1800')

    assert handlers.free_bank_skr_number(tmp_db) == 1810                    # vorher: 1800, die Nummer, die das Text-Konto trägt
    assert _selected(_page(tmp_db)) == [('1810', '1810 – Bank 1', True)]
    status, _target = handlers.handle_setup_save(tmp_db, _setup_form(company_name='Muster GmbH', iban=IBAN_A))
    assert status == 303
    assert _skr(tmp_db, 'Bankkonto') == 1810


# ── Prüfen und Anlegen gehören zusammen: zwei Anfragen zugleich ──────────────

def _race(monkeypatch, method, first, second):
    """Zwei Anfragen zugleich. ``Database.<method>`` (``insert_account``, ``update_account``) wartet kurz, bevor sie schreibt – lange genug, dass die zweite
    Anfrage ihre Prüfung längst gemacht hätte, wenn die Sperre sie nicht aufhielte. Die zweite beginnt erst, wenn die erste schreibt (und hält dann die
    Sperre). Liefert die Antworten ``(erste, zweite)``."""
    original = getattr(Database, method)
    writing = threading.Event()

    def slow(self, *args, **kwargs):
        writing.set()
        time.sleep(0.3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Database, method, slow)
    results = {}
    threads = [threading.Thread(target=lambda: results.__setitem__('first', first()))]
    threads[0].start()
    assert writing.wait(10), 'die erste Anfrage hat nie geschrieben'
    threads.append(threading.Thread(target=lambda: results.__setitem__('second', second())))
    threads[1].start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    return results['first'], results['second']


def test_two_simultaneous_requests_cannot_both_create_an_account_with_the_same_free_number(two, monkeypatch):
    first, second = _race(monkeypatch, 'insert_account',
                          lambda: _add(two, 'Giro Erst', '1820'), lambda: _add(two, 'Giro Zweit', '1820'))

    assert first == (303, '/masterdata/bankaccounts')
    assert (second[0], _error(second[1])) == (303, IN_USE.format(n=1820))
    assert [row[1] for row in two.fetch_accounts() if row[7] == 1820] == ['Giro Erst']
    assert 'Giro Zweit' not in [row[1] for row in two.fetch_accounts()]


def test_two_simultaneous_requests_cannot_both_change_an_account_to_the_same_free_number(two, monkeypatch):
    two.insert_account('Giro Gamma', 'Erika Probe', IBAN_C, 'TESTDEFF', 'Testbank')
    beta, gamma = _account(two, 'Giro Beta')[0], _account(two, 'Giro Gamma')[0]

    first, second = _race(monkeypatch, 'update_account',
                          lambda: _update(two, gamma, 'Giro Gamma', 1820), lambda: _update(two, beta, 'Giro Beta', 1820))

    assert first == (303, '/masterdata/bankaccounts')
    assert (second[0], _error(second[1])) == (303, IN_USE.format(n=1820))
    assert [row[1] for row in two.fetch_accounts() if row[7] == 1820] == ['Giro Gamma']
    assert _skr(two, 'Giro Beta') == 1810


def test_the_first_setup_and_a_new_account_at_the_same_time_do_not_get_the_same_number(tmp_db, monkeypatch):
    """Die Ersteinrichtung sucht die kleinste freie Nummer und legt das Konto an – unter derselben Sperre wie das Formular der Bankkonten."""
    first, second = _race(monkeypatch, 'insert_account', lambda: _add(tmp_db, 'Giro Erst', '1800'),
                          lambda: handlers.handle_setup_save(tmp_db, _setup_form(company_name='Muster GmbH', iban=IBAN_A)))

    assert first == (303, '/masterdata/bankaccounts') and second[0] == 303
    assert (_skr(tmp_db, 'Giro Erst'), _skr(tmp_db, 'Bankkonto')) == (1800, 1810)
