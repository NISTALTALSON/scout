"""TLS-only SMTP adapter. Never expose server responses or credentials in errors."""

import os
import re
import smtplib
import ssl
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage

from scout.models import Notification

_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
_MESSAGE_ID = re.compile(r"<[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+>\Z")


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


def _address(value: str, name: str) -> str:
    if not _ADDRESS.fullmatch(value) or len(value) > 254:
        raise ValueError(f"{name} must be one bare email address")
    return value


@dataclass(frozen=True)
class SmtpSender:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    recipient: str
    security: str
    timeout: float = 15.0

    def __post_init__(self):
        if not self.host or any(ord(c) < 33 or ord(c) > 126 for c in self.host):
            raise ValueError("invalid SMTP_HOST")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("invalid SMTP_PORT")
        if not self.username or not self.password:
            raise ValueError("SMTP credentials required")
        _address(self.from_address, "EMAIL_FROM")
        _address(self.recipient, "EMAIL_TO")
        if self.security not in {"starttls", "ssl"}:
            raise ValueError("SMTP_SECURITY must be starttls or ssl")
        if self.timeout <= 0:
            raise ValueError("positive SMTP timeout required")

    @classmethod
    def from_env(cls) -> "SmtpSender":
        if os.getenv("EMAIL_ENABLED", "").lower() != "true":
            raise ValueError("email delivery disabled (EMAIL_ENABLED must be true)")
        names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                 "EMAIL_FROM", "EMAIL_TO", "SMTP_SECURITY")
        values = {name: os.getenv(name, "") for name in names}
        if any(not value for value in values.values()):
            raise ValueError("SMTP and email configuration incomplete")
        try:
            port = int(values["SMTP_PORT"])
        except ValueError as exc:
            raise ValueError("invalid SMTP_PORT") from exc
        return cls(values["SMTP_HOST"], port, values["SMTP_USERNAME"],
                   values["SMTP_PASSWORD"], values["EMAIL_FROM"], values["EMAIL_TO"],
                   values["SMTP_SECURITY"].lower())

    def send(self, notification: Notification) -> None:
        if any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029"
               for c in notification.subject):
            raise DeliveryError("invalid mail subject", retryable=False)
        if not _MESSAGE_ID.fullmatch(notification.message_id):
            raise DeliveryError("invalid Message-ID", retryable=False)
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = self.recipient
        message["Subject"] = notification.subject
        message["Message-ID"] = notification.message_id
        message.set_content(notification.text_body)
        message.add_alternative(notification.html_body, subtype="html")
        client = None
        accepted = False
        try:
            context = ssl.create_default_context()
            if self.security == "ssl":
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                          context=context)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                client.ehlo()
                client.starttls(context=context)  # Mandatory, no insecure fallback.
                client.ehlo()
            client.login(self.username, self.password)
            refused = client.send_message(message, from_addr=self.from_address,
                                          to_addrs=[self.recipient])
            if refused:
                raise DeliveryError("SMTP recipient refused", retryable=False)
            accepted = True
        except DeliveryError:
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            code = next((item[0] for item in exc.recipients.values()
                         if isinstance(item, tuple) and isinstance(item[0], int)), 550)
            raise DeliveryError("SMTP recipient refused", retryable=400 <= code < 500) from None
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError(f"SMTP rejected request ({exc.smtp_code})",
                                retryable=400 <= exc.smtp_code < 500) from None
        except (smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError):
            raise DeliveryError("SMTP TLS unavailable or certificate invalid",
                                retryable=False) from None
        except (smtplib.SMTPException, OSError, TimeoutError):
            # A disconnect after DATA can mean acceptance; the outbox imposes a lease/backoff.
            raise DeliveryError("SMTP connection/transport failure", retryable=True,
                                uncertain=True) from None
        finally:
            if client is not None:
                try:
                    if accepted:
                        client.quit()
                    else:
                        client.close()
                except (smtplib.SMTPException, OSError):
                    # Once DATA is accepted, failed QUIT must not trigger a duplicate send.
                    pass
