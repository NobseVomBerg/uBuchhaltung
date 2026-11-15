# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen der DATEV-Tests: der Export, wie ihn der Nutzer bestätigt (2.3.4).

Gibt es Hinweise zum Stapel (Bankbewegungen ohne Buchungssatz, Überträge, die der Stapel mit ihrer Ausgangsseite in einem anderen
Zeitraum weglässt), exportiert ``handle_datev_export`` nur mit ``confirm_export`` = ``1`` und der Prüfsumme des Hinweises
(``confirmed_check``). Die Prüfsumme kennt allein der Hinweis – so bestätigt der Nutzer auch in den Tests: erst ohne, dann mit
dem Kästchen für genau diesen Hinweis.
"""
from urllib.parse import parse_qs, urlsplit

from server import handlers

CONFIRM_REDIRECT = '/miscellaneous?datev_export=confirm&'


def datev_post(date_from, date_to, **more):
    """Die Felder des Export-Formulars (``parse_form``: jeder Wert eine Liste)."""
    return {'date_from': [date_from], 'date_to': [date_to], **{key: [value] for key, value in more.items()}}


def is_confirm_redirect(result):
    return not isinstance(result[0], bytes) and result[0] == 303 and result[1].startswith(CONFIRM_REDIRECT)


def query_of(location):
    """Die Abfrage einer Weiterleitung als ``{Name: [Werte]}``."""
    return parse_qs(urlsplit(location).query)


def check_of(result):
    """Die Prüfsumme des Hinweises (``datev_check``) aus der Weiterleitung des Exports."""
    assert is_confirm_redirect(result), result
    return query_of(result[1])['datev_check'][0]


def message_of(result):
    """Die Meldung (``msg``) aus der Weiterleitung des Exports."""
    return query_of(result[1])['msg'][0]


def confirmed_export(db, date_from, date_to):
    """Der Export für den Zeitraum, wie der Nutzer ihn bestätigt: erst ohne Bestätigung – gibt es Hinweise, nennt die
    Weiterleitung die Prüfsumme –, dann mit dem Kästchen für genau diesen Hinweis. Ohne Hinweis (oder bei einer anderen
    Ablehnung) ist es das Ergebnis des ersten Aufrufs; sonst das des zweiten."""
    first = handlers.handle_datev_export(db, datev_post(date_from, date_to))
    if not is_confirm_redirect(first):
        return first
    return handlers.handle_datev_export(
        db, datev_post(date_from, date_to, confirm_export='1', confirmed_check=check_of(first)))
