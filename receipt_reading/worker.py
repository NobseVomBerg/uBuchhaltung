# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Kindprozess der Belegerkennung: ``python -m receipt_reading.worker``.

Er liest von stdin (binär) EINE JSON-Kopfzeile ``{"name": …, "own": [[Namen], [USt-IdNrn.]], "timeout": Sekunden}`` samt
Zeilenumbruch und danach die Bytes der Datei bis zum Ende, ruft :func:`receipt_reading.read_document` mit einem weichen Zeitpunkt
(0,8 × Zeitlimit) und schreibt genau ein JSON-Objekt (:meth:`receipt_reading.Reading.to_dict`) in UTF-8 auf stdout. Der
Rückgabecode ist 0, auch wenn die Lesung ``failed`` ist; 2 steht für eine Kopfzeile, die keine Anfrage ist.

Der Prozess ist die Grenze für alles, was ein Lesen kosten kann (pdfminer entpackt Querverweis- und Objektströme selbst):
Unter POSIX setzt er als Erstes ``RLIMIT_AS`` (``MEMORY_LIMIT``) und, sobald das Zeitlimit bekannt ist, ``RLIMIT_CPU`` (Zeitlimit
plus ``CPU_MARGIN`` Sekunden); unter Windows gibt es das Modul ``resource`` nicht, dort schützt allein das Zeitlimit des Aufrufers.
Auf stdout steht nichts außer dem Ergebnis – Ausgaben der Bibliotheken gehen nach stderr oder entfallen.
"""
import json
import logging
import math
import sys
import time
import warnings

try:
    import resource
except ImportError:                      # Windows
    resource = None

from receipt_reading import read_document

MEMORY_LIMIT = 768 * 1024 * 1024         # Adressraum des Prozesses (POSIX)
CPU_MARGIN = 5                           # Sekunden Rechenzeit über dem Zeitlimit
DEADLINE_SHARE = 0.8                     # so viel des Zeitlimits darf das Lesen für sich beanspruchen
MAX_HEADER_BYTES = 1024 * 1024           # die Kopfzeile ist nie länger
MAX_TIMEOUT = 3600


def _limit(which: int, value: int) -> None:
    """Die Grenze ``which`` auf ``value`` setzen, nie über die bestehende harte Grenze hinaus; wo das System sie nicht erlaubt,
    bleibt es bei der vorhandenen."""
    try:
        hard = resource.getrlimit(which)[1]
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass


def limit_memory() -> None:
    if resource is not None:
        _limit(resource.RLIMIT_AS, MEMORY_LIMIT)


def limit_cpu(timeout: float) -> None:
    if resource is not None:
        _limit(resource.RLIMIT_CPU, math.ceil(timeout) + CPU_MARGIN)


def _own_lists(value):
    ok = isinstance(value, list) and len(value) == 2 and all(
        isinstance(group, list) and all(isinstance(item, str) for item in group) for group in value)
    return (list(value[0]), list(value[1])) if ok else None


def parse_header(line: bytes):
    """``(name, own, timeout)`` aus der Kopfzeile oder ``None``, wenn sie keine Anfrage ist."""
    if not line.endswith(b'\n') or len(line) > MAX_HEADER_BYTES:
        return None
    try:
        request = json.loads(line)
    except ValueError:
        return None
    if not isinstance(request, dict):
        return None
    name, own, timeout = request.get('name'), _own_lists(request.get('own')), request.get('timeout')
    if (not isinstance(name, str) or own is None or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_TIMEOUT):
        return None
    return name, own, float(timeout)


def main() -> int:
    limit_memory()
    stdin, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr              # ein ``print`` einer Bibliothek darf das Ergebnis nicht stören
    logging.disable(logging.CRITICAL)
    warnings.simplefilter('ignore')
    request = parse_header(stdin.readline(MAX_HEADER_BYTES + 1))
    if request is None:
        return 2
    name, own, timeout = request
    limit_cpu(timeout)
    data = stdin.read()
    deadline = time.monotonic() + DEADLINE_SHARE * timeout
    reading = read_document(name, data, own, deadline=deadline)
    out.write(json.dumps(reading.to_dict(), ensure_ascii=False).encode('utf-8'))
    out.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
