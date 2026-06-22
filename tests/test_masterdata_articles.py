"""Tests für die Artikel-Stammdatenseite (server/pages_masterdata.PageArticles)."""
from server.pages_masterdata import PageArticles


def test_zero_percent_tax_shown_as_zero_in_list(tmp_db):
    """0% MwSt darf in der Übersicht nicht zu 19% verfälscht werden (or-19-Bug)."""
    tmp_db.insert_article(name='Nullsteuer-Artikel', unit_price=10, tax_rate=0)
    html = PageArticles(tmp_db)
    assert "data-tax='0'" in html
    assert '<td>0%</td>' in html
    # keine fälschliche 19%-Anzeige für diesen (einzigen) Artikel
    assert "data-tax='19'" not in html
    assert '<td>19%</td>' not in html


def test_normal_tax_rate_unaffected(tmp_db):
    """19%/7% werden weiterhin korrekt angezeigt."""
    tmp_db.insert_article(name='Standard', unit_price=10, tax_rate=19)
    tmp_db.insert_article(name='Ermäßigt', unit_price=10, tax_rate=7)
    html = PageArticles(tmp_db)
    assert '<td>19%</td>' in html
    assert '<td>7%</td>' in html
