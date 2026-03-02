"""Microsoft Teams Webhook notification module."""

import httpx

from app.config import get_settings
from app.schemas import StaffInfo


async def _send_adaptive_card(title: str, subtitle: str, facts: list[dict]) -> bool:
    """Send an Adaptive Card to Microsoft Teams via Webhook."""
    settings = get_settings()
    if not settings.TEAMS_WEBHOOK_URL:
        return False

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Large",
                            "weight": "Bolder",
                            "text": title,
                        },
                        {
                            "type": "TextBlock",
                            "text": subtitle,
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": facts,
                        },
                    ],
                },
            }
        ],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.TEAMS_WEBHOOK_URL, json=payload)
        return resp.is_success


async def send_forgot_password_notification(staff: StaffInfo) -> bool:
    """Send a forgot-password notification to Microsoft Teams via Webhook."""
    return await _send_adaptive_card(
        title="🔑 密碼重設請求",
        subtitle="有員工請求重設密碼，請管理員協助處理。",
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
        ],
    )


async def send_registration_request_notification(staff: StaffInfo, app_name: str) -> bool:
    """Send a new-user registration request notification to Microsoft Teams.

    Admin should generate a registration link and send it to the employee.
    Command: python scripts/generate_register_link.py <employee_name>
    """
    return await _send_adaptive_card(
        title="📋 新員工註冊請求",
        subtitle=(
            "有員工首次登入，尚未建立帳號。請核對身份後，"
            "執行指令產生註冊連結並發送至員工信箱。"
        ),
        facts=[
            {"title": "使用者名稱", "value": staff.employee_name},
            {"title": "組織代碼", "value": staff.org_id},
            {"title": "欲存取的 App", "value": app_name},
            {"title": "產生連結指令", "value": f"python scripts/generate_register_link.py {staff.employee_name}"},
        ],
    )
