# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
uBuchhaltung - Simple Accounting Software
Entry point for the web server
"""
from document_parser import DocumentParser
from server import run_server

if __name__ == "__main__":
    parser = DocumentParser()   # Init, if Log not exists, create it
    run_server()
