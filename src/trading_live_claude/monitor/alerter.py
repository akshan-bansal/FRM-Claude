"""Alert sink: print + optional Telegram/email. Failure to alert is logged
but never raises (an alerting outage must not break trading)."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class AlertConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    email_to: str = ""


class Alerter:
    def __init__(self, config: AlertConfig) -> None:
        self.config = config

    def send(self, title: str, body: str) -> None:
        print(f"[ALERT] {title}\n{body}\n")
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            self._telegram(title, body)
        if self.config.smtp_host and self.config.email_to:
            self._email(title, body)

    def _telegram(self, title: str, body: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            httpx.post(
                url,
                data={
                    "chat_id": self.config.telegram_chat_id,
                    "text": f"*{title}*\n{body}",
                    "parse_mode": "Markdown",
                },
                timeout=5.0,
            )
        except Exception as e:  # pragma: no cover
            log.warning("alerter.telegram.failed", error=str(e))

    def _email(self, title: str, body: str) -> None:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = self.config.smtp_user or "trading-live-claude"
            msg["To"] = self.config.email_to
            msg.set_content(body)
            with smtplib.SMTP_SSL(self.config.smtp_host) as s:
                if self.config.smtp_user:
                    s.login(self.config.smtp_user, self.config.smtp_pass)
                s.send_message(msg)
        except Exception as e:  # pragma: no cover
            log.warning("alerter.email.failed", error=str(e))
