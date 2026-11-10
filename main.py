# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
uBuchhaltung - Simple Accounting Software
Entry point for the web server
"""
import demo
import layout
from document_parser import DocumentParser
from server import run_server

if __name__ == "__main__":
    layout.migrate()            # Altbestand aus Version 1.x (Daten direkt in data/) nach data/users/<Name>/ umziehen – vor allem anderen
    demo.prepare()              # UBUCHHALTUNG_DEMO=1: Benutzer „demo“ mit Demodaten anlegen
    parser = DocumentParser()   # Init, if Log not exists, create it
    run_server()
