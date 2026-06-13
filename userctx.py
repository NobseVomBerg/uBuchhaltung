"""Thread-lokaler Request-Kontext für den Mehrbenutzer-/LAN-Betrieb (TODO #4).

Im Single-User-Default (Auth aus) verhält sich alles wie bisher: eine gemeinsame
Datenbank unter ``./data/buch.db`` und ein gemeinsames Datenverzeichnis ``./data``.
Ist Auth aktiv (Umgebungsvariable ``PYBUCH_AUTH``), liegen DB und Dateien jedes
Nutzers isoliert unter ``data/users/<user>/``.

Da der HTTP-Server pro Request einen eigenen Thread nutzt, wird der angemeldete
Benutzer thread-lokal gehalten und am Ende jedes Requests wieder geleert.
"""
import os
import threading

DATA_ROOT = "./data"
DEFAULT_DB = "./data/buch.db"
USERS_DIR = "users"

_local = threading.local()


def auth_enabled():
    """True, wenn der Mehrbenutzer-/Login-Modus per Env-Flag aktiviert ist."""
    return os.environ.get("PYBUCH_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


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
    return DEFAULT_DB
