"""
Pure text-formatting helpers — no router, no state, just string building.
Kept separate from keyboards.py because these are about message *content*
(card text, regex matching) rather than button layout.
"""
import re

from core.instructions import MANUAL_CONNECT_STEPS

# Matches the connection card the admin sends to clients:
# 👤 Username: SWAnton
# 🔑 Password: SWAnton123
CARD_RE = re.compile(
    r"username\s*:\s*(\S+).*?password\s*:\s*(\S+)",
    re.IGNORECASE | re.DOTALL
)


def looks_like_card(text: str) -> bool:
    """
    Cheap pre-check used as a message filter.

    NOTE: aiogram's F.text.regexp() filter matches from the START of the
    string (re.match semantics), so a pattern like r"username\\s*:" never
    fires on real cards — they start with an emoji ("👤 Username: ..."),
    not the literal word "Username". Using a plain callable filter with
    re.search() avoids that trap entirely.
    """
    if not text:
        return False
    return bool(re.search(r"username\s*:", text, re.IGNORECASE)) and \
        bool(re.search(r"password\s*:", text, re.IGNORECASE))


def format_connection_message(username: str, password: str, expires_at, link: str) -> str:
    """
    The connection card. `link` comes from core.connect.primary_link: the join
    page (buttons for Incy / Happ / v2RayTun), a raw vless:// link when
    subscriptions are not configured, or "" while nothing is ready yet.
    """
    head = (
        f"👤 Username: {username}\n"
        f"🔑 Password: {password}\n"
        f"⏳ Expires: {expires_at or '∞'}\n\n"
    )
    if not link:
        return head + (
            "Ссылка для подключения ещё готовится. Нажмите «🔗 Мои подключения» "
            "чуть позже или напишите администратору."
        )
    if link.startswith("vless://"):
        return head + f"Ссылка для импорта в приложение (Incy, Happ, v2RayTun):\n{link}"
    return head + (
        f"Для подключения откройте страницу 👇\n{link}\n\n"
        "На ней кнопки для вашего приложения (Incy, Happ, v2RayTun): нажмите «Добавить подписку», маршрутизация подтянется сама."
    )


def format_full_instructions_message(username: str, password: str, expires_at, link: str) -> str:
    """
    Card + generic post-install connection steps in one continuous message —
    for the manual admin flow (Add user / Get link). App installation itself
    isn't explained here since the admin already walks the client through
    that separately before sending the card.
    """
    card = format_connection_message(username, password, expires_at, link)
    return f"{card}\n\n{MANUAL_CONNECT_STEPS}"


def extract_telegram_id_from_message(msg):
    """Used by both the 🆔 Записать/перезаписать ID flow (list_users.py)
    and could be reused anywhere else a Telegram ID needs to be pulled out
    of either a forwarded message or a plain numeric reply."""
    if msg.forward_from:
        return msg.forward_from.id
    if msg.text and msg.text.strip().lstrip("-").isdigit():
        return int(msg.text.strip())
    return None
