# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""EÜR-Klassifizierung: Einnahmen = SKR04-Ertragsklasse 4000-4999.

Die frühere Aufzählung einzelner Erlöskonten (4400/4640/4845) ließ z. B.
4185 (§19-Kleinunternehmer) und 4300 (7%) fälschlich unter "Ausgaben"
erscheinen.
"""
from db.reporting import is_income_account
from server.handlers import handle_add_transaction
from server.pages_dashboard import PageDashboard


def test_is_income_account_boundaries():
    assert is_income_account(4000)
    assert is_income_account(4185)   # §19-Kleinunternehmer
    assert is_income_account(4300)   # 7% USt
    assert is_income_account(4400)
    assert is_income_account(4999)
    assert not is_income_account(3999)
    assert not is_income_account(5000)
    assert not is_income_account(3806)   # USt-Konto (Dashboard behandelt es separat)
    assert not is_income_account(5200)   # Wareneingang
    assert not is_income_account(None)
    assert not is_income_account('abc')


def _book_payment(db, coa_number, amount='100.00'):
    db.insert_account('EÜR-Bank', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0,
                      skr_account=1800)
    acct_id = [a for a in db.fetch_accounts() if a[1] == 'EÜR-Bank'][0][0]
    coa = db.get_coa_id_by_account_number(coa_number)
    handle_add_transaction(db, {
        'transaction_id': ['0'], 'date': ['2026-03-01'], 'amount': [amount],
        'account': [str(acct_id)], 'coa_id': [str(coa)],
    })


def test_euer_data_contains_4185_revenue(tmp_db):
    _book_payment(tmp_db, 4185)
    rows = {nr: total for nr, _, total in
            tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert rows.get(4185) == 100.0


def test_dashboard_shows_4185_under_einnahmen(tmp_db):
    _book_payment(tmp_db, 4185)
    html = PageDashboard(tmp_db, '2026-01-01', '2026-12-31')
    assert '<td>4185</td>' in html
    # Die Zeile muss VOR der Einnahmen-Summe stehen (= Einnahmen-Tabelle),
    # nicht im Ausgaben-Block danach.
    assert html.index('<td>4185</td>') < html.index('Summe Betriebseinnahmen')
