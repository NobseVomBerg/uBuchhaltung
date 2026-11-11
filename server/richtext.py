# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Richtext-Filter für die Seiten; die Umsetzung liegt im Modul ``richtext`` (Projektwurzel),
damit auch der PDF-Satz (``export``) sie nutzen kann, ohne das Server-Paket zu laden."""
from richtext import ALLOWED_TAGS, MAX_DEPTH, sanitize_richtext

__all__ = ['ALLOWED_TAGS', 'MAX_DEPTH', 'sanitize_richtext']
