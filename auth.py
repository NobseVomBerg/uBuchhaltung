# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Benutzer-Authentifizierung für den Mehrbenutzer-/LAN-Betrieb (TODO #4).

Eigene SQLite-Datenbank (``data/auth.db``), getrennt von den Nutzer-Büchern.
Passwörter werden mit ``scrypt`` (stdlib) und einem zufälligen Salt pro Nutzer
gehasht – niemals im Klartext gespeichert; der Vergleich erfolgt zeitkonstant.

Alle Funktionen akzeptieren optional einen ``db_path`` (für Tests), sonst gilt
die zentrale ``AUTH_DB``.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

AUTH_DB = "./data/auth.db"
SESSION_TTL = 7 * 24 * 3600          # 7 Tage
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
_MIN_PW_LEN = 6

# scrypt-Parameter (RFC 7914): n=2^14 ⇒ ~16 MB, für interaktiven Login angemessen.
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

# Prozess-Geheimnis für CSRF-Token (im Speicher; Neustart invalidiert offene
# Formulare, Sessions bleiben gültig). Zusätzlich zu SameSite=Strict.
_APP_SECRET = secrets.token_bytes(32)


def csrf_for(session_token):
    """CSRF-Token für eine Session (HMAC des Session-Tokens)."""
    if not session_token:
        return ""
    return hmac.new(_APP_SECRET, session_token.encode("utf-8"), "sha256").hexdigest()


def check_csrf(session_token, token):
    """Zeitkonstanter Vergleich des übermittelten CSRF-Tokens."""
    if not session_token or not token:
        return False
    return hmac.compare_digest(csrf_for(session_token), token)


# ── DB-Helfer ────────────────────────────────────────────────────────────────
def _path(db_path):
    """AUTH_DB erst beim Aufruf auflösen – nicht als Default-Argument binden,
    damit ein zur Laufzeit gesetztes ``auth.AUTH_DB`` (Tests/Config) greift."""
    return db_path or AUTH_DB


def _conn(db_path=None):
    db_path = _path(db_path)
    d = os.path.dirname(db_path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    return sqlite3.connect(db_path)


def init_auth_db(db_path=None):
    """Auth-Tabellen anlegen (idempotent)."""
    con = _conn(db_path)
    con.execute('''
        CREATE TABLE IF NOT EXISTS Users (
            Username  TEXT PRIMARY KEY,
            PwHash    BLOB NOT NULL,
            Salt      BLOB NOT NULL,
            IsAdmin   INTEGER NOT NULL DEFAULT 0,
            CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    con.execute('''
        CREATE TABLE IF NOT EXISTS Sessions (
            Token     TEXT PRIMARY KEY,
            Username  TEXT NOT NULL,
            ExpiresAt INTEGER NOT NULL,
            FOREIGN KEY (Username) REFERENCES Users(Username) ON DELETE CASCADE
        )
    ''')
    con.commit()
    con.close()


# ── Passwort-Hashing ─────────────────────────────────────────────────────────
def valid_username(username):
    return bool(_USERNAME_RE.match(username or ""))


def _hash(password, salt):
    return hashlib.scrypt((password or "").encode("utf-8"), salt=salt, **_SCRYPT)


# ── Benutzerverwaltung ───────────────────────────────────────────────────────
def has_any_user(db_path=None):
    init_auth_db(db_path)
    con = _conn(db_path)
    n = con.execute("SELECT COUNT(*) FROM Users").fetchone()[0]
    con.close()
    return n > 0


def create_user(username, password, is_admin=False, db_path=None):
    """Neuen Benutzer anlegen. Wirft ValueError bei ungültigen Eingaben/Duplikat."""
    if not valid_username(username):
        raise ValueError("Ungültiger Benutzername (3–32 Zeichen: A–Z, a–z, 0–9, _ , -).")
    if not password or len(password) < _MIN_PW_LEN:
        raise ValueError(f"Passwort zu kurz (mindestens {_MIN_PW_LEN} Zeichen).")
    init_auth_db(db_path)
    salt = secrets.token_bytes(16)
    pwhash = _hash(password, salt)
    con = _conn(db_path)
    try:
        con.execute(
            "INSERT INTO Users (Username, PwHash, Salt, IsAdmin) VALUES (?, ?, ?, ?)",
            (username, pwhash, salt, 1 if is_admin else 0),
        )
        con.commit()
    except sqlite3.IntegrityError:
        raise ValueError("Benutzer existiert bereits.")
    finally:
        con.close()


def authenticate(username, password, db_path=None):
    """True, wenn Username/Passwort stimmen. Zeitkonstanter Vergleich."""
    con = _conn(db_path)
    row = con.execute("SELECT PwHash, Salt FROM Users WHERE Username = ?",
                      (username,)).fetchone()
    con.close()
    if not row:
        # Dummy-Hash gegen Timing-Orakel (User-Existenz nicht unterscheidbar)
        _hash(password or "", b"\x00" * 16)
        return False
    pwhash, salt = row
    return hmac.compare_digest(bytes(pwhash), _hash(password, bytes(salt)))


def set_password(username, password, db_path=None):
    if not password or len(password) < _MIN_PW_LEN:
        raise ValueError(f"Passwort zu kurz (mindestens {_MIN_PW_LEN} Zeichen).")
    salt = secrets.token_bytes(16)
    pwhash = _hash(password, salt)
    con = _conn(db_path)
    con.execute("UPDATE Users SET PwHash = ?, Salt = ? WHERE Username = ?",
                (pwhash, salt, username))
    con.commit()
    con.close()


def delete_user(username, db_path=None):
    con = _conn(db_path)
    con.execute("DELETE FROM Sessions WHERE Username = ?", (username,))
    con.execute("DELETE FROM Users WHERE Username = ?", (username,))
    con.commit()
    con.close()


def is_admin(username, db_path=None):
    con = _conn(db_path)
    row = con.execute("SELECT IsAdmin FROM Users WHERE Username = ?", (username,)).fetchone()
    con.close()
    return bool(row and row[0])


def set_admin(username, is_admin_flag, db_path=None):
    con = _conn(db_path)
    con.execute("UPDATE Users SET IsAdmin = ? WHERE Username = ?",
                (1 if is_admin_flag else 0, username))
    con.commit()
    con.close()


def count_admins(db_path=None):
    con = _conn(db_path)
    n = con.execute("SELECT COUNT(*) FROM Users WHERE IsAdmin = 1").fetchone()[0]
    con.close()
    return n


def user_exists(username, db_path=None):
    con = _conn(db_path)
    row = con.execute("SELECT 1 FROM Users WHERE Username = ?", (username,)).fetchone()
    con.close()
    return row is not None


def list_users(db_path=None):
    """Liste (Username, IsAdmin, CreatedAt), nach Name sortiert."""
    con = _conn(db_path)
    rows = con.execute(
        "SELECT Username, IsAdmin, CreatedAt FROM Users ORDER BY Username"
    ).fetchall()
    con.close()
    return rows


# ── Sessions ─────────────────────────────────────────────────────────────────
def create_session(username, db_path=None, ttl=SESSION_TTL):
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + ttl
    con = _conn(db_path)
    con.execute("INSERT INTO Sessions (Token, Username, ExpiresAt) VALUES (?, ?, ?)",
                (token, username, expires))
    con.commit()
    con.close()
    return token


def get_session_user(token, db_path=None):
    """Username zu einem gültigen Session-Token (oder None). Räumt abgelaufene Token ab."""
    if not token:
        return None
    con = _conn(db_path)
    row = con.execute("SELECT Username, ExpiresAt FROM Sessions WHERE Token = ?",
                      (token,)).fetchone()
    if row and row[1] < int(time.time()):
        con.execute("DELETE FROM Sessions WHERE Token = ?", (token,))
        con.commit()
        row = None
    con.close()
    return row[0] if row else None


def delete_session(token, db_path=None):
    if not token:
        return
    con = _conn(db_path)
    con.execute("DELETE FROM Sessions WHERE Token = ?", (token,))
    con.commit()
    con.close()
