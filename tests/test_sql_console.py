# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole (Sonstiges → „SQL-Befehle ausführen"): auf die eigene Datenbank beschränkt.

Drei Teile:
  1. handle_execute_sql(): Der SQLite-Authorizer sperrt ATTACH, DETACH, VACUUM (auch
     VACUUM INTO) und alle PRAGMAs außer einer Positivliste (lesende ohne Argument,
     wenige mit Argument – auch als pragma_…-Tabellenfunktion), in beiden Modi: im
     Lesemodus (Standard) lehnt die Konsole sie wie alles Schreibende ab und läuft mit
     dem nächsten Befehl weiter, im Schreibmodus (``write=1``) beendet der erste
     abgelehnte Befehl die Anfrage, und alles ist zurückgenommen. Was nur im
     Schreibmodus läuft, ändert die eigene Datenbank (Modi: tests/test_sql_console_modes.py),
  2. auth.sql_console_allowed(): ohne eingerichtete Installation immer, danach nur
     Administratoren,
  3. HTTP gegen einen echten Server: POST /execute_sql und die Seite Sonstiges (mit
     automatischer Anmeldung, mit Sitzungen, vor der Ersteinrichtung), dazu der
     ursprüngliche Angriff – ein Administrator öffnet per ATTACH die Datenbank eines
     anderen Nutzers bzw. auth.db.

Benutzernamen, Passwörter und Kontakte sind erfundene Testwerte. Server-Fixture
(``server_port``) und der Betrieb mit automatischer Anmeldung (``auto_login``) kommen aus
conftest.py, die HTTP-Hilfen aus tests/http_client.py.
"""
import contextlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import auth
import userctx
from server import handlers
from tests import installation
from tests.http_client import get, post

#: Was die Konsole im Lesemodus (Standard) jedem Befehl antwortet, den sie ablehnt.
READONLY = "nicht erlaubt – die Konsole läuft nur lesend; zum Ändern „Schreiben erlauben“ ankreuzen"
#: Was sie im Schreibmodus ablehnt (die alte Meldung, seit 2.3.11 um die Transaktionsbefehle ergänzt).
CONSOLE_DENIED = ("nicht erlaubt – ATTACH, DETACH, VACUUM sowie verändernde oder unbekannte "
                  "PRAGMAs sind in der Konsole gesperrt, ebenso die Funktionen fts3_tokenizer "
                  "und load_extension; im Schreibmodus außerdem BEGIN, COMMIT, ROLLBACK, SAVEPOINT, "
                  "RELEASE und PRAGMA foreign_keys mit Wert (die Befehle laufen dort in einer "
                  "eigenen Transaktion)")
NOTHING_CHANGED = ("Nichts wurde geändert – im Schreibmodus laufen alle Befehle einer Anfrage in "
                   "einer Transaktion.")
ADMIN_ONLY = "Die SQL-Konsole steht nur Administratoren zur Verfügung."
NOTICE = "nur Administratoren zur Verfügung"                     # Hinweis statt der Konsole
EXPORT_HINT = "Ein kleinerer Export lässt sich im SQL-Konsolenbereich einfügen"
FOREIGN_SECRET = "Fremdgeheimnis-4711"
BOB_CONTACT = "Bobs Geheimkunde GmbH"
ADMIN_CONTACT = "Admins Stammkunde GmbH"


# ── Hilfen ───────────────────────────────────────────────────────────────────
# Die Sperren der Konsole gelten in beiden Modi; ihre Tests laufen mit diesem Zeichen für beide.
BOTH_MODES = pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])


def _run(db, sql, write=False):
    """handle_execute_sql wie im Server aufgerufen (Formularfeld = Liste); liefert das JSON.
    ``write`` schickt das Feld ``write=1`` (Schreibmodus); ohne läuft die Konsole nur lesend."""
    form = {"sql_commands": [sql]}
    if write:
        form["write"] = ["1"]
    return json.loads(handlers.handle_execute_sql(db, form))


def denied(write, number=1, total=1):
    """Die Antwort auf eine Anfrage, deren Befehl ``number`` die Konsole ablehnt und die sonst nichts meldet:
    im Lesemodus ein Fehler (die übrigen Befehle laufen weiter), im Schreibmodus der Abbruch mit Rollback."""
    if write:
        return {"success_count": 0, "total": total, "output": "", "rolled_back": True,
                "errors": [f"Befehl {number}: {CONSOLE_DENIED}", NOTHING_CHANGED]}
    return {"success_count": 0, "total": total, "output": "",
            "errors": [f"Befehl {number}: {READONLY}"]}


def _query(db, sql):
    """Zweite Verbindung: sieht nur, was die Konsole wirklich festgeschrieben hat."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql).fetchall()


def _plain_attach(host_db_path, target_path, query):
    """Was die Konsole ohne Authorizer könnte: eine fremde Datei als 'x' anhängen und lesen."""
    with contextlib.closing(sqlite3.connect(host_db_path)) as con:
        con.execute(f"ATTACH '{target_path}' AS x")
        return con.execute(query).fetchall()


@pytest.fixture
def foreign_db(tmp_path):
    """Fremde Datenbankdatei mit einem Geheimnis (Ziel des ATTACH-Angriffs)."""
    path = str(tmp_path / "fremd.db")
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE secret (v TEXT)")
        con.execute("INSERT INTO secret VALUES (?)", (FOREIGN_SECRET,))
        con.commit()
    return path


@pytest.fixture
def probe_db(tmp_db):
    """tmp_db mit einer kleinen Tabelle für die Schreibtests."""
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("CREATE TABLE Probe (A TEXT, B INTEGER)")
        con.execute("INSERT INTO Probe VALUES ('x', 1), ('y', 2)")
        con.commit()
    return tmp_db


# ── Teil 1: Handler und Authorizer ───────────────────────────────────────────
@pytest.mark.parametrize("template", [
    pytest.param("ATTACH '{other}' AS x", id="ATTACH"),
    pytest.param("-- Kommentar vorweg\nattach database '{other}' as x", id="attach-klein-mit-Kommentar"),
    pytest.param("/* Kommentar */ ATTACH '{other}' AS x", id="ATTACH-mit-Blockkommentar"),
    pytest.param("ATTACH ':memory:' AS m", id="ATTACH-memory"),
    pytest.param("DETACH DATABASE main", id="DETACH"),
    pytest.param("VACUUM", id="VACUUM"),
    pytest.param("VACUUM INTO '{target}'", id="VACUUM-INTO"),
    pytest.param("VACUUM main INTO '{target}'", id="VACUUM-main-INTO"),
    pytest.param("PRAGMA user_version=99", id="PRAGMA-user_version"),
    pytest.param("PRAGMA main.user_version = 99", id="PRAGMA-user_version-mit-Schema"),
    pytest.param("PRAGMA journal_mode=WAL", id="PRAGMA-journal_mode"),
    pytest.param("PRAGMA writable_schema=ON", id="PRAGMA-writable_schema"),
])
@BOTH_MODES
def test_console_denies_command(tmp_db, tmp_path, foreign_db, template, write):
    target = tmp_path / "kopie.db"
    result = _run(tmp_db, template.format(other=foreign_db, target=target), write)
    expected = denied(write)
    if write and template.startswith("VACUUM"):
        # In der Transaktion des Schreibmodus lehnt SQLite VACUUM schon selbst ab, noch vor dem Authorizer
        expected["errors"][0] = "Befehl 1: cannot VACUUM from within a transaction"
    assert result == expected
    assert not target.exists()


@BOTH_MODES
def test_denied_vacuum_into_creates_no_file(tmp_db, tmp_path, write):
    target = tmp_path / "kopie.db"
    _run(tmp_db, f"VACUUM INTO '{target}'", write)
    assert not target.exists()
    assert list(tmp_path.glob("kopie*")) == []


@BOTH_MODES
def test_denied_pragma_user_version_keeps_the_value(tmp_db, write):
    before = _query(tmp_db, "PRAGMA user_version")
    _run(tmp_db, "PRAGMA user_version=99", write)
    assert _query(tmp_db, "PRAGMA user_version") == before
    assert before != [(99,)]


@BOTH_MODES
def test_denied_pragma_journal_mode_keeps_delete(tmp_db, write):
    _run(tmp_db, "PRAGMA journal_mode=WAL", write)
    assert _query(tmp_db, "PRAGMA journal_mode") == [("delete",)]


@BOTH_MODES
def test_denied_pragma_writable_schema_keeps_the_schema_protected(probe_db, write):
    result = _run(probe_db, "PRAGMA writable_schema=ON; UPDATE sqlite_master SET name = name WHERE 0", write)
    assert result["success_count"] == 0
    if write:
        assert result == denied(True, total=2)             # Befehl 1 beendet die Anfrage, Befehl 2 läuft nie
    else:                                                  # beide Befehle sind abgelehnt, jeder für sich
        assert [e.split(":")[0] for e in result["errors"]] == ["Befehl 1", "Befehl 2"]
        assert result["errors"][0] == f"Befehl 1: {READONLY}"


@BOTH_MODES
def test_attach_then_select_leaks_nothing_from_the_foreign_file(tmp_db, foreign_db, write):
    # Kontrolle: ohne Authorizer wäre der Angriff gelungen
    assert _plain_attach(tmp_db.db_name, foreign_db, "SELECT v FROM x.secret") == [(FOREIGN_SECRET,)]

    result = _run(tmp_db, f"ATTACH '{foreign_db}' AS x; SELECT * FROM x.secret", write)
    assert result["success_count"] == 0
    assert result["errors"][0] == f"Befehl 1: {CONSOLE_DENIED if write else READONLY}"
    assert result["output"] == ""
    assert FOREIGN_SECRET not in json.dumps(result, ensure_ascii=False)


@BOTH_MODES
@pytest.mark.parametrize("sql", [
    pytest.param("SELECT COUNT(*) FROM ChartOfAccounts", id="SELECT"),
    pytest.param("PRAGMA table_info(Bookings)", id="PRAGMA-table_info"),
    pytest.param("PRAGMA user_version", id="PRAGMA-user_version-lesen"),
    pytest.param("PRAGMA integrity_check", id="PRAGMA-integrity_check"),
])
def test_console_allows_command(tmp_db, sql, write):
    result = _run(tmp_db, sql, write)
    assert result["errors"] == []
    assert result["success_count"] == 1
    assert result["total"] == 1


def test_foreign_keys_pragma_with_a_value_is_allowed_while_reading_only(tmp_db):
    # Im Schreibmodus ist es gesperrt (die Fremdschlüssel sind dort immer an): tests/test_sql_console_modes.py
    result = _run(tmp_db, "PRAGMA foreign_keys=ON")
    assert (result["errors"], result["success_count"], result["total"]) == ([], 1, 1)


@BOTH_MODES
def test_select_output_has_header_and_rows(tmp_db, write):
    expected = _query(tmp_db, "SELECT COUNT(*) FROM ChartOfAccounts")[0][0]
    result = _run(tmp_db, "SELECT COUNT(*) FROM ChartOfAccounts", write)
    assert result["output"] == f"COUNT(*)\n{expected}"


@pytest.mark.parametrize("sql, check, expected", [
    pytest.param("CREATE TABLE Neu (A TEXT)",
                 "SELECT name FROM sqlite_master WHERE name = 'Neu'", [("Neu",)], id="CREATE-TABLE"),
    pytest.param("INSERT INTO Probe VALUES ('z', 3)",
                 "SELECT A, B FROM Probe ORDER BY B", [("x", 1), ("y", 2), ("z", 3)], id="INSERT"),
    pytest.param("UPDATE Probe SET B = B + 10 WHERE A = 'x'",
                 "SELECT A, B FROM Probe ORDER BY B", [("y", 2), ("x", 11)], id="UPDATE"),
    pytest.param("DELETE FROM Probe WHERE A = 'y'",
                 "SELECT A, B FROM Probe", [("x", 1)], id="DELETE"),
    pytest.param("DROP TABLE Probe",
                 "SELECT name FROM sqlite_master WHERE name = 'Probe'", [], id="DROP-TABLE"),
])
def test_console_changes_the_own_database(probe_db, sql, check, expected):
    result = _run(probe_db, sql, write=True)
    assert (result["success_count"], result["errors"], result["rolled_back"]) == (1, [], False)
    assert _query(probe_db, check) == expected


def test_denied_command_does_not_stop_the_others_while_reading(probe_db):
    result = _run(probe_db, "SELECT A FROM Probe ORDER BY A; PRAGMA user_version=99; "
                            "SELECT COUNT(*) FROM Probe")
    assert result["success_count"] == 2
    assert result["total"] == 3
    assert result["errors"] == [f"Befehl 2: {READONLY}"]
    assert result["output"] == "A\nx\ny\nCOUNT(*)\n2"


def test_denied_command_ends_the_request_and_undoes_the_others_while_writing(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); PRAGMA user_version=99; "
                            "DELETE FROM Probe WHERE A = 'x'", write=True)
    assert result == denied(True, number=2, total=3)
    assert _query(probe_db, "SELECT A FROM Probe ORDER BY A") == [("x",), ("y",)]


# ── Teil 2: auth.sql_console_allowed() ───────────────────────────────────────
@pytest.fixture
def auth_db(tmp_path):
    """auth.db mit einem Administrator und einem normalen Nutzer (Dummy-Zugangsdaten)."""
    path = str(tmp_path / "konsole_auth.db")
    auth.create_user("admin", "geheim123", is_admin=True, db_path=path)
    auth.create_user("bob", "geheim123", db_path=path)
    return path


@pytest.mark.parametrize("user", [None, "admin", "bob"])
def test_console_is_unrestricted_before_setup(auth_db, user):
    # Werkzeuge und Tests ohne eingerichtete Installation: keine Einschränkung
    assert userctx.get_mode() is None
    userctx.set_user(user)
    assert auth.sql_console_allowed(auth_db) is True


@pytest.mark.parametrize("user, expected", [
    pytest.param("admin", True, id="Administrator"),
    pytest.param("bob", False, id="normaler-Nutzer"),
    pytest.param("niemand_bekannt", False, id="unbekannter-Nutzer"),
    pytest.param(None, False, id="niemand-angemeldet"),
])
def test_console_after_setup_only_for_administrators(auth_db, user, expected):
    userctx.set_mode("multi")
    userctx.set_user(user)
    assert auth.sql_console_allowed(auth_db) is expected


def test_console_permission_follows_the_admin_flag(auth_db):
    userctx.set_mode("multi")
    userctx.set_user("bob")
    assert auth.sql_console_allowed(auth_db) is False
    auth.set_admin("bob", True, db_path=auth_db)
    assert auth.sql_console_allowed(auth_db) is True
    auth.set_admin("bob", False, db_path=auth_db)
    assert auth.sql_console_allowed(auth_db) is False


# ── Teil 3: HTTP gegen einen echten Server ───────────────────────────────────
def _post_sql(port, sql, session=None, write=False):
    """POST /execute_sql; ``session`` (Sitzung oder ``auto_login``) liefert Cookie und Token.
    ``write`` schickt ``write=1`` (Schreibmodus); ohne läuft die Konsole nur lesend."""
    headers = {} if session is None else session.cookie
    form = {"sql_commands": sql, **({"write": "1"} if write else {})}
    return post(port, "/execute_sql", headers=headers, form=form,
                csrf=None if session is None else session.csrf)


@pytest.fixture
def console_users():
    """Installation mit Anmeldung, ein Administrator und ein normaler Nutzer. Jeder hat
    eine Sitzung (Cookie, CSRF-Token) und eine eigene Datenbank mit einem Kontakt."""
    installation.set_up(admin="admin", auto_login=False, users=[("bob", False)])
    users = {}
    for name, contact in (("admin", ADMIN_CONTACT), ("bob", BOB_CONTACT)):
        token = auth.create_session(name)
        db = installation.open_db(name)
        db.insert_contact(contact_type="customer", entity_type="company", display_name=contact,
                          company_name=contact, street="Musterweg 1", city="Musterstadt")
        users[name] = SimpleNamespace(db=db, cookie={"Cookie": f"session={token}"},
                                      csrf=auth.csrf_for(token))
    return SimpleNamespace(**users)


def test_non_admin_post_is_403_json_and_writes_nothing(server_port, console_users):
    bob = console_users.bob
    resp = _post_sql(server_port, "CREATE TABLE Hack (X TEXT); "
                                  "UPDATE Contacts SET DisplayName = 'Manipuliert'", bob, write=True)
    assert resp.status == 403
    assert resp.headers["Content-Type"].startswith("application/json")
    assert json.loads(resp.text) == {"success_count": 0, "total": 0, "output": "",
                                     "errors": [ADMIN_ONLY]}
    assert _query(bob.db, "SELECT name FROM sqlite_master WHERE name = 'Hack'") == []
    assert _query(bob.db, "SELECT DisplayName FROM Contacts") == [(BOB_CONTACT,)]


def test_non_admin_page_shows_the_notice_instead_of_the_console(server_port, console_users):
    resp = get(server_port, "/miscellaneous", headers=console_users.bob.cookie)
    assert resp.status == 200
    assert 'id="sql_input"' not in resp.text
    assert "executeSql" not in resp.text
    assert NOTICE in resp.text
    # Der Kasten DB-Export bleibt, nur der Satz zur Konsole entfällt
    assert "<h2>DB-Export</h2>" in resp.text
    assert "db-export.sql" in resp.text
    assert EXPORT_HINT not in resp.text


def test_admin_post_runs_in_the_own_database(server_port, console_users):
    admin = console_users.admin
    resp = _post_sql(server_port, "SELECT 1", admin)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert json.loads(resp.text) == {"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}

    own = json.loads(_post_sql(server_port, "SELECT DisplayName FROM Contacts", admin).text)
    assert own["output"] == f"DisplayName\n{ADMIN_CONTACT}"     # die eigene, nicht Bobs Datenbank

    resp = _post_sql(server_port, "CREATE TABLE Probe (X TEXT)", admin, write=True)
    assert resp.status == 200
    assert _query(admin.db, "SELECT name FROM sqlite_master WHERE name = 'Probe'") == [("Probe",)]
    assert _query(console_users.bob.db, "SELECT name FROM sqlite_master WHERE name = 'Probe'") == []


def test_admin_page_shows_the_console(server_port, console_users):
    resp = get(server_port, "/miscellaneous", headers=console_users.admin.cookie)
    assert resp.status == 200
    assert 'id="sql_input"' in resp.text
    assert "executeSql" in resp.text
    assert NOTICE not in resp.text
    assert "<h2>DB-Export</h2>" in resp.text
    assert EXPORT_HINT in resp.text


@pytest.mark.parametrize("target, schema, query, secrets", [
    pytest.param("bob_db", "a", "SELECT * FROM a.Contacts", [BOB_CONTACT],
                 id="Datenbank-des-anderen-Nutzers"),
    pytest.param("auth_db", "au", "SELECT * FROM au.Users", ["bob", "admin"], id="auth-db"),
])
@BOTH_MODES
def test_admin_cannot_attach_foreign_databases(server_port, console_users, target, schema,
                                               query, secrets, write):
    """Der ursprüngliche Angriff: per ATTACH die Datenbank eines anderen Nutzers bzw. auth.db
    öffnen und lesen."""
    admin = console_users.admin
    path = {"bob_db": console_users.bob.db.db_name, "auth_db": auth.AUTH_DB}[target]

    # Kontrolle: ohne Authorizer wäre der Angriff gelungen
    leaked = str(_plain_attach(admin.db.db_name, path, query.replace(f"{schema}.", "x.")))
    assert all(secret in leaked for secret in secrets)

    resp = _post_sql(server_port, f"ATTACH '{path}' AS {schema}; {query}", admin, write)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["success_count"] == 0
    assert body["errors"][0] == f"Befehl 1: {CONSOLE_DENIED if write else READONLY}"
    assert body["output"] == ""
    assert not any(secret in resp.text for secret in secrets)


@BOTH_MODES
def test_auto_login_console_works_but_attach_is_denied(server_port, auto_login, foreign_db, write):
    resp = _post_sql(server_port, "SELECT 1", auto_login, write)
    assert resp.status == 200
    expected = {"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}
    assert json.loads(resp.text) == {**expected, **({"rolled_back": False} if write else {})}

    resp = _post_sql(server_port, f"ATTACH '{foreign_db}' AS x; SELECT * FROM x.secret", auto_login, write)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["success_count"] == 0
    assert body["errors"][0] == f"Befehl 1: {CONSOLE_DENIED if write else READONLY}"
    assert body["output"] == ""
    assert FOREIGN_SECRET not in resp.text


def test_auto_login_console_runs_in_the_admins_own_database(server_port, auto_login):
    resp = _post_sql(server_port, "CREATE TABLE Probe (X TEXT)", auto_login, write=True)
    assert resp.status == 200
    assert json.loads(resp.text)["errors"] == []
    assert _query(auto_login.db, "SELECT name FROM sqlite_master WHERE name = 'Probe'") == [("Probe",)]


@pytest.mark.parametrize("token", [None, "falsch"], ids=["ohne-Token", "falsches-Token"])
def test_auto_login_console_without_valid_token_is_403_and_runs_nothing(server_port, auto_login,
                                                                         token):
    # Ohne Sitzung hängt allein das Token an der Anfrage – ohne es läuft kein SQL
    resp = post(server_port, "/execute_sql",
                form={"sql_commands": "CREATE TABLE Hack (X TEXT)", "write": "1"}, csrf=token)
    assert resp.status == 403
    assert "Sicherheits-Token" in resp.text
    assert _query(auto_login.db, "SELECT name FROM sqlite_master WHERE name = 'Hack'") == []


def test_auto_login_page_shows_the_console(server_port, auto_login):
    resp = get(server_port, "/miscellaneous")
    assert resp.status == 200
    assert 'id="sql_input"' in resp.text
    assert "executeSql" in resp.text
    assert NOTICE not in resp.text
    assert EXPORT_HINT in resp.text


def test_console_is_unreachable_before_setup(server_port):
    # Vor der Ersteinrichtung führt jede Seite zur Einrichtung – SQL läuft nirgends,
    # und es entsteht keine Datenbank
    resp = post(server_port, "/execute_sql", form={"sql_commands": "SELECT 1"})
    assert resp.status == 303
    assert resp.headers["Location"] == "/setup"
    assert get(server_port, "/miscellaneous").headers["Location"] == "/setup"
    assert not os.path.exists(userctx.DATA_ROOT)
