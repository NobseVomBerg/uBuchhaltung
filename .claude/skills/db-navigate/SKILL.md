---
name: db-navigate
description: Navigate db.py efficiently without reading the entire ~4600-line file — find tables, methods, and schema with targeted grep
user-invocable: false
---

db.py ist ~4600 Zeilen (~15k Tokens wenn komplett geladen). NIE komplett lesen.

> Hinweis (Windows): Statt der bash-`grep`-Beispiele unten das **Grep-Tool** nutzen
> (gleiches Ergebnis, plattformunabhängig). Die Befehle zeigen nur das Muster.

## Gezielt navigieren

**Tabellen-Übersicht:**
```bash
grep -n "CREATE TABLE\|class.*:" db.py | head -50
```

**Methode finden:**
```bash
grep -n "def <methodname>" db.py
```

**Danach nur 30-60 Zeilen um den Treffer lesen:**
```python
Read("db.py", offset=<zeile-10>, limit=60)
```

**Alle Methoden einer Kategorie (z.B. Buchungen):**
```bash
grep -n "def.*[Bb]ook\|def.*[Bb]uchung" db.py
```

**Fremdschlüssel / Joins verstehen:**
```bash
grep -n "FOREIGN KEY\|REFERENCES\|JOIN" db.py | head -30
```

## Token-Kalkulation

| Aktion | Tokens |
|--------|--------|
| db.py komplett lesen | ~15k |
| Gezielter grep + 60 Zeilen lesen | ~800 |
| Ersparnis pro Task | ~14k |

Erst grep, dann Read mit offset. Nie blind das ganze File laden.
