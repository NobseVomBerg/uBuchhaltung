# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Regression: erzeugte PDFs landen dort, wo der Nutzer sie erwartet.

Angebot und Stundenzettel öffnen das PDF inline in einem neuen Tab
(window.open + inline=1). Rechnungen werden stattdessen echt heruntergeladen –
ein Server-Link ist ohne laufenden Server wertlos (todo #1).
"""
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from server.pages_worktime import PageWorkTimes


def test_invoice_downloads_pdf_as_file(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    # Echter Download über appDownload (Blob + <a download>), kein Tab mit
    # Server-URI (todo #1) – Liste und Formular nutzen denselben Weg.
    assert "appDownload('/invoice/pdf_download?id='" in html
    assert "&inline=1" not in html


def test_quote_opens_pdf_inline_new_tab(db_with_coa):
    html = PageQuote(db_with_coa)
    assert "window.open('about:blank', '_blank')" in html
    assert "&inline=1" in html
    assert "/quote/pdf_download?id=" in html


def test_worktime_opens_pdf_inline_new_tab(db_with_coa):
    html = PageWorkTimes(db_with_coa, person_id=None,
                         date_from='2026-06-01', date_to='2026-06-30')
    assert "window.open(url + '&inline=1', '_blank')" in html
