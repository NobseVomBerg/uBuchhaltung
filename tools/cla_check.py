# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Prüft für einen Pull Request, ob die Zustimmung zum CLA (CLA.md) vorliegt – nur Standardbibliothek.

Aufruf: ``python3 tools/cla_check.py`` aus ``.github/workflows/cla.yml`` (Ereignisse
``pull_request_target`` und ``issue_comment``). Das Skript

* ermittelt, wer zustimmen muss (``required_signers``): der Autor des Pull Requests und jeder
  Commit-Autor, den GitHub einem Konto zuordnet – ohne das Konto des Repository-Inhabers und ohne
  Bot-Konten. Ein Commit ohne zugeordnetes Konto lässt die Prüfung scheitern, denn niemand kann ihn
  per Kommentar abdecken;
* sucht je Person einen eigenen Kommentar im Pull Request, der den Satz aus CLA.md Ziffer 2 mit der
  aktuellen Fassung in einer eigenen Zeile trägt (``has_consent``; Zitate und Sätze mit Zusatz in
  derselben Zeile zählen nicht);
* setzt am letzten Commit des Pull Requests den Status ``CLA-Zustimmung``;
* hält jede gefundene Zustimmung in einem eigenen Kommentar fest (``record_body``): der Nachweis
  bleibt, wenn der Kommentar später geändert oder gelöscht wird.

Die Zustimmung gilt für den ganzen Pull Request, auch für spätere Commits – sie nennt keinen Commit.
Der Lauf endet mit 0, auch wenn die Zustimmung fehlt: Das Ergebnis trägt der Status. Als Pflicht-Check
gehört deshalb der Status ``CLA-Zustimmung`` ins Ruleset, nicht der Lauf. Mit 1 endet er nur, wenn die
Prüfung gar nicht laufen konnte (fehlende Angaben, Fehler der GitHub-API).

Sicherheit – der Lauf hat Schreibrechte:

* Das Skript kommt immer vom Standard-Branch, nie aus dem Pull Request. Autor, letzten Commit,
  Commits und Kommentare liest es frisch über die API; das Ereignis liefert nur die Nummer, den
  Inhaber und den Namen des Standard-Branches.
* Text aus dem Pull Request (Kommentare, Titel, Branchnamen) steht weder in der Ausgabe noch in einer
  Fehlermeldung noch in dem, was das Skript schreibt: GitHub Actions deutet Zeilen wie ``::add-mask::``
  in der Ausgabe als Befehle. Ausgegeben werden nur Nummern, Zustand, Anzahlen, Kennungen und
  Anmeldenamen, in denen alles außer ``A-Z a-z 0-9 -`` (und dem Suffix ``[bot]``) durch ``?``
  ersetzt ist (``safe_login``).
* Adressen entstehen nur aus geprüften Werten (Repository-Name, ganze Zahlen, 40 Hex-Zeichen). Das
  Token geht nur per HTTPS an die API-Adresse, folgt keiner Weiterleitung und steht nie in einer
  Meldung. Fehler der API nennen Methode, Pfad und Statuscode, nie den Antworttext.
* Dass eine Zustimmung schon festgehalten ist, zeigt nur ein Kommentar eines Bot-Kontos an, der mit
  dem Marker beginnt (``record_marker``): Ein Kommentar eines Nutzers mit demselben Marker
  verhindert das Festhalten nicht.

Grenzen: GitHub liefert zu einem Pull Request höchstens 250 Commits; hat er mehr, besteht die Prüfung
nicht (``unseen_commits``). ``Co-authored-by`` kennt die API nicht; nur der Autor eines Commits wird einem Konto zugeordnet.
Es zählen Kommentare der Unterhaltung des Pull Requests, keine Review-Kommentare an Codezeilen.

Umgebung (in GitHub Actions gesetzt): ``GITHUB_EVENT_PATH``, ``GITHUB_REPOSITORY``, ``GITHUB_TOKEN``,
optional ``GITHUB_API_URL`` (Standard https://api.github.com) und ``GITHUB_SERVER_URL`` (Standard
https://github.com). ``CLA.md`` liegt eine Ebene über dem Skript.
"""
import hashlib
import http.client
import json
import os
import re
import sys
import traceback
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STATUS_CONTEXT = "CLA-Zustimmung"
API_URL = "https://api.github.com"
SERVER_URL = "https://github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "ubuchhaltung-cla-check"
TIMEOUT = 30                      # Sekunden je Anfrage
PAGE_SIZE = 100
MAX_COMMIT_PAGES = 3              # GitHub liefert zu einem Pull Request höchstens 250 Commits
MAX_COMMITS = 250
MAX_COMMENT_PAGES = 50
DESCRIPTION_LIMIT = 140           # längere Statusbeschreibungen lehnt GitHub ab
CLA_PATH = Path(__file__).resolve().parent.parent / "CLA.md"

# Alle Muster werden mit fullmatch angewendet (``$`` ließe einen Zeilenumbruch am Ende durch) und
# nennen Ziffern und Buchstaben ausdrücklich in ASCII (``\d`` träfe auch andere Schriften).
_VERSION_LINE = re.compile(r"\*\*Version:\*\*\s*([0-9]+(?:\.[0-9]+)*)")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_API_URL = re.compile(r"https://[A-Za-z0-9_.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9_.~/-]*)?")
_SERVER_URL = re.compile(r"https://[A-Za-z0-9_.-]+(?::[0-9]{1,5})?")
_TOKEN = re.compile(r"[\x21-\x7e]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_LOGIN_UNSAFE = re.compile(r"[^A-Za-z0-9-]")
_URL_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f-\x9f<>\"'`()\[\]\\]")


class ClaError(Exception):
    """Die Prüfung kann nicht ausgeführt werden. Die Meldung enthält nie Text aus Antworten von GitHub."""


def _log(message):
    print(f"cla-check: {message}", file=sys.stderr, flush=True)


def _info(message):
    print(f"cla-check: {message}", flush=True)


# ── Reine Funktionen (ohne Netz) ─────────────────────────────────────────────
def cla_version(text):
    """Fassungsnummer aus der ersten Zeile ``**Version:** …`` von CLA.md."""
    if isinstance(text, str):
        for line in text.splitlines():
            match = _VERSION_LINE.match(line)
            if match:
                return match.group(1)
    raise ClaError("In CLA.md fehlt die Zeile mit der Fassung (**Version:** …).")


def consent_sentences(version):
    """Die beiden Erklärungen aus CLA.md Ziffer 2: ``(deutsch, englisch)``."""
    return (f"Ich stimme dem Contributor License Agreement (CLA.md), Version {version}, für diesen Beitrag zu.",
            f"I agree to the Contributor License Agreement (CLA.md), version {version}, for this contribution.")


def _normalize(line):
    """Zeile für den Vergleich: NFC, Leerraum zu einem Leerzeichen, ohne Groß-/Kleinschreibung, ohne einen Schlusspunkt."""
    text = " ".join(unicodedata.normalize("NFC", line).split()).casefold()
    return text[:-1] if text.endswith(".") else text


def matched_sentence(body, version):
    """Der Satz aus ``consent_sentences``, den eine Zeile des Kommentars trägt, sonst ``None``.

    Die Zeile muss dem Satz gleichen – kein Teilstring: ein Zusatz in derselben Zeile, eine andere
    Fassung oder ein Satz über zwei Zeilen zählen nicht. Zitatzeilen (``>``) zählen nicht. Zurück gibt
    es immer den eigenen Satz, nie Text aus dem Kommentar.
    """
    if not isinstance(body, str):
        return None
    wanted = [(sentence, _normalize(sentence)) for sentence in consent_sentences(version)]
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.lstrip().startswith(">"):
            continue
        normalized = _normalize(line)
        for sentence, expected in wanted:
            if normalized == expected:
                return sentence
    return None


def has_consent(body, version):
    """Trägt der Kommentar die Erklärung zur Fassung ``version``?"""
    return matched_sentence(body, version) is not None


def safe_login(login):
    """Anmeldename für Ausgaben: nur ``A-Z a-z 0-9 -`` und das Suffix ``[bot]``, alles andere wird ``?``."""
    text = login if isinstance(login, str) else ""
    suffix = ""
    if text.endswith("[bot]"):
        text, suffix = text[:-len("[bot]")], "[bot]"
    return _LOGIN_UNSAFE.sub("?", text) + suffix


def _plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value, what):
    if not _plain_int(value):
        raise ClaError(f"{what}: Die Kennung ist keine ganze Zahl.")
    return value


def _account(value, what):
    """``(id, login, type)`` eines GitHub-Kontos aus einer Antwort der API."""
    if not isinstance(value, dict):
        raise ClaError(f"{what}: Die Antwort von GitHub enthält kein Konto.")
    login = value.get("login")
    if not isinstance(login, str):
        raise ClaError(f"{what}: Das Konto in der Antwort von GitHub hat keinen Anmeldenamen.")
    return _identifier(value.get("id"), what), login, value.get("type")


def required_signers(pr, commits, owner_id):
    """Wer zustimmen muss: ``([(id, login), …], Anzahl der Commits ohne GitHub-Konto)``.

    Kandidaten sind der Autor des Pull Requests und jeder Commit-Autor, den GitHub einem Konto
    zuordnet; ausgenommen sind das Konto des Repository-Inhabers und Bot-Konten. Die Liste ist
    eindeutig nach Kennung: der Autor des Pull Requests zuerst, die übrigen in der Reihenfolge ihres
    ersten Auftretens. Ein Commit ohne Konto lässt sich durch keinen Kommentar abdecken und wird nur
    gezählt.
    """
    owner_id = _identifier(owner_id, "Konto des Repository-Inhabers")
    if not isinstance(pr, dict) or not isinstance(commits, (list, tuple)):
        raise ClaError("Pull Request oder Commits fehlen in der Antwort von GitHub.")
    accounts = [_account(pr.get("user"), "Autor des Pull Requests")]
    problems = 0
    for commit in commits:
        if not isinstance(commit, dict):
            raise ClaError("Ein Commit in der Antwort von GitHub ist kein Objekt.")
        author = commit.get("author")
        if author:
            accounts.append(_account(author, "Commit-Autor"))
        else:
            problems += 1
    signers = []
    seen = set()
    for account_id, login, kind in accounts:
        if account_id == owner_id or kind == "Bot" or account_id in seen:
            continue
        seen.add(account_id)
        signers.append((account_id, login))
    return signers, problems


def _is_comment(value):
    return (isinstance(value, dict) and _plain_int(value.get("id"))
            and isinstance(value.get("user"), dict) and _plain_int(value["user"].get("id")))


def find_consents(comments, signers, version):
    """Je Unterzeichner der älteste eigene Kommentar mit der Erklärung: ``{id: Kommentar}``.

    Maßgeblich ist die Kennung des Kontos, nie der Anmeldename. Kommentare ohne Konto (gelöschte
    Konten) und Kommentare anderer Konten zählen nicht.
    """
    if not isinstance(comments, (list, tuple)):
        raise ClaError("Die Kommentare fehlen in der Antwort von GitHub.")
    wanted = {account_id for account_id, _ in signers}
    found = {}
    for comment in sorted((c for c in comments if _is_comment(c)), key=lambda c: c["id"]):
        author_id = comment["user"]["id"]
        if author_id in wanted and author_id not in found and has_consent(comment.get("body"), version):
            found[author_id] = comment
    return found


def _clip(text):
    return text if len(text) <= DESCRIPTION_LIMIT else text[:DESCRIPTION_LIMIT - 1] + "…"


def _names(logins):
    shown = ", ".join(logins[:3])
    if len(logins) > 3:
        shown += f" und {len(logins) - 3} weitere"
    return shown


def _safe_url(url):
    """Die Adresse, wenn sie auf github.com zeigt und nichts enthält, was Anzeige oder Markdown stört; sonst ``None``."""
    if isinstance(url, str) and url.startswith("https://github.com/") and not _URL_UNSAFE.search(url):
        return url
    return None


def _timestamp(value):
    return value if isinstance(value, str) and _TIMESTAMP.fullmatch(value) else "unbekannt"


@dataclass
class Result:
    """Ergebnis der Prüfung; ``missing`` enthält bereinigte Anmeldenamen (``safe_login``)."""
    state: str
    description: str
    target_url: Optional[str] = None
    consents: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)


def unseen_commits(pr, commits):
    """Wie viele Commits des Pull Requests die Liste nicht enthält (GitHub liefert höchstens 250).

    Ihre Autoren sieht die Prüfung nicht – sie darf dann nicht bestehen. Nennt der Pull Request
    keine Anzahl, gilt die Liste als vollständig, solange sie die Grenze nicht erreicht.
    """
    total = pr.get("commits") if isinstance(pr, dict) else None
    if _plain_int(total):
        return max(0, total - len(commits))
    return 1 if len(commits) >= MAX_COMMITS else 0


def evaluate(pr, commits, comments, owner_id, version):
    """Ergebnis für einen Pull Request.

    ``target_url`` ist nur bei Erfolg mit Zustimmung gesetzt (der Kommentar des ersten Unterzeichners);
    sonst setzt der Aufrufer die Adresse von CLA.md.
    """
    signers, problems = required_signers(pr, commits, owner_id)
    consents = find_consents(comments, signers, version)
    missing = [safe_login(login) for account_id, login in signers if account_id not in consents]
    if unseen_commits(pr, commits):
        state = "failure"
        text = f"Mehr als {MAX_COMMITS} Commits – nicht vollständig prüfbar, bitte von Hand prüfen."
    elif problems:
        state = "failure"
        text = f"{problems} Commit(s) ohne zugeordnetes GitHub-Konto – bitte von Hand prüfen."
    elif missing:
        state = "failure"
        text = f"Zustimmung zu Version {version} fehlt: {_names(missing)}"
    elif not signers:
        state = "success"
        text = "Nicht erforderlich (Projektinhaber oder Bot)."
    else:
        state = "success"
        text = f"Zustimmung zu Version {version} liegt vor."
    target_url = None
    if state == "success" and signers:
        target_url = _safe_url(consents[signers[0][0]].get("html_url"))
    return Result(state, _clip(text), target_url, consents, missing)


def record_marker(comment_id):
    """Erste Zeile des Nachweises zu einem Zustimmungskommentar; daran erkennt der nächste Lauf ihn."""
    return f"<!-- cla-zustimmung:{_identifier(comment_id, 'Kommentar')} -->"


def record_body(signer_login, signer_id, comment, version, cla_sha256):
    """Text des Kommentars, der eine gefundene Zustimmung festhält.

    Enthält nur eigene Sätze und geprüfte Werte, nie Text aus dem Kommentar des Unterzeichners (der
    Satz kommt aus ``consent_sentences``) und keine @-Erwähnung.
    """
    if not isinstance(comment, dict):
        raise ClaError("Der Zustimmungskommentar fehlt.")
    comment_id = _identifier(comment.get("id"), "Kommentar")
    signer_id = _identifier(signer_id, "Unterzeichner")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ClaError("Ungültige Fassungsnummer.")
    if not isinstance(cla_sha256, str) or not _SHA256.fullmatch(cla_sha256):
        raise ClaError("Ungültige Prüfsumme von CLA.md.")
    sentence = matched_sentence(comment.get("body"), version)
    if sentence is None:
        raise ClaError("Der Kommentar enthält keine Zustimmung – es gibt nichts festzuhalten.")
    details = (f"ID {comment_id}, erstellt {_timestamp(comment.get('created_at'))}, "
               f"zuletzt geändert {_timestamp(comment.get('updated_at'))}")
    link = _safe_url(comment.get("html_url"))
    return "\n".join([
        record_marker(comment_id),
        "",
        "**Zustimmung zum CLA festgehalten**",
        "",
        f"- Konto: `{safe_login(signer_login)}` (GitHub-ID {signer_id})",
        f"- CLA-Fassung: {version} (SHA-256 von `CLA.md`: `{cla_sha256}`)",
        f"- Kommentar: {link} ({details})" if link else f"- Kommentar: {details}",
        f"- Erklärung: „{sentence}“",
    ])


def recorded_ids(comments, comment_ids):
    """Kennungen aus ``comment_ids``, zu denen ein Bot-Konto schon einen Nachweis geschrieben hat.

    Ein Kommentar mit dem Marker von einem Nicht-Bot zählt nicht: Jeder könnte ihn schreiben und so
    das Festhalten verhindern.
    """
    markers = {record_marker(comment_id): comment_id for comment_id in comment_ids}
    done = set()
    for comment in comments:
        if not (isinstance(comment, dict) and isinstance(comment.get("user"), dict)
                and comment["user"].get("type") == "Bot" and isinstance(comment.get("body"), str)):
            continue
        for marker, comment_id in markers.items():
            if comment["body"].startswith(marker):
                done.add(comment_id)
    return done


# ── Netzschicht ──────────────────────────────────────────────────────────────
def _valid_repo(repo):
    return (isinstance(repo, str) and bool(_REPO.fullmatch(repo))
            and not {".", ".."} & set(repo.split("/")))


def check_repo(repo):
    if not _valid_repo(repo):
        raise ClaError("Ungültiger Repository-Name (erwartet: Inhaber/Name).")
    return repo


def check_api_url(url):
    if not isinstance(url, str) or not _API_URL.fullmatch(url.rstrip("/")):
        raise ClaError("Die API-Adresse muss mit https:// beginnen und darf keine Sonderzeichen enthalten.")
    return url.rstrip("/")


def _check_number(number):
    if not _plain_int(number) or number < 1:
        raise ClaError("Ungültige Nummer des Pull Requests.")
    return number


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen sind ein Fehler: urllib würde das Token sonst an den neuen Host mitschicken."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(method, url, headers, data):
    """``(Status, Header, Body)``; HTTP-Fehlerstatus sind normale Antworten, Netzfehler werden ``ConnectionError``."""
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, dict(err.headers), err.read()
        finally:
            err.close()
    except (OSError, http.client.HTTPException, ValueError) as err:
        raise ConnectionError(type(err).__name__) from None


class GitHub:
    """Schmaler Client für die GitHub-REST-API: nur, was die Prüfung braucht.

    ``opener(method, url, headers, data)`` liefert ``(Status, Header, Body als Bytes)``; er ist
    austauschbar, damit Tests ohne Netz laufen.
    """

    def __init__(self, token, repo, api_url=API_URL, opener=None):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ClaError("Das Token fehlt oder enthält ungültige Zeichen.")
        self.repo = check_repo(repo)
        self.api_url = check_api_url(api_url)
        self._token = token
        self._opener = opener or _default_opener

    def _call(self, method, path, payload, expected):
        headers = {"Authorization": f"Bearer {self._token}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, _, body = self._opener(method, self.api_url + path, headers, data)
        except (OSError, http.client.HTTPException) as err:
            raise ClaError(f"GitHub-API: {method} {path} nicht erreichbar ({type(err).__name__}).") from None
        if not _plain_int(status):
            raise ClaError(f"GitHub-API: {method} {path} lieferte keinen Statuscode.")
        if not 200 <= status < 300:
            raise ClaError(f"GitHub-API: {method} {path} lieferte Status {status}.")
        try:
            result = json.loads(body.decode("utf-8"))
        except ValueError:
            raise ClaError(f"GitHub-API: {method} {path} lieferte kein JSON (Status {status}).") from None
        if not isinstance(result, expected):
            raise ClaError(f"GitHub-API: {method} {path} lieferte eine unerwartete Antwort (Status {status}).")
        return result

    def _pages(self, path, max_pages):
        """Eine Liste seitenweise lesen: ``(Einträge, vollständig)``.

        Unvollständig heißt: ``max_pages`` volle Seiten sind gelesen, weitere könnten folgen.
        """
        items = []
        for page in range(1, max_pages + 1):
            chunk = self._call("GET", f"{path}?per_page={PAGE_SIZE}&page={page}", None, list)
            items.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                return items, True
        return items, False

    def pull(self, number):
        return self._call("GET", f"/repos/{self.repo}/pulls/{_check_number(number)}", None, dict)

    def commits(self, number):
        items, _ = self._pages(f"/repos/{self.repo}/pulls/{_check_number(number)}/commits", MAX_COMMIT_PAGES)
        return items

    def comments(self, number):
        items, complete = self._pages(f"/repos/{self.repo}/issues/{_check_number(number)}/comments",
                                      MAX_COMMENT_PAGES)
        if not complete:
            raise ClaError(f"Mehr als {MAX_COMMENT_PAGES * PAGE_SIZE} Kommentare – nicht prüfbar.")
        return items

    def set_status(self, sha, state, description, target_url):
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ClaError("Ungültige Prüfsumme des Commits.")
        payload = {"state": state, "description": description, "context": STATUS_CONTEXT}
        if target_url:
            payload["target_url"] = target_url
        return self._call("POST", f"/repos/{self.repo}/statuses/{sha}", payload, dict)

    def add_comment(self, number, body):
        return self._call("POST", f"/repos/{self.repo}/issues/{_check_number(number)}/comments",
                          {"body": body}, dict)


# ── Ablauf ───────────────────────────────────────────────────────────────────
def pull_request_number(event):
    """Nummer des Pull Requests aus dem Ereignis, ``None`` wenn es keinen betrifft (Kommentar an einem Issue)."""
    if not isinstance(event, dict):
        raise ClaError("Die Ereignisdatei enthält kein JSON-Objekt.")
    pull = event.get("pull_request")
    if isinstance(pull, dict):
        return _check_number(pull.get("number"))
    issue = event.get("issue")
    if isinstance(issue, dict) and issue.get("pull_request"):
        return _check_number(issue.get("number"))
    return None


def _owner_id(event):
    repository = event.get("repository")
    owner = repository.get("owner") if isinstance(repository, dict) else None
    if not isinstance(owner, dict):
        raise ClaError("Das Ereignis nennt den Inhaber des Repositorys nicht.")
    return _identifier(owner.get("id"), "Konto des Repository-Inhabers")


def cla_url(event, env):
    """Adresse von CLA.md im Standard-Branch: Ziel des Status, solange es keinen Zustimmungskommentar gibt.

    ``None`` ohne brauchbaren Repository-Namen: Der Status kommt dann ohne Ziel – der Link ist nur Zierde
    und soll die Prüfung nicht kippen (``main`` hat den Namen schon geprüft).
    """
    repo = env.get("GITHUB_REPOSITORY")
    if not _valid_repo(repo):
        return None
    server = (env.get("GITHUB_SERVER_URL") or SERVER_URL).rstrip("/")
    if not _SERVER_URL.fullmatch(server):
        server = SERVER_URL
    repository = event.get("repository")
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch):
        branch = "main"
    return f"{server}/{repo}/blob/{branch}/CLA.md"


def run(event, env, github, cla_text):
    """Prüfung für ein Ereignis; 0 auch bei fehlender Zustimmung (das Ergebnis trägt der Status)."""
    number = pull_request_number(event)
    if number is None:
        _info("Das Ereignis gehört zu keinem Pull Request – nichts zu prüfen.")
        return 0
    version = cla_version(cla_text)
    cla_sha256 = hashlib.sha256(cla_text.encode("utf-8")).hexdigest()
    owner_id = _owner_id(event)
    fallback_url = cla_url(event, env)

    pr = github.pull(number)
    head = pr.get("head") if isinstance(pr, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    commits = github.commits(number)
    comments = github.comments(number)

    result = evaluate(pr, commits, comments, owner_id, version)
    github.set_status(sha, result.state, result.description, result.target_url or fallback_url)
    _info(f"Pull Request #{number}: {result.state} – {result.description}")

    signers, _ = required_signers(pr, commits, owner_id)
    done = recorded_ids(comments, [comment["id"] for comment in result.consents.values()])
    for account_id, login in signers:
        comment = result.consents.get(account_id)
        if comment is None or comment["id"] in done:
            continue
        github.add_comment(number, record_body(login, account_id, comment, version, cla_sha256))
        _info(f"Zustimmung von {safe_login(login)} (GitHub-ID {account_id}) festgehalten.")
    return 0


def _main(env):
    names = ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN")
    missing = [name for name in names if not env.get(name)]
    if missing:
        _log("Es fehlen Umgebungsvariablen: " + ", ".join(missing))
        return 1
    try:
        with open(env["GITHUB_EVENT_PATH"], "rb") as handle:
            event = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        raise ClaError("Die Ereignisdatei (GITHUB_EVENT_PATH) lässt sich nicht lesen.") from None
    try:
        cla_text = CLA_PATH.read_bytes().decode("utf-8")
    except (OSError, ValueError):
        raise ClaError("CLA.md lässt sich nicht als UTF-8-Text lesen.") from None
    github = GitHub(env["GITHUB_TOKEN"], env["GITHUB_REPOSITORY"], env.get("GITHUB_API_URL") or API_URL)
    return run(event, env, github, cla_text)


def main(env=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        return _main(os.environ if env is None else env)
    except ClaError as err:
        _log(str(err))
        return 1
    except Exception as err:             # noqa: BLE001 – ein Traceback könnte fremden Text enthalten
        frame = traceback.extract_tb(err.__traceback__)[-1]
        _log(f"Unerwarteter Fehler ({type(err).__name__}) in {Path(frame.filename).name}, "
             f"Funktion {frame.name}, Zeile {frame.lineno}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
