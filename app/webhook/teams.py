"""Microsoft Teams Webhook notification module."""

import httpx

from app.config import get_settings
from app.schemas import StaffInfo


async def send_forgot_password_notification(staff: StaffInfo) -> bool:
    """Send a forgot-password notification to Microsoft Teams via Webhook.

    Returns True if the webhook was sent successfully.
    """
    settings = get_settings()
    if not settings.TEAMS_WEBHOOK_URL:
        return False

    # Microsoft Teams Adaptive Card payload
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
                            "text": "🔑 密碼重設請求",
                        },
                        {
                            "type": "TextBlock",
                            "text": "有員工請求重設密碼，請管理員協助處理。",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "員工編號", "value": staff.staff_id},
                                {"title": "姓名", "value": staff.name},
                                {"title": "部門", "value": staff.dept_code},
                                {"title": "權限等級", "value": f"Level {staff.level}"},
                            ],
                        },
                    ],
                },
            }
        ],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.TEAMS_WEBHOOK_URL, json=payload)
        return resp.is_success
