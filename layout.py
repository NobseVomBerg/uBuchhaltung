# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Umzug der früheren Einzelbenutzer-Ablage in die Mehrbenutzer-Ablage.

Bis Version 1.x lagen die Daten im Einzelbenutzer-Modus direkt im Datenverzeichnis
(``data/buch.db``, ``data/invoices/`` …). Ab Version 2 gibt es nur noch
``data/users/<Name>/``. ``migrate()`` läuft beim Start (``main.py``), verschiebt
einen Altbestand dorthin und schaltet für ihn die automatische Anmeldung ein –
der Betrieb bleibt für den Nutzer derselbe (kein Login).

Es wird nichts gelöscht und nichts überschrieben. Der Umzug ist wiederaufnehmbar:
die Fortschrittsmarke ``migrating_to`` in ``config.json`` wird vor dem ersten
Verschieben geschrieben und erst am Ende entfernt.
"""
import os
import secrets
import sqlite3

import auth
import userctx

# Einträge direkt im Datenverzeichnis, die nicht zu einem Nutzer gehören
_KEEP = frozenset({userctx.CONFIG_FILE, 'auth.db', userctx.USERS_DIR, 'tests', 'lost+found'})
_MIGRATION_KEY = 'migrating_to'
_DEMO_MARKER = '.demo'          # früheres Demo-Verzeichnis (siehe demo.py)

# In der Datenbank gespeicherte Dateipfade, die beim Umzug mitwandern müssen
_PATH_COLUMNS = (('Invoices', ('PDFPath', 'XMLPath')), ('Documents', ('Path',)))


def needs_migration():
    """True, wenn ein Altbestand umzuziehen ist (oder ein Umzug unterbrochen wurde)."""
    cfg = userctx._read_config()
    if cfg.get(_MIGRATION_KEY):
        return True
    mode = cfg.get('mode')
    if mode == 'single':
        return True
    if mode != 'multi':
        # Uralt-Stand ohne config.json: die Datenbank liegt direkt im Datenverzeichnis
        return os.path.isfile(os.path.join(userctx.DATA_ROOT, 'buch.db'))
    return False


def _free_username(base):
    """``base``, sonst ``base2``, ``base3`` … – der erste Name ohne Benutzer und ohne Ordner."""
    name, n = base, 1
    while (auth.user_exists(name)
           or os.path.lexists(os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, name))):
        n += 1
        name = f"{base}{n}"
    return name


def _moved_path(value, old_root, new_root):
    """Neuer Pfad für einen gespeicherten Dateipfad oder ``None``, wenn nichts zu ändern ist.

    Nur Pfade unterhalb von ``old_root`` (und nicht in einem der behaltenen Einträge)
    wandern mit, und nur, wenn die Datei am neuen Ort tatsächlich liegt."""
    try:
        rel = os.path.relpath(os.path.abspath(value), os.path.abspath(old_root))
    except ValueError:
        return None
    if rel.startswith('..') or os.path.isabs(rel) or rel.split(os.sep)[0] in _KEEP:
        return None
    cand = os.path.join(new_root, rel)
    return cand if os.path.exists(cand) else None


def _rewrite_paths(db_path, old_root, new_root):
    """Gespeicherte Dateipfade (Rechnungs-PDF/-XML, Belege) auf den neuen Ort umschreiben.
    Liefert die Anzahl geänderter Pfade."""
    if not os.path.isfile(db_path):
        return 0
    changed = 0
    con = sqlite3.connect(db_path)
    try:
        for table, columns in _PATH_COLUMNS:
            for column in columns:
                try:
                    rows = con.execute(
                        f"SELECT ID, {column} FROM {table} "
                        f"WHERE {column} IS NOT NULL AND {column} <> ''").fetchall()
                except sqlite3.Error:        # Tabelle/Spalte gibt es in alten Schemaständen nicht
                    continue
                for row_id, value in rows:
                    new = _moved_path(value, old_root, new_root)
                    if new and new != value:
                        con.execute(f"UPDATE {table} SET {column} = ? WHERE ID = ?",
                                    (new, row_id))
                        changed += 1
        con.commit()
    finally:
        con.close()
    return changed


def migrate(log=print):
    """Altbestand in ``data/users/<Name>/`` umziehen. Liefert den Benutzernamen,
    oder ``None``, wenn nichts umzuziehen war."""
    if not needs_migration():
        return None
    root = userctx.DATA_ROOT
    cfg = userctx._read_config()
    auth.init_auth_db()

    name = cfg.get(_MIGRATION_KEY)
    if not name:
        is_demo = os.path.isfile(os.path.join(root, _DEMO_MARKER))
        name = _free_username('demo' if is_demo else 'admin')
        cfg[_MIGRATION_KEY] = name
        userctx._write_config(cfg)             # Marke zuerst: ab hier ist der Umzug wiederaufnehmbar

    target = os.path.join(root, userctx.USERS_DIR, name)
    os.makedirs(target, exist_ok=True)
    for entry in sorted(os.listdir(root)):
        if entry in _KEEP:
            continue
        src, dst = os.path.join(root, entry), os.path.join(target, entry)
        if os.path.lexists(dst):
            raise RuntimeError(
                f"Umzug abgebrochen: '{dst}' existiert bereits. Es wurde nichts überschrieben – "
                "bitte von Hand klären (siehe DEPLOYMENT.md, Umzug auf Version 2).")
        os.replace(src, dst)

    changed = _rewrite_paths(os.path.join(target, 'buch.db'), root, target)

    is_demo = os.path.isfile(os.path.join(target, _DEMO_MARKER))
    if not auth.user_exists(name):
        auth.create_user(name, secrets.token_urlsafe(24), is_admin=not is_demo,
                         allow_reserved=True)

    cfg = userctx._read_config()
    cfg.pop(_MIGRATION_KEY, None)
    cfg['mode'] = 'multi'
    if not is_demo:
        cfg['auto_login'] = name
    userctx._write_config(cfg)
    userctx._mode_cache.pop(userctx.config_path(), None)
    userctx._auto_login_cache.pop(userctx.config_path(), None)

    log(f"Umzug auf die Mehrbenutzer-Ablage (Version 2): '{root}' → '{target}'.")
    log(f"  {changed} gespeicherte Dateipfade (Rechnungen, Belege) angepasst.")
    if is_demo:
        log(f"  Die Demodaten gehören jetzt dem Benutzer '{name}' (siehe DEPLOYMENT.md, Umzug auf Version 2).")
    else:
        log(f"  '{name}' wird automatisch angemeldet, es ändert sich nichts an der Bedienung "
            "(siehe DEPLOYMENT.md, Umzug auf Version 2).")
    return name
