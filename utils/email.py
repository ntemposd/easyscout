import os
import smtplib
from email.message import EmailMessage
from typing import List, Optional, Union


def send_email(
    to: Union[str, List[str]],
    subject: str,
    *,
    html: Optional[str] = None,
    text: Optional[str] = None,
    reply_to: Optional[Union[str, List[str]]] = None,
):
    """Send an email via Mailjet SMTP.

    Requires env vars:
    - MAILJET_API_KEY
    - MAILJET_SECRET_KEY
    - MAILJET_SENDER_EMAIL (verified sender/domain in Mailjet)
    """

    api_key = os.getenv("MAILJET_API_KEY")
    secret = os.getenv("MAILJET_SECRET_KEY")
    sender = os.getenv("MAILJET_SENDER_EMAIL") or os.getenv("EMAIL_FROM")

    if not api_key or not secret:
        raise RuntimeError("Missing MAILJET_API_KEY or MAILJET_SECRET_KEY")
    if not sender:
        raise RuntimeError("Missing MAILJET_SENDER_EMAIL (or EMAIL_FROM)")
    if not html and not text:
        raise ValueError("Provide at least one body: html or text")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to) if isinstance(reply_to, list) else reply_to

    if text:
        msg.set_content(text)
    if html:
        # add_alternative preserves text if set_content was called
        msg.add_alternative(html, subtype="html")

    host = os.getenv("MAILJET_SMTP_HOST", "in-v3.mailjet.com")
    port = int(os.getenv("MAILJET_SMTP_PORT", "587"))  # TLS by default

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(api_key, secret)
        server.send_message(msg)

    return {"ok": True}
