# Mitwirken

Danke für dein Interesse, zu diesem Projekt beizutragen!

## Lizenz deiner Beiträge

Das Projekt steht unter der **AGPL-3.0-or-later** und wird zusätzlich unter
einer kommerziellen Lizenz angeboten (Dual Licensing). Damit das möglich
bleibt, müssen alle Beiträge durch das **Contributor License Agreement
([`CLA.md`](CLA.md))** abgedeckt sein.

Kurz gesagt: Du behältst alle Rechte an deinem Beitrag und räumst dem
Projektinhaber zusätzlich ein einfaches Nutzungsrecht ein, das auch die
kommerzielle Lizenzierung erlaubt. Dein Beitrag erscheint in jedem Fall auch
unter der AGPL.

**So stimmst du zu:** Schreibe in deinem Pull Request einen Kommentar mit
genau diesem Satz (oder der englischen Fassung aus dem CLA):

```text
Ich stimme dem Contributor License Agreement (CLA.md), Version 1.0, für diesen Beitrag zu.
```

- Der Satz steht in einer eigenen Zeile, ohne Zusatz in derselben Zeile und
  nicht als Zitat (`>`); davor und danach darf der Kommentar weiteren Text
  enthalten.
- Der Kommentar muss von dem Konto stammen, das den Pull Request geöffnet hat.
  Enthält der Pull Request Commits weiterer Personen, stimmt jede von ihnen
  mit ihrem eigenen Konto zu.
- Jeder Commit muss einem GitHub-Konto zugeordnet sein (die E-Mail-Adresse
  des Commits ist im Konto hinterlegt). Sonst meldet die Prüfung „Commit ohne
  zugeordnetes GitHub-Konto“: Adresse im Konto ergänzen oder den Commit mit
  der richtigen Adresse neu schreiben.
- Die Zustimmung gilt für diesen einen Pull Request, auch für spätere
  Änderungen darin; jeder weitere Pull Request braucht seinen eigenen
  Kommentar.
- Eine automatische Prüfung zeigt am Pull Request als Status „CLA-Zustimmung“
  an, ob die Zustimmung vorliegt, und hält sie in einem Kommentar fest. Ohne
  sie wird ein Beitrag nicht aufgenommen.
- Stammt ein Teil des Beitrags nicht von dir (Code aus anderen Projekten) oder
  ist er im Rahmen eines Arbeitsverhältnisses entstanden, lies bitte vorher
  Ziffer 7 des CLA.

## Workflow

1. Issue anlegen oder ein bestehendes kommentieren, bevor du größere
   Änderungen beginnst.
2. Fork erstellen und einen Feature-Branch anlegen.
3. Änderungen umsetzen. **Jede neue Quelldatei bekommt die SPDX-Kopfzeilen**
   (siehe bestehende `.py`-Dateien als Vorlage).
4. Pull Request gegen den `main`-Branch öffnen und kurz beschreiben, was und
   warum geändert wurde.

## Code-Stil

- Halte dich an den bestehenden Stil des Projekts.
- Kleine, fokussierte Commits sind leichter zu reviewen als ein großer.
- Tests nur mit anonymisierten Daten (`seed_data/test/`); ein automatischer
  Check (`tests/check_anonymized.py`) erzwingt das.
- Vor dem Pull Request `python -m pytest -q` (Voraussetzung:
  `pip install -r requirements-dev.txt`) und `python tests/check_anonymized.py`
  ausführen; beides läuft auch in der CI. Die Tests lassen sich auf mehrere
  Prozesse verteilen (`python -m pytest -q -n 4`, so läuft die CI); jeder Test
  arbeitet dafür in einem eigenen Verzeichnis (`tmp_path`).
