# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Diagnose-MCP-Server (tools/mcp_ubuchhaltung.py): nur lesend, gegen die echte App geprüft.

Fünf Teile:
  1. Wächter für SQL und Seitenpfade, HTML-Umwandlung, Kürzung (reine Funktionen),
  2. Zugangsdaten (Umgebung, Windows-Tresor, keyring),
  3. MCP-Protokoll über ``Server.handle``/``serve`` (ohne Instanz),
  4. die Werkzeuge gegen einen echten Server auf 127.0.0.1 (Anmeldung mit Passwort, abgelaufene
     Sitzung, automatische Anmeldung, falsches Passwort, Nachweis „nichts wird geschrieben“),
  5. ein echter stdio-Lauf als Kindprozess.

``tools/`` ist kein Paket, das Modul wird per Dateipfad geladen. Fixtures ``server_port`` und
``auto_login`` stehen in conftest.py, ``installation`` in tests/installation.py. Benutzer,
Passwörter und Adressen sind erfundene Testwerte.
"""
import contextlib
import email.message
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
from types import SimpleNamespace

import pytest

import auth
import version
from tests import installation
from tests.installation import ADMIN, PASSWORD

TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tools", "mcp_ubuchhaltung.py")
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32",
                                  reason="Windows-Anmeldeinformationsverwaltung")
# Ein Tresorziel, das es garantiert nicht gibt: Auf einem Entwicklungsrechner mit eingerichtetem
# Eintrag "uBuchhaltung-MCP" läse der Standardname sonst echte Zugangsdaten.
NO_SUCH_ENTRY = f"nicht-vorhanden-{uuid.uuid4()}"


def _load_module():
    spec = importlib.util.spec_from_file_location("mcp_ubuchhaltung", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcp = _load_module()


# ── Hilfen ───────────────────────────────────────────────────────────────────
def rpc(server, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def call(server, name, **arguments):
    """Werkzeug aufrufen; liefert (Text, isError) und prüft die Form der Antwort."""
    reply = rpc(server, "tools/call", {"name": name, "arguments": arguments})
    result = reply["result"]
    assert [part["type"] for part in result["content"]] == ["text"]
    return result["content"][0]["text"], result["isError"]


def instance_env(port, **extra):
    return {"UBUCHHALTUNG_MCP_URL": f"http://127.0.0.1:{port}",
            "UBUCHHALTUNG_MCP_CREDENTIAL": NO_SUCH_ENTRY, **extra}


def admin_env(port, password=PASSWORD):
    return instance_env(port, UBUCHHALTUNG_MCP_USER=ADMIN, UBUCHHALTUNG_MCP_PASSWORD=password)


# ── Teil 1: Wächter, HTML, Kürzung ───────────────────────────────────────────
@pytest.mark.parametrize("query, expected", [
    pytest.param("SELECT 1", "SELECT 1", id="einfach"),
    pytest.param("  select * from Invoices", "select * from Invoices", id="klein-und-Leerraum"),
    pytest.param("/* c */ SELECT 1", "SELECT 1", id="Blockkommentar-vorweg"),
    pytest.param("-- c\nSELECT 1", "SELECT 1", id="Zeilenkommentar-vorweg"),
    pytest.param("/* a */ -- b\n /* c */ SELECT 1", "SELECT 1", id="mehrere-Kommentare"),
    pytest.param("SELECT 1;", "SELECT 1", id="abschliessendes-Semikolon"),
    pytest.param("SELECT 1 ;  ", "SELECT 1", id="Semikolon-mit-Leerraum"),
    pytest.param("SELECT 1 -- Ende", "SELECT 1 -- Ende", id="Kommentar-am-Ende"),
    pytest.param("SELECT * FROM pragma_table_info('Invoices')", "SELECT * FROM pragma_table_info('Invoices')",
                 id="pragma_table_info"),
    pytest.param("SELECT * FROM pragma_foreign_key_check", "SELECT * FROM pragma_foreign_key_check",
                 id="pragma_foreign_key_check"),
    pytest.param("SELECT * FROM pragma_database_list", "SELECT * FROM pragma_database_list",
                 id="pragma_database_list"),
    pytest.param("SELECT * FROM PRAGMA_Quick_Check", "SELECT * FROM PRAGMA_Quick_Check", id="Groß-Kleinschreibung"),
    pytest.param("SELECT 'without' AS w, 'Withdrawal' AS x", "SELECT 'without' AS w, 'Withdrawal' AS x",
                 id="Wörter-mit-with-als-Anfang"),
    pytest.param("SELECT 1 AS " + "x" * (mcp.MAX_QUERY_CHARS - 12), "SELECT 1 AS " + "x" * (mcp.MAX_QUERY_CHARS - 12),
                 id="genau-20000-Zeichen"),
])
def test_check_select_lets_one_select_through(query, expected):
    assert mcp.check_select(query) == expected


@pytest.mark.parametrize("query", [
    pytest.param("", id="leer"),
    pytest.param("DELETE FROM Contacts", id="DELETE"),
    pytest.param("WITH a AS (SELECT 1) DELETE FROM Contacts", id="WITH-DELETE"),
    pytest.param("SELECT 1; DELETE FROM Contacts", id="zweite-Anweisung"),
    pytest.param("PRAGMA foreign_keys=OFF", id="PRAGMA"),
    pytest.param("ATTACH 'x' AS y", id="ATTACH"),
    pytest.param("INSERT INTO Contacts SELECT * FROM Contacts", id="INSERT-SELECT"),
    pytest.param("UPDATE Contacts SET Name='x'", id="UPDATE"),
    pytest.param("SELECTX 1", id="SELECTX"),
    pytest.param("EXPLAIN SELECT 1", id="EXPLAIN"),
    pytest.param("/* offen SELECT 1", id="offener-Kommentar"),
    pytest.param("SELECT 'a;b'", id="Semikolon-in-Zeichenkette"),
    pytest.param("SELECT 1\x00", id="Nullzeichen"),
    pytest.param("SELECT 1;;", id="doppeltes-Semikolon"),
    pytest.param("SELECT 1; -- Rest", id="Semikolon-vor-Kommentar"),
    pytest.param("-- nur ein Kommentar", id="nur-Kommentar"),
    pytest.param(None, id="kein-Text"),
    pytest.param(42, id="Zahl"),
])
def test_check_select_rejects_everything_else(query):
    with pytest.raises(mcp.ReadOnlyViolation):
        mcp.check_select(query)


@pytest.mark.parametrize("query, name", [
    pytest.param("SELECT * FROM pragma_optimize", "optimize", id="pragma_optimize"),
    pytest.param('SELECT * FROM "pragma_optimize"', "optimize", id="in-doppelten-Anfuehrungszeichen"),
    pytest.param("SELECT * FROM [PRAGMA_OPTIMIZE]", "optimize", id="in-eckigen-Klammern-gross"),
    pytest.param("SELECT * FROM `pragma_optimize`", "optimize", id="in-Backticks"),
    pytest.param("SELECT 1 WHERE EXISTS(SELECT 1 FROM pragma_optimize)", "optimize", id="in-Unterabfrage"),
    pytest.param("SELECT 'pragma_optimize'", "optimize", id="in-Zeichenkette"),
    pytest.param("SELECT 1 -- pragma_optimize", "optimize", id="im-Kommentar"),
    pytest.param("SELECT * FROM pragma_analysis_limit", "analysis_limit", id="pragma_analysis_limit"),
    pytest.param("SELECT * FROM pragma_wal_checkpoint", "wal_checkpoint", id="pragma_wal_checkpoint"),
    pytest.param("SELECT * FROM pragma_table_info('A'), pragma_shrink_memory", "shrink_memory",
                 id="erlaubte-und-verbotene-zusammen"),
    pytest.param("SELECT * FROM pragma_pragma_list", "pragma_list", id="pragma_pragma_list"),
])
def test_check_select_refuses_pragma_functions_that_are_not_read_only(query, name):
    with pytest.raises(mcp.ReadOnlyViolation, match=f"PRAGMA-Funktion nicht freigegeben: pragma_{name}$"):
        mcp.check_select(query)


def test_the_read_pragmas_are_exactly_the_reviewed_ones():
    assert mcp._READ_PRAGMAS == {
        "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
        "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check", "user_version",
        "journal_mode", "page_count", "page_size", "freelist_count", "compile_options", "database_list"}


@pytest.mark.parametrize("query", [
    pytest.param("SELECT count(*) FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                 "SELECT x FROM c)", id="WITH-RECURSIVE"),
    pytest.param("SELECT count(*) FROM (WITH c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT x FROM c)",
                 id="rekursiv-ohne-das-Wort-RECURSIVE"),
    pytest.param("SELECT 1 AS recursive", id="recursive-als-Wort"),
    pytest.param("SELECT zeroblob(10)", id="zeroblob"),
    pytest.param("SELECT randomblob(10)", id="randomblob"),
    pytest.param("SELECT ZEROBLOB (10)", id="zeroblob-gross-mit-Leerzeichen"),
    pytest.param('SELECT "zeroblob"(10)', id="zeroblob-in-Anfuehrungszeichen"),
    pytest.param("SELECT [randomblob](10)", id="randomblob-in-Klammern"),
    pytest.param("SELECT 1 AS " + "x" * (mcp.MAX_QUERY_CHARS + 1 - 12), id="20001-Zeichen"),
])
def test_check_select_refuses_what_could_keep_the_database_busy(query):
    with pytest.raises(mcp.ReadOnlyViolation, match="aus Lastgründen gesperrt"):
        mcp.check_select(query)


def test_the_length_limit_is_20000_characters():
    assert mcp.MAX_QUERY_CHARS == 20_000
    at_limit = "SELECT 1 AS " + "x" * (20_000 - 12)
    assert len(at_limit) == 20_000 and mcp.check_select(at_limit) == at_limit
    with pytest.raises(mcp.ReadOnlyViolation, match="Abfragen über 20000 Zeichen sind aus Lastgründen gesperrt"):
        mcp.check_select(at_limit + "x")


@pytest.mark.parametrize("query", [
    pytest.param("SELECT ID FROM Invoices) /*", id="offen-nach-schliessender-Klammer"),
    pytest.param("SELECT 1 /* x */", id="geschlossen-mitten-in-der-Abfrage"),
    pytest.param("SELECT 1 /*", id="offen-am-Ende"),
    pytest.param("/* vorweg */ SELECT 1 /* nochmal */", id="zweiter-Kommentar-nach-dem-Anfang"),
    pytest.param("SELECT length/**/('a')", id="Kommentar-statt-Leerraum"),
    pytest.param("SELECT '/*'", id="in-Zeichenkette"),
])
def test_check_select_allows_block_comments_only_at_the_start(query):
    with pytest.raises(mcp.ReadOnlyViolation, match="Blockkommentare nur am Anfang erlaubt"):
        mcp.check_select(query)


def test_a_violation_is_a_value_error():
    assert issubclass(mcp.ReadOnlyViolation, ValueError)


# GET-Routen der App, die Daten ändern oder PDFs erzeugen und ablegen (server/app.py, do_GET) –
# sie dürfen nie in die Positivliste.
SIDE_EFFECT_ROUTES = ("/quote/delete", "/invoice/pdf_generate", "/quote/pdf_generate",
                      "/quote/pdf_download", "/worktime/pdf")


@pytest.mark.parametrize("path", [
    "/", "/about", "/invoice?id=3", "/invoice/edit?id=3", "/masterdata/contacts?type=customer",
    "/users?info=x", "/receipts/edit?number=B%C3%BCro-1", "/transactions?from=2024-01-01&to=2024-12-31",
    "/transactions", "/transactions?", "/dashboard?acct=1&acct=2&from=2024-01-01",
    "/invoice?search=M%C3%BCller&status=paid", "/assets?status",
])
def test_check_page_path_allows_listed_display_pages(path):
    assert mcp.check_page_path(path) == path


@pytest.mark.parametrize("path", [
    pytest.param("/quote/delete?id=1", id="Angebot-loeschen"),
    pytest.param("/invoice/pdf_generate?id=1", id="Rechnungs-PDF-erzeugen"),
    pytest.param("/quote/pdf_generate?id=1", id="Angebots-PDF-erzeugen"),
    pytest.param("/quote/pdf_download?id=1", id="Angebots-PDF-erzeugt-neu"),
    pytest.param("/worktime/pdf?person=1", id="Stundenzettel-PDF"),
    pytest.param("/invoice/xml?id=1", id="XRechnung-erzeugen"),
    pytest.param("/invoice/pdf_download?id=1", id="PDF-laden"),
    pytest.param("/setup", id="Einrichtung"),
    pytest.param("/data/logos/x.png", id="Logo-Datei"),
    pytest.param("/seed_data/private/x.png", id="private-Seed-Datei"),
    pytest.param("/buch.css", id="Stylesheet"),
    pytest.param("/login", id="login"),
    pytest.param("/logout", id="logout"),
    pytest.param("/LOGIN/", id="LOGIN-gross-mit-Schrägstrich"),
    pytest.param("/login?x=1", id="login-mit-Query"),
    pytest.param("/invoice/edit/../pdf_generate?id=1", id="Punktsegmente"),
    pytest.param("/invoice/%65dit?id=1", id="prozentkodierter-Pfad"),
    pytest.param("/About", id="Groß-Kleinschreibung"),
    pytest.param("/invoice/editX", id="angehaengter-Buchstabe"),
    pytest.param("/invoice/edit/", id="angehaengter-Schraegstrich"),
    pytest.param("/about;x=1", id="Pfadparameter"),
    pytest.param("/invoice/xml_download?id=1", id="liest-gespeicherte-Pfade-ungeprueft"),
])
def test_check_page_path_refuses_everything_off_the_list(path):
    with pytest.raises(ValueError, match=r"Seite nicht freigegeben – .*_PAGE_ALLOWLIST in tools/mcp_ubuchhaltung\.py"):
        mcp.check_page_path(path)


@pytest.mark.parametrize("path, key", [
    pytest.param("/transactions?from_invoice=1", "from_invoice", id="legt-ein-Konto-an"),
    pytest.param("/transactions?from=2024-01-01&from_invoice=1", "from_invoice", id="neben-erlaubtem-Schluessel"),
    pytest.param("/transactions?from%5Finvoice=1", "from_invoice", id="Schluessel-prozentkodiert"),
    pytest.param("/transactions?from_invoice", "from_invoice", id="ohne-Wert"),
    pytest.param("/transactions?FROM=2024-01-01", "FROM", id="Grossschreibung-zaehlt"),
    pytest.param("/invoice?copy_from=1", "copy_from", id="Kopie-einer-Rechnung"),
    pytest.param("/about?x=1", "x", id="Seite-ohne-Parameter"),
    pytest.param("/about?=x", "", id="leerer-Schluessel"),
    pytest.param("/invoice/view?id=1&error=x", "error", id="Schluessel-einer-anderen-Seite"),
])
def test_check_page_path_refuses_query_keys_that_are_not_listed(path, key):
    with pytest.raises(ValueError, match=f"Query-Parameter nicht freigegeben: {key}$"):
        mcp.check_page_path(path)


ALL_QUERY_KEYS = [(page, key) for page, keys in sorted(mcp._PAGE_ALLOWLIST.items()) for key in sorted(keys)]


@pytest.mark.parametrize("page, key", ALL_QUERY_KEYS)
def test_each_listed_query_key_passes_and_its_upper_case_form_does_not(page, key):
    assert mcp.check_page_path(f"{page}?{key}=1") == f"{page}?{key}=1"
    with pytest.raises(ValueError, match="Query-Parameter nicht freigegeben"):
        mcp.check_page_path(f"{page}?{key.upper()}=1")


@pytest.mark.parametrize("page", sorted(mcp._PAGE_ALLOWLIST))
def test_each_listed_page_refuses_an_unknown_query_key(page):
    with pytest.raises(ValueError, match="Query-Parameter nicht freigegeben: unbekannt$"):
        mcp.check_page_path(f"{page}?unbekannt=1")
    for key in sorted(mcp._PAGE_ALLOWLIST[page])[:1]:
        with pytest.raises(ValueError, match="Query-Parameter nicht freigegeben: unbekannt$"):
            mcp.check_page_path(f"{page}?{key}=1&unbekannt=1")


@pytest.mark.parametrize("path", [
    pytest.param("about", id="ohne-Schrägstrich"),
    pytest.param("//evil.example/x", id="Doppelschrägstrich"),
    pytest.param("http://evil.example", id="absolute-Adresse"),
    pytest.param("/a\\b", id="Backslash"),
    pytest.param("/a b", id="Leerzeichen"),
    pytest.param("/a\nb", id="Zeilenumbruch"),
    pytest.param("/a\x7fb", id="DEL"),
    pytest.param("/about?x=a b", id="Leerzeichen-in-der-Query"),
    pytest.param("/about#x", id="Fragment"),
    pytest.param("/about?x=1#y", id="Fragment-nach-der-Query"),
    pytest.param("/logout#x", id="Fragment-nach-logout"),
    pytest.param("", id="leer"),
    pytest.param(None, id="kein-Text"),
    pytest.param(5, id="Zahl"),
])
def test_check_page_path_refuses_malformed_paths(path):
    with pytest.raises(ValueError):
        mcp.check_page_path(path)


def test_the_allowlist_holds_plain_paths_and_no_side_effect_route():
    assert "/" in mcp._PAGE_ALLOWLIST and "/about" in mcp._PAGE_ALLOWLIST      # /about holt das CSRF-Token
    for page, keys in mcp._PAGE_ALLOWLIST.items():
        assert mcp.check_page_path(page) == page
        assert "?" not in page
        # die App wählt ihre Routen per Präfix: auch Pfade, die damit beginnen, wären gefährlich
        assert not page.startswith(SIDE_EFFECT_ROUTES), page
        assert isinstance(keys, frozenset) and all(re.fullmatch(r"[a-z_]+", key) for key in keys), page


def test_the_allowlist_leaves_out_the_pages_the_review_found_unsafe():
    assert "/invoice/xml_download" not in mcp._PAGE_ALLOWLIST                  # liest Pfade aus Invoices.XMLPath
    assert not {"from_invoice", "copy_from"} & {key for keys in mcp._PAGE_ALLOWLIST.values() for key in keys}


def test_html_to_text_keeps_the_readable_part():
    markup = ("<html><head><title>Titel</title><style>p { color: red }</style>"
              "<script>var geheim = 1;</script></head><body><h1>Kopf</h1>"
              "<p>Text &amp; mehr &uuml;</p>"
              "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
              "<div>eins<br>zwei<br><br><br>drei</div>"
              "<svg><path d='M0 0'/></svg><noscript>nein</noscript><template>nie</template>"
              "</body></html>")
    assert mcp.html_to_text(markup) == "Titel\nKopf\nText & mehr ü\nA | B\n1 | 2\neins\nzwei\n\ndrei"


def test_html_to_text_collapses_whitespace_and_blank_lines():
    text = mcp.html_to_text("<p>  viel \n\t Raum   hier </p>\n\n<br><br><br><br><p>danach</p><br>")
    assert text == "viel Raum hier\n\ndanach"


def test_clip_marks_what_was_cut():
    assert mcp.clip("abc", 5) == "abc"
    assert mcp.clip("abcdef", 3) == "abc\n… gekürzt (6 Zeichen insgesamt)"


# ── Teil 2: Zugangsdaten ─────────────────────────────────────────────────────
def test_credentials_come_from_the_environment_first():
    env = {"UBUCHHALTUNG_MCP_USER": "anna", "UBUCHHALTUNG_MCP_PASSWORD": "pw-umgebung"}
    assert mcp.load_credentials(env) == ("anna", "pw-umgebung")


def test_a_password_without_a_user_is_an_error():
    with pytest.raises(ValueError, match="UBUCHHALTUNG_MCP_USER"):
        mcp.load_credentials({"UBUCHHALTUNG_MCP_PASSWORD": "pw-umgebung"})


def test_without_any_source_there_are_no_credentials():
    assert mcp.load_credentials({"UBUCHHALTUNG_MCP_CREDENTIAL": NO_SUCH_ENTRY}) == (None, None)


def test_the_windows_entry_supplies_user_and_password(monkeypatch):
    asked = []
    monkeypatch.setattr(mcp, "_read_windows_credential",
                        lambda target: asked.append(target) or ("anna", "pw-tresor"))
    assert mcp.load_credentials({}) == ("anna", "pw-tresor")
    assert mcp.load_credentials({"UBUCHHALTUNG_MCP_CREDENTIAL": "anderes-ziel"}) == ("anna", "pw-tresor")
    assert asked == ["uBuchhaltung-MCP", "anderes-ziel"]


def test_the_user_variable_wins_over_the_windows_entry(monkeypatch):
    monkeypatch.setattr(mcp, "_read_windows_credential", lambda target: ("anna", "pw-tresor"))
    assert mcp.load_credentials({"UBUCHHALTUNG_MCP_USER": "bert"}) == ("bert", "pw-tresor")


def test_the_environment_password_wins_over_the_windows_entry(monkeypatch):
    monkeypatch.setattr(mcp, "_read_windows_credential", lambda target: ("anna", "pw-tresor"))
    env = {"UBUCHHALTUNG_MCP_USER": "bert", "UBUCHHALTUNG_MCP_PASSWORD": "pw-umgebung"}
    assert mcp.load_credentials(env) == ("bert", "pw-umgebung")


def test_keyring_is_used_for_a_named_user(monkeypatch):
    asked = []
    fake = SimpleNamespace(get_password=lambda service, user: asked.append((service, user)) or "pw-keyring")
    monkeypatch.setattr(mcp, "_read_windows_credential", lambda target: None)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert mcp.load_credentials({"UBUCHHALTUNG_MCP_USER": "anna"}) == ("anna", "pw-keyring")
    assert asked == [("uBuchhaltung-MCP", "anna")]


def test_keyring_is_not_asked_without_a_user(monkeypatch):
    fake = SimpleNamespace(get_password=lambda service, user: pytest.fail("keyring ohne Benutzer gefragt"))
    monkeypatch.setattr(mcp, "_read_windows_credential", lambda target: None)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert mcp.load_credentials({}) == (None, None)


def test_a_missing_keyring_package_is_fine(monkeypatch):
    monkeypatch.setattr(mcp, "_read_windows_credential", lambda target: None)
    monkeypatch.setitem(sys.modules, "keyring", None)          # import keyring → ImportError
    assert mcp.load_credentials({"UBUCHHALTUNG_MCP_USER": "anna"}) == (None, None)


@WINDOWS_ONLY
def test_a_missing_windows_entry_is_none():
    assert mcp._read_windows_credential(NO_SUCH_ENTRY) is None


@pytest.mark.skipif(sys.platform == "win32", reason="nur außerhalb von Windows")
def test_the_windows_vault_is_not_consulted_elsewhere():
    assert mcp._read_windows_credential("uBuchhaltung-MCP") is None


@pytest.mark.parametrize("env, message", [
    pytest.param({}, "UBUCHHALTUNG_MCP_URL", id="Adresse-fehlt"),
    pytest.param({"UBUCHHALTUNG_MCP_URL": "ftp://nas.local"}, "http://", id="falsches-Schema"),
    pytest.param({"UBUCHHALTUNG_MCP_URL": "nas.local:2824"}, "http://", id="ohne-Schema"),
    pytest.param({"UBUCHHALTUNG_MCP_URL": "http://anna:geheim@example.org"}, "Zugangsdaten",
                 id="Zugangsdaten-in-der-Adresse"),
    pytest.param({"UBUCHHALTUNG_MCP_URL": "http://nas.local", "UBUCHHALTUNG_MCP_TIMEOUT": "schnell"},
                 "UBUCHHALTUNG_MCP_TIMEOUT", id="Zeitlimit-keine-Zahl"),
    pytest.param({"UBUCHHALTUNG_MCP_URL": "http://nas.local", "UBUCHHALTUNG_MCP_TIMEOUT": "0"},
                 "UBUCHHALTUNG_MCP_TIMEOUT", id="Zeitlimit-null"),
])
def test_client_from_env_rejects_a_bad_configuration(env, message):
    with pytest.raises(ValueError, match=message):
        mcp.client_from_env({"UBUCHHALTUNG_MCP_CREDENTIAL": NO_SUCH_ENTRY, **env})


def test_client_from_env_stores_the_address_without_trailing_slash():
    client = mcp.client_from_env({"UBUCHHALTUNG_MCP_URL": "http://nas.local:2824/",
                                  "UBUCHHALTUNG_MCP_TIMEOUT": "7.5",
                                  "UBUCHHALTUNG_MCP_CREDENTIAL": NO_SUCH_ENTRY})
    assert client.base_url == "http://nas.local:2824"
    assert client.timeout == 7.5


def test_the_vault_blob_is_decoded_as_utf16_without_the_trailing_null():
    assert mcp._decode_credential_blob("geheim".encode("utf-16-le") + b"\x00\x00") == "geheim"
    assert mcp._decode_credential_blob("geheim".encode("utf-16-le")) == "geheim"
    assert mcp._decode_credential_blob("Pä$$wört-ß€".encode("utf-16-le")) == "Pä$$wört-ß€"
    assert mcp._decode_credential_blob(b"") == ""


@pytest.mark.parametrize("raw", [
    pytest.param(b"A\x00!", id="ungerade-Laenge"),
    pytest.param(b"\x00\xd8A\x00", id="Surrogat-ohne-Partner"),
    pytest.param(b"\x00\xdc", id="einzelnes-Low-Surrogat"),
])
def test_a_broken_vault_blob_is_reported_without_revealing_any_byte(raw):
    with pytest.raises(ValueError) as caught:
        mcp._decode_credential_blob(raw)
    message = str(caught.value)
    assert message == "Tresoreintrag ist kein UTF-16-Text – bitte mit cmdkey neu anlegen"
    assert not re.search(r"\d|0x|position", message.replace("UTF-16", ""))       # weder Byte noch Länge
    assert caught.value.__suppress_context__                                    # und kein Zwischenfehler in der Kette


def test_a_broken_vault_entry_is_a_tool_error_with_that_message(monkeypatch):
    def broken(target):
        return mcp._decode_credential_blob(b"A\x00!")
    monkeypatch.setattr(mcp, "_read_windows_credential", broken)
    text, is_error = call(mcp.Server(env={"UBUCHHALTUNG_MCP_URL": "https://nas.local"}), "info")
    assert is_error
    assert text == "Tresoreintrag ist kein UTF-16-Text – bitte mit cmdkey neu anlegen"


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("ja", True), (" Ja ", True),
    ("0", False), ("", False), ("nein", False), ("on", False), ("false", False),
])
def test_client_from_env_reads_the_http_exception(value, expected):
    client = mcp.client_from_env({"UBUCHHALTUNG_MCP_URL": "http://nas.local", "UBUCHHALTUNG_MCP_ALLOW_HTTP": value,
                                  "UBUCHHALTUNG_MCP_CREDENTIAL": NO_SUCH_ENTRY})
    assert client.allow_http is expected


# ── Teil 2b: Client-Logik ohne Instanz (Attrappen) ───────────────────────────
def _login_reply():
    reply = email.message.Message()
    reply["Set-Cookie"] = "session=abc123; HttpOnly; SameSite=Strict; Path=/"
    reply["Location"] = "/"
    return reply


class _RecordingClient(mcp.UBuchhaltungClient):
    """Merkt sich jede Anfrage und antwortet wie eine Instanz, die den Login annimmt."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def _request(self, method, path, form=None, headers=None):
        self.sent.append((method, path))
        return 303, _login_reply(), b""


@pytest.mark.parametrize("url", [
    "http://nas.local:2824", "http://192.0.2.10:2824", "http://[2001:db8::1]:2824", "http://buch.example.org",
    "http://localhost.example.org", "http://127.0.0.1.example.org",
])
def test_credentials_are_not_sent_over_plain_http_to_another_machine(url):
    client = _RecordingClient(url, "admin", "geheim123")
    with pytest.raises(PermissionError, match="Zugangsdaten werden über http nicht gesendet") as caught:
        client.login()
    assert str(caught.value) == ("Zugangsdaten werden über http nicht gesendet – HTTPS mit UBUCHHALTUNG_MCP_CAFILE "
                                 "nutzen oder bewusst UBUCHHALTUNG_MCP_ALLOW_HTTP=1 setzen")
    assert "geheim123" not in str(caught.value)
    assert client.sent == [] and client.session_token is None             # nichts wurde gesendet


@pytest.mark.parametrize("url", ["http://nas.local:2824", "http://192.0.2.10:2824", "http://[2001:db8::1]:2824"])
def test_the_explicit_exception_sends_the_credentials_over_http(url):
    client = _RecordingClient(url, "admin", "geheim123", allow_http=True)
    client.login()
    assert client.sent == [("POST", "/login")]
    assert client.session_token == "abc123"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:2824", "http://127.5.5.5", "http://localhost:8080", "http://LOCALHOST", "http://[::1]:2824",
    "https://nas.local:2824", "https://192.0.2.10",
])
def test_loopback_and_https_need_no_exception(url):
    client = _RecordingClient(url, "admin", "geheim123")
    client.login()
    assert client.sent == [("POST", "/login")]


def test_without_credentials_the_login_asks_for_the_setup_not_for_https():
    client = _RecordingClient("http://nas.local:2824")
    with pytest.raises(PermissionError, match="cmdkey /generic:uBuchhaltung-MCP"):
        client.login()
    assert client.sent == []


def test_automatic_login_works_over_plain_http_without_credentials():
    class AutoLogin(_RecordingClient):
        def _request(self, method, path, form=None, headers=None):
            self.sent.append((method, path))
            reply = email.message.Message()
            reply["Content-Type"] = "text/html"
            return 200, reply, b"<html><meta name='csrf-token' content='tok-1'></html>"

    client = AutoLogin("http://nas.local:2824")
    client.ensure_session()
    assert client.csrf_token == "tok-1"
    assert client.sent == [("GET", "/about")]                           # kein Login, also keine Zugangsdaten unterwegs


class _Chunks:
    """Antwort-Attrappe: liefert die Stücke nacheinander über read1, wie http.client.HTTPResponse."""

    status = 200

    def __init__(self, *chunks, pause=0.0):
        self.headers = email.message.Message()
        self._chunks = list(chunks)
        self._pause = pause

    def read1(self, amount):
        time.sleep(self._pause)
        return self._chunks.pop(0)[:amount] if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeOpener:
    def __init__(self, outcome):
        self._outcome = outcome

    def open(self, request, timeout=None):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def test_a_response_is_read_completely_in_pieces():
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    assert client._read_limited(_Chunks(b"ab", b"cd", b"e"), time.monotonic() + 5) == b"abcde"


def test_an_oversized_response_is_cut_off(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_BODY", 100)
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    assert client._read_limited(_Chunks(b"x" * 50, b"x" * 50), time.monotonic() + 5) == b"x" * 100     # genau am Limit
    with pytest.raises(ConnectionError, match="^Antwort zu groß"):
        client._read_limited(_Chunks(b"x" * 50, b"x" * 51), time.monotonic() + 5)


def test_the_limit_message_names_sixteen_megabytes():
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    assert mcp.MAX_BODY == 16 * 1024 * 1024
    with pytest.raises(ConnectionError, match=r"^Antwort zu groß \(> 16 MB\) – Abfrage eingrenzen$"):
        client._read_limited(_Chunks(*[b"x" * mcp._CHUNK] * 257), time.monotonic() + 30)       # 16 MiB + 64 KiB


def test_a_slow_response_runs_into_the_deadline():
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1", timeout=0.05)
    with pytest.raises(ConnectionError, match=r"^Antwort dauert zu lange \(> 0\.1 s\)$"):
        client._read_limited(_Chunks(b"a", b"b", b"c", b"d", pause=0.04), time.monotonic() + 2 * client.timeout)


def test_the_deadline_is_twice_the_timeout():
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1", timeout=30)
    with pytest.raises(ConnectionError, match=r"^Antwort dauert zu lange \(> 60 s\)$"):
        client._read_limited(_Chunks(b"a"), time.monotonic() - 1)


def test_request_reports_the_limit_as_such_not_as_an_unreachable_instance(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_BODY", 100)
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    client._opener = _FakeOpener(_Chunks(b"x" * 64, b"x" * 64))
    with pytest.raises(ConnectionError) as caught:
        client._request("GET", "/about")
    assert str(caught.value).startswith("Antwort zu groß")


def test_the_body_of_an_error_response_is_limited_too(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_BODY", 100)
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    error = urllib.error.HTTPError("http://127.0.0.1:1/x", 500, "Fehler", email.message.Message(),
                                   io.BytesIO(b"x" * 200))
    client._opener = _FakeOpener(error)
    with pytest.raises(ConnectionError, match="^Antwort zu groß"):
        client._request("GET", "/x")
    small = urllib.error.HTTPError("http://127.0.0.1:1/x", 404, "Nicht da", email.message.Message(),
                                   io.BytesIO(b"klein"))
    client._opener = _FakeOpener(small)
    status, _, body = client._request("GET", "/x")
    assert (status, body) == (404, b"klein")


def test_a_reset_connection_is_still_reported_as_unreachable():
    client = mcp.UBuchhaltungClient("http://127.0.0.1:1")
    client._opener = _FakeOpener(ConnectionResetError("Verbindung zurückgesetzt"))
    with pytest.raises(ConnectionError, match="^Instanz nicht erreichbar: Verbindung zurückgesetzt$"):
        client._request("GET", "/about")


class _Stream(http.server.BaseHTTPRequestHandler):
    """Gegenstelle für die echten Socket-Tests: /gross sendet zügig sehr viel, /langsam tröpfelt."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(10 ** 9))
        self.end_headers()
        try:
            for _ in range(10 ** 4):
                self.wfile.write(b"x" * 65536 if self.path == "/gross" else b"x")
                self.wfile.flush()
                if self.path == "/langsam":
                    time.sleep(0.1)
        except OSError:
            pass                                                  # der Client hat abgebrochen


@pytest.fixture
def stream_server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stream)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=10)


def test_an_endless_download_over_a_real_socket_is_cut_off(stream_server, monkeypatch):
    monkeypatch.setattr(mcp, "MAX_BODY", 256 * 1024)
    client = mcp.UBuchhaltungClient(stream_server, timeout=10)
    with pytest.raises(ConnectionError, match="^Antwort zu groß"):
        client._request("GET", "/gross")


def test_a_trickling_response_over_a_real_socket_runs_into_the_deadline(stream_server):
    client = mcp.UBuchhaltungClient(stream_server, timeout=0.5)          # jedes Byte kommt vor Ablauf des Socket-Zeitlimits
    started = time.monotonic()
    with pytest.raises(ConnectionError, match=r"^Antwort dauert zu lange \(> 1 s\)$"):
        client._request("GET", "/langsam")
    assert time.monotonic() - started < 5                                # nicht erst, wenn 64 KiB zusammen wären


class _ScriptedClient(mcp.UBuchhaltungClient):
    """Beantwortet die Konsolenbefehle aus einer Liste und merkt sie sich (kein HTTP)."""

    def __init__(self, *replies):
        super().__init__("http://127.0.0.1:1")
        self.replies = list(replies)
        self.statements = []

    def _execute(self, sql):
        self.statements.append(sql)
        return self.replies.pop(0)


def test_run_select_asks_for_the_column_names_first_and_then_for_json_rows():
    client = _ScriptedClient(["id;Name"], ['json_array("id","Name")', '[1,"Müller"]', '[2,null]'])
    columns, rows, more = client.run_select("SELECT id, Name FROM Contacts", 200)
    assert client.statements == [
        "SELECT * FROM (\nSELECT id, Name FROM Contacts\n) LIMIT 0",
        'SELECT json_array("id","Name") FROM (\nSELECT id, Name FROM Contacts\n) LIMIT 201',
    ]
    assert (columns, rows, more) == (["id", "Name"], [[1, "Müller"], [2, None]], False)


def test_run_select_quotes_column_names_and_cuts_at_max_rows():
    client = _ScriptedClient(['a"b;c d'], ["json_array(…)", "[1,2]", "[3,4]", "[5,6]"])
    columns, rows, more = client.run_select("SELECT 1 AS \"a\"\"b\", 2 AS \"c d\"", 2)
    assert client.statements[1].startswith('SELECT json_array("a""b","c d") FROM (\n')
    assert client.statements[1].endswith("\n) LIMIT 3")
    assert (columns, rows, more) == (['a"b', "c d"], [[1, 2], [3, 4]], True)


def test_run_select_refuses_a_column_name_with_a_line_break():
    client = _ScriptedClient(["a", "b"])                                 # Spaltenname "a\nb" – die Konsole gibt zwei Zeilen aus
    with pytest.raises(mcp.QueryError, match="^Spaltenname mit Zeilenumbruch – bitte mit AS benennen$"):
        client.run_select("SELECT 1", 10)
    assert len(client.statements) == 1                                   # die Zeilenabfrage wird gar nicht erst gestellt


@pytest.mark.parametrize("header", [[], [""], ["a;;b"]])
def test_run_select_refuses_an_empty_column_name(header):
    client = _ScriptedClient(header)
    with pytest.raises(mcp.QueryError, match="^Leerer Spaltenname – bitte mit AS benennen$"):
        client.run_select("SELECT 1", 10)


def test_run_select_reports_output_that_is_not_json():
    client = _ScriptedClient(["a"], ["json_array(a)", "kein JSON"])
    with pytest.raises(ConnectionError, match="kein JSON"):
        client.run_select("SELECT 1 AS a", 10)


def test_run_select_checks_the_query_before_anything_is_sent():
    client = _ScriptedClient()
    with pytest.raises(mcp.ReadOnlyViolation):
        client.run_select("SELECT * FROM pragma_optimize", 10)
    assert client.statements == []


@pytest.mark.parametrize("sql", ["DELETE FROM Contacts", "SELECT 1; DELETE FROM Contacts", "  SELECT 1",
                                 "select 1", "PRAGMA user_version"])
def test_the_console_gets_only_a_single_select_without_semicolon(sql):
    class Unreachable(mcp.UBuchhaltungClient):
        def _request(self, *args, **kwargs):
            raise AssertionError("es darf nichts gesendet werden")

    with pytest.raises(mcp.ReadOnlyViolation, match="Nur ein einzelnes SELECT ohne ';'"):
        Unreachable("http://127.0.0.1:1")._execute(sql)


def test_a_blob_error_gets_a_hint():
    class Failing(mcp.UBuchhaltungClient):
        def _request(self, method, path, form=None, headers=None):
            reply = email.message.Message()
            reply["Content-Type"] = "application/json"
            body = json.dumps({"success_count": 0, "total": 1, "output": "",
                               "errors": ["Befehl 1: JSON cannot hold BLOB values"]})
            return 200, reply, body.encode("utf-8")

    client = Failing("http://127.0.0.1:1")
    client.csrf_token = "x"
    with pytest.raises(mcp.QueryError) as caught:
        client._execute("SELECT 1")
    assert str(caught.value) == ("Befehl 1: JSON cannot hold BLOB values – "
                                 "BLOB-Spalten mit hex(x) oder length(x) abfragen")


# ── Teil 3: MCP-Protokoll ohne Instanz ───────────────────────────────────────
@pytest.fixture
def bare_server():
    return mcp.Server(env={})


def test_initialize_echoes_a_supported_protocol_version(bare_server):
    reply = rpc(bare_server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}}, "a1")
    assert reply["jsonrpc"] == "2.0" and reply["id"] == "a1"
    result = reply["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "ubuchhaltung", "version": "1.0.0"}
    assert result["instructions"]


def test_initialize_answers_an_unknown_version_with_the_newest(bare_server):
    reply = rpc(bare_server, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    assert mcp.SUPPORTED_PROTOCOL_VERSIONS[0] == "2025-11-25"


def test_tools_list_names_five_read_only_tools(bare_server):
    tools = rpc(bare_server, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["info", "sql", "db_check", "schema", "page"]
    for tool in tools:
        assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False,
                                       "idempotentHint": True, "openWorldHint": False}
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["title"] and tool["description"]
    assert json.dumps(tools)                                     # JSON-tauglich


def test_ping_is_answered_with_an_empty_result(bare_server):
    assert rpc(bare_server, "ping", request_id=7) == {"jsonrpc": "2.0", "id": 7, "result": {}}


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
    {"jsonrpc": "2.0", "method": "irgendwas/unbekanntes"},
])
def test_notifications_get_no_reply(bare_server, message):
    assert bare_server.handle(message) is None


def test_an_unknown_method_is_32601(bare_server):
    reply = rpc(bare_server, "resources/list")
    assert reply["error"]["code"] == -32601
    assert "resources/list" in reply["error"]["message"]


def test_an_unknown_tool_is_32602(bare_server):
    reply = rpc(bare_server, "tools/call", {"name": "loeschen", "arguments": {}})
    assert reply["error"]["code"] == -32602
    assert "loeschen" in reply["error"]["message"]


@pytest.mark.parametrize("params", [None, {}, {"name": 5}, "sql"])
def test_tools_call_without_a_name_is_32602(bare_server, params):
    assert rpc(bare_server, "tools/call", params)["error"]["code"] == -32602


@pytest.mark.parametrize("message", [
    pytest.param({"jsonrpc": "2.0", "id": 3}, id="ohne-method"),
    pytest.param({"jsonrpc": "2.0", "id": 3, "method": 5}, id="method-keine-Zeichenkette"),
    pytest.param("text", id="kein-Objekt"),
    pytest.param(5, id="Zahl"),
    pytest.param({"jsonrpc": "2.0", "id": [1], "method": "ping"}, id="ungueltige-id"),
])
def test_an_invalid_request_is_32600(bare_server, message):
    reply = bare_server.handle(message)
    assert reply["error"]["code"] == -32600


def test_a_tool_call_without_an_address_names_the_variable(bare_server):
    text, is_error = call(bare_server, "info")
    assert is_error
    assert "UBUCHHALTUNG_MCP_URL" in text


@pytest.mark.parametrize("name, arguments, message", [
    pytest.param("sql", {"query": "SELECT 1", "max_rows": 0}, "max_rows", id="max_rows-null"),
    pytest.param("sql", {"query": "SELECT 1", "max_rows": 2001}, "max_rows", id="max_rows-zu-gross"),
    pytest.param("sql", {"query": "SELECT 1", "max_rows": "5"}, "max_rows", id="max_rows-Text"),
    pytest.param("sql", {"query": "SELECT 1", "max_rows": True}, "max_rows", id="max_rows-Wahrheitswert"),
    pytest.param("sql", {"query": "SELECT 1", "unbekannt": 1}, "unbekannt", id="unbekannter-Schluessel"),
    pytest.param("sql", {}, "query", id="Pflichtargument-fehlt"),
    pytest.param("sql", {"query": 5}, "query", id="query-keine-Zeichenkette"),
    pytest.param("db_check", {"full": "ja"}, "full", id="full-Text"),
    pytest.param("schema", {"table": "a b"}, "table", id="Tabellenname-mit-Leerzeichen"),
    pytest.param("schema", {"table": "Invoices\n"}, "table", id="Tabellenname-mit-Zeilenumbruch"),
    pytest.param("page", {"path": "/about", "max_chars": 10}, "max_chars", id="max_chars-zu-klein"),
    pytest.param("info", {"x": 1}, "x", id="info-ohne-Argumente"),
])
def test_wrong_arguments_are_a_tool_error(bare_server, name, arguments, message):
    text, is_error = call(bare_server, name, **arguments)
    assert is_error
    assert message in text
    assert "UBUCHHALTUNG_MCP_URL" not in text                    # vor dem Verbinden geprüft


def test_arguments_may_be_missing_or_null(bare_server):
    reply = rpc(bare_server, "tools/call", {"name": "info", "arguments": None})
    assert reply["result"]["isError"] is True                    # weiter bis zur fehlenden Adresse
    assert "UBUCHHALTUNG_MCP_URL" in reply["result"]["content"][0]["text"]
    reply = rpc(bare_server, "tools/call", {"name": "info"})
    assert "UBUCHHALTUNG_MCP_URL" in reply["result"]["content"][0]["text"]


def test_a_tool_text_is_cut_at_fifty_thousand_characters():
    client = SimpleNamespace(run_select=lambda query, max_rows: (["x"], [["y" * 100]] * 1000, False))
    text, is_error = call(mcp.Server(client=client), "sql", query="SELECT 1")
    assert not is_error
    assert len(text) < 50_100
    assert text.endswith("Zeichen insgesamt)")


def test_an_unexpected_exception_becomes_a_tool_error():
    def explode(query, max_rows):
        raise KeyError("kaputt")
    text, is_error = call(mcp.Server(client=SimpleNamespace(run_select=explode)), "sql", query="SELECT 1")
    assert is_error
    assert text == "Interner Fehler: KeyError: 'kaputt'"


def test_handle_line_answers_garbage_with_32700(bare_server):
    reply = bare_server.handle_line("{kaputt")
    assert reply == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Ungültiges JSON"}}


def test_a_batch_is_answered_as_an_array_without_notifications(bare_server):
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    replies = bare_server.handle_line(json.dumps(batch))
    assert [reply["id"] for reply in replies] == [1, 2]
    assert bare_server.handle_line(json.dumps(batch[1:2])) is None          # nur Benachrichtigungen
    assert bare_server.handle_line("[]")["error"]["code"] == -32600


def test_serve_reads_lines_and_writes_one_json_line_each(bare_server):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "{kaputt",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "sql", "arguments": {"query": "SELECT 'ä€'; DROP TABLE x"}}}, ensure_ascii=False),
    ]
    out = io.BytesIO()
    bare_server.serve(io.BytesIO(("\n".join(lines) + "\n").encode("utf-8") + b"\xff\xfe\n"), out)
    replies = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    assert out.getvalue().endswith(b"\n")
    assert [reply["id"] for reply in replies] == [1, None, 2, None]
    assert replies[0]["result"] == {}
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["result"]["isError"] is True
    assert replies[3]["error"]["code"] == -32700                  # kein UTF-8


# ── Teil 4: Werkzeuge gegen einen echten Server ──────────────────────────────
@pytest.fixture
def login_mode(server_port):
    """Installation mit Anmeldung (keine automatische) und ein MCP-Server mit den Zugangsdaten
    des Administrators; ``client`` ist derselbe Client, mit dem der Server arbeitet."""
    installation.set_up(auto_login=False)
    client = mcp.client_from_env(admin_env(server_port))
    return SimpleNamespace(port=server_port, client=client, server=mcp.Server(client=client))


def test_a_loopback_instance_gets_the_credentials_over_http_without_any_exception(login_mode):
    assert login_mode.client.allow_http is False and login_mode.client.base_url.startswith("http://127.0.0.1:")
    text, is_error = call(login_mode.server, "info")
    assert not is_error
    assert login_mode.client.session_token                                    # angemeldet – ohne UBUCHHALTUNG_MCP_ALLOW_HTTP


def test_info_names_the_instance_the_login_and_the_version(login_mode):
    text, is_error = call(login_mode.server, "info")
    assert not is_error
    assert f"Instanz: http://127.0.0.1:{login_mode.port}\n" in text
    assert f"Angemeldet als: {ADMIN}\n" in text
    assert f"Version {version.APP_VERSION}" in text


def test_sql_returns_header_rows_and_a_count(login_mode):
    text, is_error = call(login_mode.server, "sql", query="SELECT COUNT(*) AS n FROM Contacts")
    assert not is_error
    lines = text.splitlines()
    assert lines[0] == '["n"]'
    assert re.fullmatch(r"\[\d+\]", lines[1])
    assert lines[-1] == "1 Zeile(n)"


def test_sql_returns_json_lines_with_types(login_mode):
    text, _ = call(login_mode.server, "sql",
                   query="SELECT 1 AS a, 'x' AS b, NULL AS c, 'ä€' AS d, 1.5 AS e, 9223372036854775807 AS f")
    assert text.splitlines()[:2] == ['["a","b","c","d","e","f"]', '[1,"x",null,"ä€",1.5,9223372036854775807]']


def test_sql_keeps_line_breaks_and_semicolons_in_values_exact(login_mode):
    wert = "Zeile1\nZeile2; mit Semikolon, \"Anführungszeichen\" und \\ Rückwärtsstrich"
    db = installation.open_db(ADMIN)
    db.insert_contact(contact_type="customer", entity_type="company", display_name=wert, company_name="Probefirma",
                      street="Musterweg 1", city="Musterstadt")
    text, is_error = call(login_mode.server, "sql",
                          query="SELECT DisplayName, NULL AS Leer, 7 AS Zahl, 2.5 AS Bruch FROM Contacts "
                                "WHERE DisplayName LIKE 'Zeile1%'")
    assert not is_error
    lines = text.splitlines()
    assert lines[0] == '["DisplayName","Leer","Zahl","Bruch"]'
    assert json.loads(lines[1]) == [wert, None, 7, 2.5]                      # eine Zeile, exakt zurück
    assert lines[2:] == ["", "1 Zeile(n)"]


def test_sql_keeps_rows_apart_even_when_values_look_like_rows(login_mode):
    text, _ = call(login_mode.server, "sql",
                   query="SELECT 'a' || char(10) || 'b' || char(59) || 'c' AS x, 'd' AS y "     # char(59) = ';'
                         "UNION ALL SELECT 'e', 'f'")
    lines = text.splitlines()
    assert [json.loads(line) for line in lines[:3]] == [["x", "y"], ["a\nb;c", "d"], ["e", "f"]]
    assert lines[-1] == "2 Zeile(n)"


def test_sql_names_duplicate_columns_the_way_sqlite_does(login_mode):
    text, is_error = call(login_mode.server, "sql",
                          query="SELECT c1.ID, c2.ID FROM Contacts c1, Contacts c2 LIMIT 1")
    assert not is_error
    assert text.splitlines()[0] == '["ID","ID:1"]'                           # gleich benannte Spalten: SQLite hängt :1 an
    text, _ = call(login_mode.server, "sql", query="SELECT 1 AS a, 2 AS A, 3 AS \"a b\", 4 AS \"q\"\"r\"")
    assert text.splitlines()[:2] == ['["a","A:1","a b","q\\"r"]', "[1,2,3,4]"]


def test_sql_refuses_a_column_name_with_a_line_break(login_mode):
    text, is_error = call(login_mode.server, "sql", query='SELECT 1 AS "Zeile1\nZeile2"')
    assert is_error
    assert text == "Spaltenname mit Zeilenumbruch – bitte mit AS benennen"


def test_sql_refuses_an_empty_column_name(login_mode):
    text, is_error = call(login_mode.server, "sql", query='SELECT 1 AS ""')
    assert is_error
    assert text == "Leerer Spaltenname – bitte mit AS benennen"


def test_sql_explains_how_to_read_blob_columns(login_mode):
    text, is_error = call(login_mode.server, "sql", query="SELECT x'00ff' AS Bild")
    assert is_error
    assert text.endswith("BLOB-Spalten mit hex(x) oder length(x) abfragen")
    text, is_error = call(login_mode.server, "sql", query="SELECT hex(x'00ff') AS Bild, length(x'00ff') AS Laenge")
    assert not is_error
    assert text.splitlines()[:2] == ['["Bild","Laenge"]', '["00FF",2]']


def test_sql_accepts_comments_pragma_functions_and_a_trailing_semicolon(login_mode):
    server = login_mode.server
    text, is_error = call(server, "sql", query="-- Fremdschlüssel\nSELECT * FROM pragma_foreign_key_check;")
    assert not is_error
    assert text.splitlines()[0] == '["table","rowid","parent","fkid"]'
    text, is_error = call(server, "sql", query="SELECT 5 AS n -- Kommentar am Ende")
    assert not is_error
    assert text.splitlines()[:2] == ['["n"]', "[5]"]


def test_every_read_pragma_function_runs_on_the_instance(login_mode):
    for name in sorted(mcp._READ_PRAGMAS):
        argument = "('Invoices')" if name in ("table_info", "table_xinfo", "index_list", "foreign_key_list") else (
            "('sqlite_autoindex_Invoices_1')" if name in ("index_info", "index_xinfo") else "")
        text, is_error = call(login_mode.server, "sql", query=f"SELECT * FROM pragma_{name}{argument}")
        assert not is_error, (name, text)


def test_sql_cuts_the_result_at_max_rows_and_says_so(login_mode):
    text, is_error = call(login_mode.server, "sql", query="SELECT name FROM sqlite_master", max_rows=2)
    assert not is_error
    lines = text.splitlines()
    assert lines[0] == '["name"]' and len(lines) == 2 + 3                   # Kopf, 2 Zeilen, Leerzeile, Fußzeile
    assert lines[-1] == "2 Zeilen gezeigt – weitere vorhanden (LIMIT/max_rows)"


def test_sql_with_exactly_max_rows_rows_is_complete(login_mode):
    text, _ = call(login_mode.server, "sql", query="SELECT 1 AS n UNION ALL SELECT 2", max_rows=2)
    assert text.splitlines() == ['["n"]', "[1]", "[2]", "", "2 Zeile(n)"]


def test_a_sql_error_of_the_instance_is_reported(login_mode):
    text, is_error = call(login_mode.server, "sql", query="SELECT * FROM GibtEsNicht")
    assert is_error
    assert "no such table" in text


def test_money_is_returned_as_integer_minor_units(login_mode):
    # Beträge stehen als Ganzzahl (SCALE 4), keine Kommazahl – so beschreibt es das Werkzeug
    db = installation.open_db(ADMIN)
    db.insert_article(name="Probeartikel", unit_price=119, tax_rate=19)
    text, _ = call(login_mode.server, "sql", query="SELECT Name, UnitPrice FROM Articles WHERE Name = 'Probeartikel'")
    assert json.loads(text.splitlines()[1]) == ["Probeartikel", 1190000]
    assert "SCALE 4" in next(tool for tool in mcp.TOOLS if tool["name"] == "sql")["description"]


def test_the_sql_description_states_the_output_format_and_has_no_line_break_workaround():
    description = next(tool for tool in mcp.TOOLS if tool["name"] == "sql")["description"]
    assert "JSON-Array" in description and "1190000 = 119,00 €" in description and "Minor Units" in description
    assert "char(10)" not in description and "hex(x)" in description


def test_db_check_reports_a_healthy_database(login_mode):
    text, is_error = call(login_mode.server, "db_check")
    assert not is_error
    assert "Integrität (quick_check): ok" in text
    assert "Fremdschlüssel: keine Verstöße" in text
    assert f"Schema-Version (user_version): {version.SCHEMA_VERSION}" in text
    assert "Journal-Modus: delete" in text
    assert re.search(r"^Tabellen: \d+ \(einschließlich interner sqlite_-Tabellen\)$", text, re.MULTILINE)


def test_db_check_full_runs_the_integrity_check(login_mode):
    text, _ = call(login_mode.server, "db_check", full=True)
    assert "Integrität (integrity_check): ok" in text


def test_db_check_names_foreign_key_violations(login_mode):
    db = installation.open_db(ADMIN)
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:          # Fremdschlüssel sind hier aus
        con.execute("INSERT INTO ContactTypeLinks (ContactID, TypeKey) VALUES (987654, 'customer')")
        con.commit()
    text, is_error = call(login_mode.server, "db_check")
    assert not is_error
    assert "Fremdschlüssel: 1 Verstöße (table | rowid | parent | fkid)" in text
    assert "\nContactTypeLinks | 1 | Contacts | 0" in text


def test_schema_lists_the_tables_with_their_row_counts(login_mode):
    text, is_error = call(login_mode.server, "schema")
    assert not is_error
    lines = text.splitlines()
    assert re.fullmatch(r"Tabellen \(\d+, ohne interne sqlite_-Tabellen\), Name: Zeilen", lines[0])
    assert "Invoices: 0" in lines
    assert not any(line.startswith("sqlite_") for line in lines)
    assert all(": " in line for line in lines[1:])


def test_schema_describes_one_table(login_mode):
    text, is_error = call(login_mode.server, "schema", table="Invoices")
    assert not is_error
    head, *sections = text.split("\n\n== ")
    assert head == "Tabelle Invoices"
    assert [section.split(" ==\n")[0] for section in sections] == [
        "CREATE-Anweisung", "Spalten", "Fremdschlüssel", "Indizes"]
    create, columns, _, indexes = (section.split(" ==\n", 1)[1] for section in sections)
    header, statement = create.splitlines()                                   # eine Zeile: Umbrüche geglättet
    assert header == "sql" and statement.startswith("CREATE TABLE")
    assert columns.splitlines()[0] == "cid | name | type | notnull | dflt_value | pk"
    assert any(line.split(" | ")[1:3] == ["InvoiceNumber", "TEXT"] for line in columns.splitlines()[1:])
    assert indexes.splitlines()[0] == "seq | name | unique | origin | partial"   # InvoiceNumber ist UNIQUE


def test_schema_rejects_an_unknown_table(login_mode):
    text, is_error = call(login_mode.server, "schema", table="GibtEsNicht")
    assert is_error
    assert "gibt es nicht" in text


def test_page_returns_the_page_as_text(login_mode):
    text, is_error = call(login_mode.server, "page", path="/about")
    assert not is_error
    assert text.splitlines()[0] == "Status 200"
    assert f"Version {version.APP_VERSION}" in text
    assert "<" not in text and "var " not in text


def test_page_reports_a_redirect_instead_of_following_it(login_mode):
    text, is_error = call(login_mode.server, "page", path="/")
    assert not is_error
    assert text == "Status 303\nWeiterleitung nach /setup"


def test_page_passes_the_status_of_a_refused_page_on(server_port):
    installation.set_up(auto_login=False, users=[("alice", False)])           # normale Benutzerin: /users ist 403
    server = mcp.Server(env=instance_env(server_port, UBUCHHALTUNG_MCP_USER="alice",
                                         UBUCHHALTUNG_MCP_PASSWORD=PASSWORD))
    text, is_error = call(server, "page", path="/users")
    assert not is_error
    assert text.splitlines()[0] == "Status 403"
    assert "Administratorrechte erforderlich" in text


def _fake_page_client(content_type, body, status=200):
    headers = email.message.Message()
    headers["Content-Type"] = content_type
    return SimpleNamespace(get=lambda path: (status, headers, body))


@pytest.mark.parametrize("content_type, body, expected", [
    pytest.param("text/plain; charset=utf-8", "Klartext ä".encode("utf-8"), "Klartext ä", id="text-plain"),
    pytest.param("application/json", b'{"exists": false}', '{"exists": false}', id="json"),
    pytest.param("application/xml", "<a>ä</a>".encode("utf-8"), "<a>ä</a>", id="xml"),
    pytest.param("text/xml", b"<a/>", "<a/>", id="text-xml"),
    pytest.param("text/csv", b"a;b\n1;2", "a;b\n1;2", id="csv"),
    pytest.param("text/html; charset=utf-8", b"<p>Hallo<br>Welt</p>", "Hallo\nWelt", id="html"),
    pytest.param("text/plain", b"kaputt \xff ende", "kaputt � ende", id="ungueltiges-UTF-8"),
])
def test_page_prints_text_content_types(content_type, body, expected):
    text, is_error = call(mcp.Server(client=_fake_page_client(content_type, body)), "page", path="/about")
    assert not is_error
    assert text == f"Status 200\n{expected}"


@pytest.mark.parametrize("content_type, body", [
    pytest.param("application/pdf", b"%PDF-1.7 \x00\xff", id="pdf"),
    pytest.param("text/css", b"p { color: red }", id="css"),
    pytest.param("image/png", b"\x89PNG\r\n", id="png"),
])
def test_page_does_not_print_other_content_types(content_type, body):
    text, is_error = call(mcp.Server(client=_fake_page_client(content_type, body)), "page", path="/about")
    assert not is_error
    assert text == f"Status 200\nBinärinhalt ({content_type}), {len(body)} Bytes – nicht ausgegeben"


def test_page_cuts_the_text_at_max_chars(login_mode):
    text, _ = call(login_mode.server, "page", path="/about", max_chars=500)
    assert len(text) < 600
    assert "… gekürzt (" in text.splitlines()[-1]


@pytest.mark.parametrize("path", ["/login", "/logout", "/LOGOUT/", "/logout?x=1", "/x/../logout"])
def test_page_refuses_login_and_logout_and_keeps_the_session(login_mode, path):
    call(login_mode.server, "info")
    token = login_mode.client.session_token
    text, is_error = call(login_mode.server, "page", path=path)
    assert is_error
    assert "Seite nicht freigegeben" in text
    assert auth.get_session_user(token) == ADMIN                  # nicht abgemeldet


def test_page_percent_encodes_non_ascii_characters(login_mode):
    text, is_error = call(login_mode.server, "page", path="/masterdata/contacts?type=Büro")
    assert not is_error
    assert text.splitlines()[0] == "Status 200"


# -- nur lesend: nichts wird geschrieben ---------------------------------------
FORBIDDEN_QUERIES = [
    "DELETE FROM Contacts",
    "SELECT 1; DELETE FROM Contacts",
    "UPDATE Contacts SET DisplayName = 'Manipuliert'",
    "INSERT INTO Contacts (DisplayName) VALUES ('Neu')",
    "WITH a AS (SELECT 1) DELETE FROM Contacts",
    "DROP TABLE Contacts",
    "PRAGMA user_version = 1",
    "ATTACH 'fremd.db' AS x",
    "VACUUM",
    "/* SELECT */ DELETE FROM Contacts",
    "SELECT * FROM pragma_optimize",
    "SELECT ID FROM Contacts) /*",
    "SELECT count(*) FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT x FROM c)",
    "SELECT length(zeroblob(1000000000))",
]


def test_forbidden_statements_are_refused_and_change_nothing(login_mode):
    db = installation.open_db(ADMIN)
    db.insert_contact(contact_type="customer", entity_type="company", display_name="Probefirma",
                      company_name="Probefirma", street="Musterweg 1", city="Musterstadt")
    text, is_error = call(login_mode.server, "sql", query="SELECT COUNT(*) AS n FROM Contacts")
    assert not is_error and text.splitlines()[1] == "[1]"
    before = installation.dump(db.db_name)

    for query in FORBIDDEN_QUERIES:
        assert call(login_mode.server, "sql", query=query)[1] is True, query

    assert installation.dump(db.db_name) == before
    text, _ = call(login_mode.server, "sql", query="SELECT COUNT(*) AS n FROM Contacts")
    assert text.splitlines()[1] == "[1]"


def _tables_named(db, name):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute("SELECT name FROM sqlite_master WHERE name = ?", (name,)).fetchall()


def test_pragma_optimize_is_refused_and_writes_no_statistics_table(login_mode, tmp_path):
    db = installation.open_db(ADMIN)
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:               # genug Zeilen mit Index zum Analysieren
        con.execute("CREATE TABLE Probe (A INTEGER, B TEXT)")
        con.execute("CREATE INDEX ix_probe_a ON Probe (A)")
        con.executemany("INSERT INTO Probe VALUES (?, ?)", [(i % 7, f"z{i}") for i in range(2000)])
        con.commit()
    # Kontrolle: Auf einer Kopie legt PRAGMA optimize die Tabelle wirklich an – der Test ist also nicht leer
    copy = tmp_path / "kopie.db"
    shutil.copy(db.db_name, copy)
    with contextlib.closing(sqlite3.connect(copy)) as con:
        con.execute("PRAGMA optimize")
        con.commit()
        assert con.execute("SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchall() == [("sqlite_stat1",)]

    for query in ("SELECT * FROM pragma_optimize", 'SELECT * FROM "pragma_optimize"',
                  "SELECT * FROM [PRAGMA_OPTIMIZE]", "SELECT 1 WHERE EXISTS(SELECT 1 FROM pragma_optimize)",
                  "SELECT * FROM Probe, pragma_optimize WHERE Probe.A = 3"):
        text, is_error = call(login_mode.server, "sql", query=query)
        assert is_error, query
        assert text == "PRAGMA-Funktion nicht freigegeben: pragma_optimize"
    assert _tables_named(db, "sqlite_stat1") == []
    assert login_mode.client.session_token is None                             # es ging nicht einmal eine Anfrage raus


@pytest.mark.parametrize("query, message", [
    pytest.param("SELECT ID FROM Contacts) /*", "Blockkommentare nur am Anfang erlaubt", id="offener-Kommentar-hebelt-LIMIT-aus"),
    pytest.param("SELECT 1 /* x */", "Blockkommentare nur am Anfang erlaubt", id="Kommentar-mitten-in-der-Abfrage"),
    pytest.param("SELECT count(*) FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT x FROM c)",
                 "'with' ist aus Lastgründen gesperrt", id="rekursive-Abfrage"),
    pytest.param("SELECT zeroblob(10)", "'zeroblob' ist aus Lastgründen gesperrt", id="zeroblob"),
    pytest.param("SELECT randomblob(10)", "'randomblob' ist aus Lastgründen gesperrt", id="randomblob"),
    pytest.param("SELECT 1 " + "x" * 20001, "Abfragen über 20000 Zeichen sind aus Lastgründen gesperrt",
                 id="zu-lange-Abfrage"),
])
def test_load_and_limit_tricks_are_refused_before_anything_is_sent(login_mode, query, message):
    text, is_error = call(login_mode.server, "sql", query=query)
    assert is_error
    assert message in text
    assert "kein Zeitlimit" in text or "Blockkommentare" in text
    assert login_mode.client.session_token is None


def test_the_row_limit_holds_for_a_large_result(login_mode):
    db = installation.open_db(ADMIN)
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute("CREATE TABLE Probe (A INTEGER)")
        con.executemany("INSERT INTO Probe VALUES (?)", [(i,) for i in range(500)])
        con.commit()
    text, is_error = call(login_mode.server, "sql", query="SELECT A FROM Probe ORDER BY A", max_rows=5)
    assert not is_error
    assert text.splitlines() == ['["A"]', "[0]", "[1]", "[2]", "[3]", "[4]", "",
                                 "5 Zeilen gezeigt – weitere vorhanden (LIMIT/max_rows)"]


def test_a_get_route_that_deletes_is_refused_and_the_quote_survives(login_mode):
    db = installation.open_db(ADMIN)
    quote_id = db.insert_invoice({
        "invoice_number": "26A001", "invoice_date": "2026-06-09",
        "seller_name": "S", "seller_company": "S GmbH", "buyer_name": "B", "buyer_company": "B AG",
        "tax_rate": 0.19, "sum_net": 100.0, "tax_amount": 19.0, "sum_gross": 119.0, "amount_due": 119.0,
        "status": "sent", "document_type": "quote"})
    assert [quote[1] for quote in db.fetch_quotes()] == ["26A001"]

    text, is_error = call(login_mode.server, "page", path=f"/quote/delete?id={quote_id}")

    assert is_error
    assert "Seite nicht freigegeben" in text
    assert login_mode.client.session_token is None                # nicht einmal eine Anfrage ging raus
    assert [quote[1] for quote in db.fetch_quotes()] == ["26A001"]


# Beleg, dass die Positivliste nur Seiten und Query-Schlüssel enthält, die nichts schreiben: Jede Seite
# wird auf einer befüllten Instanz aufgerufen – ohne Query und mit jedem ihrer erlaubten Schlüssel –,
# danach sind Datenbank und Ordner des Benutzers unverändert. Die Aufrufe entstehen aus _PAGE_ALLOWLIST
# selbst; ein neuer Eintrag oder Schlüssel braucht hier nur noch einen Beispielwert (sonst schlägt der
# Test fehl) und gehört damit unter den Nachweis.
REQUIRED_QUERY = {                        # Seiten, die ohne diese Schlüssel nichts Sinnvolles zeigen
    "/invoice/view": ("id",), "/invoice/edit": ("id",), "/quote/edit": ("id",),
    "/masterdata/articles/edit": ("id",), "/masterdata/contacts/edit": ("id",),
    "/masterdata/skr/edit": ("id",), "/masterdata/skr/copy": ("id",),
    "/masterdata/bankaccounts/edit": ("id",), "/masterdata/numberranges/edit": ("id",),
    "/receipts/edit": ("number",), "/transactions/edit": ("id",), "/assets/edit": ("id",),
    "/assets/view": ("id",), "/asset_categories/edit": ("id",), "/worktime/edit": ("id",),
    "/trips/edit": ("id",),
}
SAMPLE_VALUES = {
    "id": "1", "parent_id": "1", "person": "1", "acct": "1",
    "from": "2024-01-01", "to": "2024-12-31", "date_from": "2024-01-01", "date_to": "2024-12-31",
    "search": "R", "status": "paid", "type": "customer", "entity": "company",
    "error": "Fehlertext", "msg": "Hinweis", "info": "Hinweis", "err": "Fehler",
}
TOLERATED_SERVER_ERRORS = {"/invoice/reminders"}      # PageReminders scheitert mit den Seed-Daten in der App selbst
NOT_ROUTED_WITH_QUERY = {"/"}                         # do_GET kennt das Dashboard unter "/" nur ohne Query (sonst /dashboard)


def _page_calls(page, receipt_number):
    """Pfade mit Query für eine Seite: nur die nötigen Schlüssel, dann je ein weiterer erlaubter dazu."""
    values = {**SAMPLE_VALUES, "number": receipt_number}
    required = REQUIRED_QUERY.get(page, ())
    base = {key: values[key] for key in required}
    for extra in [None, *sorted(mcp._PAGE_ALLOWLIST[page] - set(required))]:
        query = urllib.parse.urlencode({**base, **({} if extra is None else {extra: values[extra]})})
        yield f"{page}?{query}" if query else page


def test_the_read_proof_covers_every_page_and_key_of_the_allowlist():
    assert set(REQUIRED_QUERY) <= set(mcp._PAGE_ALLOWLIST)
    for page, required in REQUIRED_QUERY.items():
        assert set(required) <= mcp._PAGE_ALLOWLIST[page], page
    keys = {key for allowed in mcp._PAGE_ALLOWLIST.values() for key in allowed}
    assert keys <= set(SAMPLE_VALUES) | {"number"}, keys - set(SAMPLE_VALUES)
    for page in mcp._PAGE_ALLOWLIST:
        calls = list(_page_calls(page, "B-1"))
        assert len(calls) == 1 + len(mcp._PAGE_ALLOWLIST[page] - set(REQUIRED_QUERY.get(page, ()))), page
        assert all(mcp.check_page_path(call_path) == call_path for call_path in calls), page


@pytest.fixture
def seeded_instance(login_mode):
    """Anmeldemodus mit den Test-Seed-Daten in der Datenbank des Administrators. Der erste Aufruf
    (Anmeldung, Öffnen der Datenbank) ist gemacht, bevor der Zustand gemerkt wird."""
    db = installation.open_db(ADMIN)
    db.load_test_seed_data()
    assert call(login_mode.server, "page", path="/about")[1] is False
    return SimpleNamespace(db=db, server=login_mode.server, folder=os.path.dirname(db.db_name))


def _state(instance):
    """Datenbankinhalt und alle Dateien im Ordner des Benutzers."""
    return installation.dump(instance.db.db_name), installation.tree(instance.folder)


def _status_of(text):
    return int(text.splitlines()[0].split()[1])


def test_every_listed_page_and_query_key_exists_and_writes_nothing(seeded_instance):
    with contextlib.closing(sqlite3.connect(seeded_instance.db.db_name)) as con:
        number = con.execute("SELECT Number FROM Documents WHERE Number IS NOT NULL LIMIT 1").fetchone()[0]
    paths = [path for page in sorted(mcp._PAGE_ALLOWLIST) for path in _page_calls(page, number)]
    assert len(paths) > 2 * len(mcp._PAGE_ALLOWLIST)                # mehr als je Seite ein Aufruf: auch die Schlüssel
    before = _state(seeded_instance)
    for path in paths:
        text, is_error = call(seeded_instance.server, "page", path=path, max_chars=500)
        assert not is_error, path
        status = _status_of(text)
        page = path.split("?")[0]
        if "?" in path and page in NOT_ROUTED_WITH_QUERY:
            assert status == 404, path                              # Erwartung dokumentiert; ändert die App das, hier anpassen
            continue
        assert status != 404, path                                  # die Route gibt es (kein Tippfehler im Eintrag)
        assert status != 500 or page in TOLERATED_SERVER_ERRORS, (path, text)
    assert _state(seeded_instance) == before


def test_a_page_query_that_could_write_is_refused_without_a_request(login_mode):
    for path, message in [("/transactions?from_invoice=1", "Query-Parameter nicht freigegeben: from_invoice"),
                          ("/invoice?copy_from=1", "Query-Parameter nicht freigegeben: copy_from"),
                          ("/invoice/xml_download?id=1", "Seite nicht freigegeben")]:
        text, is_error = call(login_mode.server, "page", path=path)
        assert is_error, path
        assert text.startswith(message), (path, text)
    assert login_mode.client.session_token is None                 # nichts wurde gesendet
    text, is_error = call(login_mode.server, "page", path="/transactions?from=2024-01-01&to=2024-12-31")
    assert not is_error and text.splitlines()[0] == "Status 200"


def test_the_state_comparison_notices_a_write(seeded_instance):
    # Kontrolle für die beiden Nachweise oben: Der Vergleich schlägt bei Datenbank und Ordner an
    before = _state(seeded_instance)
    seeded_instance.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    after_row = _state(seeded_instance)
    assert after_row[0] != before[0] and after_row[1] != before[1]
    with open(os.path.join(seeded_instance.folder, "neu.txt"), "w", encoding="utf-8") as handle:
        handle.write("x")
    assert _state(seeded_instance)[1] != after_row[1]


def test_the_tools_send_only_allowed_requests(server_port):
    installation.set_up(auto_login=False)

    class Recording(mcp.UBuchhaltungClient):
        def __init__(self, *args):
            super().__init__(*args)
            self.sent = []

        def _request(self, method, path, form=None, headers=None):
            self.sent.append((method, path, form))
            return super()._request(method, path, form, headers)

    client = Recording(f"http://127.0.0.1:{server_port}", ADMIN, PASSWORD)
    server = mcp.Server(client=client)
    for name, arguments in [("info", {}), ("sql", {"query": "SELECT 1"}), ("sql", {"query": "DELETE FROM Contacts"}),
                            ("db_check", {"full": True}), ("schema", {}), ("schema", {"table": "Invoices"}),
                            ("page", {"path": "/invoice?id=1"}), ("page", {"path": "/logout"}),
                            ("page", {"path": "/quote/delete?id=1"}),
                            ("page", {"path": "/invoice/pdf_generate?id=1"})]:
        call(server, name, **arguments)

    assert {method for method, _, _ in client.sent} == {"GET", "POST"}
    for method, path, form in client.sent:
        if method == "GET":
            assert mcp.check_page_path(path) == path, path                # Seite und Query-Schlüssel sind freigegeben
        else:
            assert path in ("/login", "/execute_sql")
            if path == "/execute_sql":
                sql = form["sql_commands"]
                assert list(form) == ["sql_commands"]
                assert sql.startswith(("SELECT * FROM (\n", "SELECT json_array(")) and ";" not in sql
            else:
                assert sorted(form) == ["password", "username"]
    assert [path for method, path, _ in client.sent if method == "POST" and path == "/login"] == ["/login"]


# -- Anmeldung -----------------------------------------------------------------
def test_a_wrong_password_is_reported_and_never_shown(server_port):
    installation.set_up(auto_login=False)
    wrong = "falsches-passwort-4711"
    server = mcp.Server(env=admin_env(server_port, wrong))
    for name, arguments in [("info", {}), ("sql", {"query": "SELECT 1"}), ("db_check", {}),
                            ("schema", {}), ("page", {"path": "/about"})]:
        text, is_error = call(server, name, **arguments)
        assert is_error, name
        assert "Anmeldung fehlgeschlagen" in text, name
        assert wrong not in text, name


def test_a_wrong_user_is_reported_like_a_wrong_password(server_port):
    installation.set_up(auto_login=False)
    server = mcp.Server(env=instance_env(server_port, UBUCHHALTUNG_MCP_USER="niemand",
                                         UBUCHHALTUNG_MCP_PASSWORD=PASSWORD))
    text, is_error = call(server, "info")
    assert is_error and "Anmeldung fehlgeschlagen" in text


def test_missing_credentials_point_to_the_setup(server_port):
    installation.set_up(auto_login=False)
    text, is_error = call(mcp.Server(env=instance_env(server_port)), "sql", query="SELECT 1")
    assert is_error
    assert "cmdkey /generic:uBuchhaltung-MCP" in text


@pytest.mark.parametrize("name, arguments, expected", [
    pytest.param("sql", {"query": "SELECT 2 AS n"}, '["n"]\n[2]', id="sql"),
    pytest.param("page", {"path": "/about"}, "Status 200", id="page"),
    pytest.param("info", {}, "Angemeldet als: admin", id="info"),
])
def test_an_expired_session_is_replaced_by_a_new_login(login_mode, name, arguments, expected):
    assert call(login_mode.server, "sql", query="SELECT 1 AS n")[1] is False
    old = login_mode.client.session_token
    assert auth.get_session_user(old) == ADMIN
    auth.delete_session(old)                                      # Sitzung serverseitig weg
    text, is_error = call(login_mode.server, name, **arguments)
    assert not is_error
    assert expected in text
    assert login_mode.client.session_token not in (None, old)


def test_a_stale_csrf_token_is_fetched_again(login_mode):
    call(login_mode.server, "sql", query="SELECT 1 AS n")
    good = login_mode.client.csrf_token
    login_mode.client.csrf_token = "veraltet"
    text, is_error = call(login_mode.server, "sql", query="SELECT 2 AS n")
    assert not is_error and text.splitlines()[:2] == ['["n"]', "[2]"]
    assert login_mode.client.csrf_token == good


def test_a_user_without_administrator_rights_gets_an_error(server_port):
    installation.set_up(auto_login=False, users=[("alice", False)])
    server = mcp.Server(env=instance_env(server_port, UBUCHHALTUNG_MCP_USER="alice",
                                         UBUCHHALTUNG_MCP_PASSWORD=PASSWORD))
    text, is_error = call(server, "sql", query="SELECT 1")
    assert is_error
    assert "Administratoren" in text


def test_automatic_login_needs_no_credentials(server_port, auto_login):
    server = mcp.Server(env=instance_env(server_port))
    text, is_error = call(server, "sql", query="SELECT COUNT(*) AS n FROM Contacts")
    assert not is_error
    assert text.splitlines()[0] == '["n"]'
    info, is_error = call(server, "info")
    assert not is_error
    assert "automatische Anmeldung" in info and f"Version {version.APP_VERSION}" in info
    assert installation.session_count() == 0                      # es entstand nie eine Sitzung


def test_an_unreachable_instance_is_reported():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]                             # danach geschlossen: nichts lauscht
    text, is_error = call(mcp.Server(env=instance_env(port, UBUCHHALTUNG_MCP_TIMEOUT="5")), "info")
    assert is_error
    assert text.startswith("Instanz nicht erreichbar:")


def test_an_instance_before_the_setup_is_reported(server_port):
    text, is_error = call(mcp.Server(env=instance_env(server_port)), "info")
    assert is_error
    assert "Status 303" in text and "/setup" in text


# ── Teil 5: echter stdio-Lauf ────────────────────────────────────────────────
def test_the_server_speaks_mcp_over_stdio(server_port, tmp_path):
    installation.set_up(auto_login=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith("UBUCHHALTUNG_MCP_")}
    env.update(admin_env(server_port))
    stderr_path = tmp_path / "mcp-stderr.log"
    with open(stderr_path, "wb") as stderr:
        proc = subprocess.Popen([sys.executable, TOOL_PATH], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, env=env)
        watchdog = threading.Timer(60, proc.kill)                 # ein hängender Lauf soll scheitern, nicht blockieren
        watchdog.start()
        try:
            def send(text):
                proc.stdin.write(text.encode("utf-8") + b"\n")
                proc.stdin.flush()

            def receive():
                line = proc.stdout.readline()
                assert line.endswith(b"\n"), "keine vollständige Antwortzeile"
                return json.loads(line.decode("utf-8"))

            send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "pytest", "version": "0"}}}))
            init = receive()
            assert init["id"] == 1 and init["result"]["protocolVersion"] == "2025-06-18"
            assert init["result"]["serverInfo"]["name"] == "ubuchhaltung"

            send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
            tools = receive()                                     # die Benachrichtigung bekam keine Antwort
            assert tools["id"] == 2
            assert [tool["name"] for tool in tools["result"]["tools"]] == ["info", "sql", "db_check", "schema", "page"]

            send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "sql", "arguments": {"query": "SELECT 'Grüße' AS gruss, 7 AS n"}}}))
            result = receive()
            assert result["id"] == 3 and result["result"]["isError"] is False
            assert result["result"]["content"][0]["text"].splitlines()[:2] == ['["gruss","n"]', '["Grüße",7]']

            send("{kaputt")
            assert receive()["error"]["code"] == -32700

            send(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "sql", "arguments": {"query": "DELETE FROM Contacts"}}}))
            refused = receive()
            assert refused["result"]["isError"] is True
        finally:
            watchdog.cancel()
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
    assert proc.returncode == 0
    assert PASSWORD not in stderr_path.read_text(encoding="utf-8", errors="replace")
