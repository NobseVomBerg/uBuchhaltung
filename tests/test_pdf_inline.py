"""Regression: erzeugte PDFs werden inline in einem neuen Tab geöffnet.

Prüft die Frontend-Verdrahtung (window.open + inline=1) auf allen Seiten, an
denen PDFs erstellt werden: Rechnung (Liste + Formular), Angebot (Liste +
Formular) und Arbeitszeiten-Stundenzettel.
"""
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from server.pages_worktime import PageWorkTimes


def test_invoice_opens_pdf_inline_new_tab(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    # Tab popup-sicher im Klick-Kontext öffnen ...
    assert "window.open('about:blank', '_blank')" in html
    # ... und inline-Ansicht laden (Liste + Formular nutzen denselben Pfad)
    assert "/invoice/pdf_download?id=' + invoiceId + '&inline=1'" in html


def test_quote_opens_pdf_inline_new_tab(db_with_coa):
    html = PageQuote(db_with_coa)
    assert "window.open('about:blank', '_blank')" in html
    assert "&inline=1" in html
    assert "/quote/pdf_download?id=" in html


def test_worktime_opens_pdf_inline_new_tab(db_with_coa):
    html = PageWorkTimes(db_with_coa, person_id=None,
                         date_from='2026-06-01', date_to='2026-06-30')
    assert "window.open(url + '&inline=1', '_blank')" in html
