# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Benutzer-Authentifizierung für den Mehrbenutzer-/LAN-Betrieb (TODO #4).

Eigene SQLite-Datenbank (``data/auth.db``), getrennt von den Nutzer-Büchern.
Passwörter werden mit ``scrypt`` (stdlib) und einem zufälligen Salt pro Nutzer
gehasht – niemals im Klartext gespeichert; der Vergleich erfolgt zeitkonstant.

Alle Funktionen akzeptieren optional einen ``db_path`` (für Tests), sonst gilt
die zentrale ``AUTH_DB``.

Die Anmeldung über ``attempt_login`` ist gedrosselt (Sperre nach Fehlversuchen, höchstens zwei
gleichzeitige scrypt-Läufe); ``authenticate`` prüft nur Name und Passwort.
"""
import contextlib
import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import threading
import time

AUTH_DB = "./data/auth.db"
SESSION_TTL = 7 * 24 * 3600          # 7 Tage
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
# Reservierte Benutzernamen (Vergleich ohne Groß-/Kleinschreibung): "tests" ist
# der Ordner für Testartefakte im Datenverzeichnis (data/tests/) und wird von
# Datensicherung und Umzug übergangen, "demo" ist der Benutzer des Demo-Modus
# (demo.py) – kein Nutzer darf so heißen. Nur der Code selbst legt sie an
# (create_user(..., allow_reserved=True)).
_RESERVED_USERNAMES = frozenset({"tests", "demo"})
_MIN_PW_LEN = 8      # gilt beim Setzen; bestehende kürzere Passwörter bleiben gültig

# scrypt-Parameter (RFC 7914): n=2^14 ⇒ ~16 MB, für interaktiven Login angemessen.
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

# Höchstens zwei scrypt-Läufe gleichzeitig (je ~16 MB): Ohne Grenze belegten parallele Anmeldeversuche
# beliebig viel Speicher und Rechenzeit.
_HASH_SLOTS = threading.BoundedSemaphore(2)

# Drosselung der Anmeldung (``attempt_login``), alles im Speicher: ein Neustart hebt Sperren auf.
LOGIN_FREE_ATTEMPTS = 5        # mit dem fünften Fehlversuch in Folge beginnt die Sperre (davor keine)
LOGIN_BLOCK_START = 30         # Sekunden Sperre nach dem fünften Fehlversuch, danach jeweils doppelt …
LOGIN_BLOCK_MAX = 900          # … höchstens so viele Sekunden
LOGIN_FORGET_AFTER = 900       # Ein Eintrag verfällt so lange nach dem letzten Fehlversuch bzw. dem Ende der Sperre
LOGIN_TRACK_MAX = 10000        # Obergrenze für die Einträge zu unbekannten Namen
LOGIN_SLOT_WAIT = 10           # Sekunden, die eine Anmeldung auf einen freien scrypt-Platz wartet


class LoginBusy(Exception):
    """Beide scrypt-Plätze sind belegt, und die Wartezeit ist um."""


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
def valid_username(username, allow_reserved=False):
    name = username or ""
    return bool(_USERNAME_RE.match(name)) and (allow_reserved
                                               or name.lower() not in _RESERVED_USERNAMES)


def _scrypt(password, salt):
    return hashlib.scrypt((password or "").encode("utf-8"), salt=salt, **_SCRYPT)


@contextlib.contextmanager
def _hash_slot(wait=None):
    """Einen der beiden scrypt-Plätze belegen. ``wait``: Sekunden, die auf einen freien gewartet wird
    (``None``: so lange wie nötig); bekommt der Aufrufer keinen, wird ``LoginBusy`` geworfen."""
    if not _HASH_SLOTS.acquire(timeout=wait):
        raise LoginBusy()
    try:
        yield
    finally:
        _HASH_SLOTS.release()


def _hash(password, salt, wait=None):
    with _hash_slot(wait):
        return _scrypt(password, salt)


# ── Benutzerverwaltung ───────────────────────────────────────────────────────
def has_any_user(db_path=None):
    init_auth_db(db_path)
    con = _conn(db_path)
    n = con.execute("SELECT COUNT(*) FROM Users").fetchone()[0]
    con.close()
    return n > 0


def create_user(username, password, is_admin=False, db_path=None, allow_reserved=False):
    """Neuen Benutzer anlegen. Wirft ValueError bei ungültigen Eingaben/Duplikat.

    ``allow_reserved`` erlaubt reservierte Namen (``demo``) – nur für den Code selbst
    (Demo-Modus, Umzug), nie für Eingaben aus dem Browser."""
    if not allow_reserved and (username or "").lower() in _RESERVED_USERNAMES:
        raise ValueError("Dieser Benutzername ist reserviert.")
    if not valid_username(username, allow_reserved=allow_reserved):
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


def _user_row(username, db_path=None):
    """``(PwHash, Salt)`` des Benutzers oder ``None``, wenn es ihn nicht gibt."""
    con = _conn(db_path)
    try:
        return con.execute("SELECT PwHash, Salt FROM Users WHERE Username = ?",
                           (username,)).fetchone()
    finally:
        con.close()


def _password_matches(row, password):
    """Stimmt ``password`` mit der Zeile aus ``_user_row`` überein? Zeitkonstanter Vergleich. Ohne Zeile
    läuft ein Dummy-Hash gegen Timing-Orakel (User-Existenz nicht unterscheidbar). Der Aufrufer hält
    einen scrypt-Platz (``_hash_slot``)."""
    if not row:
        _scrypt(password or "", b"\x00" * 16)
        return False
    pwhash, salt = row
    return hmac.compare_digest(bytes(pwhash), _scrypt(password, bytes(salt)))


def authenticate(username, password, db_path=None):
    """True, wenn Username/Passwort stimmen. Zeitkonstanter Vergleich, keine Drosselung: Die Anmeldung
    im Browser geht über ``attempt_login``."""
    row = _user_row(username, db_path)
    with _hash_slot():
        return _password_matches(row, password)


# ── Drosselung der Anmeldung ──────────────────────────────────────────────────
class _Failures:
    """Fehlversuche in Folge zu einem Namen; die Zeiten sind ``time.monotonic()``-Werte."""
    __slots__ = ("count", "last", "blocked_until")

    def __init__(self):
        self.count = 0
        self.last = 0.0
        self.blocked_until = 0.0


_login_lock = threading.Lock()
# Zwei Tabellen, nach außen gleich: Einträge vorhandener Benutzer werden nie verdrängt (sonst ließe sich
# ihre Sperre mit erfundenen Namen wegspülen) und verfallen nur über die Zeit; die zu unbekannten Namen
# sind auf LOGIN_TRACK_MAX begrenzt. Schlüssel ist der Name ohne Groß-/Kleinschreibung.
_known_failures = {}
_unknown_failures = {}


def reset_login_throttle():
    """Alle gemerkten Fehlversuche vergessen (Tests)."""
    with _login_lock:
        _known_failures.clear()
        _unknown_failures.clear()


def _block_seconds(count):
    """Sperre in Sekunden nach dem ``count``-ten Fehlversuch in Folge: 30 s beim fünften, dann doppelt,
    höchstens 900 s."""
    if count < LOGIN_FREE_ATTEMPTS:
        return 0
    return min(LOGIN_BLOCK_MAX, LOGIN_BLOCK_START << min(count - LOGIN_FREE_ATTEMPTS, 16))


def _expired(entry, now):
    return now >= max(entry.last, entry.blocked_until) + LOGIN_FORGET_AFTER


def _find_failures(key, now):
    """Eintrag zu ``key`` (erst die vorhandenen Benutzer) oder ``None``; ein verfallener wird entfernt.
    Nur mit ``_login_lock`` aufrufen."""
    for table in (_known_failures, _unknown_failures):
        entry = table.get(key)
        if entry is None:
            continue
        if _expired(entry, now):
            del table[key]
            continue
        return entry
    return None


def _block_left(entry, now):
    return max(0.0, entry.blocked_until - now) if entry is not None else 0.0


def _failures_for(key, entry, exists, now):
    """Eintrag, in dem ein Fehlversuch zu ``key`` gezählt wird: der vorhandene (ein Eintrag zu einem
    inzwischen angelegten Benutzer zieht zu den vorhandenen um), sonst ein neuer. Nur mit ``_login_lock``."""
    if entry is not None:
        if exists and _unknown_failures.get(key) is entry:
            del _unknown_failures[key]
            _known_failures[key] = entry
        return entry
    if exists:
        entry = _known_failures[key] = _Failures()
        return entry
    if len(_unknown_failures) >= LOGIN_TRACK_MAX:
        for stale in [k for k, e in _unknown_failures.items() if _expired(e, now)]:
            del _unknown_failures[stale]
        if len(_unknown_failures) >= LOGIN_TRACK_MAX:
            del _unknown_failures[min(_unknown_failures, key=lambda k: _unknown_failures[k].last)]
    entry = _unknown_failures[key] = _Failures()
    return entry


def attempt_login(username, password, db_path=None, now=None, on_block=None):
    """Anmeldeversuch mit Drosselung. Liefert ``(ergebnis, wartezeit)``, ``ergebnis`` ist

    * ``'ok'`` – Name und Passwort stimmen; die Fehlversuche des Namens sind vergessen.
    * ``'wrong'`` – falsch (auch ein Name, der keiner sein kann, und ein leeres Passwort: ohne Hash, ohne Eintrag).
    * ``'blocked'`` – gesperrt, ``wartezeit`` in Sekunden (aufgerundet bzw. die Dauer einer eben ausgelösten
      Sperre). Ein Versuch während der Sperre wird nicht geprüft, nicht gezählt und verlängert nichts –
      auch nicht mit dem richtigen Passwort.
    * ``'busy'`` – beide scrypt-Plätze blieben ``LOGIN_SLOT_WAIT`` Sekunden belegt; nichts wurde gezählt.

    Der Fehlversuch wird gezählt, **bevor** gehasht wird, unter der Sperre und mit einem Platz in der Hand:
    So zählen auch gleichzeitige Versuche, und es werden nie mehr Passwörter geprüft, als die Sperre
    erlaubt. Ob es den Namen gibt, sieht man von außen nicht (gleiche Antworten, gleicher Aufwand).
    ``now`` (Sekunden, ``time.monotonic``) gibt Tests die Uhr; ``on_block(sekunden)`` wird gerufen, wenn
    dieser Versuch eine Sperre auslöst."""
    name = username or ""
    if not password or not valid_username(name, allow_reserved=True):
        return "wrong", 0
    key = name.casefold()

    def clock():
        return time.monotonic() if now is None else now

    with _login_lock:
        left = _block_left(_find_failures(key, clock()), clock())
    if left > 0:
        return "blocked", math.ceil(left)

    try:
        with _hash_slot(LOGIN_SLOT_WAIT):
            row = _user_row(name, db_path)
            with _login_lock:
                moment = clock()
                entry = _find_failures(key, moment)
                left = _block_left(entry, moment)
                if left > 0:
                    return "blocked", math.ceil(left)
                entry = _failures_for(key, entry, row is not None, moment)
                entry.count += 1
                entry.last = moment
                started = _block_seconds(entry.count)
                if started:
                    entry.blocked_until = moment + started
            matches = _password_matches(row, password)
    except LoginBusy:
        return "busy", 0

    if matches:
        with _login_lock:
            _known_failures.pop(key, None)
            _unknown_failures.pop(key, None)
        return "ok", 0
    if started:
        if on_block is not None:
            on_block(started)
        return "blocked", started
    return "wrong", 0


def set_password(username, password, db_path=None, keep_session=None):
    """Passwort setzen. Alle Sitzungen des Benutzers enden dabei (in derselben Transaktion), bis auf die
    mit dem Token ``keep_session`` – wer sein eigenes Passwort ändert, bleibt angemeldet."""
    if not password or len(password) < _MIN_PW_LEN:
        raise ValueError(f"Passwort zu kurz (mindestens {_MIN_PW_LEN} Zeichen).")
    salt = secrets.token_bytes(16)
    pwhash = _hash(password, salt)
    con = _conn(db_path)
    try:
        con.execute("UPDATE Users SET PwHash = ?, Salt = ? WHERE Username = ?",
                    (pwhash, salt, username))
        # IS NOT: Ohne keep_session (NULL) trifft es alle Sitzungen des Benutzers
        con.execute("DELETE FROM Sessions WHERE Username = ? AND Token IS NOT ?",
                    (username, keep_session))
        con.commit()
    finally:
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


def admin_exists(db_path=None):
    """Gibt es mindestens einen Administrator? Legt dabei nichts an: Fehlt die
    auth.db oder sind ihre Tabellen noch nicht angelegt, lautet die Antwort Nein."""
    if not os.path.isfile(_path(db_path)):
        return False
    try:
        return count_admins(db_path) > 0
    except sqlite3.OperationalError:          # Tabellen fehlen noch
        return False


def sql_console_allowed(db_path=None):
    """Darf der aktuelle Nutzer die SQL-Konsole verwenden?

    Nur Administratoren. Solange die Installation nicht eingerichtet ist (Werkzeuge,
    Tests ohne Einrichtung), gibt es keine Einschränkung.
    """
    import userctx
    if not userctx.auth_enabled():
        return True
    user = userctx.get_user()
    return bool(user and is_admin(user, db_path))


def auto_login_user(db_path=None):
    """Benutzer der automatischen Anmeldung – nur, wenn er existiert und
    Administrator ist; sonst None (dann gilt die normale Anmeldung)."""
    import userctx
    name = userctx.get_auto_login()
    if name and is_admin(name, db_path):
        return name
    return None


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


def delete_all_sessions(db_path=None):
    """Alle Sitzungen aller Benutzer beenden (beim Ein- und Ausschalten der automatischen Anmeldung)."""
    con = _conn(db_path)
    try:
        con.execute("DELETE FROM Sessions")
        con.commit()
    finally:
        con.close()
