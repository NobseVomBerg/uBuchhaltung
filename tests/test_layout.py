# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Umzug der früheren Einzelbenutzer-Ablage (layout.py): Daten direkt in data/ wandern beim Start
nach data/users/<Name>/, der Betrieb bleibt für den Nutzer derselbe (automatische Anmeldung).

Reine Funktionstests mit tmp_path (DATA_ROOT und auth.db sind je Test isoliert); zuletzt
ein Test über den echten Server und die Startreihenfolge in main.py bzw. run_server().
Datenbanken entstehen über ``Database(...)`` bzw. per sqlite3 mit den drei Pfadspalten
(Invoices.PDFPath/XMLPath, Documents.Path). Namen, Dateiinhalte und Kunden sind erfundene Werte.
"""
import contextlib
import os
import runpy
import shutil
import socket
import sqlite3
from types import SimpleNamespace

import pytest

import auth
import demo
import demo_seed
import document_parser
import layout
import server
import userctx
from db import Database
from server import app as server_app
from tests import installation
from tests.http_client import get
from tests.installation import PASSWORD, forget_config_cache, read_config, tree, write_config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = {"config.json", "auth.db", "users", "tests", "lost+found"}

# Dateien einer Installation aus Version 1.x (Pfad im Datenverzeichnis → Inhalt)
LEGACY_FILES = {
    "invoices/2026/R26001 Probekunde.pdf": b"%PDF-1.7 Rechnung R26001",
    "invoices/2026/R26001 Probekunde.xml": b"<Invoice>R26001</Invoice>",
    "Belege/beleg-1.pdf": b"%PDF-1.7 Beleg 1",
    "logos/logo.png": b"\x89PNG Probe",
    "quotes/2026/A26001 Probekunde.pdf": b"%PDF-1.7 Angebot A26001",
    "sql_operations.log": "Protokoll mit Umlaut ä\n".encode("utf-8"),
}
LEGACY_ENTRIES = ["Belege", "buch.db", "invoices", "logos", "quotes", "sql_operations.log"]  # sortiert


# ── Hilfen ───────────────────────────────────────────────────────────────────
def _put(root, rel, content):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _stored(*parts):
    """Pfad im Datenverzeichnis, so wie die App ihn in die Datenbank schreibt (relativ, wenn
    DATA_ROOT relativ ist: ./data/…)."""
    return os.path.join(userctx.DATA_ROOT, *parts)


def _new(*parts, user="admin"):
    return os.path.join(userctx.DATA_ROOT, "users", user, *parts)


def _insert_invoice(con, number, pdf, xml):
    con.execute(
        "INSERT INTO Invoices (InvoiceNumber, InvoiceDate, SellerName, SellerCompany, BuyerName, "
        "BuyerCompany, TaxRate, SumNet, TaxAmount, SumGross, AmountDue, PDFPath, XMLPath) "
        "VALUES (?, '2026-03-01', 'Muster', 'Muster GmbH', 'Probekunde', 'Probekunde GmbH', 19, "
        "1000000, 190000, 1190000, 1190000, ?, ?)", (number, pdf, xml))


def _insert_document(con, number, path):
    con.execute("INSERT INTO Documents (Number, Date, Filename, Path, Info) "
                "VALUES (?, '2026-01-15', 'beleg.pdf', ?, 'Probebeleg')", (number, path))


def make_legacy(demo_marker=False, extra_config=None):
    """Installation aus Version 1.x unter DATA_ROOT: config.json mit ``mode: single``,
    buch.db mit Rechnungen und Belegen, die zugehörigen Dateien, dazu Einträge, die nicht
    umziehen (tests/, lost+found/, users/bob/). Liefert die Pfade, die in der Datenbank stehen."""
    root = userctx.DATA_ROOT
    os.makedirs(root, exist_ok=True)
    write_config({"mode": "single", **(extra_config or {})})
    for rel, content in LEGACY_FILES.items():
        _put(root, rel, content)
    extern = _put(os.path.dirname(root), "extern/R26002.pdf", b"%PDF-1.7 liegt ausserhalb")
    Database(db_name=os.path.join(root, "buch.db"))
    stored = SimpleNamespace(
        pdf=_stored("invoices", "2026", "R26001 Probekunde.pdf"),
        xml=_stored("invoices", "2026", "R26001 Probekunde.xml"),
        beleg=_stored("Belege", "beleg-1.pdf"),
        extern=extern,
        missing_xml=_stored("invoices", "2026", "fehlt.xml"),
        missing_beleg=_stored("Belege", "fehlt.pdf"))
    with contextlib.closing(sqlite3.connect(os.path.join(root, "buch.db"))) as con:
        _insert_invoice(con, "R26001", stored.pdf, stored.xml)          # beides zieht um
        _insert_invoice(con, "R26002", extern, stored.missing_xml)      # außerhalb / Datei fehlt
        _insert_invoice(con, "R26003", None, "")                        # NULL / leer
        _insert_document(con, "B1", stored.beleg)                       # zieht um
        _insert_document(con, "B2", stored.missing_beleg)               # Datei fehlt
        _insert_document(con, "B3", None)
        con.commit()
    if demo_marker:
        _put(root, ".demo", b"uBuchhaltung Demo-Daten\n")
    # Einträge, die bleiben
    _put(root, "tests/tmp/lauf/probe.txt", b"Testartefakt")
    os.makedirs(os.path.join(root, "lost+found"), exist_ok=True)
    _put(root, "users/bob/notizen.txt", b"Notizen von bob")
    assert sorted(set(os.listdir(root)) - KEEP - {".demo"}) == LEGACY_ENTRIES
    return stored


def paths_in(db_path):
    """Gespeicherte Dateipfade: {Rechnungsnummer: (PDFPath, XMLPath)} und {Belegnummer: Path}."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        invoices = {n: (p, x) for n, p, x in con.execute(
            "SELECT InvoiceNumber, PDFPath, XMLPath FROM Invoices")}
        documents = {n: p for n, p in con.execute("SELECT Number, Path FROM Documents")}
    return invoices, documents


def paths_in_old(db_path):
    """Gespeicherte Pfade einer Datenbank mit dem alten Schema (nur Invoices)."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return con.execute("SELECT PDFPath, XMLPath FROM Invoices").fetchall()


@pytest.fixture(params=["absolute", "relative"])
def style(request, tmp_path, monkeypatch):
    """Wie die App die Pfade speichert: absolut, oder relativ zum Arbeitsverzeichnis
    (``./data/…``, so läuft die App normalerweise). Bei ``relative`` liegt DATA_ROOT als
    ``./data`` unter einem Arbeitsverzeichnis in tmp_path."""
    if request.param == "relative":
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(userctx, "DATA_ROOT", "./data")
        monkeypatch.setattr(auth, "AUTH_DB", "./data/auth.db")
    return request.param


@pytest.fixture
def legacy(style):
    return make_legacy()


@pytest.fixture
def logs():
    return []


def _migrate(logs):
    return layout.migrate(log=logs.append)


# ── needs_migration() ────────────────────────────────────────────────────────
def test_needs_migration_for_a_single_config():
    write_config({"mode": "single"})
    assert layout.needs_migration() is True


def test_needs_migration_for_a_single_config_without_any_other_file():
    write_config({"mode": "single", "extra": 1})
    assert os.listdir(userctx.DATA_ROOT) == ["config.json"]
    assert layout.needs_migration() is True


@pytest.mark.parametrize("config", [{"mode": "multi"}, {"mode": "multi", "auto_login": "admin"}],
                         ids=["Mehrbenutzer", "mit-automatischer-Anmeldung"])
def test_no_migration_for_a_multi_installation_even_with_legacy_leftovers(config):
    write_config(config)
    _put(userctx.DATA_ROOT, "buch.db", b"Altlast")
    _put(userctx.DATA_ROOT, "invoices/2026/x.pdf", b"%PDF")
    assert layout.needs_migration() is False


def test_needs_migration_for_an_old_install_without_config_but_with_a_database():
    _put(userctx.DATA_ROOT, "buch.db", b"alte Buchhaltung")
    assert not os.path.exists(userctx.config_path())
    assert layout.needs_migration() is True


@pytest.mark.parametrize("make", [
    pytest.param(lambda root: None, id="Datenverzeichnis-fehlt"),
    pytest.param(lambda root: os.makedirs(root), id="leer"),
    pytest.param(lambda root: (_put(root, "invoices/2026/x.pdf", b"%PDF"),
                               _put(root, "tests/tmp/x.txt", b"x"),
                               os.makedirs(os.path.join(root, "lost+found"))),
                 id="Dateien-aber-keine-Datenbank"),
])
def test_no_migration_without_config_and_without_database(make):
    make(userctx.DATA_ROOT)
    assert layout.needs_migration() is False


def test_needs_migration_does_not_create_anything():
    layout.needs_migration()
    assert not os.path.exists(userctx.DATA_ROOT)


def test_needs_migration_for_an_unknown_mode_depends_on_the_database():
    write_config({"mode": "foo"})
    assert layout.needs_migration() is False
    _put(userctx.DATA_ROOT, "buch.db", b"x")
    assert layout.needs_migration() is True


@pytest.mark.parametrize("config", [{"mode": "single", "migrating_to": "admin"},
                                    {"mode": "multi", "migrating_to": "admin"},
                                    {"migrating_to": "demo"}],
                         ids=["single", "multi", "ohne-Modus"])
def test_needs_migration_while_the_marker_is_set(config):
    write_config(config)
    assert layout.needs_migration() is True


def test_needs_migration_reads_the_file_not_the_mode_cache():
    userctx.set_mode("multi")                           # Cache: eingerichtet
    with open(userctx.config_path(), "w", encoding="utf-8") as f:
        f.write('{"mode": "single"}')                   # Datei von Hand zurückgesetzt, Cache nicht
    assert userctx.get_mode() == "multi"
    assert layout.needs_migration() is True


# ── migrate(): der Normalfall ────────────────────────────────────────────────
def test_migrate_does_nothing_without_legacy_data(logs):
    assert _migrate(logs) is None
    assert logs == []
    assert not os.path.exists(userctx.DATA_ROOT)


def test_migrate_returns_the_user_name(legacy, logs):
    assert _migrate(logs) == "admin"


def test_migrate_moves_everything_except_the_kept_entries(legacy, logs):
    before = tree(userctx.DATA_ROOT)
    _migrate(logs)
    assert set(os.listdir(userctx.DATA_ROOT)) == KEEP
    after = tree(userctx.DATA_ROOT)
    for path, digest in before.items():
        top = path.split(os.sep)[0]
        if top in KEEP:
            if top != "config.json":
                assert after[path] == digest, path        # tests/, lost+found/, users/bob/ unberührt
        elif path != "buch.db":                            # buch.db: Pfade werden umgeschrieben
            assert after[os.path.join("users", "admin", path)] == digest, path


def test_migrate_writes_the_result_config(legacy, logs):
    _migrate(logs)
    assert read_config() == {"mode": "multi", "auto_login": "admin"}


def test_migrate_keeps_foreign_keys_in_config(style, logs):
    make_legacy(extra_config={"extra": {"a": [1, 2]}, "zahl": 7})
    _migrate(logs)
    assert read_config() == {"mode": "multi", "auto_login": "admin", "extra": {"a": [1, 2]}, "zahl": 7}


def test_migrate_creates_the_administrator_and_switches_the_automatic_login_on(legacy, logs):
    assert userctx.get_mode() is None                  # davor gilt der Altbestand als nicht eingerichtet …
    _migrate(logs)                                     # … ein veralteter Cache darf das nicht festhalten
    assert auth.user_exists("admin") is True
    assert auth.is_admin("admin") is True
    assert auth.auto_login_user() == "admin"
    assert userctx.get_mode() == "multi"                # der Modus-Cache ist nicht veraltet
    assert userctx.get_auto_login() == "admin"


def test_the_administrator_password_is_unknown(legacy, logs):
    _migrate(logs)
    assert not any(auth.authenticate("admin", guess) for guess in ("", "admin", PASSWORD, "geheim"))


def test_migrate_logs_the_move_the_path_count_and_the_automatic_login(legacy, logs):
    _migrate(logs)
    text = "\n".join(logs)
    assert userctx.DATA_ROOT in text and os.path.join("users", "admin") in text
    assert "3 gespeicherte Dateipfade" in text
    assert "'admin' wird automatisch angemeldet" in text


def test_a_second_run_changes_nothing(legacy, logs):
    _migrate(logs)
    before = tree(userctx.DATA_ROOT)
    config_before = read_config()
    logs.clear()
    assert _migrate(logs) is None
    assert logs == []
    assert tree(userctx.DATA_ROOT) == before
    assert read_config() == config_before
    assert layout.needs_migration() is False


def test_an_empty_legacy_install_still_gets_the_user(logs):
    write_config({"mode": "single"})                    # nur die Konfiguration, keine Daten
    assert _migrate(logs) == "admin"
    assert os.listdir(_new()) == []
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    assert auth.auto_login_user() == "admin"


def test_an_old_install_without_config_is_migrated_too(logs):
    _put(userctx.DATA_ROOT, "buch.db", b"alte Buchhaltung")
    _put(userctx.DATA_ROOT, "invoices/2026/x.pdf", b"%PDF")
    assert _migrate(logs) == "admin"
    assert sorted(os.listdir(_new())) == ["buch.db", "invoices"]
    assert read_config() == {"mode": "multi", "auto_login": "admin"}


def test_the_users_and_tests_directories_are_not_moved_into_the_user(legacy, logs):
    _migrate(logs)
    assert not os.path.exists(_new("users")) and not os.path.exists(_new("tests"))
    assert not os.path.exists(_new("lost+found")) and not os.path.exists(_new("config.json"))


# ── migrate(): gespeicherte Dateipfade ───────────────────────────────────────
def test_stored_paths_follow_the_moved_files(legacy, logs):
    _migrate(logs)
    invoices, documents = paths_in(_new("buch.db"))
    assert invoices["R26001"] == (_new("invoices", "2026", "R26001 Probekunde.pdf"),
                                  _new("invoices", "2026", "R26001 Probekunde.xml"))
    assert documents["B1"] == _new("Belege", "beleg-1.pdf")
    assert os.path.isfile(invoices["R26001"][0]) and os.path.isfile(invoices["R26001"][1])
    assert os.path.isfile(documents["B1"])


def test_a_path_outside_the_data_folder_stays(legacy, logs):
    _migrate(logs)
    invoices, _ = paths_in(_new("buch.db"))
    assert invoices["R26002"][0] == legacy.extern
    assert os.path.isfile(legacy.extern)


def test_a_path_outside_never_points_into_another_users_directory(style, logs):
    # Außerhalb des Datenverzeichnisses liegt ./extern/R26009.pdf – und zufällig gibt es einen
    # Benutzer "extern" mit einer gleichnamigen Datei. Der gespeicherte Pfad darf nicht dorthin wandern.
    make_legacy()
    outside = _put(os.path.dirname(userctx.DATA_ROOT), "extern/R26009.pdf", b"%PDF-1.7 ausserhalb")
    _put(userctx.DATA_ROOT, "users/extern/R26009.pdf", b"%PDF-1.7 Datei eines anderen Benutzers")
    with contextlib.closing(sqlite3.connect(os.path.join(userctx.DATA_ROOT, "buch.db"))) as con:
        _insert_invoice(con, "R26009", outside, None)
        con.commit()
    _migrate(logs)
    invoices, _ = paths_in(_new("buch.db"))
    assert invoices["R26009"][0] == outside


def test_a_path_whose_file_is_missing_stays(legacy, logs):
    _migrate(logs)
    invoices, documents = paths_in(_new("buch.db"))
    assert invoices["R26002"][1] == legacy.missing_xml
    assert documents["B2"] == legacy.missing_beleg


def test_empty_and_null_values_stay(legacy, logs):
    _migrate(logs)
    invoices, documents = paths_in(_new("buch.db"))
    assert invoices["R26003"] == (None, "")
    assert documents["B3"] is None


def test_only_the_path_columns_change(legacy, logs):
    def rows(db_path):
        with contextlib.closing(sqlite3.connect(db_path)) as con:
            return (con.execute("SELECT ID, InvoiceNumber, InvoiceDate, BuyerCompany, SumGross FROM Invoices "
                                "ORDER BY ID").fetchall(),
                    con.execute("SELECT ID, Number, Date, Filename, Info FROM Documents ORDER BY ID").fetchall())
    before = rows(os.path.join(userctx.DATA_ROOT, "buch.db"))
    _migrate(logs)
    assert rows(_new("buch.db")) == before


def test_a_second_run_does_not_rewrite_the_paths_again(legacy, logs):
    _migrate(logs)
    first = paths_in(_new("buch.db"))
    layout._rewrite_paths(_new("buch.db"), userctx.DATA_ROOT, _new())     # wie nach einem Abbruch
    assert paths_in(_new("buch.db")) == first
    assert layout._rewrite_paths(_new("buch.db"), userctx.DATA_ROOT, _new()) == 0


def _bare_database(path, *statements):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        for sql in statements:
            con.execute(sql)
        con.commit()


def test_an_old_schema_without_the_xml_column_is_migrated(style, logs):
    write_config({"mode": "single"})
    _put(userctx.DATA_ROOT, "invoices/2026/R1.pdf", b"%PDF")
    _bare_database(os.path.join(userctx.DATA_ROOT, "buch.db"),
                   "CREATE TABLE Invoices (ID INTEGER PRIMARY KEY, PDFPath TEXT)",
                   f"INSERT INTO Invoices (PDFPath) VALUES ('{_stored('invoices', '2026', 'R1.pdf')}')")
    assert _migrate(logs) == "admin"
    with contextlib.closing(sqlite3.connect(_new("buch.db"))) as con:
        assert con.execute("SELECT PDFPath FROM Invoices").fetchall() == [
            (_new("invoices", "2026", "R1.pdf"),)]                    # PDFPath zieht trotzdem um


def test_an_old_schema_without_the_documents_table_is_migrated(style, logs):
    write_config({"mode": "single"})
    _put(userctx.DATA_ROOT, "invoices/2026/R1.pdf", b"%PDF")
    _bare_database(os.path.join(userctx.DATA_ROOT, "buch.db"),
                   "CREATE TABLE Invoices (ID INTEGER PRIMARY KEY, PDFPath TEXT, XMLPath TEXT)",
                   f"INSERT INTO Invoices (PDFPath, XMLPath) VALUES ('{_stored('invoices', '2026', 'R1.pdf')}', NULL)")
    assert _migrate(logs) == "admin"
    assert paths_in_old(_new("buch.db")) == [(_new("invoices", "2026", "R1.pdf"), None)]


def test_a_database_without_any_of_the_tables_is_migrated(style, logs):
    write_config({"mode": "single"})
    _bare_database(os.path.join(userctx.DATA_ROOT, "buch.db"), "CREATE TABLE Etwas (A TEXT)")
    assert _migrate(logs) == "admin"
    assert "0 gespeicherte Dateipfade" in "\n".join(logs)


def test_a_file_that_is_no_database_is_moved_as_it_is(style, logs):
    write_config({"mode": "single"})
    _put(userctx.DATA_ROOT, "buch.db", b"das ist keine SQLite-Datei" * 10)
    assert _migrate(logs) == "admin"
    with open(_new("buch.db"), "rb") as f:
        assert f.read() == b"das ist keine SQLite-Datei" * 10


def test_a_legacy_install_without_a_database_is_migrated(style, logs):
    write_config({"mode": "single"})
    _put(userctx.DATA_ROOT, "invoices/2026/R1.pdf", b"%PDF")
    assert _migrate(logs) == "admin"
    assert "0 gespeicherte Dateipfade" in "\n".join(logs)
    assert os.path.isfile(_new("invoices", "2026", "R1.pdf"))


# ── migrate(): früheres Demo-Verzeichnis ─────────────────────────────────────
def test_a_former_demo_directory_becomes_the_user_demo(style, logs):
    make_legacy(demo_marker=True)
    assert _migrate(logs) == "demo"
    assert os.path.isfile(_new(".demo", user="demo"))               # der Marker zieht mit
    assert os.path.isfile(_new("buch.db", user="demo"))
    assert set(os.listdir(userctx.DATA_ROOT)) == KEEP


def test_a_former_demo_user_is_no_administrator_and_gets_no_automatic_login(style, logs):
    make_legacy(demo_marker=True)
    _migrate(logs)
    assert auth.user_exists("demo") is True
    assert auth.is_admin("demo") is False
    assert auth.count_admins() == 0
    assert read_config() == {"mode": "multi"}
    assert auth.auto_login_user() is None
    assert "automatisch angemeldet" not in "\n".join(logs)


def test_a_former_demo_directory_is_the_demo_again_with_the_variable(style, logs, monkeypatch):
    make_legacy(demo_marker=True)
    _migrate(logs)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.is_active() is True and demo.active_user() == "demo"
    monkeypatch.delenv(demo.ENV_VAR)
    assert demo.is_active() is False


def test_the_paths_of_a_former_demo_directory_follow_too(style, logs):
    make_legacy(demo_marker=True)
    _migrate(logs)
    invoices, documents = paths_in(_new("buch.db", user="demo"))
    assert invoices["R26001"][0] == _new("invoices", "2026", "R26001 Probekunde.pdf", user="demo")
    assert documents["B1"] == _new("Belege", "beleg-1.pdf", user="demo")


# ── migrate(): Namen ─────────────────────────────────────────────────────────
def test_a_taken_user_name_gives_admin2(style, logs):
    make_legacy()
    auth.create_user("admin", PASSWORD, is_admin=True)             # z. B. Rest einer früheren Installation
    assert _migrate(logs) == "admin2"
    assert os.path.isfile(_new("buch.db", user="admin2"))
    assert read_config()["auto_login"] == "admin2"
    assert auth.auto_login_user() == "admin2"
    assert auth.authenticate("admin", PASSWORD) is True            # der vorhandene Benutzer bleibt


def test_a_taken_directory_gives_admin2_and_stays_untouched(style, logs):
    make_legacy()
    _put(userctx.DATA_ROOT, "users/admin/eigene-datei.txt", b"gehoert niemandem der Migration")
    taken = tree(_new())
    assert _migrate(logs) == "admin2"
    assert tree(_new()) == taken
    assert auth.user_exists("admin") is False
    assert auth.is_admin("admin2") is True


def test_taken_admin_and_admin2_give_admin3(style, logs):
    make_legacy()
    auth.create_user("admin", PASSWORD)
    _put(userctx.DATA_ROOT, "users/admin2/x.txt", b"x")
    assert _migrate(logs) == "admin3"


def test_a_taken_demo_name_gives_demo2(style, logs):
    make_legacy(demo_marker=True)
    auth.create_user("demo", PASSWORD, allow_reserved=True)
    assert _migrate(logs) == "demo2"
    assert auth.is_admin("demo2") is False
    assert "auto_login" not in read_config()


# ── migrate(): Abbruch und Wiederaufnahme ────────────────────────────────────
def _end_state():
    """Endlage: Verzeichnisbaum mit Datei-Hashes (ohne auth.db: Salt und Zeitstempel), die
    config.json und die Benutzer."""
    state = tree(userctx.DATA_ROOT)
    state.pop("auth.db", None)
    return state, read_config(), [(name, is_admin) for name, is_admin, _ in auth.list_users()]


def _restore(template):
    shutil.rmtree(userctx.DATA_ROOT)
    shutil.copytree(template, userctx.DATA_ROOT)
    forget_config_cache()


def _failing_replace(monkeypatch_context, fail_at):
    """os.replace innerhalb des Datenverzeichnisses beim ``fail_at``-ten Aufruf scheitern lassen."""
    real_replace = os.replace
    seen = []

    def replace(src, dst, *args, **kwargs):
        if os.path.abspath(str(src)).startswith(os.path.abspath(userctx.DATA_ROOT)):
            seen.append(src)
            if len(seen) == fail_at:
                raise OSError(28, "Auf dem Gerät ist kein Speicherplatz mehr verfügbar")
        return real_replace(src, dst, *args, **kwargs)
    monkeypatch_context.setattr(os, "replace", replace)
    return seen


@pytest.mark.parametrize("fail_at", range(1, len(LEGACY_ENTRIES) + 1))
def test_an_interrupted_migration_resumes_to_the_same_end_state(tmp_path, monkeypatch, logs, fail_at):
    make_legacy(extra_config={"extra": "bleibt"})
    template = tmp_path / "ausgangslage"
    shutil.copytree(userctx.DATA_ROOT, template)

    assert _migrate(logs) == "admin"                              # ungestörter Lauf als Vorbild
    expected = _end_state()

    _restore(template)
    with monkeypatch.context() as patched:
        seen = _failing_replace(patched, fail_at)
        with pytest.raises(OSError):
            layout.migrate(log=lambda m: None)
    assert len(seen) == fail_at

    # Zwischenstand: Marke steht, der Betrieb gilt noch nicht als eingerichtet
    assert read_config()["migrating_to"] == "admin"
    assert read_config()["mode"] == "single"
    forget_config_cache()
    assert userctx.get_mode() is None
    assert layout.needs_migration() is True
    assert auth.user_exists("admin") is False
    moved = [e for e in LEGACY_ENTRIES if os.path.exists(_new(e))]
    assert moved == LEGACY_ENTRIES[:fail_at - 1]                   # genau die davor sind umgezogen
    assert [e for e in LEGACY_ENTRIES if os.path.exists(os.path.join(userctx.DATA_ROOT, e))] \
        == LEGACY_ENTRIES[fail_at - 1:]

    assert _migrate(logs) == "admin"                              # Wiederaufnahme
    assert _end_state() == expected
    assert layout.needs_migration() is False
    assert userctx.get_mode() == "multi"


def test_an_interruption_after_the_moves_resumes_too(tmp_path, monkeypatch, logs):
    make_legacy()
    template = tmp_path / "ausgangslage"
    shutil.copytree(userctx.DATA_ROOT, template)
    _migrate(logs)
    expected = _end_state()
    _restore(template)

    def boom(*args, **kwargs):
        raise RuntimeError("auth.db gesperrt")
    with monkeypatch.context() as patched:
        patched.setattr(auth, "create_user", boom)
        with pytest.raises(RuntimeError):
            layout.migrate(log=lambda m: None)
    assert set(os.listdir(userctx.DATA_ROOT)) == KEEP              # alles ist schon umgezogen
    assert read_config()["migrating_to"] == "admin"
    assert layout.needs_migration() is True

    assert _migrate(logs) == "admin"
    assert _end_state() == expected


def test_a_resumed_migration_keeps_the_name_from_the_marker(style, monkeypatch, logs):
    make_legacy()
    with monkeypatch.context() as patched:
        _failing_replace(patched, 2)
        with pytest.raises(OSError):
            layout.migrate(log=lambda m: None)
    auth.create_user("admin", PASSWORD, is_admin=True)             # Name ist jetzt vergeben …
    assert _migrate(logs) == "admin"                              # … aber die Marke gewinnt
    assert os.path.isfile(_new("buch.db"))
    assert not os.path.exists(_new("buch.db", user="admin2"))


def test_a_resumed_migration_of_a_former_demo_stays_a_demo(style, monkeypatch, logs):
    make_legacy(demo_marker=True)
    with monkeypatch.context() as patched:
        _failing_replace(patched, 3)
        with pytest.raises(OSError):
            layout.migrate(log=lambda m: None)
    assert _migrate(logs) == "demo"
    assert auth.is_admin("demo") is False
    assert "auto_login" not in read_config()


# ── migrate(): nichts wird überschrieben ─────────────────────────────────────
def _interrupt_after(count, monkeypatch):
    with monkeypatch.context() as patched:
        _failing_replace(patched, count + 1)
        with pytest.raises(OSError):
            layout.migrate(log=lambda m: None)


@pytest.mark.parametrize("conflict", LEGACY_ENTRIES)
def test_an_existing_target_aborts_without_overwriting_anything(style, monkeypatch, logs, conflict):
    make_legacy()
    index = LEGACY_ENTRIES.index(conflict)
    _interrupt_after(index, monkeypatch)                           # die Einträge davor sind umgezogen
    # Im Ziel liegt unter genau diesem Namen schon etwas anderes (z. B. von Hand angelegt) …
    target_entry = _new(conflict)
    if os.path.isdir(os.path.join(userctx.DATA_ROOT, conflict)):
        _put(_new(), conflict + "/fremd.txt", b"schon vorhanden im Ziel")
    else:
        _put(_new(), conflict, b"schon vorhanden im Ziel")
    # … und im Datenverzeichnis liegt der Eintrag ja noch
    everything_before = tree(userctx.DATA_ROOT)

    with pytest.raises(RuntimeError) as error:
        layout.migrate(log=logs.append)

    assert target_entry in str(error.value) and "nichts überschrieben" in str(error.value)
    assert tree(userctx.DATA_ROOT) == everything_before            # weder Quelle noch Ziel verändert
    assert read_config()["migrating_to"] == "admin"                # die Marke bleibt
    assert read_config()["mode"] == "single"
    assert auth.user_exists("admin") is False
    forget_config_cache()
    assert userctx.get_mode() is None
    assert layout.needs_migration() is True


def test_the_conflict_can_be_resolved_by_hand_and_the_migration_resumes(style, monkeypatch, logs):
    make_legacy()
    _interrupt_after(1, monkeypatch)                               # Belege ist umgezogen
    os.makedirs(os.path.join(userctx.DATA_ROOT, "Belege"))         # Version 1.x legt ihn wieder an
    _put(os.path.join(userctx.DATA_ROOT, "Belege"), "neu.pdf", b"%PDF-1.7 neuer Beleg")
    with pytest.raises(RuntimeError):
        layout.migrate(log=lambda m: None)
    os.rename(os.path.join(userctx.DATA_ROOT, "Belege"), os.path.join(userctx.DATA_ROOT, "Belege-alt"))
    assert _migrate(logs) == "admin"
    assert os.path.isfile(_new("Belege", "beleg-1.pdf"))           # unberührt
    assert os.path.isfile(_new("Belege-alt", "neu.pdf"))
    assert set(os.listdir(userctx.DATA_ROOT)) == KEEP


def test_a_stale_marker_with_an_existing_target_overwrites_nothing(style, logs):
    # Marke von Hand gesetzt, im Ziel liegt schon eine Datenbank: nicht überschreiben
    make_legacy()
    write_config({**read_config(), "migrating_to": "admin"})
    _put(_new(), "buch.db", b"andere Buchhaltung im Ziel")
    source_db = tree(userctx.DATA_ROOT)["buch.db"]
    with pytest.raises(RuntimeError, match="buch.db"):
        layout.migrate(log=logs.append)
    assert tree(userctx.DATA_ROOT)["buch.db"] == source_db         # Quelle unverändert
    with open(_new("buch.db"), "rb") as f:
        assert f.read() == b"andere Buchhaltung im Ziel"            # Ziel unverändert
    for entry in ("invoices", "logos", "quotes", "sql_operations.log"):
        assert os.path.exists(os.path.join(userctx.DATA_ROOT, entry))   # ab dem Konflikt liegt alles noch


# ── Start: main.py und run_server() ──────────────────────────────────────────
class _Stop(Exception):
    pass


def _stop():
    raise _Stop


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_server_migrates_before_it_does_anything_else(style, monkeypatch):
    make_legacy()
    seen = []

    def spy():
        seen.append(("_ensure_directories", layout.needs_migration(), os.path.isfile(_new("buch.db"))))
        raise _Stop
    monkeypatch.setattr(server_app, "_ensure_directories", spy)
    with pytest.raises(_Stop):
        server_app.run_server(host="127.0.0.1", port=_free_port())
    assert seen == [("_ensure_directories", False, True)]       # beim ersten Schritt ist alles umgezogen


def test_run_server_does_not_migrate_an_installation_that_needs_none(monkeypatch):
    installation.set_up()
    monkeypatch.setattr(layout, "migrate",
                        lambda *a, **k: pytest.fail("layout.migrate() darf nicht laufen"))
    monkeypatch.setattr(server_app, "_ensure_directories", _stop)
    with pytest.raises(_Stop):
        server_app.run_server(host="127.0.0.1", port=_free_port())


class _FakeHTTPD:
    """Statt des echten Servers: serve_forever() kehrt wie nach Strg+C sofort zurück."""
    instances = []

    def __init__(self, address, handler):
        self.address = address
        self.closed = False
        _FakeHTTPD.instances.append(self)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


@pytest.fixture
def fake_server(monkeypatch):
    _FakeHTTPD.instances = []
    monkeypatch.setattr(server_app, "ThreadingHTTPServer", _FakeHTTPD)
    return _FakeHTTPD


def test_run_server_brings_a_legacy_install_up_to_date(style, fake_server):
    make_legacy()
    server_app.run_server(host="127.0.0.1", port=_free_port())
    assert len(fake_server.instances) == 1 and fake_server.instances[0].closed
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    assert auth.auto_login_user() == "admin"
    assert os.path.isfile(_new("buch.db"))
    assert os.path.isdir(os.path.join(userctx.DATA_ROOT, "users"))


def test_run_server_creates_the_auth_tables_for_a_headless_config(fake_server):
    write_config({"mode": "multi"})
    assert not os.path.exists(auth.AUTH_DB)
    server_app.run_server(host="127.0.0.1", port=_free_port())
    assert os.path.isfile(auth.AUTH_DB)
    assert auth.count_admins() == 0                               # Tabellen sind da, noch ohne Benutzer


def test_run_server_on_a_fresh_install_creates_only_the_data_root(fake_server):
    server_app.run_server(host="127.0.0.1", port=_free_port())
    assert os.listdir(userctx.DATA_ROOT) == []


def test_main_migrates_first_then_prepares_the_demo_then_serves(monkeypatch):
    calls = []
    monkeypatch.setattr(layout, "migrate", lambda *a, **k: calls.append("layout.migrate"))
    monkeypatch.setattr(demo, "prepare", lambda *a, **k: calls.append("demo.prepare"))
    monkeypatch.setattr(document_parser, "DocumentParser", lambda *a, **k: calls.append("DocumentParser"))
    monkeypatch.setattr(server, "run_server", lambda *a, **k: calls.append("run_server"))
    runpy.run_path(os.path.join(PROJECT_ROOT, "main.py"), run_name="__main__")
    assert calls == ["layout.migrate", "demo.prepare", "DocumentParser", "run_server"]


# ── zusammen mit dem Demo-Modus und dem Server ───────────────────────────────
def test_legacy_books_and_the_demo_variable_live_side_by_side(style, monkeypatch, logs):
    make_legacy()
    monkeypatch.setattr(demo_seed, "load_demo_scenario", lambda db: None)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert _migrate(logs) == "admin"                              # wie main.py: zuerst der Umzug …
    admin_before = tree(_new())
    assert demo.prepare(log=lambda m: None) == demo.SEEDED        # … dann die Demo, daneben
    assert tree(_new()) == admin_before                           # die echten Bücher bleiben, wie sie sind
    assert auth.is_admin("admin") is True and auth.is_admin("demo") is False
    assert auth.auto_login_user() == "admin"
    assert demo.active_user() == "demo"


def test_after_the_migration_the_server_works_as_before_without_a_login(style, server_port, logs):
    stored = make_legacy()
    _migrate(logs)
    page = get(server_port, "/about")                              # ohne Cookie, ohne Anmeldeseite
    assert page.status == 200 and "👤 admin" in page.text and "Abmelden" not in page.text
    assert get(server_port, "/login").headers["Location"] == "/"
    assert os.path.exists(stored.pdf) is False                     # am alten Ort liegt nichts mehr


def test_after_the_migration_a_logo_is_served_under_its_old_address(style, server_port, logs):
    # Kontakte speichern nur den logischen Pfad data/logos/<Datei>; er zeigt nun auf users/admin/logos
    make_legacy()
    _migrate(logs)
    resp = get(server_port, "/data/logos/logo.png")
    assert resp.status == 200
    assert resp.body == LEGACY_FILES["logos/logo.png"]


def test_after_the_migration_the_stored_invoice_documents_are_found(style, server_port, logs):
    # Die Datei heißt nicht nach dem Namensschema ("R26001 Probekunde GmbH.pdf"): Der Download
    # findet sie nur über den umgeschriebenen Pfad in der Datenbank.
    make_legacy()
    _migrate(logs)
    with contextlib.closing(sqlite3.connect(_new("buch.db"))) as con:
        invoice_id = con.execute("SELECT ID FROM Invoices WHERE InvoiceNumber = 'R26001'").fetchone()[0]
    pdf = get(server_port, f"/invoice/pdf_download?id={invoice_id}")
    assert pdf.status == 200
    assert pdf.body == LEGACY_FILES["invoices/2026/R26001 Probekunde.pdf"]
    xml = get(server_port, f"/invoice/xml_download?id={invoice_id}")
    assert xml.status == 200
    assert xml.body == LEGACY_FILES["invoices/2026/R26001 Probekunde.xml"]
