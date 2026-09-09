"""
Telegram alert integration for HDD price drops.
"""
import httpx
import db


async def send_telegram_alert(bot_token: str, chat_id: str, message: str):
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


async def check_and_alert():
    """Check for drives below threshold and send alerts."""
    settings = db.get_all_settings()
    if settings.get("alerts_enabled") != "true":
        return

    bot_token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if not bot_token or not chat_id:
        return

    try:
        threshold = float(settings.get("alert_aed_per_tb_threshold", "100"))
    except ValueError:
        return

    deals = db.get_drives_below_threshold(threshold)
    if not deals:
        return

    lines = [f"🚨 <b>HDD Price Alert — {len(deals)} drive(s) below AED {threshold:.0f}/TB!</b>\n"]
    for d in deals[:10]:
        lines.append(
            f"• <b>{d['capacity_tb']}TB</b> {d['category']} — "
            f"<b>AED {d['price_aed']:.0f}</b> "
            f"(<b>AED {d['aed_per_tb']:.1f}/TB</b>)\n"
            f"  {d['title'][:60]}...\n"
            f"  <a href=\"{d['url']}\">View on Amazon.ae</a>"
        )

    await send_telegram_alert(bot_token, chat_id, "\n".join(lines))
