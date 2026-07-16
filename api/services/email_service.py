import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import logging

logger = logging.getLogger(__name__)

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # Google App Password
ALERT_RECIPIENT_EMAIL = os.environ.get("ALERT_RECIPIENT_EMAIL", "")

def send_alert_email(subject: str, html_body: str):
    """
    Sends an HTML email alert using the configured SMTP settings.
    """
    if not SMTP_USER or not SMTP_PASS or not ALERT_RECIPIENT_EMAIL:
        logger.warning("SMTP credentials or recipient email not configured. Skipping email alert.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_RECIPIENT_EMAIL

    part = MIMEText(html_body, "html")
    msg.attach(part)

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_RECIPIENT_EMAIL, msg.as_string())
        server.quit()
        logger.info(f"Alert email sent successfully to {ALERT_RECIPIENT_EMAIL}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False
