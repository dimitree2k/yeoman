"""SMTP email sender for overseer alerts."""
from __future__ import annotations

from email.message import EmailMessage

from yeoman_overseer.comms.cascading import CommsChannel


class SmtpChannel(CommsChannel):
    def __init__(self, *, host: str, port: int = 587, username: str,
                 password: str, from_addr: str, to_addr: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_addr = from_addr
        self._to_addr = to_addr

    @property
    def name(self) -> str:
        return "smtp"

    async def send(self, message: str) -> None:
        import aiosmtplib
        msg = EmailMessage()
        msg["From"] = self._from_addr
        msg["To"] = self._to_addr
        msg["Subject"] = "Yeoman Overseer Alert"
        msg.set_content(message)
        await aiosmtplib.send(
            msg, hostname=self._host, port=self._port,
            username=self._username, password=self._password, start_tls=True,
        )
