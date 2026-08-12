# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PreCompact-Hook: Zustand festhalten, bevor der Kontext verdichtet wird.

Auto-Compact laeuft ohne Vorwarnung. Was nur im Gespraechsverlauf steht, ist
danach weg. Dieser Hook schreibt deshalb bei jeder Verdichtung einen
Zeitstempel-Eintrag mit dem *objektiven* Stand nach
``data/handover-journal.md``: Branch, letzte Commits, geaenderte Dateien und
die Kennzahlen des Datenbestands.

Bewusste Arbeitsteilung:

* **dieses Journal** – maschinell, datiert, raeumt sich selbst auf. Beantwortet
  „wo stand das Projekt, als der Kontext gekappt wurde?"
* **data/handover-<thema>.md** – von Hand gepflegt, kuratiert. Beantwortet
  „was ist offen und warum, und welcher Ansatz ist warum gescheitert?"
* **Memory und CLAUDE.md** – das konzeptionell Dauerhafte. Nicht hier.

Aufraeumen: Eintraege aelter als ``KEEP_DAYS`` entfallen, es bleiben aber
immer mindestens ``KEEP_MIN`` Eintraege stehen (sonst leert eine ruhige Phase
die Datei). Zusaetzlich eine harte Groessengrenze.

Der Hook schreibt nur; er blockiert nie und laesst nichts scheitern.
"""
import datetime
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOURNAL = os.path.join(ROOT, 'data', 'handover-journal.md')

#: Aufbewahrung. Aelteres faellt weg – wer laenger zurueckschauen muss, findet
#: das Dauerhafte in den Commits und im Memory.
KEEP_DAYS = 30
KEEP_MIN = 10
MAX_BYTES = 200_000

HEADER = """# Journal: Stand vor jeder Kontext-Verdichtung

Maschinell geschrieben von `.claude/hooks/precompact_snapshot.py`.
Neueste Eintraege oben. Aelteres als {days} Tage wird automatisch entfernt
(mindestens die letzten {keep} Eintraege bleiben stehen).

Das hier ist die *objektive* Mitschrift. Was offen ist und warum, steht in den
`handover-<thema>.md` daneben; das konzeptionell Dauerhafte im Memory.

---
""".format(days=KEEP_DAYS, keep=KEEP_MIN)

ENTRY_RE = re.compile(r'^## (\d{4}-\d{2}-\d{2})', re.MULTILINE)


def _git(*args):
    try:
        out = subprocess.run(('git',) + args, cwd=ROOT, capture_output=True,
                             text=True, timeout=10, encoding='utf-8',
                             errors='replace')
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:
        return ''


def _kennzahlen():
    """Kennzahlen der Nutzerdatenbanken – rein lesend, Fehler egal."""
    import glob
    import sqlite3

    zeilen = []
    for pfad in sorted(glob.glob(os.path.join(ROOT, 'data', 'users', '*',
                                              'buch.db'))):
        nutzer = os.path.basename(os.path.dirname(pfad))
        try:
            conn = sqlite3.connect(f'file:{pfad}?mode=ro', uri=True, timeout=5)
            try:
                def zahl(sql):
                    return conn.execute(sql).fetchone()[0]

                bank = zahl("SELECT COUNT(*) FROM Bookings "
                            "WHERE BookingType='bank'")
                entry = zahl("SELECT COUNT(*) FROM Bookings "
                             "WHERE BookingType='entry'")
                offen = zahl("SELECT COUNT(*) FROM Bookings "
                             "WHERE BookingType='bank' AND ID NOT IN "
                             '(SELECT ParentBooking_ID FROM Bookings '
                             'WHERE ParentBooking_ID IS NOT NULL)')
                rechnungen = zahl('SELECT COUNT(*) FROM Invoices')
                zeilen.append(f'- Datenbestand `{nutzer}`: {bank} Bankbewegungen '
                              f'({offen} unverknuepft), {entry} Buchungssaetze, '
                              f'{rechnungen} Rechnungen')
            finally:
                conn.close()
        except Exception:
            zeilen.append(f'- Datenbestand `{nutzer}`: nicht lesbar')
    return zeilen


def _eintrag(trigger):
    jetzt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    branch = _git('rev-parse', '--abbrev-ref', 'HEAD') or '?'
    head = _git('log', '-1', '--format=%h %s') or '?'
    geaendert = [z for z in _git('status', '--short').split('\n') if z.strip()]
    commits = [z for z in _git('log', '-5', '--format=%h %s').split('\n')
               if z.strip()]

    zeilen = [f'## {jetzt} — {trigger}-Compact', '',
              f'- Branch `{branch}`, HEAD `{head}`',
              f'- Arbeitsverzeichnis: '
              + (f'{len(geaendert)} geaenderte Dateien' if geaendert
                 else 'sauber')]
    zeilen += _kennzahlen()
    if geaendert:
        zeilen.append('- Geaendert: '
                      + ', '.join(z.strip()[:60] for z in geaendert[:12]))
    if commits:
        zeilen.append('- Letzte Commits:')
        zeilen += [f'  - {c}' for c in commits]
    zeilen.append('')
    return '\n'.join(zeilen)


def _aufraeumen(text):
    """Eintraege aelter als KEEP_DAYS entfernen, KEEP_MIN aber behalten."""
    stellen = [(m.start(), m.group(1)) for m in ENTRY_RE.finditer(text)]
    if not stellen:
        return text
    grenze = (datetime.date.today()
              - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    behalten = []
    for i, (start, datum) in enumerate(stellen):
        ende = stellen[i + 1][0] if i + 1 < len(stellen) else len(text)
        behalten.append((datum, text[start:ende]))
    frisch = [b for b in behalten if b[0] >= grenze]
    if len(frisch) < KEEP_MIN:
        frisch = behalten[:KEEP_MIN]        # neueste stehen oben
    return HEADER + '\n' + ''.join(b[1] for b in frisch)


def main():
    try:
        roh = sys.stdin.read()
    except Exception:
        roh = ''
    try:
        daten = json.loads(roh) if roh.strip() else {}
    except Exception:
        daten = {}
    trigger = daten.get('trigger') or 'auto'

    try:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        alt = ''
        if os.path.exists(JOURNAL):
            with open(JOURNAL, encoding='utf-8') as f:
                alt = f.read()
        rumpf = alt[len(HEADER):] if alt.startswith(HEADER) else \
            alt[alt.find('\n## '):] if '\n## ' in alt else ''
        neu = HEADER + '\n' + _eintrag(trigger) + '\n' + rumpf.lstrip('\n')
        neu = _aufraeumen(neu)
        if len(neu.encode('utf-8')) > MAX_BYTES:      # harte Notbremse
            schnitt = neu[:MAX_BYTES].rfind('\n## ')
            if schnitt > len(HEADER):
                neu = neu[:schnitt] + '\n'
        with open(JOURNAL, 'w', encoding='utf-8', newline='\n') as f:
            f.write(neu)
        print(json.dumps({'systemMessage':
                          f'Stand vor dem {trigger}-Compact in '
                          'data/handover-journal.md festgehalten.',
                          'suppressOutput': True}))
    except Exception as fehler:                        # nie blockieren
        print(json.dumps({'systemMessage':
                          f'PreCompact-Journal fehlgeschlagen: {fehler}'}))


if __name__ == '__main__':
    main()
