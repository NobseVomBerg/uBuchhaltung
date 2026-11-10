# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Tests für Datensicherung/Wiederherstellung (server/backup.py).

- Archivname JJJJMMTT_Backup.7zip/.zip, laufende Nummer bei Kollision
- 'db'-Scope vs. 'all'-Scope
- Wiederherstellen: überschreiben vs. vorher löschen (backup/ bleibt erhalten)
- Pfad-Traversal-Schutz beim Archivnamen
- tests/ (Artefakte des letzten pytest-Laufs, data/tests/) wird weder gesichert
  noch beim Wiederherstellen mit wipe gelöscht – im zip- und im 7-Zip-Weg

Alle Inhalte sind synthetische Testdaten.
"""
import os
import re
import shutil
import sqlite3

import pytest

from server import backup as backup_mod


@pytest.fixture
def user_dir(tmp_path):
    d = tmp_path / 'user'
    d.mkdir()
    conn = sqlite3.connect(d / 'buch.db')
    conn.execute('CREATE TABLE T (X TEXT)')
    conn.execute("INSERT INTO T VALUES ('original')")
    conn.commit()
    conn.close()
    (d / 'Belege').mkdir()
    (d / 'Belege' / 'beleg1.txt').write_text('Beleginhalt', encoding='utf-8')
    (d / 'notiz.txt').write_text('Notiz Version 1', encoding='utf-8')
    return str(d)


def _read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _db_value(user_dir):
    conn = sqlite3.connect(os.path.join(user_dir, 'buch.db'))
    val = conn.execute('SELECT X FROM T').fetchone()[0]
    conn.close()
    return val


def test_create_backup_naming_and_numbering(user_dir):
    a1, size1 = backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'db')
    a2, _ = backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'db')

    assert os.path.dirname(a1) == os.path.join(user_dir, 'backup')
    assert re.match(r'^\d{8}_Backup\.(7zip|zip)$', os.path.basename(a1)), a1
    assert re.match(r'^\d{8}_Backup_2\.(7zip|zip)$', os.path.basename(a2)), a2
    assert size1 > 0
    assert backup_mod.list_backups(user_dir) == sorted(
        [os.path.basename(a2), os.path.basename(a1)], reverse=True)


def test_restore_overwrite_keeps_new_files(user_dir):
    db_path = os.path.join(user_dir, 'buch.db')
    archive, _ = backup_mod.create_backup(user_dir, db_path, 'all')

    # Nach dem Backup: DB und Datei ändern, neue Datei anlegen
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE T SET X = 'geaendert'")
    conn.commit()
    conn.close()
    with open(os.path.join(user_dir, 'notiz.txt'), 'w', encoding='utf-8') as f:
        f.write('Notiz Version 2')
    with open(os.path.join(user_dir, 'neu.txt'), 'w', encoding='utf-8') as f:
        f.write('nach dem Backup entstanden')

    backup_mod.restore_backup(user_dir, os.path.basename(archive), wipe=False)

    assert _db_value(user_dir) == 'original'
    assert _read_text(os.path.join(user_dir, 'notiz.txt')) == 'Notiz Version 1'
    # Überschreiben-Modus: nachträglich entstandene Dateien bleiben liegen
    assert os.path.isfile(os.path.join(user_dir, 'neu.txt'))
    assert _read_text(os.path.join(user_dir, 'Belege', 'beleg1.txt')) == 'Beleginhalt'


def test_restore_wipe_removes_new_files_keeps_backups(user_dir):
    db_path = os.path.join(user_dir, 'buch.db')
    archive, _ = backup_mod.create_backup(user_dir, db_path, 'all')

    with open(os.path.join(user_dir, 'neu.txt'), 'w', encoding='utf-8') as f:
        f.write('nach dem Backup entstanden')

    backup_mod.restore_backup(user_dir, os.path.basename(archive), wipe=True)

    assert _db_value(user_dir) == 'original'
    assert not os.path.exists(os.path.join(user_dir, 'neu.txt'))
    # backup/ übersteht den Wipe
    assert backup_mod.list_backups(user_dir) == [os.path.basename(archive)]


def test_db_scope_contains_only_db(user_dir):
    db_path = os.path.join(user_dir, 'buch.db')
    archive, _ = backup_mod.create_backup(user_dir, db_path, 'db')

    # In leeres Verzeichnis entpacken und Inhalt prüfen
    import shutil, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(os.path.join(user_dir, 'backup'), os.path.join(tmp, 'backup'))
        backup_mod.restore_backup(tmp, os.path.basename(archive), wipe=False)
        entries = sorted(os.listdir(tmp))
        assert entries == ['backup', 'buch.db'], entries
        conn = sqlite3.connect(os.path.join(tmp, 'buch.db'))
        assert conn.execute('SELECT X FROM T').fetchone()[0] == 'original'
        conn.close()


def test_restore_rejects_unknown_and_traversal_names(user_dir):
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_backup(user_dir, 'gibtsnicht.7zip')
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_backup(user_dir, '..\\..\\boese.7zip')


# ── tests/ gehört nicht zur Sicherung ────────────────────────────────────────
@pytest.fixture(params=["zip", "7z"])
def archive_format(request, monkeypatch):
    """Beide Wege von create_backup/restore_backup: stdlib-zip (7-Zip 'nicht gefunden')
    und 7-Zip – der zweite entfällt, wo es nicht installiert ist."""
    if request.param == "zip":
        monkeypatch.setattr(backup_mod, "_find_sevenzip", lambda: None)
    elif not backup_mod._find_sevenzip():
        pytest.skip("7-Zip nicht installiert")
    return request.param


def _add_test_artifacts(user_dir):
    """tests/ wie nach einem pytest-Lauf (tmp/ mit Arbeitsdaten, cache/); liefert {Pfad: Inhalt}."""
    files = {os.path.join("tests", "tmp", "test_probe0", "buch.db"): "Testdatenbank",
             os.path.join("tests", "cache", "lastfailed"): "{}"}
    for rel, text in files.items():
        path = os.path.join(user_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return files


def _backup_all(user_dir, archive_format):
    archive, _ = backup_mod.create_backup(user_dir, os.path.join(user_dir, "buch.db"), "all")
    assert archive.endswith(".zip" if archive_format == "zip" else ".7zip")   # der gewollte Weg
    return archive


def test_tests_folder_is_not_part_of_the_backup(user_dir, archive_format, tmp_path):
    _add_test_artifacts(user_dir)
    archive = _backup_all(user_dir, archive_format)

    # In ein leeres Verzeichnis entpacken: alles Übrige ist da, aber nichts aus tests/
    restored = str(tmp_path / "entpackt")
    os.makedirs(os.path.join(restored, "backup"))
    shutil.copy(archive, os.path.join(restored, "backup"))
    backup_mod.restore_backup(restored, os.path.basename(archive))

    assert sorted(os.listdir(restored)) == ["Belege", "backup", "buch.db", "notiz.txt"]


def test_tests_folder_survives_restore_with_wipe(user_dir, archive_format):
    archive = _backup_all(user_dir, archive_format)
    # Der Testlauf kommt nach der Sicherung: Das Archiv kann tests/ nicht zurückbringen,
    # es muss den Wipe überstehen
    artifacts = _add_test_artifacts(user_dir)
    with open(os.path.join(user_dir, "neu.txt"), "w", encoding="utf-8") as f:
        f.write("nach dem Backup entstanden")

    backup_mod.restore_backup(user_dir, os.path.basename(archive), wipe=True)

    assert not os.path.exists(os.path.join(user_dir, "neu.txt"))      # der Wipe hat gewirkt …
    for rel, text in artifacts.items():                                # … tests/ blieb unberührt
        assert _read_text(os.path.join(user_dir, rel)) == text
    assert backup_mod.list_backups(user_dir) == [os.path.basename(archive)]
