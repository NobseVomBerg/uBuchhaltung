"""Tests für den zentralen Zeitraum-Filter (server/period.py).

Deckt das erweiterte period_filter_widget ab:
  - Jahres- + Monats-Buttons vorhanden
  - aktiver Monat hervorgehoben (single_month)
  - Monats-Toggle: Klick auf aktiven Monat führt zurück aufs ganze Jahr
  - Jahres-Buttons monatserhaltend bei Monatswahl
  - extra_params werden (URL-enkodiert) mitgeführt
Sowie die Integration in den betroffenen Seiten (Filter in Header2,
Monats-Buttons in Header3).
"""
import re

from server.period import period_filter_widget


# ── reine Widget-Funktion ────────────────────────────────────────────────────

def test_widget_full_year_has_year_and_month_buttons():
    h = period_filter_widget('2026-01-01', '2026-12-31', '/transactions')
    # alle Monatskürzel vorhanden
    for m in ['Jan', 'Feb', 'Mär', 'Dez']:
        assert f'>{m}<' in h
    # aktives Jahr markiert
    assert "class='active'" in h
    # im Ganzjahres-Modus ist kein Monat aktiv
    assert not re.search(r"class='active'[^>]*>(Jan|Feb|Mär|Apr|Mai|Jun|Jul|Aug|Sep|Okt|Nov|Dez)<", h)


def test_widget_single_month_marks_active_month():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    assert re.search(r"<button class='active' type=\"button\"[^>]*>Jun<", h)


def test_widget_active_month_toggles_back_to_full_year():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    m = re.search(r"<button class='active' type=\"button\" onclick=\"window.location.href='([^']+)'\">Jun<", h)
    assert m, 'aktiver Monats-Button nicht gefunden'
    assert m.group(1) == '/transactions?from=2026-01-01&to=2026-12-31'


def test_widget_year_button_is_month_preserving_in_month_mode():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    m = re.search(r"onclick=\"window.location.href='([^']+)'\">2025<", h)
    assert m
    assert m.group(1) == '/transactions?from=2025-06-01&to=2025-06-30'


def test_widget_extra_params_are_carried_and_encoded():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/invoice',
                             extra_params={'status': 'paid', 'search': 'a b', 'id': 3})
    # an Buttons angehängt
    assert '&status=paid&search=a%20b&id=3' in h
    # auch im gotoPeriod-JS für die Von/Bis-Felder
    assert "+ t + '&status=paid&search=a%20b&id=3'" in h


def test_widget_empty_extra_params_are_skipped():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/invoice',
                             extra_params={'status': '', 'search': None, 'id': ''})
    assert "from=2026-01-01&to=2026-12-31'" in h  # Toggle-Ziel ohne Anhang
    assert '&status=' not in h
    assert '&search=' not in h


# ── Integration in die Seiten ────────────────────────────────────────────────

def test_invoice_filters_in_header2_and_month_in_header3(db_with_coa):
    from server.pages_invoice import PageInvoice
    h = PageInvoice(db_with_coa,
                    filters={'date_from': '2026-06-01', 'date_to': '2026-06-30'},
                    invoice_id=None)
    assert 'id="searchQuery"' in h
    assert 'id="statusFilter"' in h
    assert '>Jun<' in h
    # alte Inline-Jahreswahl ist weg
    assert 'setInvoiceYear' not in h


def test_receipts_search_present_and_month_buttons(db_with_coa):
    from server.pages_receipts import PageReceipts
    h = PageReceipts(db_with_coa, date_from='2026-06-01', date_to='2026-06-30')
    assert 'id="receiptSearch"' in h
    assert '>Jun<' in h


def test_transactions_account_dropdown_and_filters(db_with_coa):
    from server.pages_transactions import PageTransactions
    h = PageTransactions(db_with_coa, date_from='2026-06-01', date_to='2026-06-30')
    assert 'id="acctMenuPanel"' in h
    assert 'position:absolute' in h          # Overlay statt 2. Header-Zeile
    assert 'id="account_all"' in h          # Checkbox-Logik bleibt erhalten
    assert 'id="txSearch"' in h
    assert 'function toggleAcctMenu' in h
    assert '>Jun<' in h


def test_worktime_uses_central_widget(db_with_coa):
    from server.pages_worktime import PageWorkTimes
    h = PageWorkTimes(db_with_coa, person_id=None,
                      date_from='2026-06-01', date_to='2026-06-30')
    assert '>Jun<' in h
    assert '&person=' in h
    assert 'wtGotoDates' not in h           # eigene JS-Funktion ersetzt
