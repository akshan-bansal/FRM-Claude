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

    # Telegram message length cap. sendMessage rejects payloads over 4096 chars with 400. Our
    # rich alerts CAN cross that once the WF evidence + sizing chain + interpret block all fire
    # together, so trim the tail rather than lose the whole message.
    _TELEGRAM_MAX = 4096
    _TELEGRAM_TAIL = "\n[…truncated for phone limit]"

    def _telegram(self, title: str, body: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            # Plain text — no parse_mode. Classic Markdown rejects unescaped _*[]`\ and MarkdownV2
            # requires escaping ~a dozen chars including - . ! + < >, which our sizing-chain and
            # WF-evidence bodies routinely use. Sending as plain text lets the reader see the
            # message verbatim; the title on its own line + all-caps is enough visual separation.
            text = f"{title}\n{body}"
            if len(text) > self._TELEGRAM_MAX:
                text = text[: self._TELEGRAM_MAX - len(self._TELEGRAM_TAIL)] + self._TELEGRAM_TAIL
            httpx.post(
                url,
                data={
                    "chat_id": self.config.telegram_chat_id,
                    "text": text,
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
