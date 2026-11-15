# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Email sending functionality for invoices

Der Versand läuft nur über eine verschlüsselte Verbindung mit geprüftem Zertifikat (Port 465: TLS von
Anfang an, sonst STARTTLS); bietet der Server keine Verschlüsselung an, wird nichts gesendet und nie
unverschlüsselt angemeldet. Eine eigene CA macht man dem Prozess über ``SSL_CERT_FILE`` bekannt
(DEPLOYMENT.md). Die Meldungen an den Nutzer nennen nie den Text einer Ausnahme (Pfade, Antworten des
Servers); der volle Fehler steht im Server-Log.
"""
import re
import smtplib
import ssl
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import formataddr, getaddresses
import os

# Verbindungsaufbau und jede Antwort des Servers dürfen höchstens so lange dauern (Sekunden); ohne Grenze
# hinge der Versand an einem Mailserver, der nicht antwortet, bis der Browser aufgibt
SMTP_TIMEOUT = 30
SMTP_SSL_PORT = 465        # SMTPS: TLS von der ersten Zeile an (alle anderen Ports: STARTTLS)


def _is_name(value):
    """Anzeigename: leer oder ein Text ohne Zeilenumbruch (sonst ließe sich ein weiterer Kopf einschleusen)."""
    return value is None or (isinstance(value, str) and '\r' not in value and '\n' not in value)


# Was in EINER Adresse nichts verloren hat: die Trenner und Klammern einer Adressliste, Anführungszeichen,
# Kommentare und der Backslash – dazu jeder Leerraum und jedes Steuerzeichen
_ADDRESS_FORBIDDEN = re.compile(r'[<>,;:"()\[\]\\\s\x00-\x1f\x7f]')


def _is_address(value):
    """Genau EINE schlichte Adresse ``lokal@domain``: nur ASCII, keines der Zeichen ``<>,;:"()[]\\``, kein Leerraum und
    kein Steuerzeichen, genau ein ``@`` mit Text davor und danach – und ``getaddresses`` liest daraus genau dieses eine
    Paar. Alles andere (eine Liste, ``Name <adresse>``, ein Kommentar, ein angehängter zweiter Empfänger) würde im Kopf
    oder im Umschlag zu mehr als diesem einen Empfänger."""
    if not isinstance(value, str) or not value.isascii() or _ADDRESS_FORBIDDEN.search(value):
        return False
    local, at, domain = value.partition('@')
    if not (local and at and domain) or '@' in domain:
        return False
    pairs = getaddresses([value])
    return len(pairs) == 1 and pairs[0][1] == value


def _delivery_error(error):
    """Klartext für den Nutzer zu einer Ausnahme beim Versand – nie ihr eigener Text."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "SMTP-Authentifizierung fehlgeschlagen. Bitte Zugangsdaten überprüfen."
    if isinstance(error, ssl.SSLCertVerificationError):
        return "Das Zertifikat des Mailservers konnte nicht geprüft werden."
    if isinstance(error, ssl.SSLError):
        return "Die verschlüsselte Verbindung zum Mailserver ist fehlgeschlagen."
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return "Der Mailserver ist nicht erreichbar."
    if isinstance(error, smtplib.SMTPException):
        code = getattr(error, 'smtp_code', None)
        return f"SMTP-Fehler beim Versand (Statuscode {code})." if isinstance(code, int) else "SMTP-Fehler beim Versand."
    if isinstance(error, OSError):          # Zeitüberschreitung, Verbindung abgelehnt, Name nicht auflösbar …
        return "Der Mailserver ist nicht erreichbar."
    return "Fehler beim E-Mail-Versand."


class EmailSender:
    """Handle email sending for invoices"""
    
    def __init__(self, smtp_host=None, smtp_port=None, smtp_user=None, smtp_password=None):
        """Initialize email sender with SMTP configuration
        
        Default values can be overridden or set via environment variables
        """
        self.smtp_host = smtp_host or os.getenv('SMTP_HOST', 'smtp.gmail.com')
        try:
            port = int(smtp_port or os.getenv('SMTP_PORT', '587'))
        except (TypeError, ValueError):
            port = None
        # ``None``: kein brauchbarer Port (keine Zahl, außerhalb von 1–65535); der Versand sagt es mit Klartext
        self.smtp_port = port if port is not None and 0 < port < 65536 else None
        self.smtp_user = smtp_user or os.getenv('SMTP_USER', '')
        self.smtp_password = smtp_password or os.getenv('SMTP_PASSWORD', '')
        
    def send_invoice_email(self, recipient_email, recipient_name, invoice_number, 
                          pdf_path, sender_name, sender_email, message_text=None, storno=False):
        """Send invoice via email
        
        Args:
            recipient_email: Email address of recipient
            recipient_name: Name of recipient
            invoice_number: Invoice number for subject
            pdf_path: Path to PDF file to attach
            sender_name: Name of sender
            sender_email: Email address of sender
            message_text: Optional custom message text
            storno: Der Beleg ist eine Stornorechnung (eigener Betreff und Standardtext – sie fordert nichts an)
            
        Returns:
            tuple: (success: bool, error_message: str or None)
        """
        if not self.smtp_user or not self.smtp_password:
            return False, "SMTP-Zugangsdaten nicht konfiguriert. Bitte SMTP_USER und SMTP_PASSWORD in Umgebungsvariablen setzen."

        if self.smtp_port is None:
            return False, "Der Mailserver ist nicht richtig eingerichtet (SMTP_PORT)."

        from_email = sender_email or self.smtp_user
        if not (_is_address(recipient_email) and _is_address(from_email)
                and _is_name(recipient_name) and _is_name(sender_name)):
            return False, "Ungültige E-Mail-Adresse."

        if not os.path.exists(pdf_path):
            print(f"E-Mail-Versand abgebrochen: PDF-Datei nicht gefunden ({pdf_path})")        # der Pfad nur ins Server-Log
            return False, "Die PDF-Datei wurde nicht gefunden."
        
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = formataddr((sender_name, from_email))
            msg['To'] = formataddr((recipient_name, recipient_email))
            msg['Subject'] = f"{'Stornorechnung' if storno else 'Rechnung'} {invoice_number}"
            
            # Email body
            if not message_text and storno:
                message_text = f"""Sehr geehrte Damen und Herren,

anbei erhalten Sie die Stornorechnung {invoice_number}.

Der Betrag wird verrechnet bzw. erstattet.

Bei Rückfragen stehen wir Ihnen gerne zur Verfügung.

Mit freundlichen Grüßen
{sender_name}
"""
            elif not message_text:
                message_text = f"""Sehr geehrte Damen und Herren,

anbei erhalten Sie die Rechnung {invoice_number}.

Bitte überweisen Sie den Rechnungsbetrag unter Angabe der Rechnungsnummer 
innerhalb der angegebenen Zahlungsfrist.

Bei Rückfragen stehen wir Ihnen gerne zur Verfügung.

Mit freundlichen Grüßen
{sender_name}
"""
            
            msg.attach(MIMEText(message_text, 'plain', 'utf-8'))
            
            # Attach PDF
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
                pdf_attachment = MIMEApplication(pdf_data, _subtype='pdf')
                # Anhang trägt denselben Namen wie die abgelegte PDF-Datei
                # (Namenskonvention "[Rechnungsnummer] [Kundenname]")
                pdf_attachment.add_header('Content-Disposition', 'attachment',
                                        filename=os.path.basename(pdf_path))
                msg.attach(pdf_attachment)
        except Exception:
            traceback.print_exc()
            return False, "Fehler beim E-Mail-Versand."

        # Send email: Zertifikat und Hostname werden geprüft, es gibt keinen Schalter dagegen
        try:
            context = ssl.create_default_context()
            if self.smtp_port == SMTP_SSL_PORT:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=SMTP_TIMEOUT)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT)
            with server:
                if self.smtp_port != SMTP_SSL_PORT:
                    server.ehlo()
                    if not server.has_extn('starttls'):
                        # Nie unverschlüsselt anmelden: Zugangsdaten und Rechnung blieben sonst im Klartext
                        return False, ("Der Mailserver bietet keine verschlüsselte Verbindung (STARTTLS) an – "
                                       "die E-Mail wurde nicht gesendet.")
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            
            return True, None

        except Exception as e:
            traceback.print_exc()            # Einzelheiten nur ins Server-Log (keine Zugangsdaten darin)
            return False, _delivery_error(e)
