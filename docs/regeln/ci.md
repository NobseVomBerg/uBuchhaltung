# Docker, CI und Release

## Docker & CI

- Docker-Image (`.github/workflows/docker-publish.yml`, `ghcr.io/nobsevomberg/ubuchhaltung`):
  **Ein Versionssprung in `version.py` ist der Release:** Kommt auf `main` eine
  Version an, für die es noch keinen Tag gibt, baut der Workflow nach den Tests
  `:X.Y.Z`, `:X.Y`, `:X`, `:latest` und legt danach Tag + GitHub-Release an
  (Notizen automatisch). Ohne neue Version entsteht nur `:edge`. Folge: Reine
  Doku-/CI-Änderungen ohne Versionssprung mergen, jede App-Änderung bekommt
  ihren PATCH-Sprung und wird mit dem Merge veröffentlicht
- Container-Standardport 2824; Betriebsdoku (Unraid/Proxmox) in `DEPLOYMENT.md`
- **Actions sind auf einen Commit festgelegt** (`uses: …@<40 Hex-Zeichen>  # vX.Y.Z`, in allen Workflows – auch in dem, der
  Image und Release schreibt): Ein Tag wie `@v4` lässt sich vom Herausgeber umsetzen, ein Commit nicht. Aktualisieren heißt:
  neuen Commit des Versions-Tags eintragen (`git ls-remote https://github.com/<action> refs/tags/vX.Y.Z`) und den Kommentar
  mitziehen; je Action derselbe Commit in allen Workflows (`tests/test_workflow_pins.py`)
- Jeder Push (alle Branches) führt `.github/workflows/tests.yml` aus:
  Anonymisierungs-Guard + pytest (Python 3.13, wie das Docker-Image – die Mindestversion)
- **Die Arbeitsdaten der Tests liegen in der CI im Arbeitsspeicher:** Jeder Test legt eine eigene SQLite-Datenbank an, und auf der Platte
  wartet jedes Speichern – das macht den größten Teil der Laufzeit aus (gemessen: die ganze Suite in rund 12 statt rund 40 Minuten). Die drei
  Workflows mit pytest (`tests.yml`, `docker-publish.yml`, `tests-python-versions.yml`) legen deshalb vor pytest `data/tests` als Verweis nach
  `/dev/shm` – ein Verweis statt `--basetemp`, damit `tests/conftest.py` zuständig bleibt und der Test der Ablage (`tests/test_test_isolation.py`)
  nicht übersprungen wird. Die Schritte sind in den drei Workflows kopiert; `tests/test_workflow_test_steps.py` hält sie gleich. Lokal geht
  dasselbe von Hand (`data/tests` als Verweis in den Arbeitsspeicher), nötig ist es nicht. **Die Suite läuft dort auf vier Prozessen**
  (`pytest-xdist` in `requirements-dev.txt`, Aufruf `python -m pytest -q -rs -n 4`; gemessen auf vier Kernen mit Arbeitsdaten im Arbeitsspeicher:
  knapp 4 Minuten statt knapp 13 auf einem Prozess). Das `-n` steht nur im Aufruf der drei Workflows, nie in `pytest.ini`: Lokal bleibt
  `python -m pytest` ein Prozess und läuft auch ohne das Plugin, und `tools/project_stats.py` zählt mit `pytest --collect-only` ohne Worker.
  Welcher Test auf welchem Worker läuft, ist Zufall – ein Test hält sich deshalb an: eigene Datenwurzel je Test (`tmp_path`;
  `_isolated_data_root` sorgt von selbst dafür), Server nur auf Port 0, keine festen Dateinamen außerhalb von `tmp_path`, kein Zustand, den
  ein Test dem nächsten hinterlässt, und Test-IDs, die in jedem Prozess gleich entstehen (nie Uhr oder Zufall in `parametrize`, sonst bricht
  xdist ab). Jeder Worker hat von xdist ein eigenes `--basetemp` unter dem des Hauptprozesses (`pytest-N/popen-gwK/`) und räumt nur seine
  eigenen Tests; der Wächter über das echte `data/` (`tests/conftest.py`) läuft je Worker, und der Test der Ablage
  (`tests/test_test_isolation.py`) läuft auch dort, ohne Skip
- Neuere Python-Versionen prüft `.github/workflows/tests-python-versions.yml` einmal in der Woche und
  von Hand (`workflow_dispatch`), nicht bei jedem Push: Je Version läuft die ganze Suite noch einmal.
  Eine neue Python-Version dort in der Liste ergänzen. Der Schritt „Python-Build nennen“ schreibt ins Log, ob der Build die Dezimalrechnung
  kompiliert mitbringt (`_decimal`): Ohne sie rechnet `decimal` in reinem Python, rund zehnmal langsamer – so scheiterte der Lauf mit
  Python 3.15 an der Zeitgrenze der Satzprobe (`tests/test_receipt_reading_text.py`, `RATE_PROOF_SECONDS`: 5 s mit, 60 s ohne das Modul;
  hier nachgestellt: ohne `_decimal` scheitert genau dieser eine Test). Eine Zeitgrenze in einem Test, der fast nur rechnet, braucht
  diesen Spielraum; die Ergebnisse sind in beiden Fällen dieselben
- **CLA-Zustimmung** (`.github/workflows/cla.yml`, `tools/cla_check.py`, nur Standardbibliothek):
  Zugestimmt wird je Pull Request mit einem Kommentar, der den Satz aus `CLA.md` samt Fassung in
  einer eigenen Zeile trägt – vom Konto, das den Pull Request geöffnet hat, und von jedem weiteren
  Commit-Autor. Das Skript setzt den Status „CLA-Zustimmung“ am letzten Commit und hält eine
  gefundene Zustimmung in einem eigenen Kommentar fest; Inhaber und Bot-Konten sind ausgenommen.
  Der Lauf hat Schreibrechte (`pull_request_target`, `issue_comment`) und läuft deshalb **nie mit
  Code aus dem Pull Request**: ausgecheckt wird nur der Standard-Branch, Werte aus dem Pull Request
  gehen nie in eine Shell-Zeile, und Text daraus (Kommentare, Titel, Branchnamen) nie in die Ausgabe,
  den Status oder den Kommentar der Prüfung – nur Nummern und bereinigte Anmeldenamen (`safe_login`).
  Nicht aufweichen. Ändert sich der Text von
  `CLA.md`, bekommt er eine neue Fassungsnummer; der Satz in `CONTRIBUTING.md` und in der
  Vorlage `.github/pull_request_template.md` zieht mit (`tests/test_cla_check.py` prüft es)
