# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Erzeugung von Ausgabe-/Export-Dokumenten (abhängigkeitsarm).

Module:
  - pdf_core           – gemeinsame PDF-Primitive (Escaping, Logo-XObject, Single-Page-Builder)
  - pdf_invoice        – Rechnungs-PDF
  - pdf_worktime       – Arbeitszeit-/Monatsstundenzettel (PDF)
  - xrechnung_invoice  – Rechnungs-XML (XRechnung, EN 16931)
  - datev              – DATEV-Buchungsstapel-Export (CSV)
"""
