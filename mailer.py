"""Outgoing e-mail (password reset, verification). Never raises into request handlers: failures are logged."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .core.config import settings

log = logging.getLogger("mave.mail")


def _mask(addr: str) -> str:
    local, _, domain = addr.partition("@")
    return (local[:1] + "***@" + domain) if domain else "***"


def _deliver(msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:  # implicit TLS
        server = smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
    with server as s:
        if settings.smtp_port != 465 and settings.smtp_starttls:
            s.starttls(context=ctx)
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text mail. Returns True when handed to SMTP (or logged in development)."""
    if not settings.smtp_host:
        if settings.is_production:  # the body holds a credential: never write it to production logs
            log.warning("SMTP not configured: mail to %s was not sent", _mask(to))
            return False
        log.info("[dev mail] to=%s subject=%s\n%s", to, subject, body)
        return True
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    try:
        _deliver(msg)
        return True
    except Exception as e:  # noqa: BLE001 - background task: log and carry on
        log.error("mail to %s failed: %s", _mask(to), type(e).__name__)
        return False


def _link(path: str, token: str) -> str:
    return f"{settings.public_url}/{path}?token={token}\n\n" if settings.public_url else ""


def reset_email(token: str) -> tuple[str, str]:
    body = (
        "We received a request to reset your MAVE password.\n\n"
        + (("Open this link:\n" + _link("reset-password", token)) if settings.public_url else "")
        + f"Or enter this code in the app:\n{token}\n\n"
        "It expires in 1 hour and works once. If you did not ask for this, ignore this e-mail: "
        "your password stays unchanged.\n"
    )
    return "Reset your MAVE password", body


def verification_email(token: str) -> tuple[str, str]:
    body = (
        "Welcome to MAVE! Please confirm your e-mail address.\n\n"
        + (("Open this link:\n" + _link("verify-email", token)) if settings.public_url else "")
        + f"Or enter this code in the app:\n{token}\n\n"
        "It expires in 48 hours. If you did not create a MAVE account, ignore this e-mail.\n"
    )
    return "Confirm your MAVE e-mail", body
