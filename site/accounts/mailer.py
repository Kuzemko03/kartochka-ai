"""
Email-отправка — абстракция с реальным SMTP (reg.ru) и заглушкой для разработки.

На старте сервера вызывается ``init_mailer_from_config(CFG)`` (см. server.py):
  • если блок ``smtp`` в config.json настроен (enabled + host/user/password) —
    активируется ``SmtpMailer`` и письма реально уходят;
  • иначе остаётся ``StubMailer`` — письмо только пишется в лог (dev-режим),
    ничего не падает.

Пароль ящика лучше держать НЕ в config.json, а в переменной окружения
``SMTP_PASSWORD`` (она перебивает значение из конфига) — так секрет не лежит в
файле. См. deploy/kartochka.service.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Optional

logger = logging.getLogger("mailer")


class Mailer(ABC):
    is_stub: bool = False

    @abstractmethod
    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
        ...


class StubMailer(Mailer):
    """Ничего не отправляет — только пишет письмо в лог (для разработки)."""
    is_stub = True

    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
        logger.info("[StubMailer] письмо НЕ отправлено (заглушка):\n"
                    "  to=%s\n  subject=%s\n  %s", to, subject, text.replace("\n", "\n  "))
        return True


class SmtpMailer(Mailer):
    """Реальная отправка через SMTP.

    reg.ru: host=mail.hosting.reg.ru, port=465, ssl=True.
      ssl=True  → SMTP_SSL (порт 465)
      ssl=False → обычный SMTP + STARTTLS (порт 587)
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 sender: Optional[str] = None, use_ssl: bool = True):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.sender = sender or user
        self.use_ssl = use_ssl

    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = to
        envelope_from = parseaddr(self.sender)[1] or self.user
        try:
            if self.use_ssl:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=20) as s:
                    s.login(self.user, self.password)
                    s.sendmail(envelope_from, [to], msg.as_string())
            else:
                with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                    s.ehlo()
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                    s.login(self.user, self.password)
                    s.sendmail(envelope_from, [to], msg.as_string())
            logger.info("[SmtpMailer] отправлено → to=%s subject=%s", to, subject)
            return True
        except Exception:
            logger.exception("[SmtpMailer] не удалось отправить письмо to=%s", to)
            return False


_ACTIVE: Mailer = StubMailer()


def register_mailer(m: Mailer) -> None:
    global _ACTIVE
    _ACTIVE = m


def get_mailer() -> Mailer:
    return _ACTIVE


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def init_mailer_from_config(cfg: Optional[dict]) -> None:
    """Активировать SmtpMailer, если почта настроена; иначе оставить StubMailer.

    Источники (переменная окружения ПЕРЕБИВАЕТ config.json — секреты держим в env):
      SMTP_ENABLED  / smtp.enabled
      SMTP_HOST     / smtp.host
      SMTP_PORT     / smtp.port      (default 465)
      SMTP_USER     / smtp.user
      SMTP_PASSWORD / smtp.password
      SMTP_SENDER   / smtp.sender    (default = user)
      SMTP_SSL      / smtp.ssl       (default true)
    """
    smtp = (cfg or {}).get("smtp") or {}

    enabled = smtp.get("enabled", False)
    if "SMTP_ENABLED" in os.environ:
        enabled = _truthy(os.environ["SMTP_ENABLED"])

    host = os.environ.get("SMTP_HOST") or smtp.get("host")
    port = os.environ.get("SMTP_PORT") or smtp.get("port") or 465
    user = os.environ.get("SMTP_USER") or smtp.get("user")
    password = os.environ.get("SMTP_PASSWORD") or smtp.get("password")
    sender = os.environ.get("SMTP_SENDER") or smtp.get("sender") or user
    use_ssl = smtp.get("ssl", True)
    if "SMTP_SSL" in os.environ:
        use_ssl = _truthy(os.environ["SMTP_SSL"])

    if not (enabled and host and user and password):
        logger.info("[mailer] SMTP не настроен (нужны enabled+host+user+password) → "
                    "остаётся StubMailer, письма пишутся только в лог")
        return

    register_mailer(SmtpMailer(host, int(port), user, password, sender, use_ssl=bool(use_ssl)))
    logger.info("[mailer] SMTP активен: %s:%s, отправитель %s", host, port, sender)


def send_password_reset(to: str, link: str) -> bool:
    subject = "Восстановление пароля — карточка Ai"
    text = (
        "Вы запросили смену пароля в сервисе «карточка Ai».\n"
        "Перейдите по ссылке, чтобы задать новый пароль:\n\n"
        f"{link}\n\n"
        "Ссылка действует 1 час. Если вы не запрашивали смену пароля — просто "
        "проигнорируйте это письмо, ваш пароль останется прежним."
    )
    try:
        return get_mailer().send(to, subject, text)
    except Exception:
        logger.exception("send_password_reset failed")
        return False
