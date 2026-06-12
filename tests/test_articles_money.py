"""Phase 1 (Decimal-Migration): Artikel-Preise als Festkomma-Integer gespeichert,
als Euro-Decimal gelesen."""
import re
import sqlite3
from decimal import Decimal

from server.pages_masterdata import PageArticles


def test_article_price_roundtrip(tmp_db):
    tmp_db.insert_article(name="Testartikel", unit="Stk.", unit_price=12.34, tax_rate=19)
    arts = tmp_db.fetch_articles()
    assert len(arts) == 1
    # Konsument erhaelt Euro-Decimal (Index 3 = UnitPrice)
    assert arts[0][3] == Decimal("12.3400")


def test_article_price_stored_as_integer_minor_units(tmp_db):
    tmp_db.insert_article(name="Testartikel", unit_price=12.34)
    con = sqlite3.connect(tmp_db.db_name)
    raw = con.execute("SELECT UnitPrice FROM Articles").fetchone()[0]
    con.close()
    assert isinstance(raw, int)
    assert raw == 123400  # 12.34 * 10^4


def test_article_price_fourdigit_precision(tmp_db):
    # Vierstelliger Stueckpreis bleibt verlustfrei (Motivation fuer SCALE=4)
    tmp_db.insert_article(name="Cent-Bruchteil", unit_price="0.0079")
    assert tmp_db.fetch_articles()[0][3] == Decimal("0.0079")


def test_update_article_price(tmp_db):
    tmp_db.insert_article(name="A", unit_price=1.00)
    art_id = tmp_db.fetch_articles()[0][0]
    tmp_db.update_article(art_id, name="A", unit_price=9.99)
    assert tmp_db.get_article_by_id(art_id)[3] == Decimal("9.9900")


def test_get_article_by_id_missing_returns_none(tmp_db):
    assert tmp_db.get_article_by_id(99999) is None


def test_article_edit_form_wraps_all_fields(tmp_db):
    """Regression: alle Eingabefelder (inkl. hidden id) müssen INNERHALB des
    Update-<form> liegen – sonst werden beim Speichern leere Werte übermittelt
    (fehlerhaftes div-Nesting schloss das Formular früher schon nach den Buttons)."""
    tmp_db.insert_article(name="Bohrer", unit="Stk.", unit_price=9.99,
                          tax_rate=19, description="HSS")
    art_id = tmp_db.fetch_articles()[0][0]
    html = PageArticles(tmp_db, edit_article_id=art_id)

    m = re.search(r'<form[^>]*action="/masterdata/articles/update"[^>]*>(.*?)</form>',
                  html, re.S)
    assert m, "Update-Formular nicht gefunden"
    form = m.group(1)
    for field in ('name="id"', 'name="name"', 'name="unit_price"',
                  'name="tax_rate"', 'name="description"'):
        assert field in form, f"{field} liegt ausserhalb des <form>"
    # 'Als neu anlegen'-Button gehört ebenfalls ins Formular
    assert 'Als neu anlegen' in form
