# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""E-Mail-Versand (``email_sender.py``): verschlüsselt, mit geprüftem Zertifikat, ohne Pfade in den Meldungen.

* Port 587 u. a.: ``ehlo`` → ``starttls`` mit einem Kontext, der das Zertifikat UND den Hostnamen prüft
  (``CERT_REQUIRED``, ``check_hostname``) → ``ehlo`` → Anmeldung. Port 465: ``SMTP_SSL`` mit demselben Kontext.
  Bietet der Server kein STARTTLS an, wird nichts gesendet und nie unverschlüsselt angemeldet. Alle
  Verbindungen haben ein Zeitlimit. Einen Schalter, der die Prüfung abschaltet, gibt es nicht.
* Die Meldungen an den Nutzer nennen nie den Text der Ausnahme (Pfade, Antworten des Servers); der volle Fehler
  steht im Server-Log, die Zugangsdaten nie.
* Ein Anzeigename mit Komma ist EIN Empfänger (``formataddr``); CR/LF in Adressen und Namen wird abgelehnt, bevor
  eine Verbindung entsteht.

``smtplib.SMTP``/``SMTP_SSL`` sind durch Fakes ersetzt; zwei Mini-SMTP-Server auf 127.0.0.1 (ohne
Zertifikat – die Python-Standardbibliothek kann keines erzeugen) zeigen mit echten Sockets, dass ohne STARTTLS
kein Befehl der Anmeldung oder Übertragung beim Server ankommt. Alle Namen, Adressen und Zugangsdaten sind erfunden.
"""
import email.header
import os
import socket
import smtplib
import ssl
import threading
from email.utils import getaddresses

import pytest

import email_sender
from email_sender import EmailSender, SMTP_SSL_PORT, SMTP_TIMEOUT

REAL_SMTP = smtplib.SMTP            # vor dem Ersetzen durch die Fakes: deren Grundlage (echtes ``send_message``)

USER, PASSWORD = 'versand@example.com', 'geheim-test'
NO_STARTTLS = ('Der Mailserver bietet keine verschlüsselte Verbindung (STARTTLS) an – '
               'die E-Mail wurde nicht gesendet.')
NOT_REACHABLE = 'Der Mailserver ist nicht erreichbar.'
CERT_UNVERIFIED = 'Das Zertifikat des Mailservers konnte nicht geprüft werden.'
TLS_FAILED = 'Die verschlüsselte Verbindung zum Mailserver ist fehlgeschlagen.'
INVALID_ADDRESS = 'Ungültige E-Mail-Adresse.'


# ── Fake für smtplib.SMTP / SMTP_SSL ──────────────────────────────────────────
class Recorder:
    """Merkt sich, was der Versand tut; ``errors`` nennt je Aufruf (``init``, ``ehlo``, ``starttls``, ``login``,
    ``send_message``) eine Ausnahme, die der Fake dort wirft."""

    def __init__(self):
        self.created = []            # (Klasse, Host, Port, Schlüsselwörter) je Verbindung
        self.calls = []              # Namen der Aufrufe in ihrer Reihenfolge
        self.contexts = []           # die SSL-Kontexte, die übergeben wurden
        self.sent = []               # gesendete Nachrichten
        self.envelopes = []          # (Absender, [Empfänger]) im Umschlag, wie ``send_message`` sie bestimmt
        self.offers_starttls = True
        self.errors = {}

    def fake(self, kind):
        recorder = self

        class FakeSMTP(REAL_SMTP):
            """Ohne Netz, aber mit dem echten ``send_message``: Es bestimmt Absender und Empfänger des Umschlags aus
            den Kopfzeilen und ruft ``sendmail`` auf – das merkt sich hier den Umschlag, statt zu senden."""

            def __init__(self, host, port, **kwargs):
                recorder.created.append((kind, host, port, kwargs))
                if 'context' in kwargs:
                    recorder.contexts.append(kwargs['context'])
                recorder._maybe_fail('init')

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def ehlo(self):
                recorder.calls.append('ehlo')
                recorder._maybe_fail('ehlo')
                return 250, b'ok'

            def ehlo_or_helo_if_needed(self):
                pass                 # das echte ``send_message`` ruft es auf; mitgeschrieben wird nur, was der Versand selbst tut

            def has_extn(self, name):
                return name.lower() == 'starttls' and recorder.offers_starttls

            def starttls(self, context=None):
                recorder.calls.append('starttls')
                recorder.contexts.append(context)
                recorder._maybe_fail('starttls')

            def login(self, user, password):
                recorder.calls.append('login')
                recorder._maybe_fail('login')

            def send_message(self, msg, *args, **kwargs):
                recorder.calls.append('send_message')
                recorder._maybe_fail('send_message')
                recorder.sent.append(msg)
                return super().send_message(msg, *args, **kwargs)

            def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
                recorder.envelopes.append((from_addr, list(to_addrs)))
                return {}

        return FakeSMTP

    def _maybe_fail(self, where):
        if where in self.errors:
            raise self.errors[where]


@pytest.fixture
def smtp(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(email_sender.smtplib, 'SMTP', recorder.fake('SMTP'))
    monkeypatch.setattr(email_sender.smtplib, 'SMTP_SSL', recorder.fake('SMTP_SSL'))
    return recorder


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / 'R26001 Beispiel Kunde.pdf'
    path.write_bytes(b'%PDF-1.4 Test')
    return str(path)


def sender(port=587):
    return EmailSender(smtp_host='smtp.example.org', smtp_port=port, smtp_user=USER, smtp_password=PASSWORD)


def send(pdf, port=587, **fields):
    """``send_invoice_email`` mit sinnvollen Vorgaben; ``fields`` überschreibt einzelne Angaben."""
    args = dict(recipient_email='kunde@example.com', recipient_name='Erika Empfaenger', invoice_number='R26001',
                pdf_path=pdf, sender_name='Eigene Firma GmbH', sender_email='info@example.com')
    args.update(fields)
    return sender(port).send_invoice_email(**args)


# ── Verbindung: STARTTLS mit Prüfung ──────────────────────────────────────────
def test_starttls_runs_in_the_documented_order_with_a_verifying_context(smtp, pdf):
    assert send(pdf) == (True, None)
    assert smtp.created == [('SMTP', 'smtp.example.org', 587, {'timeout': SMTP_TIMEOUT})]
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo', 'login', 'send_message']
    (context,) = smtp.contexts
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert len(smtp.sent) == 1


def test_the_timeout_is_set_and_is_thirty_seconds(smtp, pdf):
    assert SMTP_TIMEOUT == 30
    send(pdf)
    assert smtp.created[0][3]['timeout'] == 30


@pytest.mark.parametrize('port', [25, 587, 2525, 8025])
def test_every_port_except_465_uses_starttls(smtp, pdf, port):
    assert send(pdf, port=port) == (True, None)
    assert [c[0] for c in smtp.created] == ['SMTP'] and smtp.created[0][2] == port
    assert 'starttls' in smtp.calls and smtp.calls.index('starttls') < smtp.calls.index('login')


def test_port_465_uses_tls_from_the_start_with_the_same_kind_of_context(smtp, pdf):
    assert SMTP_SSL_PORT == 465
    assert send(pdf, port=465) == (True, None)
    (kind, host, port, kwargs), = smtp.created
    assert (kind, host, port) == ('SMTP_SSL', 'smtp.example.org', 465)
    assert kwargs['timeout'] == SMTP_TIMEOUT
    context = kwargs['context']
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert smtp.calls == ['login', 'send_message']          # kein ehlo/starttls: die Verbindung ist schon verschlüsselt


def test_without_starttls_nothing_is_sent_and_nobody_logs_in(smtp, pdf):
    smtp.offers_starttls = False
    assert send(pdf) == (False, NO_STARTTLS)
    assert smtp.calls == ['ehlo']                           # weder starttls noch login noch send_message
    assert smtp.sent == []
    assert PASSWORD not in NO_STARTTLS and USER not in NO_STARTTLS


def test_there_is_no_switch_that_turns_the_check_off():
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'email_sender.py'),
                  encoding='utf-8').read()
    for forbidden in ('_create_unverified_context', 'CERT_NONE', 'check_hostname = False', 'check_hostname=False'):
        assert forbidden not in source, forbidden
    assert source.count('ssl.create_default_context()') == 1                       # der Standardkontext, sonst nichts
    assert 'smtplib.SMTP(self.smtp_host, self.smtp_port)' not in source          # nie ohne Zeitlimit


# ── Meldungen: nie der Text der Ausnahme ──────────────────────────────────────
PATH_TEXT = '/etc/ssl/private/geheim-ca.pem'


@pytest.mark.parametrize('where', ['init', 'starttls'])
def test_an_unverifiable_certificate_gets_a_plain_message_without_path_or_exception_text(smtp, pdf, where, capsys):
    smtp.errors[where] = ssl.SSLCertVerificationError(
        1, f'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer '
           f'certificate ({PATH_TEXT})')
    port = 465 if where == 'init' else 587
    assert send(pdf, port=port) == (False, CERT_UNVERIFIED)
    assert 'login' not in smtp.calls and smtp.sent == []
    assert PATH_TEXT in capsys.readouterr().err               # die Einzelheiten stehen im Server-Log


def test_other_tls_errors_get_their_own_plain_message(smtp, pdf):
    smtp.errors['starttls'] = ssl.SSLError(1, f'[SSL: WRONG_VERSION_NUMBER] wrong version number {PATH_TEXT}')
    result = send(pdf)
    assert result == (False, TLS_FAILED) and PATH_TEXT not in result[1]
    assert smtp.calls == ['ehlo', 'starttls']                 # nichts danach: keine Anmeldung


@pytest.mark.parametrize('error', [
    pytest.param(TimeoutError('timed out'), id='Zeitüberschreitung'),
    pytest.param(socket.timeout('timed out'), id='socket.timeout'),
    pytest.param(ConnectionRefusedError(111, f'Connection refused {PATH_TEXT}'), id='abgelehnt'),
    pytest.param(ConnectionResetError(104, 'Connection reset by peer'), id='zurückgesetzt'),
    pytest.param(socket.gaierror(-2, 'Name or service not known'), id='Name-nicht-auflösbar'),
    pytest.param(OSError(113, 'No route to host'), id='keine-Route'),
    pytest.param(smtplib.SMTPServerDisconnected(f'Connection unexpectedly closed: timed out {PATH_TEXT}'),
                 id='Verbindung-abgebrochen'),
    pytest.param(smtplib.SMTPConnectError(421, b'Service not available'), id='Server-nicht-bereit'),
])
@pytest.mark.parametrize('where', ['init', 'ehlo', 'send_message'])
def test_timeouts_and_connection_errors_say_the_server_is_not_reachable(smtp, pdf, error, where):
    smtp.errors[where] = error
    result = send(pdf)
    assert result == (False, NOT_REACHABLE)
    assert PATH_TEXT not in result[1] and 'timed out' not in result[1]


def test_a_failed_login_keeps_its_message_and_leaks_nothing_from_the_server(smtp, pdf):
    smtp.errors['login'] = smtplib.SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')
    result = send(pdf)
    assert result == (False, 'SMTP-Authentifizierung fehlgeschlagen. Bitte Zugangsdaten überprüfen.')
    assert 'Username' not in result[1] and smtp.sent == []


@pytest.mark.parametrize('error, message', [
    pytest.param(smtplib.SMTPDataError(554, b'Transaction failed /var/spool/mail/x'),
                 'SMTP-Fehler beim Versand (Statuscode 554).', id='mit-Statuscode'),
    pytest.param(smtplib.SMTPSenderRefused(553, b'Sender rejected', 'info@example.com'),
                 'SMTP-Fehler beim Versand (Statuscode 553).', id='Absender-abgelehnt'),
    pytest.param(smtplib.SMTPHeloError(501, b'bad helo'), 'SMTP-Fehler beim Versand (Statuscode 501).', id='HELO'),
    pytest.param(smtplib.SMTPException(f'irgendwas {PATH_TEXT}'), 'SMTP-Fehler beim Versand.', id='ohne-Code'),
    pytest.param(smtplib.SMTPRecipientsRefused({'kunde@example.com': (550, b'no such user')}),
                 'SMTP-Fehler beim Versand.', id='Empfänger-abgelehnt'),
    pytest.param(smtplib.SMTPNotSupportedError('SMTP AUTH extension not supported by server.'),
                 'SMTP-Fehler beim Versand.', id='AUTH-nicht-unterstützt'),
])
def test_other_smtp_errors_name_only_the_status_code(smtp, pdf, error, message):
    smtp.errors['send_message'] = error
    result = send(pdf)
    assert result == (False, message)
    assert PATH_TEXT not in result[1] and 'Transaction' not in result[1] and 'no such user' not in result[1]


def test_any_other_error_says_only_that_sending_failed(smtp, pdf, capsys):
    smtp.errors['send_message'] = RuntimeError(f'kaputt bei {PATH_TEXT}')
    assert send(pdf) == (False, 'Fehler beim E-Mail-Versand.')
    log = capsys.readouterr().err
    assert 'RuntimeError' in log and PATH_TEXT in log          # der volle Fehler steht im Server-Log


def test_an_unreadable_attachment_says_only_that_sending_failed(smtp, pdf, monkeypatch, capsys):
    real_open = open

    def refuse(path, *args, **kwargs):
        if str(path) == pdf:
            raise PermissionError(13, f'Permission denied: {pdf}')
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr('builtins.open', refuse)
    result = send(pdf)
    assert result == (False, 'Fehler beim E-Mail-Versand.')      # nicht: „Der Mailserver ist nicht erreichbar“
    assert smtp.created == []
    assert pdf not in result[1] and 'Permission denied' in capsys.readouterr().err


def test_credentials_never_reach_the_log(smtp, pdf, capsys):
    smtp.errors['login'] = RuntimeError('Anmeldung kaputt')
    send(pdf)
    smtp.errors['send_message'] = smtplib.SMTPDataError(554, b'nein')
    del smtp.errors['login']
    send(pdf)
    out = capsys.readouterr()
    assert PASSWORD not in out.err and PASSWORD not in out.out


def test_without_credentials_nothing_is_tried(smtp, pdf, monkeypatch):
    monkeypatch.delenv('SMTP_USER', raising=False)               # sonst sprängen die Umgebungsvariablen des Entwicklers ein
    monkeypatch.delenv('SMTP_PASSWORD', raising=False)
    result = EmailSender(smtp_host='smtp.example.org', smtp_port=587, smtp_user='', smtp_password='').send_invoice_email(
        'kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', 'info@example.com')
    assert result[0] is False and 'SMTP-Zugangsdaten nicht konfiguriert' in result[1]
    assert smtp.created == []


# ── Kopfzeilen ────────────────────────────────────────────────────────────────
def test_a_name_with_a_comma_is_one_recipient(smtp, pdf):
    assert send(pdf, recipient_name='Mustermann, Erika') == (True, None)
    msg = smtp.sent[0]
    assert getaddresses(msg.get_all('To')) == [('Mustermann, Erika', 'kunde@example.com')]
    assert msg['To'] == '"Mustermann, Erika" <kunde@example.com>'
    assert smtp.envelopes == [('info@example.com', ['kunde@example.com'])]          # im Umschlag genau EIN Empfänger


@pytest.mark.parametrize('name', ['Eigene Firma GmbH & Co. KG', 'Meier, Müller; Partner "und" Söhne', 'Dr. Ernst <Chef>'])
def test_names_with_special_characters_stay_one_address_in_both_headers(smtp, pdf, name):
    assert send(pdf, recipient_name=name, sender_name=name) == (True, None)
    msg = smtp.sent[0]
    for header, address in (('To', 'kunde@example.com'), ('From', 'info@example.com')):
        (parsed_name, parsed_address), = getaddresses(msg.get_all(header))
        assert parsed_address == address
        assert str(email.header.make_header(email.header.decode_header(parsed_name))) == name
    assert smtp.envelopes == [('info@example.com', ['kunde@example.com'])]


def test_plain_names_keep_the_form_the_mails_always_had(smtp, pdf):
    send(pdf)
    msg = smtp.sent[0]
    assert msg['To'] == 'Erika Empfaenger <kunde@example.com>'
    assert msg['From'] == 'Eigene Firma GmbH <info@example.com>'
    assert msg['Subject'] == 'Rechnung R26001'


def test_without_names_only_the_addresses_are_written_and_the_login_user_is_the_fallback_sender(smtp, pdf):
    send(pdf, recipient_name='', sender_name=None, sender_email='')
    msg = smtp.sent[0]
    assert msg['To'] == 'kunde@example.com' and msg['From'] == USER


BREAKS = ['a\r\nBcc: spion@example.com', 'a\nBcc: spion@example.com', 'a\rb', '\n', 'x\r\n\r\nGeheimer Text']


@pytest.mark.parametrize('field', ['recipient_email', 'recipient_name', 'sender_name', 'sender_email'])
@pytest.mark.parametrize('value', BREAKS)
def test_line_breaks_in_addresses_and_names_are_refused_before_any_connection(smtp, pdf, field, value):
    assert send(pdf, **{field: value}) == (False, INVALID_ADDRESS)
    assert smtp.created == [] and smtp.calls == [] and smtp.sent == []


def test_a_line_break_in_the_login_user_is_refused_when_it_would_be_the_sender(smtp, pdf):
    result = EmailSender(smtp_host='smtp.example.org', smtp_port=587, smtp_user='versand@example.com\r\nBcc: x@example.com',
                         smtp_password=PASSWORD).send_invoice_email(
        'kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', '')
    assert result == (False, INVALID_ADDRESS) and smtp.created == []


@pytest.mark.parametrize('field, value', [
    ('recipient_email', ''), ('recipient_email', None), ('recipient_email', ['kunde@example.com']),
    ('recipient_email', 'müller@example.com'), ('recipient_name', ['Erika']), ('recipient_name', 7),
    ('sender_name', {'a': 1}), ('sender_email', 'info@bücher.example.com'),
])
def test_values_that_are_not_a_plain_ascii_address_or_a_text_are_refused(smtp, pdf, field, value):
    assert send(pdf, **{field: value}) == (False, INVALID_ADDRESS)
    assert smtp.created == []


# Alles, was KEINE einzelne schlichte Adresse ist: Eine Liste, ein Name mit Klammern, ein Kommentar, ein angehängter
# zweiter Empfänger ergäben im Kopf oder im Umschlag mehr als diesen einen Empfänger.
NOT_ONE_ADDRESS = [
    pytest.param('kunde@example.org>, <spion@example.net', id='zweiter-Empfänger-hinter-Klammern'),
    pytest.param('a@example.de, c@example.net', id='Komma'),
    pytest.param('a@example.de,c@example.net', id='Komma-ohne-Leerzeichen'),
    pytest.param('a@example.de;c@example.net', id='Semikolon'),
    pytest.param('"x" <a@example.de>', id='Name-in-Klammern'),
    pytest.param('<a@example.de>', id='nur-Klammern'),
    pytest.param('a@example.de (Kommentar)', id='Kommentar'),
    pytest.param('a(Kommentar)@example.de', id='Kommentar-im-lokalen-Teil'),
    pytest.param(' a@example.de', id='Leerzeichen-davor'),
    pytest.param('a@example.de ', id='Leerzeichen-dahinter'),
    pytest.param('a b@example.com', id='Leerzeichen-im-lokalen-Teil'),
    pytest.param('a@example .de', id='Leerzeichen-in-der-Domain'),
    pytest.param('a@example.de\t', id='Tabulator'),
    pytest.param('a@example.de\x00', id='NUL'),
    pytest.param('a@example.de\x7f', id='DEL'),
    pytest.param('a@example.de\x0b', id='senkrechter-Tabulator'),
    pytest.param('a@example.de\r\nBcc: spion@example.net', id='Zeilenumbruch-und-Kopf'),
    pytest.param('a@b@example.com', id='zwei-@'),
    pytest.param('a@example.de@', id='@-am-Ende'),
    pytest.param('@example.de', id='lokaler-Teil-leer'),
    pytest.param('a@', id='Domain-leer'),
    pytest.param('@', id='nur-@'),
    pytest.param('kunde', id='ohne-@'),
    pytest.param('a<b>@example.com', id='spitze-Klammern-im-lokalen-Teil'),
    pytest.param('a:b@example.com', id='Doppelpunkt'),
    pytest.param('a[b]@example.com', id='eckige-Klammern'),
    pytest.param('a\\b@example.com', id='Backslash'),
    pytest.param('a"b@example.com', id='Anführungszeichen'),
    pytest.param('a@[127.0.0.1]', id='Domain-als-Literal'),
]


@pytest.mark.parametrize('field', ['recipient_email', 'sender_email'])
@pytest.mark.parametrize('value', NOT_ONE_ADDRESS)
def test_anything_but_one_plain_address_is_refused_before_any_connection(smtp, pdf, field, value):
    assert send(pdf, **{field: value}) == (False, INVALID_ADDRESS)
    assert smtp.created == [] and smtp.calls == [] and smtp.sent == [] and smtp.envelopes == []


@pytest.mark.parametrize('value', NOT_ONE_ADDRESS)
def test_the_same_holds_for_the_login_user_when_it_becomes_the_sender(smtp, pdf, value):
    result = EmailSender(smtp_host='smtp.example.org', smtp_port=587, smtp_user=value, smtp_password=PASSWORD
                         ).send_invoice_email('kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', '')
    assert result == (False, INVALID_ADDRESS) and smtp.created == []


@pytest.mark.parametrize('address', [
    'kunde@example.org', "o'brien@example.com", 'a.b+rechnung@example.com', 'kunde@localhost', 'a_b-c@example.de',
    'a=b@example.com', 'x@y', 'a!b#c$d%e&f*g@example.com', 'a{b}|c~d@example.com', 'a/b?c^d`e@example.com',
])
@pytest.mark.parametrize('field', ['recipient_email', 'sender_email'])
def test_a_plain_address_goes_through_and_is_the_only_recipient_in_the_envelope(smtp, pdf, address, field):
    assert send(pdf, **{field: address}) == (True, None)
    wanted_sender = address if field == 'sender_email' else 'info@example.com'
    wanted_recipient = address if field == 'recipient_email' else 'kunde@example.com'
    assert smtp.envelopes == [(wanted_sender, [wanted_recipient])]          # wie ``send_message`` sie aus den Köpfen liest
    msg = smtp.sent[0]
    assert [a for _, a in getaddresses(msg.get_all('To'))] == [wanted_recipient]
    assert [a for _, a in getaddresses(msg.get_all('From'))] == [wanted_sender]


def test_what_the_old_check_let_through_never_reaches_the_envelope(smtp, pdf):
    """Der Wert aus dem Fehlerbericht: Er ergab einen zweiten Empfänger im Umschlag."""
    hostile = 'kunde@example.org>, <spion@example.net'
    assert getaddresses([f'<{hostile}>'])[0][1] == 'kunde@example.org' and len(getaddresses([f'<{hostile}>'])) == 2
    assert send(pdf, recipient_email=hostile) == (False, INVALID_ADDRESS)
    assert smtp.envelopes == []


# ── Einrichtung und Anhang: Klartext statt Exception-Text oder Pfad ───────────
@pytest.mark.parametrize('port', ['abc', '', '0', '-1', '65536', '99999999999', '587.5', '5 87', '0x24b', '587a'])
def test_an_unusable_smtp_port_is_named_and_nothing_is_tried(smtp, pdf, monkeypatch, port):
    monkeypatch.setenv('SMTP_PORT', port)
    sender_without_port = EmailSender(smtp_host='smtp.example.org', smtp_user=USER, smtp_password=PASSWORD)  # wirft nicht
    result = sender_without_port.send_invoice_email('kunde@example.com', 'Erika', 'R26001', pdf, 'Firma',
                                                    'info@example.com')
    assert result == (False, 'Der Mailserver ist nicht richtig eingerichtet (SMTP_PORT).')
    assert smtp.created == []


@pytest.mark.parametrize('port', ['abc', 'x', '0', '70000'])
def test_an_unusable_port_given_directly_is_named_too(smtp, pdf, port):
    result = EmailSender(smtp_host='smtp.example.org', smtp_port=port, smtp_user=USER, smtp_password=PASSWORD
                         ).send_invoice_email('kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', 'info@example.com')
    assert result == (False, 'Der Mailserver ist nicht richtig eingerichtet (SMTP_PORT).') and smtp.created == []


@pytest.mark.parametrize('port, expected', [('25', 25), ('587', 587), ('465', 465), ('1', 1), ('65535', 65535),
                                           (' 2525 ', 2525), (587, 587)])
def test_a_usable_smtp_port_is_taken_as_a_number(smtp, pdf, monkeypatch, port, expected):
    monkeypatch.setenv('SMTP_PORT', str(port))
    sender_from_env = EmailSender(smtp_host='smtp.example.org', smtp_user=USER, smtp_password=PASSWORD)
    assert sender_from_env.smtp_port == expected
    assert sender_from_env.send_invoice_email('kunde@example.com', 'Erika', 'R26001', pdf, 'Firma',
                                              'info@example.com') == (True, None)
    assert smtp.created[0][2] == expected


def test_a_missing_credential_is_still_reported_before_a_bad_port(smtp, pdf, monkeypatch):
    monkeypatch.setenv('SMTP_PORT', 'abc')
    monkeypatch.delenv('SMTP_USER', raising=False)
    monkeypatch.delenv('SMTP_PASSWORD', raising=False)
    result = EmailSender(smtp_host='smtp.example.org', smtp_user='', smtp_password='').send_invoice_email(
        'kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', 'info@example.com')
    assert result[0] is False and 'SMTP-Zugangsdaten nicht konfiguriert' in result[1]


def test_a_missing_pdf_gets_a_plain_message_without_the_path_and_nothing_is_tried(smtp, tmp_path, capsys):
    missing = str(tmp_path / 'gibt-es-nicht.pdf')
    result = send(missing)
    assert result == (False, 'Die PDF-Datei wurde nicht gefunden.')
    assert missing not in result[1] and str(tmp_path) not in result[1]
    assert missing in capsys.readouterr().out                    # der Pfad steht im Server-Log
    assert smtp.created == []


# ── Mit echten Sockets (ohne Zertifikat) ──────────────────────────────────────
class MiniSmtp:
    """Ein Mini-SMTP-Server auf 127.0.0.1 für EINE Verbindung: meldet sich, antwortet auf EHLO (mit oder ohne
    STARTTLS) und merkt sich die Befehlswörter des Clients. Nach STARTTLS schickt er ``after_starttls`` statt
    eines TLS-Handshakes."""

    def __init__(self, offers_starttls=True, after_starttls=b'Das ist kein TLS.\r\n' * 4):
        self.offers_starttls, self.after_starttls = offers_starttls, after_starttls
        self.seen = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                reader = conn.makefile('rb')
                conn.sendall(b'220 mini.example.org ESMTP\r\n')
                for line in reader:
                    verb = line.decode('ascii', 'replace').split()[0].upper() if line.strip() else ''
                    self.seen.append(verb)
                    if verb == 'EHLO':
                        conn.sendall(b'250-mini.example.org\r\n' + (b'250-STARTTLS\r\n' if self.offers_starttls else b'')
                                     + b'250 AUTH PLAIN LOGIN\r\n')
                    elif verb == 'STARTTLS':
                        conn.sendall(b'220 Ready to start TLS\r\n')
                        conn.recv(4096)                      # erst nach dem ClientHello antworten: kam die Antwort
                        conn.sendall(self.after_starttls)    # früher an, läse smtplib sie noch als SMTP-Zeile
                        while conn.recv(4096):               # und dann ruhig warten, bis der Client auflegt
                            pass
                        break
                    elif verb == 'QUIT':
                        conn.sendall(b'221 bye\r\n')
                        break
                    else:
                        conn.sendall(b'502 not implemented\r\n')
            except OSError:
                pass

    def close(self):
        self.listener.close()
        self.thread.join(timeout=10)


@pytest.fixture
def mini_smtp():
    servers = []

    def start(**kwargs):
        servers.append(MiniSmtp(**kwargs))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def real_send(pdf, port, monkeypatch, timeout=5):
    monkeypatch.setattr(email_sender, 'SMTP_TIMEOUT', timeout)
    return EmailSender(smtp_host='127.0.0.1', smtp_port=port, smtp_user=USER, smtp_password=PASSWORD
                       ).send_invoice_email('kunde@example.com', 'Erika', 'R26001', pdf, 'Firma', 'info@example.com')


def test_a_real_server_without_starttls_never_sees_a_login_or_the_message(mini_smtp, pdf, monkeypatch):
    server = mini_smtp(offers_starttls=False)
    assert real_send(pdf, server.port, monkeypatch) == (False, NO_STARTTLS)
    server.thread.join(timeout=10)
    assert server.seen[0] == 'EHLO'
    assert not {'AUTH', 'MAIL', 'RCPT', 'DATA', 'STARTTLS'} & set(server.seen)       # nur EHLO (und danach QUIT)


def test_a_real_server_that_cannot_do_tls_gets_no_login_either(mini_smtp, pdf, monkeypatch):
    server = mini_smtp(offers_starttls=True)
    assert real_send(pdf, server.port, monkeypatch) == (False, TLS_FAILED)
    server.thread.join(timeout=10)
    assert server.seen == ['EHLO', 'STARTTLS']              # der Handshake scheitert; AUTH/MAIL folgen nie


def test_a_server_that_never_answers_is_given_up_after_the_timeout(pdf, monkeypatch):
    silent = socket.socket()
    silent.bind(('127.0.0.1', 0))
    silent.listen(1)                                         # die Verbindung kommt zustande, ein Banner nie
    try:
        assert real_send(pdf, silent.getsockname()[1], monkeypatch, timeout=0.5) == (False, NOT_REACHABLE)
    finally:
        silent.close()


def test_a_closed_port_says_the_server_is_not_reachable(pdf, monkeypatch):
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()                                            # nichts lauscht mehr dort
    assert real_send(pdf, port, monkeypatch) == (False, NOT_REACHABLE)
