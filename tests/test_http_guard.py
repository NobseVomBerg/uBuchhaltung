# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zugriffsschutz des HTTP-Servers: Herkunft, Hostname, Methode, CSRF-Token.

Drei Teile:
  1. reine Funktionstests für request_is_same_origin() und host_is_trusted(),
  2. Anfragen gegen einen echten ThreadingHTTPServer auf 127.0.0.1 (freier Port,
     Datenbereich unter tmp_path – nichts davon landet im echten ./data). Der Betrieb
     für eine Person (Administrator mit automatischer Anmeldung, ohne Sitzung) ist die
     Fixture ``auto_login`` aus conftest.py; die Ersteinrichtung prüft test_setup_start.py,
     die automatische Anmeldung selbst test_auto_login_http.py,
  3. die Listenseiten lösen Aktionen per POST aus, nicht per Link.

Hostnamen (*.example, *.example.org) und Zugangsdaten sind erfundene Testwerte.
"""
import email.message
import json
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

import auth
from server import app as server_app
from server.app import _ACTION_PATHS, host_is_trusted, request_is_same_origin
from server.pages_contacts import PageContacts
from server.pages_masterdata import PageArticles
from server.pages_receipts import PageReceiptEdit
from server.pages_transactions import PageTransactions
from tests import installation
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post, send


# ── Teil 1: reine Funktionen ─────────────────────────────────────────────────
def _headers(pairs):
    """Header-Objekt wie im Handler (Message: Namen ohne Beachtung der Schreibweise)."""
    msg = email.message.Message()
    for name, value in pairs.items():
        msg[name] = value
    return msg


_SAME_ORIGIN_CASES = [
    # Weder Sec-Fetch-Site noch Origin: kein Browser (curl, Skripte), kein CSRF
    pytest.param({}, True, id="keine-Header"),
    pytest.param({"Host": "127.0.0.1:8080"}, True, id="nur-Host"),
    # Sec-Fetch-Site geht vor
    pytest.param({"Sec-Fetch-Site": "same-origin"}, True, id="fetch-same-origin"),
    pytest.param({"Sec-Fetch-Site": "none"}, True, id="fetch-none"),
    pytest.param({"Sec-Fetch-Site": "cross-site"}, False, id="fetch-cross-site"),
    pytest.param({"Sec-Fetch-Site": "same-site"}, False, id="fetch-same-site"),
    pytest.param({"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1:8080",
                  "Host": "127.0.0.1:8080"}, False, id="fetch-cross-site-trotz-passendem-Origin"),
    # Origin gegen Host
    pytest.param({"Origin": "http://127.0.0.1:8080", "Host": "127.0.0.1:8080"}, True,
                 id="Origin-gleich-Host"),
    pytest.param({"Origin": "http://[::1]:8080", "Host": "[::1]:8080"}, True,
                 id="Origin-gleich-Host-IPv6"),
    pytest.param({"Origin": "HTTP://LocalHost:8080", "Host": "localhost:8080"}, True,
                 id="Schreibweise-egal"),
    pytest.param({"Origin": FOREIGN_ORIGIN, "Host": "127.0.0.1:8080"}, False,
                 id="fremder-Origin"),
    pytest.param({"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:8080"}, False,
                 id="Origin-mit-anderem-Port"),
    pytest.param({"Origin": "null", "Host": "127.0.0.1:8080"}, False, id="Origin-null"),
    pytest.param({"Origin": "http://[::1", "Host": "127.0.0.1:8080"}, False,
                 id="kaputter-Origin"),
    pytest.param({"Origin": "", "Host": "127.0.0.1:8080"}, False, id="leerer-Origin"),
    pytest.param({"Origin": "evil", "Host": "127.0.0.1:8080"}, False, id="Origin-ohne-Schema"),
    # Standardports werden nicht mitverglichen
    pytest.param({"Origin": "https://buch.example.org", "Host": "buch.example.org:443"}, True,
                 id="Port-443-im-Host"),
    pytest.param({"Origin": "https://buch.example.org:443", "Host": "buch.example.org"}, True,
                 id="Port-443-im-Origin"),
    pytest.param({"Origin": "http://buch.example.org", "Host": "buch.example.org:80"}, True,
                 id="Port-80-im-Host"),
    pytest.param({"Origin": "http://buch.example.org:80", "Host": "buch.example.org"}, True,
                 id="Port-80-im-Origin"),
    # Reverse-Proxy: X-Forwarded-Host (bei Liste zählt der erste Eintrag)
    pytest.param({"Origin": "https://buch.example.org", "Host": "127.0.0.1:2824",
                  "X-Forwarded-Host": "buch.example.org"}, True, id="X-Forwarded-Host"),
    pytest.param({"Origin": "https://buch.example.org", "Host": "127.0.0.1:2824",
                  "X-Forwarded-Host": "buch.example.org, proxy.internal"}, True,
                 id="X-Forwarded-Host-Liste"),
    pytest.param({"Origin": FOREIGN_ORIGIN, "Host": "127.0.0.1:2824",
                  "X-Forwarded-Host": "buch.example.org"}, False,
                 id="X-Forwarded-Host-fremder-Origin"),
    # Ohne Freigabe zählt ein fremder Name hinter dem Proxy nicht
    pytest.param({"Origin": "https://buch.example.org", "Host": "127.0.0.1:2824"}, False,
                 id="Proxy-Name-ohne-Freigabe"),
]


@pytest.mark.parametrize("pairs, expected", _SAME_ORIGIN_CASES)
def test_request_is_same_origin(pairs, expected):
    assert request_is_same_origin(_headers(pairs)) == expected


@pytest.mark.parametrize("allowed, expected", [
    pytest.param("", False, id="ohne-Freigabe"),
    pytest.param("buch.example.org", True, id="freigegeben"),
    pytest.param("andere.example.org, Buch.Example.ORG", True, id="Liste-Schreibweise-egal"),
    pytest.param("buch.example.org:8443", True, id="Freigabe-mit-Port"),
    pytest.param("andere.example.org", False, id="anderer-Name-freigegeben"),
])
def test_request_is_same_origin_honours_allowed_hosts(monkeypatch, allowed, expected):
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", allowed)
    headers = _headers({"Origin": "https://buch.example.org", "Host": "127.0.0.1:2824"})
    assert request_is_same_origin(headers) == expected


@pytest.mark.parametrize("host", [
    pytest.param(None, id="fehlt"),
    pytest.param("", id="leer"),
    "localhost", "localhost:8080", "LOCALHOST:2824",
    "buchhaltung", "buchhaltung:2824",                       # Name ohne Punkt
    "127.0.0.1", "127.0.0.1:8080", "192.0.2.10:2824", "10.0.0.5",
    "[::1]", "[::1]:8080", "::1", "[fe80::1]:8080",
    "nas.local", "nas.local:2824", "NAS.Local",
    "rechner.lan", "router.home.arpa", "dienst.internal", "box.fritz.box:8080",
    "app.localhost",
])
def test_host_is_trusted_for_local_names(host):
    assert host_is_trusted(host) is True


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8080", "buch.example.org", "buch.example.org:443",
    "localhost.evil.example",         # beginnt wie localhost, gehört aber jemand anderem
    "nas.local.evil.example",         # nur die Endung zählt
    "127.0.0.1.evil.example",         # IP-Adresse als Namensanfang ist keine IP
    "fakehome.arpa",                  # Endung ohne Punkt davor
    "xfritz.box",
])
def test_host_is_not_trusted_for_public_names(host):
    assert host_is_trusted(host) is False


def test_host_is_trusted_for_own_machine_name(monkeypatch):
    monkeypatch.setattr(server_app.socket, "gethostname", lambda: "Testrechner.Example.Org")
    assert host_is_trusted("testrechner.example.org") is True
    assert host_is_trusted("TESTRECHNER.example.org:8080") is True
    assert host_is_trusted("anderer.example.org") is False


@pytest.mark.parametrize("host, expected", [
    pytest.param("buch.example.org", True, id="ohne-Port"),
    pytest.param("buch.example.org:2824", True, id="mit-Port"),
    pytest.param("BUCH.Example.ORG", True, id="Schreibweise"),
    pytest.param("server.tailnet.example.net:443", True, id="zweiter-Eintrag-Schreibweise-der-Liste"),
    pytest.param("andere.example.org", False, id="nicht-in-der-Liste"),
    pytest.param("evil.example", False, id="fremd"),
])
def test_host_is_trusted_via_allowed_hosts(monkeypatch, host, expected):
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS",
                       "buch.example.org, Server.Tailnet.Example.NET:8443")
    assert host_is_trusted(host) is expected


def test_host_is_not_trusted_without_allowed_hosts():
    assert host_is_trusted("buch.example.org") is False


# ── Teil 2: echter Server ────────────────────────────────────────────────────
@pytest.fixture
def article_id(auto_login):
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    return auto_login.db.fetch_articles()[0][0]


# -- automatische Anmeldung: Löschen nur per POST von der eigenen Seite, mit Token ----
def test_get_on_delete_url_is_405_and_deletes_nothing(server_port, auto_login, article_id):
    resp = get(server_port, f"/masterdata/articles/delete?id={article_id}")
    assert resp.status == 405
    assert resp.headers["Allow"] == "POST"
    assert auto_login.db.get_article_by_id(article_id) is not None


def test_post_with_foreign_origin_is_403_and_deletes_nothing(server_port, auto_login, article_id):
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Origin": FOREIGN_ORIGIN}, csrf=auto_login.csrf)
    assert resp.status == 403
    assert auto_login.db.get_article_by_id(article_id) is not None


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_post_with_foreign_fetch_site_is_403_and_deletes_nothing(server_port, auto_login,
                                                                 article_id, site):
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Sec-Fetch-Site": site}, csrf=auto_login.csrf)
    assert resp.status == 403
    assert auto_login.db.get_article_by_id(article_id) is not None


def test_post_with_own_origin_deletes_article(server_port, auto_login, article_id):
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Origin": own_origin(server_port)}, csrf=auto_login.csrf)
    assert resp.status == 303
    assert resp.headers["Location"] == "/masterdata/articles"
    assert auto_login.db.get_article_by_id(article_id) is None


def test_post_with_same_origin_fetch_metadata_deletes_article(server_port, auto_login, article_id):
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Sec-Fetch-Site": "same-origin"}, csrf=auto_login.csrf)
    assert resp.status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


def test_post_without_origin_headers_is_accepted(server_port, auto_login, article_id):
    # Skript-Client (curl & Co.): weder Origin noch Sec-Fetch-Site – das Token ist trotzdem nötig
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}", csrf=auto_login.csrf)
    assert resp.status == 303
    assert resp.headers["Location"] == "/masterdata/articles"
    assert auto_login.db.get_article_by_id(article_id) is None


@pytest.mark.parametrize("path", [
    "/execute_sql", "/backup/create", "/backup/restore", "/setup/load_testdata",
    "/users/create", "/wiso/fdb-import", "/upload_receipts",
])
def test_every_post_route_is_behind_the_origin_check(server_port, auto_login, path):
    # Das Tor sitzt vor allen schreibenden Anfragen, auch vor denen, die ihren
    # Body selbst lesen (Upload, JSON). Das Token stimmt – nur die Herkunft nicht.
    resp = post(server_port, path, headers={"Origin": FOREIGN_ORIGIN}, form={"x": "1"},
                csrf=auto_login.csrf)
    assert resp.status == 403
    assert "fremder Herkunft" in resp.text


# -- CSRF-Token: auch ohne Sitzung Pflicht (anders als im früheren Einzelbenutzer-Modus) --
@pytest.mark.parametrize("token", [
    pytest.param(None, id="fehlt"),
    pytest.param("", id="leer"),
    pytest.param("falsch", id="falsch"),
    pytest.param(auth.csrf_for("irgendeine-sitzung"), id="Token-einer-Sitzung"),
    pytest.param(auth.csrf_for("auto-login:bob"), id="Token-eines-anderen-Benutzers"),
])
def test_post_without_valid_csrf_token_is_403_and_deletes_nothing(server_port, auto_login,
                                                                  article_id, token):
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}", csrf=token)
    assert resp.status == 403
    assert "Sicherheits-Token" in resp.text
    assert auto_login.db.get_article_by_id(article_id) is not None


def test_csrf_token_in_header_is_accepted_for_form_routes(server_port, auto_login, article_id):
    # Das Footer-JS hängt das Token an fetch-Aufrufe als Header X-CSRF-Token
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"X-CSRF-Token": auto_login.csrf})
    assert resp.status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


def _post_json(port, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-CSRF-Token"] = token
    return send(port, "POST", path, headers, json.dumps(payload))


def test_fetch_route_without_token_is_403(server_port, auto_login):
    # Routen, die ihren Body selbst lesen, prüfen das Token vorab aus dem Header
    resp = _post_json(server_port, "/invoice/status", {"invoice_id": 99999, "status": "sent"})
    assert resp.status == 403
    assert "Sicherheits-Token" in resp.text


def test_fetch_route_with_wrong_token_is_403(server_port, auto_login):
    resp = _post_json(server_port, "/invoice/status", {"invoice_id": 99999, "status": "sent"},
                      token="falsch")
    assert resp.status == 403


def test_fetch_route_with_valid_token_reaches_the_handler(server_port, auto_login):
    resp = _post_json(server_port, "/invoice/status", {"invoice_id": 99999, "status": "sent"},
                      token=auto_login.csrf)
    assert resp.status == 404                       # die Rechnung gibt es nicht – der Handler lief
    assert json.loads(resp.text)["error"] == "Rechnung nicht gefunden"


# -- alle Aktionspfade ----------------------------------------------------------
_ACTION_QUERY = ("id=99999&number=nr-99999&doc_id=99999&booking_id=99999"
                 "&person=1&from=2026-01-01&to=2026-12-31")

# Redirect-Ziel je Aktionspfad bei einer Anfrage mit nicht vorhandenen IDs
_POST_LOCATION = {
    "/asset_categories/delete": r"/asset_categories",
    "/assets/delete": r"/assets",
    "/documents/unlink": r"/transactions",
    "/masterdata/articles/delete": r"/masterdata/articles",
    "/masterdata/bankaccounts/delete": r"/masterdata/bankaccounts",
    "/masterdata/contacts/delete": r"/masterdata/contacts",
    "/masterdata/numberranges/delete": r"/masterdata/numberranges",
    "/masterdata/skr/delete": r"/masterdata/skr\?msg=[^&]+&type=error",
    "/masterdata/skr/togglemenu": r"/masterdata/skr",
    "/receipts/delete": r"/receipts",
    "/transactions/delete": r"/transactions",
    "/trips/delete": r"/trips\?person=1&from=2026-01-01&to=2026-12-31",
    "/worktime/delete": r"/worktime\?person=1&from=2026-01-01&to=2026-12-31",
}


def test_post_expectations_cover_all_action_paths():
    # Kommt ein Aktionspfad hinzu, braucht er hier ein Redirect-Ziel
    assert set(_POST_LOCATION) == set(_ACTION_PATHS)


@pytest.mark.parametrize("path", sorted(_ACTION_PATHS))
def test_get_on_action_path_is_405(server_port, auto_login, path):
    resp = get(server_port, f"{path}?{_ACTION_QUERY}")
    assert resp.status == 405
    assert resp.headers["Allow"] == "POST"


@pytest.mark.parametrize("path", sorted(_ACTION_PATHS))
def test_post_on_action_path_is_handled(server_port, auto_login, path):
    # Ein Pfad in _ACTION_PATHS ohne Zweig in _post_action_routes bliebe unbeantwortet
    # (Timeout); die Anfrage muss also mit dem erwarteten Redirect enden.
    resp = post(server_port, f"{path}?{_ACTION_QUERY}", csrf=auto_login.csrf)
    assert resp.status == 303
    assert re.fullmatch(_POST_LOCATION[path], resp.headers["Location"])


@pytest.mark.parametrize("path", sorted(_ACTION_PATHS))
def test_post_on_action_path_without_token_is_403(server_port, auto_login, path):
    resp = post(server_port, f"{path}?{_ACTION_QUERY}")
    assert resp.status == 403
    assert "Sicherheits-Token" in resp.text


@pytest.mark.parametrize("path, target", [
    ("/worktime/delete", "/worktime"),
    ("/trips/delete", "/trips"),
])
def test_delete_redirect_encodes_person_parameter(server_port, auto_login, path, target):
    # CR/LF im Parameter darf keinen eigenen Antwort-Header erzeugen
    resp = post(server_port, f"{path}?id=99999&person=1%0D%0ASet-Cookie:%20pwn=1"
                             f"&from=2026-01-01&to=2026-12-31", csrf=auto_login.csrf)
    assert resp.status == 303
    assert "Set-Cookie" not in resp.headers
    assert resp.headers["Location"] == (f"{target}?person=1%0D%0ASet-Cookie%3A%20pwn%3D1"
                                        f"&from=2026-01-01&to=2026-12-31")


# -- /documents/unlink: Redirect nur auf lokale Pfade ----------------------------
@pytest.mark.parametrize("referer, expected", [
    pytest.param("https://evil.example/x", "/x", id="fremde-Adresse-nur-Pfad"),
    pytest.param("{own}/receipts/edit?number=7", "/receipts/edit?number=7",
                 id="eigene-Seite-mit-Query"),
    pytest.param(None, "/transactions", id="ohne-Referer"),
    pytest.param("https://evil.example//attacker.example/x", "/transactions",
                 id="Pfad-mit-doppeltem-Slash"),
    pytest.param("https://evil.example/\\attacker.example", "/transactions",
                 id="Backslash-im-Pfad"),
    pytest.param("/\\attacker.example", "/transactions", id="relativ-mit-Backslash"),
    pytest.param("https://evil.example", "/transactions", id="ohne-Pfad"),
    pytest.param("javascript:alert(1)", "/transactions", id="javascript-Schema"),
    pytest.param("http://[::1", "/transactions", id="kaputte-Adresse"),
])
def test_unlink_redirects_only_to_local_part_of_referer(server_port, auto_login, referer, expected):
    headers = {} if referer is None else {"Referer": referer.replace("{own}", own_origin(server_port))}
    resp = post(server_port, "/documents/unlink?doc_id=99999&booking_id=99999", headers=headers,
                csrf=auto_login.csrf)
    location = resp.headers["Location"]
    assert resp.status == 303
    assert location == expected
    assert location.startswith("/") and not location.startswith("//")


def _booking_with_receipt(db):
    """Buchung mit verknüpftem Beleg 'R-1'; liefert (booking_id, doc_id)."""
    booking_id = db.insert_booking("2026-01-15", Decimal("100.00"), text="Probebuchung",
                                   booking_type="entry")
    db.insert_receipt("R-1", "2026-01-15", "r1.pdf", "belege/r1.pdf", "Probebeleg")
    doc_id = db.get_receipt_by_number("R-1")[0]
    db.link_booking_to_document(booking_id, doc_id)
    assert len(db.get_documents_for_booking(booking_id)) == 1
    return booking_id, doc_id


@pytest.fixture
def linked_booking(auto_login):
    return _booking_with_receipt(auto_login.db)


def test_get_on_unlink_url_is_405_and_keeps_link(server_port, auto_login, linked_booking):
    booking_id, doc_id = linked_booking
    resp = get(server_port, f"/documents/unlink?doc_id={doc_id}&booking_id={booking_id}")
    assert resp.status == 405
    assert len(auto_login.db.get_documents_for_booking(booking_id)) == 1


def test_post_on_unlink_url_removes_link(server_port, auto_login, linked_booking):
    booking_id, doc_id = linked_booking
    resp = post(server_port, f"/documents/unlink?doc_id={doc_id}&booking_id={booking_id}",
                headers={"Referer": f"{own_origin(server_port)}/receipts/edit?number=R-1"},
                csrf=auto_login.csrf)
    assert resp.status == 303
    assert resp.headers["Location"] == "/receipts/edit?number=R-1"
    assert auto_login.db.get_documents_for_booking(booking_id) == []


# -- Hostname-Prüfung (DNS-Rebinding): überall, wo niemand ein Passwort eingibt -----------
@pytest.mark.parametrize("installed", [
    pytest.param(False, id="Ersteinrichtung"),
    pytest.param(True, id="automatische-Anmeldung"),
])
def test_foreign_host_is_403_and_not_echoed(server_port, installed):
    if installed:
        installation.set_up()
    resp = get(server_port, "/", headers={"Host": "evil.example"})
    assert resp.status == 403
    assert "evil.example" not in resp.text


def test_host_from_allowed_hosts_is_accepted(server_port, auto_login, monkeypatch):
    headers = {"Host": "buch.example.org"}
    assert get(server_port, "/", headers).status == 403          # ohne Freigabe
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", "buch.example.org")
    assert get(server_port, "/", headers).status in (200, 303)    # erreicht die App


# -- Anmeldung mit Passwort: die Sitzung schützt, nicht der Hostname --------------------
@pytest.fixture
def multi_user_mode():
    """Installation mit Anmeldung: Administrator ``admin`` und Nutzerin ``alice``
    (Dummy-Zugangsdaten), keine automatische Anmeldung. Ohne Administrator wäre dies
    die Ersteinrichtung."""
    installation.set_up(auto_login=False, users=[("alice", False)])


def test_multi_mode_login_page_ignores_host_check(server_port, multi_user_mode):
    # Hier schützt die Sitzung, nicht der Hostname
    resp = get(server_port, "/login", headers={"Host": "evil.example"})
    assert resp.status == 200


def test_multi_mode_login_with_foreign_origin_is_403(server_port, multi_user_mode):
    resp = post(server_port, "/login", headers={"Origin": FOREIGN_ORIGIN},
                form={"username": "alice", "password": installation.PASSWORD})
    assert resp.status == 403
    assert "Set-Cookie" not in resp.headers


def test_multi_mode_login_with_own_origin_starts_session(server_port, multi_user_mode):
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": "alice", "password": installation.PASSWORD})
    assert resp.status == 303
    assert resp.headers["Set-Cookie"].startswith("session=")


@pytest.fixture
def alice_article(multi_user_mode):
    """Angemeldete Nutzerin: Session-Cookie, CSRF-Token und ein Artikel in ihrer Datenbank."""
    token = auth.create_session("alice")
    db = installation.open_db("alice")
    db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    return SimpleNamespace(db=db, article_id=db.fetch_articles()[0][0],
                           cookie={"Cookie": f"session={token}"}, csrf=auth.csrf_for(token))


def test_multi_mode_session_write_with_foreign_origin_is_403(server_port, alice_article):
    # Gültige Sitzung und gültiges CSRF-Token: nur die Herkunft ist falsch
    a = alice_article
    resp = post(server_port, f"/masterdata/articles/delete?id={a.article_id}",
                headers={**a.cookie, "Origin": FOREIGN_ORIGIN}, csrf=a.csrf)
    assert resp.status == 403
    assert a.db.get_article_by_id(a.article_id) is not None


def test_multi_mode_session_write_with_own_origin_deletes_article(server_port, alice_article):
    a = alice_article
    resp = post(server_port, f"/masterdata/articles/delete?id={a.article_id}",
                headers={**a.cookie, "Origin": own_origin(server_port)}, csrf=a.csrf)
    assert resp.status == 303
    assert a.db.get_article_by_id(a.article_id) is None


# ── Teil 3: Listenseiten lösen Aktionen per POST aus ─────────────────────────
def _calls(html, function):
    """Aufrufe von function(…) im HTML – ohne die Definition 'function name(' im Footer."""
    return re.findall(rf"(?<!function ){function}\(", html)


def _has_post_action(html, url):
    return re.search(r"appConfirmPost\(\s*[\"']" + re.escape(url) + r"[\"']", html) is not None


def test_article_list_deletes_via_post(tmp_db):
    tmp_db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = tmp_db.fetch_articles()[0][0]
    html = PageArticles(tmp_db)
    assert _has_post_action(html, f"/masterdata/articles/delete?id={article_id}")
    assert _calls(html, "appConfirmHref") == []


def test_contact_list_deletes_via_post(tmp_db):
    tmp_db.insert_contact(contact_type="customer", entity_type="company",
                          display_name="Probefirma", company_name="Probefirma",
                          street="Musterweg 1", city="Musterstadt")
    contact_id = list(tmp_db.fetch_contacts(entity_type="company"))[0][0]
    html = PageContacts(tmp_db)
    assert _has_post_action(html, f"/masterdata/contacts/delete?id={contact_id}")
    assert _calls(html, "appConfirmHref") == []


def test_transaction_list_deletes_via_post(db_with_coa):
    db = db_with_coa
    cash = db.fetch_accounts()[0][0]
    free = db.insert_booking("2026-02-01", Decimal("50.00"), text="freie Buchung",
                             booking_type="entry")
    bank_only = db.insert_booking("2026-02-02", Decimal("-20.00"), account_id=cash,
                                  text="nur Bank", booking_type="bank")
    bank_one = db.insert_booking("2026-02-03", Decimal("-30.00"), account_id=cash,
                                 text="Bank mit einem Satz", booking_type="bank")
    one_child = db.insert_booking("2026-02-03", Decimal("-30.00"), text="Satz",
                                  booking_type="entry", parent_booking_id=bank_one)
    split = db.insert_booking("2026-02-04", Decimal("-60.00"), account_id=cash,
                              text="Bank mit Split", booking_type="bank")
    split_a = db.insert_booking("2026-02-04", Decimal("-30.00"), text="Satz A",
                                booking_type="entry", parent_booking_id=split)
    split_b = db.insert_booking("2026-02-04", Decimal("-30.00"), text="Satz B",
                                booking_type="entry", parent_booking_id=split)
    html = PageTransactions(db, date_from="2026-01-01", date_to="2026-12-31")
    # Jede Zeilenart der Liste; die Kopfzeile eines Splits hat kein Löschsymbol,
    # gelöscht wird dort in den Zeilen der Buchungssätze
    for booking_id in (free, bank_only, bank_one, one_child, split_a, split_b):
        assert _has_post_action(html, f"/transactions/delete?id={booking_id}"), booking_id
    assert _calls(html, "appConfirmHref") == []


@pytest.mark.parametrize("render", [
    pytest.param(lambda db, booking_id: PageTransactions(
        db, edit_transaction_id=booking_id, date_from="2026-01-01", date_to="2026-12-31"),
        id="Buchung-bearbeiten"),
    pytest.param(lambda db, booking_id: PageReceiptEdit(db, "R-1"), id="Beleg-bearbeiten"),
])
def test_unlink_is_a_post_action_not_a_link(db_with_coa, render):
    booking_id, doc_id = _booking_with_receipt(db_with_coa)
    html = render(db_with_coa, booking_id)
    assert re.search(r"""href=['"]/documents/unlink""", html) is None
    assert re.search(r"appPost\(\s*['\"]/documents/unlink\?doc_id=%d&(?:amp;)?booking_id=%d['\"]"
                     % (doc_id, booking_id), html)
