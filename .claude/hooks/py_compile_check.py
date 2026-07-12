#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PostToolUse-Hook: Syntax-Check nach Write/Edit auf Python-Dateien.

Liest den Tool-Input als JSON von stdin, extrahiert file_path und kompiliert
die Datei mit py_compile. Bei einem Syntaxfehler wird die Meldung auf stderr
ausgegeben und mit Exit-Code 2 beendet – Claude Code reicht stderr dann an
das Modell zurück, sodass der Fehler direkt korrigiert werden kann.

Robustheit:
- Entfernt ein evtl. vorangestelltes BOM (Windows-Pipes).
- Harness-/Parsing-Probleme führen zu Exit 0 (kein Blockieren), NICHT zu
  stillem Verschlucken echter Compile-Fehler.
"""
import json
import sys
import subprocess


def main() -> int:
    # Bytes lesen und mit utf-8-sig dekodieren – entfernt ein evtl. vorangestelltes
    # BOM zuverlässig (Windows-Pipes stellen gern eines voran).
    raw_bytes = sys.stdin.buffer.read()
    if not raw_bytes:
        return 0
    raw = raw_bytes.decode("utf-8-sig", errors="replace").strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # Kein verwertbarer Input – nicht blockieren
        return 0

    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not isinstance(fp, str) or not fp.endswith(".py"):
        return 0

    result = subprocess.run(
        [sys.executable, "-m", "py_compile", fp],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(f"Syntax-Fehler in {fp}:\n")
        sys.stderr.write(result.stderr[:800])
        return 2  # stderr wird an Claude zurückgereicht
    return 0


if __name__ == "__main__":
    sys.exit(main())
