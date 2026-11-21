# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests der betriebswirtschaftlichen Auswertung (``test_bwa.py``, ``test_bwa_pdf.py``): ein Bericht aus erfundenen
EÜR-Zeilen, ohne Datenbank. Alle Namen, Nummern und Beträge sind erfundene Testwerte."""
from db.bwa import build_report, bwa_periods, periods_for
from server.handlers import handle_add_transaction


def report_from(month_rows, ytd_rows=None, variant='jahr', banks=(), year=2026, month=3, prev_month_rows=(), prev_ytd_rows=()):
    """Ein Bericht aus erfundenen EÜR-Zeilen ``(Nummer, Name, Saldo in Euro)``; ohne ``ytd_rows`` gilt für den aufgelaufenen
    Zeitraum dieselbe Liste wie für den Monat."""
    rows = {'month': list(month_rows), 'ytd': list(month_rows if ytd_rows is None else ytd_rows),
            'prev_month': list(prev_month_rows), 'prev_ytd': list(prev_ytd_rows)}
    spans = bwa_periods(year, month)
    periods = {key: spans[key] for key in periods_for(variant)}
    return build_report(year, month, variant, periods, rows, set(banks))


def values_of(report, key, period='month'):
    return report.line(key).values[period]


def account_numbers(report, key):
    return [account.number for account in report.line(key).accounts]


def book(db, number, amount, date, name=None, tax_rate=None, tax_amount=None):
    """Eine Zahlung auf ein Zweckkonto über die Buchungsmaske (das Bankkonto „Probe-Giro“ entsteht bei Bedarf; ein Konto, das es
    im Kontenrahmen nicht gibt, mit ``name`` – seine Nummer legt der Test selbst fest)."""
    if not any(a[1] == 'Probe-Giro' for a in db.fetch_accounts()):
        db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    if name is not None and db.get_coa_id_by_account_number(number) is None:
        assert db.insert_chart_of_accounts(4, number, name, 'Test')
    giro = next(a[0] for a in db.fetch_accounts() if a[1] == 'Probe-Giro')
    post = {'transaction_id': ['0'], 'date': [date], 'amount': [amount], 'account': [str(giro)],
            'coa_id': [str(db.get_coa_id_by_account_number(number))]}
    if tax_rate:
        post.update({'tax_rate': [tax_rate], 'tax_amount': [tax_amount]})
    status, location = handle_add_transaction(db, post)
    assert status == 303, (status, location)


def some_books(db):
    book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
    book(db, 6815, '-59.50', '2026-03-12', tax_rate='19', tax_amount='-9.50')
    book(db, 3820, '-50.00', '2026-03-15')
    book(db, 3841, '20.00', '2026-04-02', name='Umsatzsteuer-Erstattung')
    book(db, 1050, '-5.00', '2026-03-20', name='Altkonto')
    book(db, 3160, '-30.00', '2026-03-22')
    book(db, 4400, '119.00', '2025-03-10', tax_rate='19', tax_amount='19.00')
