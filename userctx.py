# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Thread-lokaler Request-Kontext + Einrichtungsstand für den Mehrbenutzer-/LAN-Betrieb.

Ob die Installation eingerichtet ist, steht in ``data/config.json`` (``"mode": "multi"``)
– keine Umgebungsvariable. Es gibt nur die Mehrbenutzer-Ablage: DB und Dateien jedes
Nutzers liegen isoliert unter ``data/users/<user>/``. ``config.json`` kann zusätzlich
``"auto_login": "<Benutzername>"`` nennen: Dieser Administrator wird dann ohne Passwort
angemeldet (Betrieb für eine Person).

Da der HTTP-Server pro Request einen eigenen Thread nutzt, wird der angemeldete
Benutzer thread-lokal gehalten und am Ende jedes Requests wieder geleert.
"""
import json
import os
import threading

DATA_ROOT = "./data"
USERS_DIR = "users"
CONFIG_FILE = "config.json"

_local = threading.local()

# Modus-Cache je Konfig-Pfad (vermeidet Datei-IO im Hot-Path; Tests nutzen
# unterschiedliche DATA_ROOT-Pfade und bleiben dadurch isoliert).
_mode_cache = {}
_auto_login_cache = {}


def config_path():
    return os.path.join(DATA_ROOT, CONFIG_FILE)


def _read_config():
    """Inhalt von config.json als dict ({} wenn die Datei fehlt oder unlesbar ist)."""
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data):
    os.makedirs(DATA_ROOT, exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_mode():
    """``'multi'`` (eingerichtet) | ``None`` (noch nicht eingerichtet). Den früheren
    Einzelbenutzer-Modus (``'single'``) zieht ``layout.migrate()`` beim Start um."""
    path = config_path()
    if path in _mode_cache:
        return _mode_cache[path]
    mode = "multi" if _read_config().get("mode") == "multi" else None
    _mode_cache[path] = mode
    _auto_login_cache.pop(path, None)      # zusammen mit dem Modus neu lesen
    return mode


def set_mode(mode):
    """Die Installation als eingerichtet markieren und persistieren (nur ``'multi'``)."""
    if mode != "multi":
        raise ValueError("mode muss 'multi' sein")
    data = _read_config()
    data["mode"] = mode
    _write_config(data)
    path = config_path()
    _mode_cache[path] = mode
    _auto_login_cache.pop(path, None)


def get_auto_login():
    """Benutzername der automatischen Anmeldung laut config.json oder None.

    Nur bei eingerichteter Installation. Ob der Benutzer existiert und Administrator
    ist, prüft ``auth.auto_login_user()`` – nur dann gilt die Einstellung.
    """
    if get_mode() != "multi":
        return None
    path = config_path()
    if path not in _auto_login_cache:
        name = _read_config().get("auto_login")
        _auto_login_cache[path] = name if isinstance(name, str) and name else None
    return _auto_login_cache[path]


def set_auto_login(username):
    """Automatische Anmeldung für ``username`` einschalten (``None`` schaltet sie aus)."""
    if get_mode() != "multi":
        raise ValueError("Automatische Anmeldung gibt es erst nach der Ersteinrichtung.")
    data = _read_config()
    if username:
        data["auto_login"] = username
    else:
        data.pop("auto_login", None)
    _write_config(data)
    _auto_login_cache[config_path()] = username or None


def auth_enabled():
    """True, sobald die Installation eingerichtet ist (Benutzerverwaltung aktiv)."""
    return get_mode() == "multi"


def tls_enabled():
    """True, wenn der Server über HTTPS läuft (für das Secure-Cookie-Flag)."""
    return bool(getattr(_local, "tls", False))


def set_tls(value):
    _local.tls = bool(value)


def set_user(username):
    """Angemeldeten Benutzer für den aktuellen Request setzen (oder None)."""
    _local.user = username or None


def get_user():
    return getattr(_local, "user", None)


def set_auto_login_active(value):
    """Kennzeichnet den aktuellen Request als automatisch angemeldet."""
    _local.auto = bool(value)


def auto_login_active():
    return bool(getattr(_local, "auto", False))


def set_csrf_token(token):
    """CSRF-Token des aktuellen Requests (für das Einbetten in Seiten)."""
    _local.csrf = token or ''


def get_csrf_token():
    return getattr(_local, 'csrf', '')


def clear():
    """Request-Kontext zurücksetzen (im finally jedes Requests aufrufen)."""
    _local.user = None
    _local.csrf = ''
    _local.auto = False


def user_data_dir():
    """Basis-Datenverzeichnis des aktuellen Nutzers (Logos, PDFs …).

    Mit angemeldetem Benutzer ⇒ ``data/users/<user>``; ohne Benutzer-Kontext
    (Werkzeuge, Tests) ⇒ ``./data``.
    """
    user = get_user()
    if auth_enabled() and user:
        return os.path.join(DATA_ROOT, USERS_DIR, user)
    return DATA_ROOT


def user_db_path():
    """DB-Pfad des aktuellen Nutzers.

    Mit angemeldetem Benutzer ⇒ ``data/users/<user>/buch.db``; ohne Benutzer-Kontext
    (Werkzeuge, Tests) ⇒ ``./data/buch.db``.
    """
    user = get_user()
    if auth_enabled() and user:
        return os.path.join(DATA_ROOT, USERS_DIR, user, "buch.db")
    return os.path.join(DATA_ROOT, "buch.db")


def user_subdir(name, create=True):
    """Unterverzeichnis (z. B. ``logos``, ``invoices``) im Datenbereich des
    aktuellen Nutzers. Legt es bei Bedarf an.

    Mit angemeldetem Benutzer ⇒ ``data/users/<user>/<name>``; ohne Benutzer-Kontext
    (Werkzeuge, Tests) ⇒ ``./data/<name>``.
    """
    path = os.path.join(user_data_dir(), name)
    if create:
        os.makedirs(path, exist_ok=True)
    return path
