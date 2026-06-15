"""Thread-lokaler Request-Kontext + App-Modus für den Mehrbenutzer-/LAN-Betrieb.

Der Betriebsmodus (``single`` oder ``multi``) wird bei der Ersteinrichtung gewählt
und in ``data/config.json`` persistiert – keine Umgebungsvariable. Im
Einzelbenutzer-Modus verhält sich alles wie bisher (eine gemeinsame
``./data/buch.db``); im Mehrbenutzer-Modus liegen DB und Dateien jedes Nutzers
isoliert unter ``data/users/<user>/``.

Da der HTTP-Server pro Request einen eigenen Thread nutzt, wird der angemeldete
Benutzer thread-lokal gehalten und am Ende jedes Requests wieder geleert.
"""
import json
import os
import threading

DATA_ROOT = "./data"
DEFAULT_DB = "./data/buch.db"
USERS_DIR = "users"
CONFIG_FILE = "config.json"

_local = threading.local()

# Modus-Cache je Konfig-Pfad (vermeidet Datei-IO im Hot-Path; Tests nutzen
# unterschiedliche DATA_ROOT-Pfade und bleiben dadurch isoliert).
_mode_cache = {}


def config_path():
    return os.path.join(DATA_ROOT, CONFIG_FILE)


def get_mode():
    """Betriebsmodus: ``'single'`` | ``'multi'`` | ``None`` (noch nicht gewählt)."""
    path = config_path()
    if path in _mode_cache:
        return _mode_cache[path]
    mode = None
    try:
        with open(path, encoding="utf-8") as f:
            mode = json.load(f).get("mode")
    except (FileNotFoundError, ValueError, OSError):
        mode = None
    if mode not in ("single", "multi"):
        mode = None
    _mode_cache[path] = mode
    return mode


def set_mode(mode):
    """Betriebsmodus persistieren (``'single'`` oder ``'multi'``)."""
    if mode not in ("single", "multi"):
        raise ValueError("mode muss 'single' oder 'multi' sein")
    os.makedirs(DATA_ROOT, exist_ok=True)
    path = config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f)
    _mode_cache[path] = mode


def auth_enabled():
    """True, wenn der Mehrbenutzer-Modus (mit Login) konfiguriert ist."""
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


def clear():
    """Request-Kontext zurücksetzen (im finally jedes Requests aufrufen)."""
    _local.user = None


def user_data_dir():
    """Basis-Datenverzeichnis des aktuellen Nutzers (Logos, PDFs …).

    Single-User-Default ⇒ ``./data``; mit aktivem Login ⇒ ``data/users/<user>``.
    """
    user = get_user()
    if auth_enabled() and user:
        return os.path.join(DATA_ROOT, USERS_DIR, user)
    return DATA_ROOT


def user_db_path():
    """DB-Pfad des aktuellen Nutzers.

    Single-User-Default ⇒ ``./data/buch.db``; mit aktivem Login ⇒
    ``data/users/<user>/buch.db``.
    """
    user = get_user()
    if auth_enabled() and user:
        return os.path.join(DATA_ROOT, USERS_DIR, user, "buch.db")
    return os.path.join(DATA_ROOT, "buch.db")


def user_subdir(name, create=True):
    """Unterverzeichnis (z. B. ``logos``, ``invoices``) im Datenbereich des
    aktuellen Nutzers. Legt es bei Bedarf an.

    Single-User-Default ⇒ ``./data/<name>``; mit aktivem Login ⇒
    ``data/users/<user>/<name>``.
    """
    path = os.path.join(user_data_dir(), name)
    if create:
        os.makedirs(path, exist_ok=True)
    return path
