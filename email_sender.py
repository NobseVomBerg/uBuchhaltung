# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Email sending functionality for invoices
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import os


class EmailSender:
    """Handle email sending for invoices"""
    
    def __init__(self, smtp_host=None, smtp_port=None, smtp_user=None, smtp_password=None):
        """Initialize email sender with SMTP configuration
        
        Default values can be overridden or set via environment variables
        """
        self.smtp_host = smtp_host or os.getenv('SMTP_HOST', 'smtp.gmail.com')
        self.smtp_port = int(smtp_port or os.getenv('SMTP_PORT', '587'))
        self.smtp_user = smtp_user or os.getenv('SMTP_USER', '')
        self.smtp_password = smtp_password or os.getenv('SMTP_PASSWORD', '')
        
    def send_invoice_email(self, recipient_email, recipient_name, invoice_number, 
                          pdf_path, sender_name, sender_email, message_text=None):
        """Send invoice via email
        
        Args:
            recipient_email: Email address of recipient
            recipient_name: Name of recipient
            invoice_number: Invoice number for subject
            pdf_path: Path to PDF file to attach
            sender_name: Name of sender
            sender_email: Email address of sender
            message_text: Optional custom message text
            
        Returns:
            tuple: (success: bool, error_message: str or None)
        """
        if not self.smtp_user or not self.smtp_password:
            return False, "SMTP-Zugangsdaten nicht konfiguriert. Bitte SMTP_USER und SMTP_PASSWORD in Umgebungsvariablen setzen."
        
        if not os.path.exists(pdf_path):
            return False, f"PDF-Datei nicht gefunden: {pdf_path}"
        
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = f"{sender_name} <{sender_email or self.smtp_user}>"
            msg['To'] = f"{recipient_name} <{recipient_email}>"
            msg['Subject'] = f"Rechnung {invoice_number}"
            
            # Email body
            if not message_text:
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
            
            # Send email
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            
            return True, None
            
        except smtplib.SMTPAuthenticationError:
            return False, "SMTP-Authentifizierung fehlgeschlagen. Bitte Zugangsdaten überprüfen."
        except smtplib.SMTPException as e:
            return False, f"SMTP-Fehler: {str(e)}"
        except Exception as e:
            return False, f"Fehler beim E-Mail-Versand: {str(e)}"
