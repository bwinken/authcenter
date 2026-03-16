"""Microsoft Teams Webhook notification module.

Supports two channels:
- TEAMS_WEBHOOK_URL: admin channel (Incoming Webhook → channel)
- TEAMS_USER_WEBHOOK_URL: user 1:1 chat (Power Automate HTTP trigger → Flow Bot)
"""

import httpx
from loguru import logger

from app.config import get_settings
from app.schemas import StaffInfo


async def _post_webhook(url: str, payload: dict) -> bool:
    """POST JSON to a webhook URL. Returns True on success."""
    settings = get_settings()
    proxy = settings.HTTP_PROXY or None
    try:
        async with httpx.AsyncClient(timeout=10, proxy=proxy, verify=False) as client:
            resp = await client.post(url, json=payload)
            if resp.is_success:
                logger.info("Webhook 發送成功: %s", url[:60])
            else:
                logger.error("Webhook 失敗: url=%s status=%d body=%s", url[:60], resp.status_code, resp.text[:200])
            return resp.is_success
    except Exception:
        logger.exception("Webhook 發送異常: %s", url[:60])
        return False


def _build_adaptive_card(title: str, subtitle: str, facts: list[dict]) -> dict:
    """Build an Adaptive Card payload for Teams Incoming Webhook."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title},
                        {"type": "TextBlock", "text": subtitle, "wrap": True},
                        {"type": "FactSet", "facts": facts},
                    ],
                },
            }
        ],
    }


async def _send_adaptive_card(title: str, subtitle: str, facts: list[dict]) -> bool:
    """Send an Adaptive Card to the admin channel via Incoming Webhook."""
    settings = get_settings()
    if not settings.TEAMS_WEBHOOK_URL:
        logger.warning("TEAMS_WEBHOOK_URL 未設定，跳過通知")
        return False
    payload = _build_adaptive_card(title, subtitle, facts)
    return await _post_webhook(settings.TEAMS_WEBHOOK_URL, payload)


# ─── User 1:1 Notification (Power Automate) ────────────────────


async def send_user_message(employee_name: str, title: str, body: str) -> bool:
    """Send a message to a specific user via Power Automate HTTP trigger.

    The Power Automate flow receives {email, title, body} and posts
    an Adaptive Card to the user's 1:1 chat with Flow Bot.
    Returns False if TEAMS_USER_WEBHOOK_URL or COMPANY_EMAIL_DOMAIN is not configured.
    """
    settings = get_settings()
    if not settings.TEAMS_USER_WEBHOOK_URL or not settings.COMPANY_EMAIL_DOMAIN:
        logger.info("TEAMS_USER_WEBHOOK_URL 或 COMPANY_EMAIL_DOMAIN 未設定，跳過使用者通知")
        return False

    email = f"{employee_name}@{settings.COMPANY_EMAIL_DOMAIN}"
    payload = {
        "email": email,
        "title": title,
        "body": body,
    }
    sent = await _post_webhook(settings.TEAMS_USER_WEBHOOK_URL, payload)
    if sent:
        logger.info("使用者通知已發送: %s → %s", title, email)
    return sent


# ─── Admin Channel Notifications ───────────────────────────────


async def send_forgot_password_notification(staff: StaffInfo) -> bool:
    """Send a forgot-password notification to admin channel."""
    return await _send_adaptive_card(
        title="🔑 密碼重設請求",
        subtitle="有員工請求重設密碼，請管理員協助處理。",
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
        ],
    )


async def send_registration_request_notification(staff: StaffInfo, app_name: str) -> bool:
    """Send a new-user registration request notification to admin channel."""
    return await _send_adaptive_card(
        title="📋 新員工註冊請求",
        subtitle=(
            "有員工首次登入，尚未建立帳號。"
            "請至管理後台 Dashboard 查看待處理註冊請求並產生註冊連結。"
        ),
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
            {"title": "欲存取的 App", "value": app_name},
        ],
    )


async def notify_reset_link(employee_name: str, link: str, admin_name: str) -> bool:
    """Send password reset link to user and notify admin channel with delivery status.

    1. Attempt to send reset link to user via Power Automate
    2. Always notify admin channel with delivery status
    Returns True if admin channel notification succeeded.
    """
    # Step 1: Try sending to user
    user_sent = await send_user_message(
        employee_name,
        title="🔑 密碼重設連結",
        body=f"管理員已為您產生密碼重設連結（6 小時內有效）：\n{link}\n\n如有問題請聯繫系統管理員。",
    )

    # Step 2: Notify admin channel with delivery status
    status = "✅ 已發送" if user_sent else "❌ 未發送（需手動轉傳）"
    return await _send_adaptive_card(
        title="🔑 密碼重設連結已產生",
        subtitle="管理員已產生密碼重設連結。",
        facts=[
            {"title": "使用者", "value": employee_name},
            {"title": "操作者", "value": admin_name},
            {"title": "使用者 Teams 通知", "value": status},
        ],
    )


async def notify_register_link(employee_name: str, link: str, admin_name: str) -> bool:
    """Send registration link to user and notify admin channel with delivery status.

    1. Attempt to send register link to user via Power Automate
    2. Always notify admin channel with delivery status
    Returns True if admin channel notification succeeded.
    """
    user_sent = await send_user_message(
        employee_name,
        title="📋 AuthCenter 帳號註冊連結",
        body=f"管理員已為您產生註冊連結（24 小時內有效）：\n{link}\n\n請點擊連結完成帳號註冊。如有問題請聯繫系統管理員。",
    )

    status = "✅ 已發送" if user_sent else "❌ 未發送（需手動轉傳）"
    return await _send_adaptive_card(
        title="📋 註冊連結已產生",
        subtitle="管理員已產生員工註冊連結。",
        facts=[
            {"title": "使用者", "value": employee_name},
            {"title": "操作者", "value": admin_name},
            {"title": "使用者 Teams 通知", "value": status},
        ],
    )
