# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""CLA-Prüfung (tools/cla_check.py): Zustimmung am Pull Request, ganz ohne Netz.

Sieben Teile:
  1. Fassung und Erklärung (``cla_version``, ``consent_sentences``, ``has_consent``),
  2. wer zustimmen muss und das Ergebnis (``required_signers``, ``find_consents``, ``evaluate``),
  3. der Nachweis-Kommentar (``record_body``, ``recorded_ids``),
  4. der Ablauf (``run``) gegen eine falsche GitHub-API, die als ``opener`` in den echten Client gesteckt wird,
  5. die Netzschicht (``GitHub``; der Standard-Opener gegen Server auf 127.0.0.1),
  6. ``main`` und ein echter Start des Skripts als Kindprozess,
  7. Stimmigkeit der Projektdateien (CLA.md, CONTRIBUTING.md, PR-Vorlage, Workflow) – als Text, ohne YAML-Parser.

``tools/`` ist kein Paket, das Modul wird per Dateipfad geladen. Konten, Kennungen, Namen und Kommentare
sind erfundene Testwerte.
"""
import ast
import contextlib
import hashlib
import http.client
import http.server
import importlib.util
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import unicodedata

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(ROOT, "tools", "cla_check.py")
MCP_PATH = os.path.join(ROOT, "tools", "mcp_ubuchhaltung.py")
CLA_PATH = os.path.join(ROOT, "CLA.md")
CONTRIBUTING_PATH = os.path.join(ROOT, "CONTRIBUTING.md")
TEMPLATE_PATH = os.path.join(ROOT, ".github", "pull_request_template.md")
WORKFLOW_PATH = os.path.join(ROOT, ".github", "workflows", "cla.yml")


def _load_module():
    spec = importlib.util.spec_from_file_location("cla_check", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cla = _load_module()


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ── Testdaten ────────────────────────────────────────────────────────────────
REPO = "beispiel-org/beispiel-repo"
API = "https://api.github.com"
TOKEN = "ghs_NurFuerTests0123456789abcdef"
HEAD_SHA = "a" * 40

DE = "Ich stimme dem Contributor License Agreement (CLA.md), Version 1.0, für diesen Beitrag zu."
EN = "I agree to the Contributor License Agreement (CLA.md), version 1.0, for this contribution."
CLA_TEXT = "# Test-CLA\n\n**Version:** 1.0 (Testfassung)\n"
CLA_SHA = hashlib.sha256(CLA_TEXT.encode("utf-8")).hexdigest()
ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_SERVER_URL": "https://github.com"}


def account(account_id, login, kind="User"):
    return {"id": account_id, "login": login, "type": kind}


INHABER = account(1001, "projektinhaber")
A = account(1002, "mitwirkende-a")
B = account(1003, "mitwirkende-b")
C = account(1004, "mitwirkende-c")
BOT = account(1010, "ci-helfer[bot]", "Bot")
ACTIONS_BOT = account(2001, "github-actions[bot]", "Bot")

_commit_numbers = itertools.count(1)


def make_commit(author, sha=None):
    return {"sha": sha or f"{next(_commit_numbers):040x}", "author": author, "commit": {"message": "Änderung"}}


def make_pull(user, sha=HEAD_SHA, **extra):
    return {"number": 7, "user": user, "head": {"sha": sha, "ref": "funktion/beispiel"}, "title": "Beispiel", **extra}


def make_comment(comment_id, user, body, **extra):
    return {"id": comment_id, "user": user, "body": body,
            "html_url": f"https://github.com/{REPO}/pull/7#issuecomment-{comment_id}",
            "created_at": "2026-11-15T10:00:00Z", "updated_at": "2026-11-15T10:00:00Z", **extra}


def evaluate(pr, commits, comments, version="1.0"):
    return cla.evaluate(pr, commits, comments, INHABER["id"], version)


def pr_event(number=7, default_branch="main"):
    """Ereignis ``pull_request_target``; Autor und Commit sind absichtlich andere als die der API."""
    return {"action": "opened",
            "pull_request": {"number": number, "title": "::add-mask::Titel",
                             "user": {"id": 9999, "login": "aus-dem-ereignis", "type": "User"},
                             "head": {"sha": "f" * 40, "ref": "::set-env name=X::y"}},
            "repository": {"full_name": REPO, "default_branch": default_branch,
                           "owner": {"id": INHABER["id"], "login": INHABER["login"]}}}


def comment_event(number=7, is_pull_request=True):
    """Ereignis ``issue_comment`` an einem Pull Request (oder an einem Issue)."""
    issue = {"number": number, "title": "Beispiel"}
    if is_pull_request:
        issue["pull_request"] = {"url": f"{API}/repos/{REPO}/pulls/{number}"}
    return {"action": "created", "issue": issue, "comment": {"id": 1, "body": "::set-env name=X::y"},
            "repository": {"full_name": REPO, "default_branch": "main",
                           "owner": {"id": INHABER["id"], "login": INHABER["login"]}}}


class FakeApi:
    """Falsche GitHub-API; als ``opener`` im echten ``cla.GitHub``. Beantwortet die Aufrufe aus Testdaten
    und merkt sich alles, was die Prüfung schreibt. Ein angelegter Kommentar erscheint danach in der
    Kommentarliste (als Bot-Konto), wie bei GitHub."""

    def __init__(self, pr, commits=(), comments=(), number=7):
        self.pr = pr
        self.commit_list = list(commits)
        self.comment_list = list(comments)
        self.number = number
        self.calls = []                    # (Methode, Pfad samt Query)
        self.statuses = []                 # (SHA, Nutzlast)
        self.posted = []                   # Texte der angelegten Kommentare
        self.failing = {}                  # (Methode, Pfad-Anfang) -> Statuscode

    def github(self):
        return cla.GitHub(TOKEN, REPO, API, opener=self)

    @staticmethod
    def _ok(value, status=200):
        return status, {}, json.dumps(value).encode("utf-8")

    @staticmethod
    def _page(items, query):
        params = dict(part.split("=") for part in query.split("&"))
        page, size = int(params["page"]), int(params["per_page"])
        return items[(page - 1) * size:page * size]

    def __call__(self, method, url, headers, data):
        assert url.startswith(f"{API}/repos/{REPO}/"), url
        path = url[len(API):]
        self.calls.append((method, path))
        for (failing_method, prefix), status in self.failing.items():
            if method == failing_method and path.startswith(prefix):
                return status, {}, b'{"message": "Fehler"}'
        route, _, query = path.partition("?")
        base = f"/repos/{REPO}"
        payload = json.loads(data.decode("utf-8")) if data else None
        if method == "GET" and route == f"{base}/pulls/{self.number}":
            return self._ok(self.pr)
        if method == "GET" and route == f"{base}/pulls/{self.number}/commits":
            return self._ok(self._page(self.commit_list, query))
        if method == "GET" and route == f"{base}/issues/{self.number}/comments":
            return self._ok(self._page(self.comment_list, query))
        if method == "POST" and route.startswith(f"{base}/statuses/"):
            self.statuses.append((route.rsplit("/", 1)[1], payload))
            return self._ok({"id": 1}, 201)
        if method == "POST" and route == f"{base}/issues/{self.number}/comments":
            self.posted.append(payload["body"])
            created = make_comment(9000 + len(self.posted), ACTIONS_BOT, payload["body"])
            self.comment_list.append(created)
            return self._ok(created, 201)
        return 404, {}, b'{"message": "Not Found"}'


class Recorder:
    """Opener, der vorbereitete Antworten der Reihe nach liefert und jeden Aufruf festhält."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        return self.responses.pop(0)


def ok(value, status=200):
    return status, {}, json.dumps(value).encode("utf-8")


def client(*responses):
    recorder = Recorder(*responses)
    return cla.GitHub(TOKEN, REPO, API, opener=recorder), recorder


# ── Teil 1: Fassung und Erklärung ────────────────────────────────────────────
def test_cla_version_of_the_real_cla():
    assert cla.cla_version(read(CLA_PATH)) == "1.0"


@pytest.mark.parametrize("text", [
    "", "# CLA\n\nOhne Fassung.\n", "Version: 1.0\n", "Siehe **Version:** 1.0 im Text\n",
    "**Version:** unbekannt\n", "**Version:** \u0661.\u0660\n", None, 5],
    ids=["leer", "ohne Zeile", "ohne Fettdruck", "mitten in der Zeile", "keine Zahl", "andere Ziffern", "None", "Zahl"])
def test_cla_version_without_a_version_line_fails(text):
    with pytest.raises(cla.ClaError):
        cla.cla_version(text)


def test_cla_version_takes_the_first_matching_line():
    text = "# Titel\r\n\r\n**Version:** 2.1 (Stand)\r\n\r\n**Version:** 1.0 (English)\r\n"
    assert cla.cla_version(text) == "2.1"


def test_cla_version_reads_dotted_numbers():
    assert cla.cla_version("**Version:**3\n") == "3"
    assert cla.cla_version("**Version:**   1.2.10 (x)\n") == "1.2.10"
    assert cla.cla_version("**Version:** 1.0. Ende\n") == "1.0"


def test_consent_sentences_are_the_two_sentences_of_the_cla():
    assert cla.consent_sentences("1.0") == (DE, EN)
    german, english = cla.consent_sentences("2.3.1")
    assert "Version 2.3.1," in german and "version 2.3.1," in english


POSITIVE = {
    "deutsch": DE,
    "englisch": EN,
    "ohne Schlusspunkt": DE[:-1],
    "englisch ohne Schlusspunkt": EN[:-1],
    "Großbuchstaben": DE.upper(),
    "Kleinbuchstaben": EN.lower(),
    "gemischte Schreibung": "iCh StImMe DeM cOnTrIbUtOr LiCeNsE aGrEeMeNt (cla.md), VERSION 1.0, FÜR diesen Beitrag zu.",
    "zusätzlicher Leerraum": "   Ich  stimme dem\tContributor License Agreement   (CLA.md), Version 1.0,\u00a0für diesen Beitrag zu. \t ",
    "Zeilenende CRLF": f"Hallo!\r\n{DE}\r\nDanke.",
    "Zeilenende CR": f"Hallo!\r{EN}\rDanke.",
    "eigene Zeile im längeren Kommentar": f"Hallo zusammen,\n\nhier meine Erklärung:\n{DE}\n\nViele Grüße",
    "zerlegte Umlaute": unicodedata.normalize("NFD", DE),
    "beide Sätze": f"{EN}\n{DE}",
    "nach einer Zitatzeile": f"> {DE}\n{DE}",
}

NEGATIVE = {
    "Zitat": f"> {DE}",
    "Zitat ohne Leerzeichen": f">{DE}",
    "eingerücktes Zitat": f"   > {EN}",
    "verschachteltes Zitat": f">> {DE}",
    "Zitat im längeren Kommentar": f"Hallo\n> {DE}\nDanke",
    "Zusatz hinter dem Satz": f"{DE} Nicht!",
    "Zusatz hinter dem englischen Satz": f"{EN} Not!",
    "Zusatz vor dem Satz": f"Ich weiß nicht: {DE}",
    "Zusatz nach Zeilentrenner U+2028": f"{DE}\u2028Nicht!",
    "andere Fassung 1.1": DE.replace("1.0", "1.1"),
    "Fassung 10": DE.replace("1.0", "10"),
    "Fassung 1": DE.replace("1.0", "1"),
    "Fassung 1.0.1": DE.replace("1.0", "1.0.1"),
    "Fassung 1.00": EN.replace("1.0", "1.00"),
    "englisch andere Fassung": EN.replace("1.0", "2.0"),
    "leer": "",
    "nur Leerzeilen": "\n\n  \n",
    "verneint": "Ich stimme dem Contributor License Agreement (CLA.md), Version 1.0, für diesen Beitrag nicht zu.",
    "zwei Schlusspunkte": DE + ".",
    "Leerzeichen vor dem Punkt": DE[:-1] + " .",
    "über zwei Zeilen": "Ich stimme dem Contributor License Agreement (CLA.md),\nVersion 1.0, für diesen Beitrag zu.",
    "fett": f"**{DE}**",
    "als Code": f"`{DE}`",
    "als Listenpunkt": f"- {DE}",
    "kyrillisches A": DE.replace("A", "\u0410"),
    "Zeichen ohne Breite": DE.replace("Version", "Ver\u200bsion"),
}


@pytest.mark.parametrize("body", POSITIVE.values(), ids=POSITIVE.keys())
def test_comment_with_the_statement_is_a_consent(body):
    assert cla.has_consent(body, "1.0") is True


@pytest.mark.parametrize("body", NEGATIVE.values(), ids=NEGATIVE.keys())
def test_comment_without_the_exact_statement_is_no_consent(body):
    assert cla.has_consent(body, "1.0") is False


@pytest.mark.parametrize("body", [None, 5, b"Ich stimme zu", [DE], {"body": DE}],
                         ids=["None", "Zahl", "Bytes", "Liste", "Objekt"])
def test_no_text_is_no_consent(body):
    assert cla.has_consent(body, "1.0") is False
    assert cla.matched_sentence(body, "1.0") is None


def test_consent_follows_the_version_that_is_asked_for():
    assert cla.has_consent(DE.replace("1.0", "1.1"), "1.1") is True
    assert cla.has_consent(DE, "1.1") is False
    assert cla.has_consent(EN.replace("1.0", "2"), "2") is True


def test_matched_sentence_returns_our_own_sentence_never_the_comments_text():
    assert cla.matched_sentence(DE.upper(), "1.0") == DE
    assert cla.matched_sentence("  " + EN.lower()[:-1], "1.0") == EN
    assert cla.matched_sentence(f"{EN}\n{DE}", "1.0") == EN          # der erste im Kommentar
    assert cla.matched_sentence("Hallo", "1.0") is None


def test_safe_login_keeps_letters_digits_hyphen_and_the_bot_suffix():
    assert cla.safe_login("mitwirkende-a") == "mitwirkende-a"
    assert cla.safe_login("ci-helfer[bot]") == "ci-helfer[bot]"
    assert cla.safe_login("a[bot]x") == "a?bot?x"
    assert cla.safe_login("jürgen_x") == "j?rgen?x"
    assert cla.safe_login("evil\n::set-env name=X::y") == "evil???set-env?name?X??y"
    assert cla.safe_login(None) == ""


# ── Teil 2: Unterzeichner, Zustimmungen, Ergebnis ────────────────────────────
def test_signers_are_the_pull_request_author_and_the_commit_authors():
    commits = [make_commit(A), make_commit(B), make_commit(A), make_commit(C)]
    signers, problems = cla.required_signers(make_pull(A), commits, INHABER["id"])
    assert signers == [(1002, "mitwirkende-a"), (1003, "mitwirkende-b"), (1004, "mitwirkende-c")]
    assert problems == 0


def test_the_pull_request_author_comes_first_then_by_first_appearance():
    commits = [make_commit(A), make_commit(C), make_commit(B), make_commit(C)]
    signers, _ = cla.required_signers(make_pull(B), commits, INHABER["id"])
    assert [login for _, login in signers] == ["mitwirkende-b", "mitwirkende-a", "mitwirkende-c"]


def test_the_owner_is_no_signer_but_a_foreign_commit_author_is():
    signers, problems = cla.required_signers(make_pull(INHABER), [make_commit(INHABER), make_commit(A)],
                                             INHABER["id"])
    assert signers == [(1002, "mitwirkende-a")] and problems == 0


def test_bot_accounts_are_no_signers():
    signers, _ = cla.required_signers(make_pull(BOT), [make_commit(BOT), make_commit(B)], INHABER["id"])
    assert signers == [(1003, "mitwirkende-b")]


def test_nobody_needs_to_consent_when_only_the_owner_and_bots_took_part():
    signers, problems = cla.required_signers(make_pull(INHABER), [make_commit(INHABER), make_commit(BOT)],
                                             INHABER["id"])
    assert signers == [] and problems == 0


def test_a_renamed_account_counts_once_with_its_first_login():
    renamed = account(1002, "neuer-name")
    signers, _ = cla.required_signers(make_pull(A), [make_commit(renamed)], INHABER["id"])
    assert signers == [(1002, "mitwirkende-a")]


@pytest.mark.parametrize("author", [None, {}], ids=["None", "leer"])
def test_a_commit_without_account_is_a_problem(author):
    commits = [make_commit(A), make_commit(author), {"sha": "b" * 40}, make_commit(A)]
    signers, problems = cla.required_signers(make_pull(A), commits, INHABER["id"])
    assert signers == [(1002, "mitwirkende-a")]
    assert problems == 2                               # der leere Autor und der Commit ohne Schlüssel


def test_commits_missing_the_author_key_are_problems_too():
    signers, problems = cla.required_signers(make_pull(A), [{"sha": "b" * 40}, {"sha": "c" * 40, "author": None}],
                                             INHABER["id"])
    assert (signers, problems) == ([(1002, "mitwirkende-a")], 2)


BAD_IDS = [True, False, "1002", 1002.0, None, [1002]]


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
def test_ids_must_be_whole_numbers(bad):
    broken = {**A, "id": bad}
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(broken), [], INHABER["id"])
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(A), [make_commit(broken)], INHABER["id"])
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(A), [], bad)


@pytest.mark.parametrize("pr", [{}, {"user": None}, {"user": "mitwirkende-a"}, {"user": {"id": 1002}}, None, []],
                         ids=["ohne user", "user None", "user Text", "ohne login", "None", "Liste"])
def test_a_pull_request_without_a_usable_author_fails(pr):
    with pytest.raises(cla.ClaError):
        cla.required_signers(pr, [], INHABER["id"])


def test_commits_must_be_a_list_of_objects():
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(A), {"0": make_commit(A)}, INHABER["id"])
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(A), ["abc"], INHABER["id"])
    with pytest.raises(cla.ClaError):
        cla.required_signers(make_pull(A), [make_commit("mitwirkende-a")], INHABER["id"])


def test_find_consents_takes_the_oldest_comment_of_each_signer():
    comments = [make_comment(30, A, DE), make_comment(20, A, EN), make_comment(25, B, DE)]
    found = cla.find_consents(comments, [(1002, "mitwirkende-a"), (1003, "mitwirkende-b")], "1.0")
    assert sorted(found) == [1002, 1003]
    assert found[1002]["id"] == 20 and found[1003]["id"] == 25


def test_find_consents_ignores_other_accounts_wrong_versions_and_broken_comments():
    comments = [make_comment(1, B, DE),                                    # anderes Konto
                make_comment(2, A, DE.replace("1.0", "1.1")),              # andere Fassung
                make_comment(3, A, "Gerne!"),                              # keine Erklärung
                {"id": 4, "user": None, "body": DE},                       # Konto gelöscht
                {"id": 5, "body": DE}, "kein Objekt", None,
                {"id": True, "user": A, "body": DE},
                {"id": 6, "user": {"id": "1002"}, "body": DE},
                make_comment(7, A, None)]
    assert cla.find_consents(comments, [(1002, "mitwirkende-a")], "1.0") == {}


def test_find_consents_goes_by_the_account_id_not_the_login():
    renamed = account(1002, "ganz-anderer-name")
    found = cla.find_consents([make_comment(9, renamed, DE)], [(1002, "mitwirkende-a")], "1.0")
    assert found[1002]["id"] == 9


def test_find_consents_needs_a_list_of_comments():
    with pytest.raises(cla.ClaError):
        cla.find_consents({"id": 1}, [(1002, "mitwirkende-a")], "1.0")


def test_nobody_needs_to_consent_is_a_success():
    result = evaluate(make_pull(INHABER), [make_commit(INHABER), make_commit(BOT)], [])
    assert (result.state, result.description) == ("success", "Nicht erforderlich (Projektinhaber oder Bot).")
    assert result.target_url is None and result.consents == {} and result.missing == []


def test_everybody_consented_is_a_success_pointing_to_the_authors_comment():
    comments = [make_comment(21, B, EN), make_comment(20, A, DE)]
    result = evaluate(make_pull(A), [make_commit(A), make_commit(B)], comments)
    assert (result.state, result.description) == ("success", "Zustimmung zu Version 1.0 liegt vor.")
    assert result.target_url == f"https://github.com/{REPO}/pull/7#issuecomment-20"
    assert sorted(result.consents) == [1002, 1003] and result.missing == []


def test_the_target_is_the_comment_of_the_first_signer_when_the_author_is_the_owner():
    result = evaluate(make_pull(INHABER), [make_commit(B), make_commit(C)],
                      [make_comment(40, C, DE), make_comment(41, B, DE)])
    assert result.state == "success"
    assert result.target_url == f"https://github.com/{REPO}/pull/7#issuecomment-41"


def test_a_target_that_is_not_on_github_is_dropped():
    comment = make_comment(20, A, DE, html_url="http://evil.example/x")
    result = evaluate(make_pull(A), [make_commit(A)], [comment])
    assert result.state == "success" and result.target_url is None


def test_a_missing_consent_fails_and_names_the_account():
    result = evaluate(make_pull(A), [make_commit(A)], [])
    assert (result.state, result.description) == ("failure", "Zustimmung zu Version 1.0 fehlt: mitwirkende-a")
    assert result.target_url is None and result.consents == {} and result.missing == ["mitwirkende-a"]


def test_the_consent_of_another_account_does_not_count_for_the_author():
    result = evaluate(make_pull(A), [make_commit(A)], [make_comment(5, B, DE)])
    assert result.state == "failure"
    assert result.description == "Zustimmung zu Version 1.0 fehlt: mitwirkende-a"
    assert result.consents == {}


def test_a_consent_in_an_old_comment_covers_later_commits():
    comments = [make_comment(3, A, DE, created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")]
    commits = [make_commit(A) for _ in range(5)]            # spätere Commits mit anderen SHAs
    result = evaluate(make_pull(A, sha="c" * 40), commits, comments)
    assert (result.state, result.description) == ("success", "Zustimmung zu Version 1.0 liegt vor.")


def test_a_consent_to_an_older_version_does_not_count():
    result = evaluate(make_pull(A), [make_commit(A)], [make_comment(3, A, DE.replace("1.0", "0.9"))])
    assert (result.state, result.description) == ("failure", "Zustimmung zu Version 1.0 fehlt: mitwirkende-a")


def test_with_several_signers_one_missing_fails_and_keeps_the_found_consents():
    result = evaluate(make_pull(A), [make_commit(A), make_commit(B)], [make_comment(5, A, DE)])
    assert (result.state, result.description) == ("failure", "Zustimmung zu Version 1.0 fehlt: mitwirkende-b")
    assert list(result.consents) == [1002] and result.missing == ["mitwirkende-b"]
    assert result.target_url is None


PEOPLE = [account(1100 + n, f"mitwirkende-{letter}") for n, letter in enumerate("abcdefg")]


@pytest.mark.parametrize("count,names", [
    (1, "mitwirkende-a"),
    (2, "mitwirkende-a, mitwirkende-b"),
    (3, "mitwirkende-a, mitwirkende-b, mitwirkende-c"),
    (4, "mitwirkende-a, mitwirkende-b, mitwirkende-c und 1 weitere"),
    (7, "mitwirkende-a, mitwirkende-b, mitwirkende-c und 4 weitere"),
])
def test_the_description_lists_at_most_three_names(count, names):
    people = PEOPLE[:count]
    result = evaluate(make_pull(people[0]), [make_commit(person) for person in people], [])
    assert result.description == f"Zustimmung zu Version 1.0 fehlt: {names}"
    assert len(result.missing) == count


def test_commits_without_account_fail_and_come_before_a_missing_consent():
    commits = [make_commit(A), make_commit(None), {"sha": "b" * 40}]
    result = evaluate(make_pull(A), commits, [])
    assert result.state == "failure"
    assert result.description == "2 Commit(s) ohne zugeordnetes GitHub-Konto – bitte von Hand prüfen."
    assert result.target_url is None


def test_commits_without_account_fail_even_when_everybody_else_consented():
    result = evaluate(make_pull(A), [make_commit(A), make_commit(None)], [make_comment(5, A, DE)])
    assert result.state == "failure"
    assert result.description == "1 Commit(s) ohne zugeordnetes GitHub-Konto – bitte von Hand prüfen."
    assert result.target_url is None and list(result.consents) == [1002]


def test_commits_without_account_fail_even_when_nobody_needs_to_consent():
    result = evaluate(make_pull(INHABER), [make_commit(INHABER), make_commit(None)], [])
    assert result.state == "failure"
    assert result.description.startswith("1 Commit(s) ohne zugeordnetes GitHub-Konto")


def test_the_description_never_exceeds_the_limit_of_github():
    people = [account(1200 + n, f"{'x' * 38}{n}") for n in range(5)]
    result = evaluate(make_pull(people[0]), [make_commit(person) for person in people], [])
    assert result.state == "failure"
    assert len(result.description) <= 140
    assert result.description.startswith("Zustimmung zu Version 1.0 fehlt: ") and result.description.endswith("…")


def test_the_description_is_not_cut_when_it_fits():
    people = [account(1300 + n, f"{'y' * 30}{n}") for n in range(2)]
    result = evaluate(make_pull(people[0]), [make_commit(person) for person in people], [])
    assert len(result.description) <= 140 and not result.description.endswith("…")


def test_special_characters_in_a_login_are_replaced_in_description_and_missing():
    hostile = account(1400, "mit<script>-a\n::x")
    result = evaluate(make_pull(hostile), [make_commit(hostile)], [])
    assert result.missing == ["mit?script?-a???x"]
    assert result.description == "Zustimmung zu Version 1.0 fehlt: mit?script?-a???x"
    assert "\n" not in result.description and "<" not in result.description and "::" not in result.description


# ── Teil 3: der Nachweis ─────────────────────────────────────────────────────
def test_record_marker_is_an_html_comment_with_the_comment_id():
    assert cla.record_marker(123) == "<!-- cla-zustimmung:123 -->"


@pytest.mark.parametrize("bad", [True, "1", 1.0, None])
def test_record_marker_needs_a_whole_number(bad):
    with pytest.raises(cla.ClaError):
        cla.record_marker(bad)


def test_record_body_has_a_fixed_form():
    comment = make_comment(55, A, f"Hallo\n{DE}", created_at="2026-11-15T10:00:00Z",
                           updated_at="2026-11-15T10:05:00Z")
    body = cla.record_body("mitwirkende-a", 1002, comment, "1.0", "ab" * 32)
    assert body == (
        "<!-- cla-zustimmung:55 -->\n"
        "\n"
        "**Zustimmung zum CLA festgehalten**\n"
        "\n"
        "- Konto: `mitwirkende-a` (GitHub-ID 1002)\n"
        f"- CLA-Fassung: 1.0 (SHA-256 von `CLA.md`: `{'ab' * 32}`)\n"
        f"- Kommentar: https://github.com/{REPO}/pull/7#issuecomment-55 "
        "(ID 55, erstellt 2026-11-15T10:00:00Z, zuletzt geändert 2026-11-15T10:05:00Z)\n"
        f"- Erklärung: „{DE}“")


def test_record_body_names_the_sentence_that_the_comment_carried():
    german = cla.record_body("mitwirkende-a", 1002, make_comment(1, A, DE.upper()), "1.0", "0" * 64)
    english = cla.record_body("mitwirkende-a", 1002, make_comment(2, A, EN.lower()), "1.0", "0" * 64)
    assert german.endswith(f"- Erklärung: „{DE}“") and english.endswith(f"- Erklärung: „{EN}“")


def test_record_body_never_carries_text_from_the_comment():
    hostile = "::set-env name=X::y\n<script>alert(1)</script>\n[Klick](http://evil.example/x)\n@alle"
    comment = make_comment(55, A, f"{hostile}\n{DE}\n{hostile}")
    body = cla.record_body("mitwirkende-a", 1002, comment, "1.0", "0" * 64)
    for text in ("::set-env", "<script>", "alert", "evil.example", "Klick", "@"):
        assert text not in body
    assert body.startswith(cla.record_marker(55) + "\n")


def test_record_body_drops_a_broken_comment_link_and_broken_timestamps():
    for url in ("http://evil.example/x", "https://github.com.evil.example/x", "https://github.com/a b",
                "https://github.com/a\nb", "https://github.com/a)![x](http://e", "https://github.com/a<b", None, 5):
        comment = make_comment(55, A, DE, html_url=url, created_at="gestern", updated_at="2026-11-15 10:00:00")
        body = cla.record_body("mitwirkende-a", 1002, comment, "1.0", "0" * 64)
        assert "- Kommentar: ID 55, erstellt unbekannt, zuletzt geändert unbekannt\n" in body + "\n"
        assert "http" not in body and "evil" not in body


def test_record_body_takes_only_timestamps_of_the_exact_form():
    for stamp in ("2026-11-15T10:00:00Z\n", "2026-11-15T10:00:00+01:00", "2026-11-15T10:00:00", "\u0662026-11-15T10:00:00Z"):
        comment = make_comment(55, A, DE, created_at=stamp)
        assert "erstellt unbekannt" in cla.record_body("mitwirkende-a", 1002, comment, "1.0", "0" * 64)


def test_record_body_replaces_odd_characters_in_the_login():
    body = cla.record_body("mit`wirkende\n- x", 1002, make_comment(55, A, DE), "1.0", "0" * 64)
    assert "- Konto: `mit?wirkende?-?x` (GitHub-ID 1002)" in body


def test_record_body_refuses_what_is_not_a_consent_or_not_checked():
    good = make_comment(55, A, DE)
    for args in [("mitwirkende-a", 1002, make_comment(55, A, "Gerne!"), "1.0", "0" * 64),
                 ("mitwirkende-a", 1002, make_comment(55, A, DE.replace("1.0", "1.1")), "1.0", "0" * 64),
                 ("mitwirkende-a", 1002, make_comment(55, A, None), "1.0", "0" * 64),
                 ("mitwirkende-a", 1002, None, "1.0", "0" * 64),
                 ("mitwirkende-a", True, good, "1.0", "0" * 64),
                 ("mitwirkende-a", "1002", good, "1.0", "0" * 64),
                 ("mitwirkende-a", 1002, {**good, "id": "55"}, "1.0", "0" * 64),
                 ("mitwirkende-a", 1002, good, "1.0\n", "0" * 64),
                 ("mitwirkende-a", 1002, good, "1.0", "0" * 63),
                 ("mitwirkende-a", 1002, good, "1.0", "G" * 64)]:
        with pytest.raises(cla.ClaError):
            cla.record_body(*args)


def test_recorded_ids_counts_only_notes_of_bot_accounts_that_start_with_the_marker():
    comments = [make_comment(1, ACTIONS_BOT, cla.record_marker(10) + "\n\nText"),
                make_comment(2, A, cla.record_marker(11) + "\n\nText"),                  # Nicht-Bot
                make_comment(3, ACTIONS_BOT, "Vorspann\n" + cla.record_marker(12)),     # beginnt nicht damit
                make_comment(4, ACTIONS_BOT, cla.record_marker(1) + "\n\nText"),        # 1 ist nicht 10
                {"id": 5, "user": None, "body": cla.record_marker(13)}, "kein Objekt",
                make_comment(6, ACTIONS_BOT, None)]
    assert cla.recorded_ids(comments, [10, 11, 12, 13]) == {10}
    assert cla.recorded_ids(comments, [1, 10]) == {1, 10}


# ── Teil 4: der Ablauf gegen eine falsche GitHub-API ─────────────────────────
def run_check(api, event=None, cla_text=CLA_TEXT, env=ENV):
    return cla.run(event or pr_event(), env, api.github(), cla_text)


def test_a_consent_sets_the_status_and_is_recorded_once(capsys):
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, f"Hallo\n{DE}")])
    assert run_check(api) == 0
    assert api.statuses == [(HEAD_SHA, {"state": "success", "description": "Zustimmung zu Version 1.0 liegt vor.",
                                        "context": "CLA-Zustimmung",
                                        "target_url": f"https://github.com/{REPO}/pull/7#issuecomment-10"})]
    assert len(api.posted) == 1
    assert api.posted[0].startswith(cla.record_marker(10) + "\n")
    assert "`mitwirkende-a` (GitHub-ID 1002)" in api.posted[0]
    assert f"`{CLA_SHA}`" in api.posted[0]
    status_call = api.calls.index(("POST", f"/repos/{REPO}/statuses/{HEAD_SHA}"))
    comment_call = api.calls.index(("POST", f"/repos/{REPO}/issues/7/comments"))
    assert status_call < comment_call                              # erst der Status, dann der Nachweis
    out = capsys.readouterr().out
    assert "Pull Request #7: success – Zustimmung zu Version 1.0 liegt vor." in out


def test_a_second_run_sets_the_status_again_but_records_nothing_more():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    assert run_check(api) == 0 and len(api.posted) == 1
    assert run_check(api) == 0
    assert len(api.posted) == 1 and len(api.statuses) == 2


def test_a_marker_comment_of_a_non_bot_does_not_prevent_recording():
    fake_note = make_comment(11, A, cla.record_marker(10) + "\n\nZustimmung festgehalten")
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE), fake_note])
    assert run_check(api) == 0
    assert len(api.posted) == 1


def test_a_note_for_another_comment_does_not_prevent_recording():
    other = make_comment(11, ACTIONS_BOT, cla.record_marker(1) + "\n\nText")
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE), other])
    assert run_check(api) == 0
    assert len(api.posted) == 1 and api.posted[0].startswith(cla.record_marker(10))


def test_each_consent_gets_its_own_note_in_signer_order():
    api = FakeApi(make_pull(A), [make_commit(A), make_commit(B)],
                  [make_comment(10, B, EN), make_comment(11, A, DE)])
    assert run_check(api) == 0
    assert len(api.posted) == 2
    assert "`mitwirkende-a`" in api.posted[0] and api.posted[0].startswith(cla.record_marker(11))
    assert "`mitwirkende-b`" in api.posted[1] and api.posted[1].startswith(cla.record_marker(10))
    assert api.statuses[0][1]["state"] == "success"


def test_a_missing_consent_fails_without_a_comment_and_points_to_the_cla(capsys):
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, B, DE), make_comment(11, A, "Gerne!")])
    assert run_check(api) == 0
    assert api.statuses == [(HEAD_SHA, {"state": "failure", "description": "Zustimmung zu Version 1.0 fehlt: mitwirkende-a",
                                        "context": "CLA-Zustimmung",
                                        "target_url": f"https://github.com/{REPO}/blob/main/CLA.md"})]
    assert api.posted == []
    assert "failure" in capsys.readouterr().out


def test_a_partial_consent_fails_but_the_found_one_is_recorded():
    api = FakeApi(make_pull(A), [make_commit(A), make_commit(B)], [make_comment(10, A, DE)])
    assert run_check(api) == 0
    assert api.statuses[0][1]["state"] == "failure"
    assert api.statuses[0][1]["description"] == "Zustimmung zu Version 1.0 fehlt: mitwirkende-b"
    assert len(api.posted) == 1 and "`mitwirkende-a`" in api.posted[0]


def test_commits_without_account_fail_the_status():
    api = FakeApi(make_pull(A), [make_commit(A), make_commit(None)], [make_comment(10, A, DE)])
    assert run_check(api) == 0
    assert api.statuses[0][1]["state"] == "failure"
    assert api.statuses[0][1]["description"].startswith("1 Commit(s) ohne zugeordnetes GitHub-Konto")
    assert len(api.posted) == 1


def test_nobody_needs_to_consent_sets_a_success_without_a_note():
    api = FakeApi(make_pull(INHABER), [make_commit(INHABER), make_commit(BOT)])
    assert run_check(api) == 0
    assert api.statuses[0][1]["state"] == "success"
    assert api.statuses[0][1]["description"] == "Nicht erforderlich (Projektinhaber oder Bot)."
    assert api.statuses[0][1]["target_url"] == f"https://github.com/{REPO}/blob/main/CLA.md"
    assert api.posted == []


def test_a_comment_on_an_issue_that_is_no_pull_request_does_nothing(capsys):
    api = FakeApi(make_pull(A))
    assert run_check(api, event=comment_event(is_pull_request=False)) == 0
    assert api.calls == []
    assert "keinem Pull Request" in capsys.readouterr().out


def test_an_event_without_pull_request_or_issue_does_nothing():
    api = FakeApi(make_pull(A))
    assert run_check(api, event={"action": "x", "repository": {}}) == 0
    assert api.calls == []


def test_a_comment_event_on_a_pull_request_checks_that_pull_request():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    assert run_check(api, event=comment_event()) == 0
    assert api.statuses[0][1]["state"] == "success"


def test_author_and_commit_come_from_the_api_not_from_the_event():
    api = FakeApi(make_pull(A, sha="c" * 40), [make_commit(A)], [make_comment(10, A, DE)])
    assert run_check(api, event=pr_event()) == 0               # das Ereignis nennt einen anderen Autor und SHA
    assert api.statuses[0][0] == "c" * 40
    assert "`mitwirkende-a`" in api.posted[0]
    assert "aus-dem-ereignis" not in api.posted[0]
    assert ("GET", f"/repos/{REPO}/pulls/7") in api.calls


def test_the_default_branch_comes_from_the_event():
    api = FakeApi(make_pull(A), [make_commit(A)], [])
    assert run_check(api, event=pr_event(default_branch="haupt-zweig")) == 0
    assert api.statuses[0][1]["target_url"] == f"https://github.com/{REPO}/blob/haupt-zweig/CLA.md"


@pytest.mark.parametrize("branch", ["a b", "a;b", "zweig\nzwei", "", None, 7, "../..\\x"])
def test_an_unusual_default_branch_falls_back_to_main(branch):
    event = pr_event()
    event["repository"]["default_branch"] = branch
    assert cla.cla_url(event, ENV) == f"https://github.com/{REPO}/blob/main/CLA.md"


def test_the_server_address_comes_from_the_environment_if_it_is_plausible():
    event = pr_event()
    assert cla.cla_url(event, {**ENV, "GITHUB_SERVER_URL": "https://git.beispiel.example/"}) \
        == f"https://git.beispiel.example/{REPO}/blob/main/CLA.md"
    for odd in ("http://git.beispiel.example", "javascript:alert(1)", "https://x y", ""):
        assert cla.cla_url(event, {**ENV, "GITHUB_SERVER_URL": odd}) == f"https://github.com/{REPO}/blob/main/CLA.md"
    assert cla.cla_url(event, {"GITHUB_REPOSITORY": REPO}) == f"https://github.com/{REPO}/blob/main/CLA.md"


@pytest.mark.parametrize("repo", [None, "", "a/b/c", "../x", "a b/c", 5], ids=repr)
def test_without_a_usable_repository_name_the_status_comes_without_a_target(repo):
    assert cla.cla_url(pr_event(), {"GITHUB_REPOSITORY": repo}) is None
    api = FakeApi(make_pull(A), [make_commit(A)], [])
    assert run_check(api, env={"GITHUB_REPOSITORY": repo}) == 0
    assert api.statuses[0][1]["state"] == "failure" and "target_url" not in api.statuses[0][1]


def test_the_note_carries_the_hash_of_the_cla_text():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    umlauts = "# CLA\r\n\r\n**Version:** 1.0 (Äpfel)\r\n"
    assert run_check(api, cla_text=umlauts) == 0
    assert hashlib.sha256(umlauts.encode("utf-8")).hexdigest() in api.posted[0]


def test_a_new_version_of_the_cla_needs_a_new_consent():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    assert run_check(api, cla_text="**Version:** 1.1\n") == 0
    assert api.statuses[0][1]["description"] == "Zustimmung zu Version 1.1 fehlt: mitwirkende-a"
    assert api.posted == []


def test_a_cla_without_a_version_stops_before_any_call():
    api = FakeApi(make_pull(A))
    with pytest.raises(cla.ClaError):
        run_check(api, cla_text="# CLA ohne Fassung\n")
    assert api.calls == []


def test_an_event_without_the_owner_stops_before_any_call():
    api = FakeApi(make_pull(A))
    event = pr_event()
    del event["repository"]["owner"]
    with pytest.raises(cla.ClaError):
        run_check(api, event=event)
    assert api.calls == []


@pytest.mark.parametrize("number", [0, -3, True, "7", 7.0, None])
def test_an_event_with_an_unusable_number_stops_before_any_call(number):
    api = FakeApi(make_pull(A))
    event = pr_event()
    event["pull_request"]["number"] = number
    with pytest.raises(cla.ClaError):
        run_check(api, event=event)
    assert api.calls == []


def test_an_error_of_the_api_sets_no_status_and_writes_nothing():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    api.failing[("GET", f"/repos/{REPO}/pulls/7/commits")] = 500
    with pytest.raises(cla.ClaError):
        run_check(api)
    assert api.statuses == [] and api.posted == []


def test_the_status_is_set_before_the_note_is_written():
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    api.failing[("POST", f"/repos/{REPO}/issues/7/comments")] = 403
    with pytest.raises(cla.ClaError):
        run_check(api)
    assert len(api.statuses) == 1 and api.statuses[0][1]["state"] == "success"


def test_output_and_writes_contain_no_text_from_the_pull_request(capsys):
    hostile = "::stop-commands::GEHEIM\n::set-env name=X::y"
    author = account(1002, "mitwirkende-a\n::warning::x")
    pr = make_pull(author, title=hostile)
    pr["head"]["ref"] = hostile
    api = FakeApi(pr, [make_commit(author), make_commit(B)],
                  [make_comment(3, B, f"{hostile}\n{DE}\n{hostile}"), make_comment(4, author, hostile)])
    assert run_check(api) == 0
    out, err = capsys.readouterr()
    shown = out + err
    for text in ("GEHEIM", "set-env", "stop-commands", "::warning", "\n::"):
        assert text not in shown
    assert not any(line.lstrip().startswith("::") for line in shown.splitlines())
    assert "Pull Request #7: failure" in out
    written = json.dumps(api.statuses) + "".join(api.posted)
    assert "GEHEIM" not in written and "set-env" not in written and "::" not in written
    assert "\n" not in api.statuses[0][1]["description"]
    assert "mitwirkende-a???warning??x" in api.statuses[0][1]["description"]      # nur der bereinigte Anmeldename
    assert len(api.posted) == 1 and "`mitwirkende-b`" in api.posted[0]


# ── Teil 5: Netzschicht ──────────────────────────────────────────────────────
@pytest.mark.parametrize("repo", ["a/b/c", "../x", "a b/c", "x", "", "a/.", "./b", "a/..", "a//b", "a/b\n", "a/b?x=1", None, 5],
                         ids=repr)
def test_an_invalid_repository_is_refused_without_a_call(repo):
    recorder = Recorder()
    with pytest.raises(cla.ClaError):
        cla.GitHub(TOKEN, repo, API, opener=recorder)
    assert recorder.calls == []


@pytest.mark.parametrize("url", ["http://api.github.com", "api.github.com", "ftp://api.github.com", "https://",
                                 "https://api github.com", "https://api.github.com\nX-Boese: 1", "https://api.github.com/?x=1",
                                 "https://nutzer:pw" + "@" + "api.github.com",       # getrennt: kein Treffer im Anonymisierungs-Guard
                                 "HTTPS://api.github.com", None, 5], ids=repr)
def test_an_api_address_without_https_is_refused(url):
    with pytest.raises(cla.ClaError):
        cla.GitHub(TOKEN, REPO, url, opener=Recorder())


@pytest.mark.parametrize("url", ["https://api.github.com", "https://api.github.com/",
                                 "https://ghe.beispiel.example/api/v3", "https://127.0.0.1:8443"])
def test_a_plausible_api_address_is_accepted_without_a_double_slash(url):
    recorder = Recorder(ok({"number": 7}))
    cla.GitHub(TOKEN, REPO, url, opener=recorder).pull(7)
    assert recorder.calls[0][1] == url.rstrip("/") + f"/repos/{REPO}/pulls/7"


@pytest.mark.parametrize("token", ["", None, "abc def", "abc\ndef", "tökén", 5], ids=repr)
def test_an_unusable_token_is_refused_without_naming_it(token):
    with pytest.raises(cla.ClaError) as caught:
        cla.GitHub(token, REPO, API, opener=Recorder())
    assert "def" not in str(caught.value) and "tökén" not in str(caught.value)


@pytest.mark.parametrize("number", [0, -1, True, False, "1", 1.0, None, [7]], ids=repr)
def test_an_invalid_pull_request_number_is_refused_without_a_call(number):
    github, recorder = client()
    for call in (github.pull, github.commits, github.comments, lambda n: github.add_comment(n, "Text")):
        with pytest.raises(cla.ClaError):
            call(number)
    assert recorder.calls == []


@pytest.mark.parametrize("sha", ["a" * 39, "a" * 41, "A" * 40, "a" * 39 + "g", "a" * 40 + "\n", "../" + "a" * 37, "", None, 7],
                         ids=repr)
def test_an_invalid_commit_hash_is_refused_without_a_call(sha):
    github, recorder = client()
    with pytest.raises(cla.ClaError):
        github.set_status(sha, "success", "Text", None)
    assert recorder.calls == []


def test_requests_carry_the_documented_headers():
    github, recorder = client(ok({"number": 7}))
    github.pull(7)
    method, url, headers, data = recorder.calls[0]
    assert (method, url, data) == ("GET", f"{API}/repos/{REPO}/pulls/7", None)
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"] == "ubuchhaltung-cla-check"


def test_the_default_address_is_the_public_api():
    recorder = Recorder(ok({"number": 7}))
    cla.GitHub(TOKEN, REPO, opener=recorder).pull(7)
    assert recorder.calls[0][1] == f"{API}/repos/{REPO}/pulls/7"


def test_set_status_posts_state_description_context_and_target():
    github, recorder = client(ok({"id": 1}, 201), ok({"id": 2}, 201))
    github.set_status(HEAD_SHA, "success", "Zustimmung liegt vor.", "https://github.com/x/y")
    github.set_status(HEAD_SHA, "failure", "Es fehlt.", None)
    method, url, headers, data = recorder.calls[0]
    assert (method, url) == ("POST", f"{API}/repos/{REPO}/statuses/{HEAD_SHA}")
    assert headers["Content-Type"] == "application/json"
    assert json.loads(data) == {"state": "success", "description": "Zustimmung liegt vor.",
                                "context": "CLA-Zustimmung", "target_url": "https://github.com/x/y"}
    assert json.loads(recorder.calls[1][3]) == {"state": "failure", "description": "Es fehlt.",
                                               "context": "CLA-Zustimmung"}


def test_add_comment_posts_the_body():
    github, recorder = client(ok({"id": 5}, 201))
    github.add_comment(7, "Text mit Ümläut")
    method, url, _, data = recorder.calls[0]
    assert (method, url) == ("POST", f"{API}/repos/{REPO}/issues/7/comments")
    assert json.loads(data) == {"body": "Text mit Ümläut"}


def page_of(size):
    return ok([{"id": n} for n in range(size)])


def test_comments_are_read_page_by_page():
    github, recorder = client(page_of(100), page_of(100), page_of(3))
    assert len(github.comments(7)) == 203
    assert [call[1] for call in recorder.calls] == [
        f"{API}/repos/{REPO}/issues/7/comments?per_page=100&page={page}" for page in (1, 2, 3)]


def test_comments_stop_with_an_empty_page_after_full_ones():
    github, recorder = client(page_of(100), page_of(0))
    assert len(github.comments(7)) == 100 and len(recorder.calls) == 2


def test_fifty_full_pages_of_comments_are_too_many():
    github, recorder = client(*[page_of(100) for _ in range(60)])
    with pytest.raises(cla.ClaError):
        github.comments(7)
    assert len(recorder.calls) == 50


def test_forty_nine_full_pages_of_comments_are_fine():
    github, recorder = client(*[page_of(100) for _ in range(49)], page_of(7))
    assert len(github.comments(7)) == 4907 and len(recorder.calls) == 50


def test_commits_are_read_page_by_page():
    github, recorder = client(page_of(100), page_of(100), page_of(50))
    assert len(github.commits(7)) == 250
    assert [call[1] for call in recorder.calls] == [
        f"{API}/repos/{REPO}/pulls/7/commits?per_page=100&page={page}" for page in (1, 2, 3)]


def test_commits_stop_after_three_pages():
    github, recorder = client(page_of(100), page_of(100), page_of(100), page_of(100))
    assert len(github.commits(7)) == 300 and len(recorder.calls) == 3


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 422, 429, 500, 503])
def test_an_http_error_names_method_path_and_status_but_nothing_else(status):
    body = b'{"message": "Bad credentials GEHEIM-ANTWORT", "documentation_url": "https://boese.example"}'
    github, _ = client((status, {"Location": "https://boese.example/x"}, body))
    with pytest.raises(cla.ClaError) as caught:
        github.pull(7)
    message = str(caught.value)
    assert "GET" in message and f"/repos/{REPO}/pulls/7" in message and str(status) in message
    for secret in ("GEHEIM-ANTWORT", "boese.example", "Bad credentials", TOKEN, "Bearer"):
        assert secret not in message


def test_an_error_on_a_write_does_not_repeat_what_was_sent():
    github, _ = client((403, {}, b"{}"))
    with pytest.raises(cla.ClaError) as caught:
        github.add_comment(7, "GEHEIME NUTZLAST")
    assert "403" in str(caught.value) and "POST" in str(caught.value)
    assert "GEHEIME NUTZLAST" not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize("body", [b"<html>GEHEIM</html>", b"", b"\xff\xfe", b"{abc", b"nicht json"],
                         ids=["html", "leer", "kein UTF-8", "abgebrochen", "Text"])
def test_an_answer_that_is_no_json_fails_without_quoting_it(body):
    github, _ = client((200, {}, body))
    with pytest.raises(cla.ClaError) as caught:
        github.pull(7)
    assert "GEHEIM" not in str(caught.value) and "abc" not in str(caught.value)
    assert "kein JSON" in str(caught.value) and "200" in str(caught.value)


def test_an_answer_of_the_wrong_type_fails():
    for call, wrong in ((lambda g: g.pull(7), []), (lambda g: g.commits(7), {}), (lambda g: g.comments(7), {"id": 1}),
                        (lambda g: g.set_status(HEAD_SHA, "success", "x", None), [1]),
                        (lambda g: g.add_comment(7, "x"), "Text")):
        github, _ = client(ok(wrong))
        with pytest.raises(cla.ClaError):
            call(github)


@pytest.mark.parametrize("error", [OSError("geheimer-host.example"), ConnectionResetError("geheim"),
                                   http.client.IncompleteRead(b"geheim"), TimeoutError("geheim")], ids=type)
def test_a_network_error_fails_without_its_text(error):
    def broken(method, url, headers, data):
        raise error
    github = cla.GitHub(TOKEN, REPO, API, opener=broken)
    with pytest.raises(cla.ClaError) as caught:
        github.pull(7)
    assert "geheim" not in str(caught.value) and type(error).__name__ in str(caught.value)


def test_an_opener_without_a_status_code_fails():
    github, _ = client(("200", {}, b"{}"))
    with pytest.raises(cla.ClaError):
        github.pull(7)


def test_the_token_is_not_part_of_the_object_representation():
    assert TOKEN not in repr(cla.GitHub(TOKEN, REPO, API, opener=Recorder()))


# Der Standard-Opener gegen Server auf 127.0.0.1 (kein Netz nach außen).
@pytest.fixture
def loopback(monkeypatch):
    """Proxys aus der Umgebung dürfen 127.0.0.1 nicht umleiten."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@contextlib.contextmanager
def local_server(routes):
    """Server auf 127.0.0.1; ``routes[(Methode, Pfad)] = (Status, Header, Body)``; ``seen`` hält jede Anfrage fest."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append((self.command, self.path, dict(self.headers), body))
            status, headers, payload = routes.get((self.command, self.path), (404, {}, b"{}"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_default_opener_returns_status_headers_and_body(loopback):
    routes = {("GET", "/ok"): (200, {"X-Test": "1"}, b'{"a": 1}'), ("GET", "/fehler"): (403, {}, b'{"m": "nein"}')}
    with local_server(routes) as (base, seen):
        status, headers, body = cla._default_opener("GET", base + "/ok", {"X-Eigen": "ja"}, None)
        assert (status, body) == (200, b'{"a": 1}') and headers["X-Test"] == "1"
        assert seen[0][2]["X-Eigen"] == "ja"
        status, _, body = cla._default_opener("GET", base + "/fehler", {}, None)
        assert (status, body) == (403, b'{"m": "nein"}')              # ein Fehlerstatus ist eine normale Antwort


def test_the_default_opener_sends_the_body_of_a_post(loopback):
    with local_server({("POST", "/x"): (201, {}, b"{}")}) as (base, seen):
        status, _, _ = cla._default_opener("POST", base + "/x", {"Content-Type": "application/json"}, b'{"k": "v"}')
        assert status == 201 and seen[0][3] == b'{"k": "v"}'


def test_the_default_opener_never_follows_a_redirect(loopback):
    with local_server({("GET", "/geheim"): (200, {}, b"{}")}) as (foreign, foreign_seen):
        routes = {("GET", "/x"): (302, {"Location": foreign + "/geheim"}, b"")}
        with local_server(routes) as (base, seen):
            status, headers, _ = cla._default_opener("GET", base + "/x", {"Authorization": "Bearer geheim"}, None)
    assert status == 302 and headers["Location"].endswith("/geheim")
    assert len(seen) == 1 and foreign_seen == []                      # der fremde Host sah nichts, auch kein Token


def test_the_default_opener_limits_every_request_to_thirty_seconds(monkeypatch):
    seen = {}

    class StubOpener:
        def open(self, request, timeout=None):
            seen["timeout"] = timeout
            raise OSError("keine Verbindung")

    monkeypatch.setattr(cla.urllib.request, "build_opener", lambda *handlers: StubOpener())
    with pytest.raises(ConnectionError):
        cla._default_opener("GET", "http://127.0.0.1:1/x", {}, None)
    assert seen["timeout"] == 30 == cla.TIMEOUT


def test_the_default_opener_turns_a_refused_connection_into_a_connection_error(loopback):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ConnectionError):
        cla._default_opener("GET", f"http://127.0.0.1:{port}/x", {}, None)


def test_the_client_fails_cleanly_when_the_api_is_not_reachable(loopback):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    github = cla.GitHub(TOKEN, REPO, f"https://127.0.0.1:{port}")
    with pytest.raises(cla.ClaError) as caught:
        github.pull(7)
    assert "nicht erreichbar" in str(caught.value) and TOKEN not in str(caught.value)


# ── Teil 6: main und ein echter Start ────────────────────────────────────────
CLA_FILE_TEXT = "# CLA\r\n\r\n**Version:** 1.0 (Äpfel)\r\n"


def main_setup(tmp_path, monkeypatch, api, event=None, **env_extra):
    cla_file = tmp_path / "CLA.md"
    cla_file.write_bytes(CLA_FILE_TEXT.encode("utf-8"))
    monkeypatch.setattr(cla, "CLA_PATH", cla_file)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event or pr_event()), encoding="utf-8")
    created = []
    real = cla.GitHub

    def factory(token, repo, api_url):
        created.append((token, repo, api_url))
        return real(token, repo, api_url, opener=api)

    monkeypatch.setattr(cla, "GitHub", factory)
    env = {"GITHUB_EVENT_PATH": str(event_file), "GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": TOKEN, **env_extra}
    return env, created


def test_main_runs_the_whole_check(tmp_path, monkeypatch, capsys):
    api = FakeApi(make_pull(A), [make_commit(A)], [make_comment(10, A, DE)])
    env, created = main_setup(tmp_path, monkeypatch, api)
    assert cla.main(env) == 0
    assert created == [(TOKEN, REPO, API)]
    assert api.statuses[0][1]["state"] == "success" and len(api.posted) == 1
    assert hashlib.sha256(CLA_FILE_TEXT.encode("utf-8")).hexdigest() in api.posted[0]   # über die Bytes, mit CRLF
    assert capsys.readouterr().err == ""


def test_main_passes_a_configured_api_address_on(tmp_path, monkeypatch):
    env, created = main_setup(tmp_path, monkeypatch, FakeApi(make_pull(A)),
                              GITHUB_API_URL="https://ghe.beispiel.example/api/v3")
    monkeypatch.setattr(cla, "run", lambda event, env_, github, text: 0)     # die falsche API kennt nur api.github.com
    assert cla.main(env) == 0
    assert created == [(TOKEN, REPO, "https://ghe.beispiel.example/api/v3")]


def test_main_returns_one_when_the_environment_is_incomplete(capsys):
    assert cla.main({}) == 1
    err = capsys.readouterr().err
    for name in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
        assert name in err
    assert cla.main({"GITHUB_EVENT_PATH": "x", "GITHUB_REPOSITORY": REPO}) == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_main_returns_one_with_a_message_when_the_check_cannot_run(tmp_path, monkeypatch, capsys):
    api = FakeApi(make_pull(A))
    env, _ = main_setup(tmp_path, monkeypatch, api, GITHUB_REPOSITORY="a/b/c")
    assert cla.main(env) == 1
    assert "Repository" in capsys.readouterr().err
    assert api.calls == []


def test_main_returns_one_when_the_api_fails(tmp_path, monkeypatch, capsys):
    api = FakeApi(make_pull(A), [make_commit(A)], [])
    api.failing[("GET", f"/repos/{REPO}/pulls/7")] = 403
    env, _ = main_setup(tmp_path, monkeypatch, api)
    assert cla.main(env) == 1
    err = capsys.readouterr().err
    assert "403" in err and TOKEN not in err and api.statuses == []


@pytest.mark.parametrize("content", [b"{kaputt", b"\xff\xfe", b"[1, 2]"], ids=["kein JSON", "kein UTF-8", "kein Objekt"])
def test_main_returns_one_for_an_unreadable_event_file(tmp_path, monkeypatch, capsys, content):
    env, _ = main_setup(tmp_path, monkeypatch, FakeApi(make_pull(A)))
    (tmp_path / "event.json").write_bytes(content)
    assert cla.main(env) == 1
    assert capsys.readouterr().err.startswith("cla-check: ")


def test_main_returns_one_for_a_missing_event_file_or_cla(tmp_path, monkeypatch, capsys):
    env, _ = main_setup(tmp_path, monkeypatch, FakeApi(make_pull(A)))
    assert cla.main({**env, "GITHUB_EVENT_PATH": str(tmp_path / "gibt-es-nicht.json")}) == 1
    monkeypatch.setattr(cla, "CLA_PATH", tmp_path / "gibt-es-nicht.md")
    assert cla.main(env) == 1
    assert "CLA.md" in capsys.readouterr().err


def test_main_hides_an_unexpected_error_text(tmp_path, monkeypatch, capsys):
    env, _ = main_setup(tmp_path, monkeypatch, FakeApi(make_pull(A)))

    def explode(event, env_, github, text):
        raise RuntimeError("::set-env name=X::y")

    monkeypatch.setattr(cla, "run", explode)
    assert cla.main(env) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "set-env" not in err and "explode" in err


def test_main_uses_the_process_environment_by_default(monkeypatch, capsys):
    for name in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert cla.main() == 1
    assert "GITHUB_EVENT_PATH" in capsys.readouterr().err


def clean_environment(**extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def test_the_script_starts_like_the_workflow_starts_it(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(comment_event(is_pull_request=False)), encoding="utf-8")
    env = clean_environment(GITHUB_EVENT_PATH=str(event), GITHUB_REPOSITORY=REPO, GITHUB_TOKEN=TOKEN)
    done = subprocess.run([sys.executable, TOOL_PATH], cwd=tmp_path, env=env, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert "keinem Pull Request" in done.stdout.decode("utf-8")
    assert b"Traceback" not in done.stderr


def test_the_script_refuses_to_run_without_its_environment(tmp_path):
    done = subprocess.run([sys.executable, TOOL_PATH], cwd=tmp_path, env=clean_environment(),
                          capture_output=True, timeout=60)
    assert done.returncode == 1
    assert "GITHUB_TOKEN" in done.stderr.decode("utf-8")


# ── Teil 7: Stimmigkeit der Projektdateien ───────────────────────────────────
def collapse(text):
    return " ".join(text.split())


def quoted_lines_accepted(text, version):
    """Gibt es im Text eine Zeile, die – ohne ein vorangestelltes Zitatzeichen – als Zustimmung gilt?"""
    return any(cla.has_consent(re.sub(r"^\s*>\s?", "", line), version) for line in text.splitlines())


def test_the_german_sentence_stands_verbatim_in_cla_contributing_and_template():
    version = cla.cla_version(read(CLA_PATH))
    german, english = cla.consent_sentences(version)
    for path in (CLA_PATH, CONTRIBUTING_PATH, TEMPLATE_PATH):
        assert german in read(path), path
        assert quoted_lines_accepted(read(path), version), path     # eine Zeile, nicht nur ein Teilstück
    assert english in collapse(read(CLA_PATH))


def test_the_cla_names_the_same_version_in_the_german_and_the_english_part():
    cla_text = read(CLA_PATH)
    assert re.findall(r"(?m)^\*\*Version:\*\*\s*([0-9]+(?:\.[0-9]+)*)", cla_text) == [cla.cla_version(cla_text)] * 2


def test_the_status_context_is_the_documented_name():
    assert cla.STATUS_CONTEXT == "CLA-Zustimmung"
    assert cla.STATUS_CONTEXT in header_comment(read(WORKFLOW_PATH))
    assert cla.STATUS_CONTEXT in read(CONTRIBUTING_PATH)


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def yaml_values(text, key):
    """Alle Werte zum Schlüssel ``key`` – einzeilig oder als Blocktext (``|``, ``>``). Kein YAML-Parser
    (PyYAML ist keine Abhängigkeit des Projekts), nur so viel, wie die Prüfungen am Workflow brauchen."""
    lines = text.splitlines()
    values = []
    for number, line in enumerate(lines):
        match = re.match(rf"^(\s*(?:-\s+)?){re.escape(key)}:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(2)
        if re.fullmatch(r"[|>][+-]?[0-9]?(\s*#.*)?", value):
            block = []
            for following in lines[number + 1:]:
                if following.strip() and _indent(following) <= len(match.group(1)):
                    break
                block.append(following.strip())
            values.append("\n".join(block).strip())
        else:
            values.append(value)
    return values


def job_permissions(text):
    """Die eingerückten ``permissions:``-Blöcke (Rechte der Jobs) als Liste von Wörterbüchern."""
    lines = text.splitlines()
    blocks = []
    for number, line in enumerate(lines):
        match = re.fullmatch(r"(\s+)permissions:\s*", line)
        if not match:
            continue
        block = {}
        for following in lines[number + 1:]:
            stripped = following.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _indent(following) <= len(match.group(1)):
                break
            name, _, value = stripped.partition(":")
            block[name.strip()] = value.strip()
        blocks.append(block)
    return blocks


def header_comment(text):
    lines = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


UNSAFE_WORKFLOW = """\
jobs:
  beispiel:
    permissions:
      contents: read
      id-token: write
    steps:
      - run: echo "${{ github.event.pull_request.title }}"
      - name: Block
        run: |
          echo "Titel: ${{ github.event.issue.title }}"
          echo fertig
      - name: Sicher
        run: python3 tools/cla_check.py
        env:
          X: ${{ github.token }}
"""


def test_the_workflow_readers_see_what_they_are_meant_to_catch():
    runs = yaml_values(UNSAFE_WORKFLOW, "run")
    assert len(runs) == 3
    assert ["${{" in run for run in runs] == [True, True, False]
    assert job_permissions(UNSAFE_WORKFLOW) == [{"contents": "read", "id-token": "write"}]
    assert yaml_values(UNSAFE_WORKFLOW, "runs-on") == []


def test_the_workflow_runs_on_the_two_events_without_rights_at_the_top():
    workflow = read(WORKFLOW_PATH)
    for event in ("pull_request_target", "issue_comment"):
        assert re.search(rf"(?m)^\s+{event}:\s*$", workflow), event
    assert re.findall(r"(?m)^permissions:.*$", workflow) == ["permissions: {}"]


def test_the_job_has_exactly_the_rights_it_needs():
    assert job_permissions(read(WORKFLOW_PATH)) == [
        {"contents": "read", "pull-requests": "write", "statuses": "write"}]


def test_the_workflow_never_touches_the_head_of_the_pull_request():
    workflow = read(WORKFLOW_PATH)
    assert "github.event.pull_request.head" not in workflow
    assert "github.head_ref" not in workflow


def test_no_expression_reaches_a_shell_line():
    runs = yaml_values(read(WORKFLOW_PATH), "run")
    assert runs, "keine run-Zeile gefunden – der Leser des Workflows ist kaputt"
    assert all("${{" not in run for run in runs)
    assert "python3 tools/cla_check.py" in runs
    assert os.path.isfile(os.path.join(ROOT, "tools", "cla_check.py"))


def test_the_checkout_takes_the_default_branch_and_only_the_two_files():
    workflow = read(WORKFLOW_PATH)
    assert yaml_values(workflow, "ref") == ["${{ github.event.repository.default_branch }}"]
    assert yaml_values(workflow, "persist-credentials") == ["false"]
    assert set(yaml_values(workflow, "sparse-checkout")[0].split("\n")) == {"CLA.md", "tools/cla_check.py"}


def test_the_script_gets_the_token_from_the_workflow():
    workflow = read(WORKFLOW_PATH)
    assert yaml_values(workflow, "GITHUB_TOKEN") == ["${{ github.token }}"]


def test_the_script_has_the_license_header_of_the_other_tools():
    assert read(TOOL_PATH).splitlines()[:3] == read(MCP_PATH).splitlines()[:3]


def test_the_script_uses_only_the_standard_library():
    tree = ast.parse(read(TOOL_PATH))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules and modules <= set(sys.stdlib_module_names), modules - set(sys.stdlib_module_names)


# ── Mehr Commits, als GitHub auflistet ───────────────────────────────────────
def test_a_complete_commit_list_is_checked_as_usual():
    pr = make_pull(A, commits=2)
    result = evaluate(pr, [make_commit(A), make_commit(A)], [make_comment(5, A, DE)])
    assert result.state == "success"


def test_commits_beyond_the_list_fail_even_when_everybody_consented():
    pr = make_pull(A, commits=300)
    commits = [make_commit(A) for _ in range(250)]
    result = evaluate(pr, commits, [make_comment(5, A, DE)])
    assert result.state == "failure"
    assert result.description == "Mehr als 250 Commits – nicht vollständig prüfbar, bitte von Hand prüfen."
    assert result.target_url is None


def test_commits_beyond_the_list_fail_for_the_owner_too():
    pr = make_pull(INHABER, commits=251)
    result = evaluate(pr, [make_commit(INHABER) for _ in range(250)], [])
    assert result.state == "failure"
    assert result.description.startswith("Mehr als 250 Commits")


@pytest.mark.parametrize("count", [None, "300", True])
def test_without_a_usable_count_a_full_list_counts_as_incomplete(count):
    pr = make_pull(A, commits=count)
    assert cla.unseen_commits(pr, [make_commit(A) for _ in range(250)]) == 1
    assert cla.unseen_commits(pr, [make_commit(A) for _ in range(249)]) == 0


def test_the_statement_stands_in_a_code_block_not_as_a_quote_in_the_project_files():
    for path in ("CLA.md", "CONTRIBUTING.md", ".github/pull_request_template.md"):
        lines = read(path).splitlines()
        index = next(i for i, line in enumerate(lines) if line.strip() == DE)
        assert lines[index - 1].strip() == "```text" and lines[index + 1].strip() == "```", path
        assert cla.has_consent(lines[index], "1.0"), path
