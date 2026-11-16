# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests des Imports aus der WISO-Datenbank, die ihn über den Upload (2.3.7) anstoßen.

Der Import liest keinen Pfad aus der Anfrage mehr: Die Dateien liegen vorher in einem Zwischenordner des Benutzers
(``server/wiso_upload.py``), das Formular nennt nur dessen Kennung (``upload``), und jeder Import löscht den Ordner. Ein Test,
der denselben Import zweimal anstößt, legt die Datei deshalb zweimal ab. Alle Dateien sind synthetische Testdaten.
"""
import json
import os
import shutil

from server import wiso_upload
from tests.http_client import own_origin, send


def uploaded(path, standard=None):
    """``path`` (und ``standard``) wie einen Upload ablegen – im Zwischenordner des gerade geltenden Benutzers – und die
    Formularfelder dafür liefern: ``{'upload': [Kennung]}``, zum Ergänzen um ``confirm_existing`` usw."""
    token = wiso_upload.begin()
    folder = wiso_upload.upload_dir(token)
    shutil.copyfile(str(path), os.path.join(folder, wiso_upload.WISO_CLIENT_DB))
    if standard is not None:
        shutil.copyfile(str(standard), os.path.join(folder, wiso_upload.WISO_STANDARD_DB))
    return {'upload': [token]}


def upload_request(port, csrf, body, slot='client', token=None, headers=None, query=None):
    """``POST /wiso/fdb-upload`` mit ``body`` (Bytes) als rohem Body, wie es das Skript der Seite schickt: eigene Herkunft,
    CSRF-Token als Header. ``query`` ersetzt die Adresse nach ``/wiso/fdb-upload`` ganz (für ungültige Anfragen)."""
    if query is None:
        query = f'?slot={slot}' + (f'&upload={token}' if token else '')
    sent = {'Origin': own_origin(port), 'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream'}
    sent.update(headers or {})
    return send(port, 'POST', '/wiso/fdb-upload' + query, sent, body)


def upload_over_http(port, csrf, path, standard=None):
    """Die Datei(en) über ``POST /wiso/fdb-upload`` hochladen und die Kennung liefern."""
    with open(str(path), 'rb') as handle:
        first = upload_request(port, csrf, handle.read())
    assert first.status == 200, first.text
    token = json.loads(first.body)['upload']
    if standard is not None:
        with open(str(standard), 'rb') as handle:
            second = upload_request(port, csrf, handle.read(), slot='standard', token=token)
        assert second.status == 200, second.text
    return token
