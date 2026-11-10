# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Sperre gegen Datenbanken aus einer neueren Programmversion.

Trägt ``PRAGMA user_version`` eine höhere Zahl als ``version.SCHEMA_VERSION``, wirft
``Database(...)`` ``db.SchemaTooNewError``, bevor irgendetwas geschrieben wird: Die Datei bleibt
byteweise unverändert, wird nicht zurückgestempelt und nicht als geöffnet vermerkt (jeder weitere
Aufruf wirft wieder). Im Server antwortet ``_open_db`` auf jede Anfrage, die diese Datenbank
braucht, mit der Seite „Datenbank ist neuer als das Programm“ (500, ohne Dateipfad).

Teil 1 prüft Database direkt, Teil 2 über einen echten Server mit automatischer Anmeldung bzw.
Sitzungen. Die Stände sind relativ zu ``version.SCHEMA_VERSION`` angegeben, damit die Tests eine
spätere Schemaänderung (SCHEMA_STEP + 1) überstehen.
"""
import contextlib
import os
import sqlite3
from types import SimpleNamespace

import pytest

import auth
import userctx
import version
from db import Database, SchemaTooNewError, _CoreMixin
from tests import installation
from tests.http_client import KeepAlive, get, post
from tests.installation import dump, file_digest, stamp_user_version, user_version

NEXT_STEP = version.SCHEMA_VERSION + 1
NEXT_MAJOR = version.SCHEMA_VERSION + 1000
VERY_HIGH = version.SCHEMA_VERSION + 123456
NEWER_STATES = [
    pytest.param(NEXT_STEP, id="naechster-Schemastand"),
    pytest.param(NEXT_MAJOR, id="naechstes-MAJOR"),
    pytest.param(VERY_HIGH, id="sehr-hoch"),
]
HEADING = "<h1>Datenbank ist neuer als das Programm</h1>"


def _snapshot(db_path):
    """Hash der Datei und Inhalt des Verzeichnisses (ein Journal oder eine Kopie fielen auf)."""
    return file_digest(db_path), sorted(os.listdir(os.path.dirname(db_path)))


# ── Teil 1: Database ─────────────────────────────────────────────────────────
def test_the_error_is_exported_by_the_db_package():
    assert issubclass(SchemaTooNewError, RuntimeError)


@pytest.mark.parametrize("number", NEWER_STATES)
def test_a_newer_database_is_refused_and_left_untouched(db_with_coa, number):
    path = db_with_coa.db_name
    stamp_user_version(path, number)
    before = _snapshot(path)
    with pytest.raises(SchemaTooNewError):
        Database(db_name=path)
    assert _snapshot(path) == before                       # byteweise unverändert, nichts daneben
    assert user_version(path) == number                    # nicht zurückgestempelt


@pytest.mark.parametrize("number", NEWER_STATES)
def test_the_message_names_both_states_readably(db_with_coa, number):
    path = db_with_coa.db_name
    stamp_user_version(path, number)
    with pytest.raises(SchemaTooNewError) as error:
        Database(db_name=path)
    text = str(error.value)
    assert version.describe_schema_version(number) in text            # z. B. "2.1"
    assert version.describe_schema_version(version.SCHEMA_VERSION) in text   # z. B. "2.0"
    assert "nicht geöffnet" in text and "nicht verändert" in text


def test_a_refused_database_is_refused_every_time(db_with_coa):
    path = db_with_coa.db_name
    stamp_user_version(path, NEXT_STEP)
    before = _snapshot(path)
    for _ in range(3):
        with pytest.raises(SchemaTooNewError):
            Database(db_name=path)
    assert _snapshot(path) == before


def test_a_refused_database_is_not_marked_as_opened(db_with_coa):
    path = db_with_coa.db_name
    stamp_user_version(path, NEXT_STEP)
    assert os.path.abspath(path) not in _CoreMixin._initialized_dbs
    with pytest.raises(SchemaTooNewError):
        Database(db_name=path)
    assert os.path.abspath(path) not in _CoreMixin._initialized_dbs


def test_nothing_is_written_into_an_empty_newer_database(tmp_path):
    path = str(tmp_path / "leer-und-neu.db")
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute(f"PRAGMA user_version = {NEXT_MAJOR}")
        con.commit()
    before = _snapshot(path)
    with pytest.raises(SchemaTooNewError):
        Database(db_name=path)
    assert _snapshot(path) == before
    assert [line for line in dump(path) if line not in ("BEGIN TRANSACTION;", "COMMIT;")] == []


def test_the_programs_own_state_is_not_too_new(db_with_coa):
    path = db_with_coa.db_name
    before = dump(path)
    stamp_user_version(path, version.SCHEMA_VERSION)
    Database(db_name=path)                                 # kein Fehler
    assert dump(path) == before


def test_a_database_below_the_programs_state_is_not_too_new(db_with_coa):
    stamp_user_version(db_with_coa.db_name, version.SCHEMA_VERSION - 1)
    Database(db_name=db_with_coa.db_name)
    assert user_version(db_with_coa.db_name) == version.SCHEMA_VERSION


def test_a_newer_database_is_refused_for_the_users_database_too():
    # Der Standardpfad (users/<Name>/buch.db) läuft über dieselbe Prüfung
    installation.set_up()
    path = installation.open_db("admin").db_name
    stamp_user_version(path, NEXT_STEP)
    with pytest.raises(SchemaTooNewError):
        installation.open_db("admin")


# ── Teil 2: Server ───────────────────────────────────────────────────────────
@pytest.fixture
def newer(auto_login):
    """Die Datenbank des automatisch angemeldeten Administrators stammt von einer neueren Version."""
    path = auto_login.db.db_name
    stamp_user_version(path, NEXT_STEP)
    return SimpleNamespace(path=path, before=_snapshot(path), csrf=auto_login.csrf)


@pytest.mark.parametrize("page", ["/about", "/", "/masterdata/articles", "/users", "/setup",
                                  "/miscellaneous"])
def test_a_get_gets_the_error_page(server_port, newer, page):
    resp = get(server_port, page)
    assert resp.status == 500
    assert HEADING in resp.text
    assert resp.headers["Content-Type"].startswith("text/html")


def test_a_post_gets_the_error_page(server_port, newer):
    resp = post(server_port, "/masterdata/articles/delete?id=1", csrf=newer.csrf)
    assert resp.status == 500
    assert HEADING in resp.text


def test_the_error_page_gives_no_file_path(server_port, newer):
    for resp in (get(server_port, "/about"),
                 post(server_port, "/masterdata/articles/delete?id=1", csrf=newer.csrf)):
        assert newer.path not in resp.text
        assert os.path.dirname(newer.path) not in resp.text
        assert "buch.db" not in resp.text
        assert "Traceback" not in resp.text


def test_the_error_page_says_what_to_do(server_port, newer):
    text = get(server_port, "/about").text
    assert "neueren Version" in text and "neuere Programmversion" in text
    assert "nicht geöffnet" in text and "nicht verändert" in text
    assert "Server-Log" in text


def test_the_database_stays_untouched_by_many_requests(server_port, newer):
    for page in ("/about", "/", "/masterdata/articles", "/users", "/setup"):
        assert get(server_port, page).status == 500
    assert post(server_port, "/masterdata/articles/delete?id=1", csrf=newer.csrf).status == 500
    assert _snapshot(newer.path) == newer.before
    assert user_version(newer.path) == NEXT_STEP


def test_the_server_log_names_the_file_and_both_states(server_port, newer, capsys):
    get(server_port, "/about")
    out = capsys.readouterr().out
    assert "FEHLER" in out
    assert newer.path in out                                         # im Log darf der Pfad stehen
    assert version.describe_schema_version(NEXT_STEP) in out
    assert version.describe_schema_version(version.SCHEMA_VERSION) in out


def test_the_stylesheet_is_still_served(server_port, newer):
    assert get(server_port, "/buch.css").status == 200


def test_the_login_redirects_still_work(server_port, newer):
    resp = get(server_port, "/login")
    assert resp.status == 303 and resp.headers["Location"] == "/"


def test_the_body_of_a_refused_post_is_drained(server_port, newer):
    # _open_db antwortet vor dem Lesen des Bodys: die nächste Anfrage derselben Verbindung muss ankommen
    with KeepAlive(server_port) as conn:
        first = conn.post("/masterdata/articles/delete?id=1", form={"fuellung": "x" * 300},
                          csrf=newer.csrf)
        assert first.status == 500 and HEADING in first.text
        second = conn.get("/about")
        assert second.status == 500 and HEADING in second.text


def test_the_page_returns_with_the_database_once_it_is_current_again(server_port, newer):
    assert get(server_port, "/about").status == 500
    # eine neuere Programmversion … oder der Betreiber stellt die Sicherung zurück: Stand wieder gültig
    with contextlib.closing(sqlite3.connect(newer.path)) as con:
        con.execute(f"PRAGMA user_version = {version.SCHEMA_VERSION}")
        con.commit()
    assert get(server_port, "/about").status == 200


def test_only_the_users_with_a_newer_database_are_affected(server_port):
    installation.set_up(auto_login=False, users=[("bob", False)])
    sessions = {name: {"Cookie": f"session={auth.create_session(name)}"} for name in ("admin", "bob")}
    for name in sessions:
        installation.open_db(name)
    stamp_user_version(os.path.join(userctx.DATA_ROOT, "users", "admin", "buch.db"),
                       NEXT_STEP)
    refused = get(server_port, "/about", headers=sessions["admin"])
    assert refused.status == 500 and HEADING in refused.text
    fine = get(server_port, "/about", headers=sessions["bob"])
    assert fine.status == 200 and "👤 bob" in fine.text


def test_the_login_page_works_while_a_users_database_is_newer(server_port):
    installation.set_up(auto_login=False)
    installation.open_db("admin")
    stamp_user_version(os.path.join(userctx.DATA_ROOT, "users", "admin", "buch.db"),
                       NEXT_STEP)
    assert get(server_port, "/login").status == 200
