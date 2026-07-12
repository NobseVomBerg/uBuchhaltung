# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Tests für Datensicherung/Wiederherstellung (server/backup.py).

- Archivname JJJJMMTT_Backup.7zip/.zip, laufende Nummer bei Kollision
- 'db'-Scope vs. 'all'-Scope
- Wiederherstellen: überschreiben vs. vorher löschen (backup/ bleibt erhalten)
- Pfad-Traversal-Schutz beim Archivnamen

Alle Inhalte sind synthetische Testdaten.
"""
import os
import re
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
    assert open(os.path.join(user_dir, 'notiz.txt'), encoding='utf-8').read() == 'Notiz Version 1'
    # Überschreiben-Modus: nachträglich entstandene Dateien bleiben liegen
    assert os.path.isfile(os.path.join(user_dir, 'neu.txt'))
    assert open(os.path.join(user_dir, 'Belege', 'beleg1.txt'), encoding='utf-8').read() == 'Beleginhalt'


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
