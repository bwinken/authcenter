"""Exchange (EWS) 寄信模組。

以 exchangelib 透過 NTLM 連線地端 Exchange Server 寄送 HTML 郵件。
exchangelib 是同步函式庫，寄信一律透過 asyncio.to_thread 執行，避免阻塞 event loop。
exchangelib 延遲匯入：未啟用寄信時，即使套件缺失也不影響登入等核心功能。
"""

import asyncio
import threading
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from app.config import get_settings

EWS_TIMEOUT = 20  # 秒；寄信在使用者送出表單時同步進行，exchangelib 預設 120 秒太久

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

_account = None
_account_lock = threading.Lock()


def is_mail_enabled() -> bool:
    """Exchange 設定是否齊全（含用來組合收件信箱的 COMPANY_EMAIL_DOMAIN）。"""
    s = get_settings()
    return all((
        s.EXCHANGE_SERVER, s.EXCHANGE_USER, s.EXCHANGE_PASSWORD, s.EXCHANGE_EMAIL,
        s.COMPANY_EMAIL_DOMAIN,
    ))


def render_email(template_name: str, **context) -> str:
    """渲染 app/templates/email/ 下的郵件範本。"""
    return _jinja.get_template(template_name).render(**context)


def _get_account():
    """建立（並快取）寄件信箱的 exchangelib Account。建立時不連線，第一次寄信才會連線。"""
    global _account
    with _account_lock:
        if _account is None:
            from exchangelib import DELEGATE, NTLM, Account, Configuration, Credentials
            from exchangelib.protocol import BaseProtocol

            BaseProtocol.TIMEOUT = EWS_TIMEOUT
            s = get_settings()
            config = Configuration(
                service_endpoint=f"https://{s.EXCHANGE_SERVER}/EWS/Exchange.asmx",
                credentials=Credentials(s.EXCHANGE_USER, s.EXCHANGE_PASSWORD),
                auth_type=NTLM,
            )
            _account = Account(
                primary_smtp_address=s.EXCHANGE_EMAIL,
                config=config,
                autodiscover=False,
                access_type=DELEGATE,
            )
        return _account


def _send_sync(to: str, subject: str, html_body: str) -> None:
    from exchangelib import HTMLBody, Mailbox, Message

    account = _get_account()
    Message(
        account=account,
        author=Mailbox(email_address=account.primary_smtp_address),
        subject=subject,
        body=HTMLBody(html_body),
        to_recipients=[Mailbox(email_address=to)],
    ).send(save_copy=False)  # 系統通知信不存寄件備份


async def send_mail(to: str, subject: str, html_body: str) -> tuple[bool, str]:
    """寄送 HTML 郵件。

    回傳 (是否成功, 錯誤摘要)。錯誤摘要只取例外訊息第一行，不含連結或認證資訊，
    可以安全地放進 Teams 通知。
    """
    try:
        await asyncio.to_thread(_send_sync, to, subject, html_body)
    except Exception as e:
        lines = str(e).strip().splitlines()
        summary = f"{type(e).__name__}: {lines[0] if lines else ''}"[:200]
        logger.error("寄信失敗 to={} subject={} error={}", to, subject, summary)
        return False, summary
    logger.info("寄信成功 to={} subject={}", to, subject)
    return True, ""
