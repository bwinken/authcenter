"""Microsoft Teams Webhook notification module.

Sends notifications to admin channel via TEAMS_WEBHOOK_URL (Incoming Webhook).
"""

import socket
from datetime import datetime

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
                logger.info("Webhook 發送成功: {}", url[:60])
            else:
                logger.error("Webhook 失敗: url={} status={} body={}", url[:60], resp.status_code, resp.text[:200])
            return resp.is_success
    except Exception:
        logger.exception("Webhook 發送異常: {}", url[:60])
        return False


def _build_adaptive_card(
    title: str, subtitle: str, facts: list[dict],
) -> dict:
    """Build an Adaptive Card payload for Teams Incoming Webhook."""
    body: list[dict] = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title},
        {"type": "TextBlock", "text": subtitle, "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


async def _send_adaptive_card(
    title: str, subtitle: str, facts: list[dict],
) -> bool:
    """Send an Adaptive Card to the admin channel via Incoming Webhook."""
    settings = get_settings()
    if not settings.TEAMS_WEBHOOK_URL:
        logger.warning("TEAMS_WEBHOOK_URL 未設定，跳過通知")
        return False
    payload = _build_adaptive_card(title, subtitle, facts)
    return await _post_webhook(settings.TEAMS_WEBHOOK_URL, payload)


async def send_test_notification() -> bool:
    """發送測試通知到管理員 Channel（scripts/preflight_check.py --test-webhook 使用）。"""
    return await _send_adaptive_card(
        title="🔧 AuthCenter 測試通知",
        subtitle="這是 preflight_check 發送的測試訊息，收到代表 Teams Webhook 設定正確。",
        facts=[
            {"title": "發送主機", "value": socket.gethostname()},
            {"title": "發送時間", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ],
    )


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


# ─── Auto Mail Status（自動寄信結果回報）──────────────────────


def _mail_result(sent: bool, error: str) -> str:
    return "✅ 已寄出" if sent else f"❌ 寄送失敗：{error}"


async def send_register_mail_status(
    staff: StaffInfo, app_name: str, email: str, sent: bool, error: str = "",
) -> bool:
    """回報註冊連結自動寄送結果到管理員 Channel（不含連結本身）。"""
    if sent:
        title = "📧 已自動寄送註冊連結"
        subtitle = "員工申請註冊，系統已將註冊連結寄到員工信箱，管理員無需處理。"
    else:
        title = "⚠️ 註冊連結自動寄送失敗"
        subtitle = "請至管理後台 Dashboard 的「待處理的註冊請求」手動產生註冊連結，並轉交給員工。"
    return await _send_adaptive_card(
        title=title,
        subtitle=subtitle,
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
            {"title": "欲存取的 App", "value": app_name},
            {"title": "收件信箱", "value": email},
            {"title": "寄送結果", "value": _mail_result(sent, error)},
        ],
    )


async def send_reset_mail_status(
    staff: StaffInfo, email: str, sent: bool, error: str = "",
) -> bool:
    """回報密碼重設連結自動寄送結果到管理員 Channel（不含連結本身）。"""
    if sent:
        title = "📧 已自動寄送密碼重設連結"
        subtitle = "員工申請重設密碼，系統已將重設連結寄到員工信箱，管理員無需處理。"
    else:
        title = "⚠️ 密碼重設連結自動寄送失敗"
        subtitle = "請至管理後台「會員管理」手動產生重設連結，並轉交給員工。"
    return await _send_adaptive_card(
        title=title,
        subtitle=subtitle,
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
            {"title": "收件信箱", "value": email},
            {"title": "寄送結果", "value": _mail_result(sent, error)},
        ],
    )
