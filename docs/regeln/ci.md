# Docker, CI und Release

## Docker und Release

- Image `ghcr.io/nobsevomberg/ubuchhaltung` (`.github/workflows/docker-publish.yml`). **Ein Versionssprung in `version.py` ist der Release:** Kommt auf `main` eine Version ohne Tag an, baut der Workflow nach den Tests `:X.Y.Z`, `:X.Y`, `:X`, `:latest`
  und legt Tag + GitHub-Release an. Ohne neue Version nur `:edge`. Folge: reine Doku-/CI-Änderungen ohne Versionssprung mergen; jede App-Änderung bekommt ihren PATCH-Sprung und wird mit dem Merge veröffentlicht.
- Container-Standardport 2824; Betriebsdoku in `DEPLOYMENT.md`.

## Workflows

- **Actions sind auf einen Commit festgelegt** (`uses: …@<40 Hex-Zeichen>  # vX.Y.Z`, in ALLEN Workflows, auch dem für Image und Release). Aktualisieren: neuen Commit des Versions-Tags eintragen
  (`git ls-remote https://github.com/<action> refs/tags/vX.Y.Z`), Kommentar mitziehen; je Action derselbe Commit in allen Workflows (`tests/test_workflow_pins.py`).
- Jeder Push (alle Branches) → `.github/workflows/tests.yml`: Anonymisierungs-Guard + pytest (Python 3.13, wie das Docker-Image – die Mindestversion).
- **Testdaten im Arbeitsspeicher:** `tests.yml`, `docker-publish.yml`, `tests-python-versions.yml` legen vor pytest `data/tests` als Verweis nach `/dev/shm` – ein Verweis statt `--basetemp`, damit `tests/conftest.py` zuständig bleibt und
  `tests/test_test_isolation.py` nicht übersprungen wird. Die Schritte sind in den drei Workflows kopiert; `tests/test_workflow_test_steps.py` hält sie gleich. Lokal nicht nötig.
- **Vier Prozesse in der CI:** `pytest-xdist` (`requirements-dev.txt`), Aufruf `python -m pytest -q -rs -n 4`. Das `-n` steht nur im Aufruf der drei Workflows, nie in `pytest.ini` (lokal bleibt `python -m pytest` ein Prozess ohne Plugin;
  `tools/project_stats.py` zählt mit `pytest --collect-only` ohne Worker). Worker-Zuordnung ist Zufall – jeder Test hält sich an: eigene Datenwurzel (`tmp_path`; `_isolated_data_root`), Server nur auf Port 0,
  keine festen Dateinamen außerhalb von `tmp_path`, kein Zustand für den nächsten Test, Test-IDs in jedem Prozess gleich (nie Uhr oder Zufall in `parametrize`, sonst bricht xdist ab).
  Jeder Worker hat ein eigenes `--basetemp` (`pytest-N/popen-gwK/`) und räumt nur seine Tests; der Wächter über das echte `data/` (`tests/conftest.py`) läuft je Worker, `tests/test_test_isolation.py` auch dort ohne Skip.
- Neuere Python-Versionen: `.github/workflows/tests-python-versions.yml` wöchentlich und per `workflow_dispatch`, ganze Suite je Version; neue Version dort in der Liste ergänzen. Der Schritt „Python-Build nennen“ loggt, ob `_decimal` kompiliert ist
  (ohne: `decimal` ~10× langsamer). Zeitgrenze `RATE_PROOF_SECONDS` in `tests/test_receipt_reading_text.py`: 5 s mit, 60 s ohne `_decimal` – ein Test, der fast nur rechnet, braucht diesen Spielraum.
- **CLA-Zustimmung** (`.github/workflows/cla.yml`, `tools/cla_check.py`, nur Standardbibliothek): Zustimmung je Pull Request per Kommentar mit dem Satz aus `CLA.md` samt Fassung in eigener Zeile – vom PR-Eröffner und jedem weiteren Commit-Autor.
  Das Skript setzt den Status „CLA-Zustimmung“ am letzten Commit und hält die Zustimmung in einem eigenen Kommentar fest; Inhaber und Bot-Konten ausgenommen.
  - Der Lauf hat Schreibrechte (`pull_request_target`, `issue_comment`) und läuft **nie mit Code aus dem Pull Request**: ausgecheckt wird nur der Standard-Branch; Werte aus dem PR gehen nie in eine Shell-Zeile, Text daraus (Kommentare, Titel, Branchnamen)
    nie in Ausgabe, Status oder Kommentar – nur Nummern und bereinigte Anmeldenamen (`safe_login`). **Nicht aufweichen.**
  - Ändert sich `CLA.md`, bekommt es eine neue Fassungsnummer; der Satz in `CONTRIBUTING.md` und `.github/pull_request_template.md` zieht mit (`tests/test_cla_check.py`).
