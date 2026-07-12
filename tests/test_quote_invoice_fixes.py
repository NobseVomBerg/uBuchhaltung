# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Regression: Angebot/Rechnung – 0%-Steuer beim Speichern und Logo im PDF."""
import os

from export.pdf_core import resolve_logo_path
from server.pages_quote import PageQuote
from server.pages_invoice import PageInvoice


# ── Bug 1: 0% MwSt darf beim Speichern nicht zu 19% werden (JS-||-19-Bug) ─────

_NAN_SAFE = 'isNaN(_tr) ? 19 : _tr'
_BUGGY = "parseFloat(document.getElementById('tax_rate').value)||19"
_BUGGY_INV = "parseFloat(document.getElementById('tax_rate').value) || 19"


def test_quote_tax_rate_nan_safe(db_with_coa):
    html = PageQuote(db_with_coa)
    assert _NAN_SAFE in html          # 0% bleibt 0%
    assert _BUGGY not in html         # alter Bug entfernt


def test_invoice_tax_rate_nan_safe(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    assert _NAN_SAFE in html
    assert _BUGGY_INV not in html


def _make_doc(db, tax_rate, number):
    """Minimale Rechnung/Angebot (gleiche Tabelle) mit gegebenem Steuersatz."""
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-01',
        'seller_name': 'S', 'seller_company': 'S GmbH',
        'buyer_name': 'B', 'buyer_company': 'B GmbH',
        'tax_rate': tax_rate, 'sum_net': 0, 'tax_amount': 0,
        'sum_gross': 0, 'amount_due': 0, 'status': 'draft',
    })


def test_invoice_tax_prefill_from_stored_value(db_with_coa):
    """Beim Bearbeiten wird der gespeicherte Steuersatz angezeigt – 0% bleibt 0%."""
    zero_id = _make_doc(db_with_coa, 0, 'TAX-0')
    seven_id = _make_doc(db_with_coa, 0.07, 'TAX-7')
    html0 = PageInvoice(db_with_coa, filters={}, invoice_id=zero_id)
    assert 'id="tax_rate" value="0"' in html0
    html7 = PageInvoice(db_with_coa, filters={}, invoice_id=seven_id)
    assert 'id="tax_rate" value="7"' in html7
    # kein Platzhalter mehr im Output
    assert '__TAX_PCT__' not in html0


def test_quote_tax_prefill_from_stored_value(db_with_coa):
    qid = _make_doc(db_with_coa, 0, 'QTAX-0')
    html = PageQuote(db_with_coa, {}, qid)
    assert 'id="tax_rate" value="0"' in html
    assert '__TAX_PCT__' not in html


# ── Bug 2: Logo-Pfad fuer PDF aufloesen (Mehrbenutzer-Dateiisolation) ─────────

def test_resolve_logo_none_and_url():
    assert resolve_logo_path('') is None
    assert resolve_logo_path(None) is None
    assert resolve_logo_path('http://example.com/logo.png') is None
    assert resolve_logo_path('https://example.com/logo.png') is None


def test_resolve_logo_existing_path(tmp_path):
    f = tmp_path / 'logo.png'
    f.write_bytes(b'\x89PNG')
    got = resolve_logo_path(str(f))
    assert got and os.path.samefile(got, str(f))


def test_resolve_logo_logical_maps_to_user_logos(tmp_path, monkeypatch):
    """Logischer Pfad data/logos/<datei> wird auf das Nutzer-logos-Verzeichnis
    aufgeloest (dort liegt die Datei im Mehrbenutzer-Modus physisch)."""
    import userctx
    logos = tmp_path / 'logos'
    logos.mkdir()
    (logos / 'firma.png').write_bytes(b'\x89PNG')
    monkeypatch.setattr(userctx, 'user_subdir',
                        lambda name, create=True: str(logos) if name == 'logos'
                        else str(tmp_path / name))
    got = resolve_logo_path('data/logos/firma.png')
    assert got and os.path.samefile(got, str(logos / 'firma.png'))


def test_resolve_logo_missing_returns_none(tmp_path, monkeypatch):
    import userctx
    logos = tmp_path / 'logos'
    logos.mkdir()
    monkeypatch.setattr(userctx, 'user_subdir',
                        lambda name, create=True: str(logos) if name == 'logos'
                        else str(tmp_path / name))
    assert resolve_logo_path('data/logos/fehlt.png') is None
