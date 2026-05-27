#!/usr/bin/env python3
"""
Anonymisierungs-Guard für PyBuch Testdaten.

Prüft alle Dateien unter tests/ und seed_data/test/ auf personenbezogene
Echtdaten. Nur E-Mail-Domains, IBANs und Steuernummern aus der jeweiligen
Whitelist sind erlaubt — alles andere blockiert den Commit.

Usage:
    python tests/check_anonymized.py              # Alle Testdaten prüfen
    python tests/check_anonymized.py --quiet      # Nur Ausgabe bei Fehlern
    python tests/check_anonymized.py path/to/file # Einzelne Datei prüfen

Exit 0 = alles OK
Exit 1 = verdächtige Echtdaten gefunden
"""

import re
import sys
from pathlib import Path

# ─── Projekt-Wurzel ──────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── Zu prüfende Pfade ───────────────────────────────────────────────────────

SCAN_PATHS = [
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "seed_data" / "test",
]

SCAN_EXTENSIONS = {".py", ".json", ".csv", ".txt", ".sql", ".xml"}

# Diese Ordner/Dateien werden nicht geprüft (Rohdaten-Fixtures aus echten Imports)
IGNORE_DIRS = {"__pycache__", ".venv", "fixtures"}

# ─── Whitelist: erlaubte E-Mail-Domains ──────────────────────────────────────
# Alle Domains die in Testdateien vorkommen dürfen (Kleinbuchstaben).
# Neue Test-Domains einfach hier eintragen.

ALLOWED_EMAIL_DOMAINS = {
    # RFC-Standard Testdomains – immer erlaubt
    "example.com", "example.de", "example.org", "example.net",
    "test.de", "test.com", "testbank.de",
    # PyBuch seed_data/test – alle bekannten Muster-Domains
    "muster.de", "muster-gmbh.de",
    "beispiel-kunde.de",
    "lieferant.de",
    "baeckerei-mueller.de",
    "schreibwaren-hoffmann.de",
    "gasthof-loewen.de",
    "apotheke-schmidt.de",
    "blumenhaus-rosengarten.de",
    "kfz-fischer.de",
    "optik-braun.de",
    "haarmonie.de",
    "buchhandlung-lesezeit.de",
    "dr-weber.de",
    "technik-meier.de",
    "tabakwaren-schulz.de",
    "kiosk-frisch.de",
    "elektriker-strom.de",
    "lebensmittel-oezdemir.de",
    "musterbank.de",
}

# ─── Whitelist: erlaubte Test-IBANs ──────────────────────────────────────────
# Nur diese IBANs dürfen in Testdaten erscheinen.
# Quelle: Bundesbank-Testset + bekannte Dummy-IBANs

ALLOWED_IBANS = {
    "DE89370400440532013000",  # Bundesbank Standard-Test-IBAN (in tests/ verwendet)
    "DE02200505501015871393",
    "DE12500105170648489890",
    "DE75512108001245126199",
    "DE02300209000106531065",
    "DE02120300000000202051",
    "DE02100500000054540402",
}

# ─── Whitelist: erlaubte Test-Steuernummern ──────────────────────────────────
# Steuernummern aus seed_data/test/test_contacts.json

ALLOWED_TAX_IDS = {
    "DE123456789",
    "DE987654321",
    "DE112233445",
    "DE292827190",
    "DE838383838",
    "DE484848484",
    "DE757575757",
    "DE626262626",
    "DE515151515",
    "DE404040404",
    "DE939393939",
    "DE101010101",
    "DE222222222",
    "DE333333333",
    "DE444444444",
    "DE555555555",
    "DE666666666",
    "DE777777777",
}

# ─── Regex-Muster ────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b'
)

# Deutsche IBAN: DE + 2 Prüfziffern + 18 alphanumerische Zeichen (optional Leerzeichen)
_IBAN_RE = re.compile(
    r'\b(DE\s*\d{2}(?:\s*[\dA-Z]{4}){4}\s*[\dA-Z]{2})\b'
)

# Umsatzsteuer-ID: DE + 9 Ziffern
_TAX_ID_RE = re.compile(
    r'\b(DE\d{9})\b'
)

# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def _normalize_iban(raw: str) -> str:
    return re.sub(r'\s+', '', raw).upper()


def _is_obviously_fake_iban(iban: str) -> bool:
    """
    True wenn die IBAN strukturell keine echte gültige IBAN sein kann.

    Nach ISO 7064 sind die Prüfziffern 00, 01 und 99 bei DE-IBANs nie vergeben.
    Außerdem: Kontonummern aus reinen 0en oder 9en sind klare Platzhalter.
    Damit werden typische Test-IBANs wie DE00..., DE99... automatisch akzeptiert,
    ohne sie einzeln whitelisten zu müssen.
    """
    if len(iban) != 22:
        return False
    check_digits = iban[2:4]
    if check_digits in ('00', '01', '99'):
        return True
    account_part = iban[4:]
    if account_part in ('0' * 18, '9' * 18):
        return True
    return False


# ─── Prüflogik ───────────────────────────────────────────────────────────────

def check_file(filepath: Path) -> list[dict]:
    """Gibt Liste von Fundstellen (Dicts) zurück, leer = alles OK."""
    findings = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"file": str(filepath), "line": 0, "type": "IO", "value": "", "issue": str(exc)}]

    # Für die Ausgabe: relativer Pfad wenn möglich, sonst absolut
    try:
        display_path = str(filepath.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        display_path = str(filepath)

    for lineno, line in enumerate(content.splitlines(), start=1):
        # ── E-Mails ─────────────────────────────────────────────────────────
        for m in _EMAIL_RE.finditer(line):
            domain = m.group(1).lower()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                findings.append({
                    "file": display_path,
                    "line": lineno,
                    "type": "E-Mail",
                    "value": m.group(0),
                    "issue": f"Domain '{domain}' nicht in Whitelist → echte Adresse?",
                })

        # ── IBANs ────────────────────────────────────────────────────────────
        for m in _IBAN_RE.finditer(line):
            iban = _normalize_iban(m.group(1))
            if iban not in ALLOWED_IBANS and not _is_obviously_fake_iban(iban):
                findings.append({
                    "file": display_path,
                    "line": lineno,
                    "type": "IBAN",
                    "value": iban,
                    "issue": "IBAN nicht in Whitelist und kein erkannter Platzhalter → echte Bankdaten?",
                })

        # ── Steuernummern ────────────────────────────────────────────────────
        for m in _TAX_ID_RE.finditer(line):
            tax_id = m.group(1).upper()
            if tax_id not in ALLOWED_TAX_IDS:
                findings.append({
                    "file": display_path,
                    "line": lineno,
                    "type": "Steuer-ID",
                    "value": tax_id,
                    "issue": "USt-IdNr. nicht in Whitelist → echte Steuerdaten?",
                })

    return findings


def should_scan(path: Path) -> bool:
    """True wenn der Pfad geprüft werden soll."""
    for part in path.parts:
        if part in IGNORE_DIRS:
            return False
    return path.suffix.lower() in SCAN_EXTENSIONS and path.is_file()


def run_check(extra_paths: list[Path] | None = None, quiet: bool = False) -> int:
    """
    Hauptfunktion.
    Gibt Exit-Code zurück: 0 = OK, 1 = Probleme gefunden.
    """
    targets = extra_paths if extra_paths else SCAN_PATHS
    all_findings: list[dict] = []
    files_checked = 0

    for base in targets:
        if not base.exists():
            if not quiet:
                print(f"  ⚠ Pfad nicht gefunden, übersprungen: {base}")
            continue
        walk = [base] if base.is_file() else sorted(base.rglob("*"))
        for filepath in walk:
            if should_scan(filepath):
                files_checked += 1
                all_findings.extend(check_file(filepath))

    if all_findings:
        print(f"\n🚫  ANONYMISIERUNGS-FEHLER — {len(all_findings)} Problem(e) in {files_checked} geprüften Dateien:\n")
        current_file = None
        for f in all_findings:
            if f["file"] != current_file:
                current_file = f["file"]
                print(f"  📄 {current_file}")
            print(f"     Zeile {f['line']:>4}  [{f['type']}]  {f['value']}")
            print(f"              → {f['issue']}")
        print()
        print("  Lösung: Echte Daten durch anonymisierte Werte aus seed_data/test/ ersetzen.")
        print("  Neue Test-Domains/IBANs können in tests/check_anonymized.py zur Whitelist hinzugefügt werden.")
        print()
        return 1

    if not quiet:
        print(f"✓  Anonymisierungs-Check OK ({files_checked} Dateien geprüft)")
    return 0


# ─── Entry-Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows-Konsole: UTF-8 erzwingen damit Sonderzeichen korrekt ausgegeben werden
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    quiet = "--quiet" in args
    paths = [Path(a) for a in args if not a.startswith("--")]
    sys.exit(run_check(paths or None, quiet=quiet))
