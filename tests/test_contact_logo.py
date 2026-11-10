# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Logo im Kontaktformular: Hinweis statt kaputtem Bild.

Ein Logo-Pfad, der auf keine Datei dieses Servers zeigt (eingetippter Pfad vom
eigenen PC, ``/static/…``), erschien bisher als kaputtes Bild – im Docker-
Betrieb ohne jeden Hinweis, dass die Datei gar nicht im Datenverzeichnis liegt.
"""
import pytest

import userctx
from server.pages import logo_on_server
from server.pages_contacts import _contact_form

HINT = 'liegt nicht auf dem Server'


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Eigenes Datenverzeichnis – nie das echte ./data/logos des Entwicklers."""
    monkeypatch.setattr(userctx, 'DATA_ROOT', str(tmp_path))
    return tmp_path


def _form_with_logo(db, logo):
    db.insert_contact(contact_type='own', entity_type='company',
                      display_name='Muster GmbH', company_name='Muster GmbH',
                      street='Hauptstr. 1', city='Berlin', logo=logo)
    c = list(db.fetch_contacts(contact_type='own'))[0]
    return _contact_form(db, '/x', entity_type='company', c=c)


def test_hint_instead_of_broken_image_for_foreign_path(tmp_db, data_root):
    html = _form_with_logo(tmp_db, '/static/firmenlogo.png')
    assert HINT in html
    assert '<img src="/static/firmenlogo.png"' not in html
    # der gespeicherte Wert bleibt sichtbar, damit man sieht, was drinsteht
    assert 'value="/static/firmenlogo.png"' in html


def test_uploaded_logo_shows_preview_without_hint(tmp_db, data_root):
    (data_root / 'logos').mkdir()
    (data_root / 'logos' / 'firmenlogo.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    html = _form_with_logo(tmp_db, 'data/logos/firmenlogo.png')
    assert HINT not in html
    assert '<img src="/data/logos/firmenlogo.png" class="logo-preview">' in html


def test_web_url_is_not_flagged(tmp_db, data_root):
    html = _form_with_logo(tmp_db, 'https://example.org/logo.png')
    assert HINT not in html
    assert '<img src="https://example.org/logo.png"' in html


def test_logo_value_is_escaped(tmp_db, data_root):
    html = _form_with_logo(tmp_db, '"><img src=x onerror=alert(1)>')
    assert '<img src=x' not in html
    assert '&quot;&gt;&lt;img' in html


def test_logo_on_server_follows_static_handler(data_root):
    (data_root / 'logos').mkdir()
    (data_root / 'logos' / 'da.png').write_bytes(b'x')
    assert logo_on_server('data/logos/da.png') is True
    assert logo_on_server('/data/logos/da.png') is True
    assert logo_on_server('data\\logos\\da.png') is True        # Windows-Trenner
    assert logo_on_server('data/logos/fehlt.png') is False
    assert logo_on_server('/static/da.png') is False            # wird nicht ausgeliefert
    assert logo_on_server('C:\\Users\\jemand\\Bilder\\logo.png') is False
    assert logo_on_server('') is False
    assert logo_on_server('https://example.org/logo.png') is None


def test_logo_on_server_does_not_reveal_files_outside_the_private_seed_folder(data_root, tmp_path,
                                                                               monkeypatch):
    """„/seed_data/private/../…“ darf nicht verraten, ob es anderswo eine Datei gibt."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'seed_data' / 'private').mkdir(parents=True)
    (tmp_path / 'seed_data' / 'private' / 'logo.png').write_bytes(b'x')
    (tmp_path / 'geheim.db').write_bytes(b'x')
    assert logo_on_server('/seed_data/private/logo.png') is True
    assert logo_on_server('/seed_data/private/fehlt.png') is False
    assert logo_on_server('/seed_data/private/../../geheim.db') is False
    assert logo_on_server('/seed_data/private/%2e%2e/%2e%2e/geheim.db') is False
    assert logo_on_server('/seed_data/private') is False               # der Ordner selbst ist keine Datei
