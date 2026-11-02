"""自動寄送註冊連結／密碼重設連結，並將寄送結果通知管理員 Teams Channel。

連結只寄到員工本人的公司信箱（nt_account + COMPANY_EMAIL_DOMAIN），
因此能收信即代表是本人，不需要管理員再手動核對身份。
Teams 通知只包含寄送結果，不包含連結本身。
"""

import math

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.config import get_settings
from app.mail.exchange import render_email, send_mail
from app.schemas import StaffInfo
from app.webhook.teams import send_register_mail_status, send_reset_mail_status

REGISTER_SUBJECT = "【AuthCenter】帳號註冊連結"
RESET_SUBJECT = "【AuthCenter】密碼重設連結"


def _cooldown_ctx(email: str, remaining: int) -> dict:
    return {"mail_status": "cooldown", "mail_to": email, "retry_minutes": math.ceil(remaining / 60)}


async def mail_register_link(
    sqlite_session: AsyncSession,
    staff: StaffInfo,
    employee_name: str,
    request_token: str,
    app_id: str,
    redirect_uri: str,
    app_name: str,
) -> dict:
    """產生註冊連結寄到員工信箱，並通知管理員 Channel 寄送結果。

    - 寄送成功：作廢該員工其他 registration tokens（含本次申請用的 request token），只留剛寄出的連結
    - 寄送失敗：作廢未送達的連結，保留 request token 24 小時，讓管理員在 Dashboard 手動產生連結
    - 冷卻中（5 分鐘內已寄過）：不寄信、不通知

    回傳頁面 context：mail_status（sent / failed / cooldown）、mail_to、expire_hours 或 retry_minutes。
    """
    employee_name = service.normalize_employee_name(employee_name)
    email = service.build_staff_email(employee_name)
    cooldown_key = f"register:{employee_name}"
    remaining = service.mail_cooldown_remaining(cooldown_key)
    if remaining:
        return _cooldown_ctx(email, remaining)

    token = await service.generate_registration_token(
        sqlite_session, employee_name, app_id, redirect_uri,
        ttl=service.REGISTER_LINK_TTL, purpose=service.TOKEN_PURPOSE_REGISTER,
    )
    base_url = get_settings().AUTH_CENTER_BASE_URL.rstrip("/")
    expire_hours = service.REGISTER_LINK_TTL // 3600
    html = render_email(
        "register_link.html",
        employee_name=employee_name,
        app_name=app_name if app_id else "",
        link=f"{base_url}/auth/register?token={token}",
        expire_hours=expire_hours,
    )
    sent, error = await send_mail(email, REGISTER_SUBJECT, html)

    if sent:
        service.record_mail_sent(cooldown_key)
        await service.delete_other_registration_tokens(sqlite_session, employee_name, keep_token=token)
    else:
        await service.invalidate_registration_token(sqlite_session, token)
        await service.extend_registration_token(sqlite_session, request_token, ttl=86400)

    await send_register_mail_status(staff, app_name, email, sent, error)
    return {"mail_status": "sent" if sent else "failed", "mail_to": email, "expire_hours": expire_hours}


async def mail_reset_link(
    sqlite_session: AsyncSession,
    staff: StaffInfo,
    employee_name: str,
) -> dict:
    """產生密碼重設連結寄到員工信箱，並通知管理員 Channel 寄送結果。

    - 寄送成功：作廢該員工其他 reset tokens，只留剛寄出的連結
    - 寄送失敗：作廢未送達的連結，由管理員在「會員管理」手動產生
    - 冷卻中（5 分鐘內已寄過）：不寄信、不通知

    回傳頁面 context：mail_status（sent / failed / cooldown）、mail_to、expire_hours 或 retry_minutes。
    """
    employee_name = service.normalize_employee_name(employee_name)
    email = service.build_staff_email(employee_name)
    cooldown_key = f"reset:{employee_name}"
    remaining = service.mail_cooldown_remaining(cooldown_key)
    if remaining:
        return _cooldown_ctx(email, remaining)

    token = await service.generate_password_reset_token(sqlite_session, employee_name)
    base_url = get_settings().AUTH_CENTER_BASE_URL.rstrip("/")
    expire_hours = service.PASSWORD_RESET_TOKEN_TTL // 3600
    html = render_email(
        "reset_link.html",
        employee_name=employee_name,
        link=f"{base_url}/auth/reset-password?token={token}",
        expire_hours=expire_hours,
    )
    sent, error = await send_mail(email, RESET_SUBJECT, html)

    if sent:
        service.record_mail_sent(cooldown_key)
        await service.delete_other_password_reset_tokens(sqlite_session, employee_name, keep_token=token)
    else:
        await service.invalidate_password_reset_token(sqlite_session, token)

    await send_reset_mail_status(staff, email, sent, error)
    return {"mail_status": "sent" if sent else "failed", "mail_to": email, "expire_hours": expire_hours}
