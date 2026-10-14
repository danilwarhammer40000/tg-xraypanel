"""
Shared helper for creating a brand-new linked sub-account ("follower")
under an existing user — used by:
  - the admin's "➕ Выпустить нового" button (handlers/leader_link.py)
  - a client's self-service "📱 Подключить ещё устройства" request, once
    an admin approves it (handlers/extra_links.py)

Deliberately NOT in core/db.py: this needs core.connect.primary_link() to
hand back a ready-to-send connection card, which core/db.py has no business
depending on. core/db.py itself is left completely untouched by this feature:
every DB operation here goes through its existing
add_user()/link_user()/get_followers() functions.

IMPORTANT ORDERING RULE (see build_connection_card()'s docstring):
issue_follower() only touches the DB. It deliberately does NOT build a
connection card. A new account only gets its Xray client (UUID + subscription
token) during bot.access.run_sync(), so callers MUST call run_sync() (when
leader_is_active()) BEFORE calling build_connection_card(); earlier, the card
says the link is still being prepared.
"""
import re

from bot.formatting import format_full_instructions_message
from core.dates import utcnow_naive
from core.connect import primary_link
from core.db import DuplicateUsernameError, add_user, get_followers, get_user, link_user

# How many extra links (sub-accounts beyond the leader's own) a client gets
# WITHOUT paying: none. One link = one device = core.payment.PRICE_PER_DEVICE
# a month, and a link is issued only after the payment is made and the receipt
# is verified (handlers/extra_links.py; or by an admin directly through
# handlers/leader_link.py). Kept as a named constant so the "0 free" rule is
# explicit in core.payment.calc_monthly_price and in the client screens.
FREE_EXTRA_LINKS = 0

# Followers of a leader are named "{leader}-2", "{leader}-3", etc. — the
# leader account itself is implicitly "slot 1". Chosen over "{leader}/2"
# specifically because the username ends up in a URL path
# (core/generator.py's fallback link is /connect/{username}) and gets
# passed as a CLI argument to the Xray_endpoint binary — "-" is
# safe in both contexts, "/" risked being read as an extra path segment
# by code we don't control.
_SUFFIX_RE = re.compile(r"^-(\d+)$")


def next_follower_username(leader_username: str, existing_followers: list) -> str:
    """
    Picks the lowest unused slot number >= 2 among CURRENT followers of
    leader_username whose name actually matches the "{leader}-N" pattern
    — a follower linked in some other way (e.g. an existing independent
    account manually attached via "🔗 Сделать ведомым") doesn't occupy a
    numbered slot, so it's simply skipped rather than colliding.
    """
    used = set()
    prefix_len = len(leader_username)
    for f in existing_followers:
        name = f.get("username", "")
        if name.startswith(leader_username):
            m = _SUFFIX_RE.match(name[prefix_len:])
            if m:
                used.add(int(m.group(1)))

    n = 2
    while n in used:
        n += 1
    return f"{leader_username}-{n}"


def issue_follower(leader_username: str, existing_followers: list = None):
    """
    Creates one new sub-account linked to leader_username in the DB only:
    SAME password as the leader (by design — these are the same person's
    extra devices, not separate clients), expires_at/status synced from
    the leader via link_user() (identical to manually linking an existing
    account).

    existing_followers can be passed in to avoid a redundant
    get_followers() call when issuing several in a row (see
    handlers/extra_links.py, which issues multiple and must account for
    each new one before picking the next slot number) — the CALLER is
    responsible for keeping that list current between calls in that case.

    Returns the new username on success, or None if leader_username
    doesn't exist OR if new_username collided with an existing record
    (DuplicateUsernameError from core.db.add_user() -- next_follower_username()
    picks the next free numbered slot from a snapshot that can go stale
    between the check and the write under concurrent calls; this is rare
    enough not to warrant a retry loop, and the caller already treats a
    None return as "stop issuing, something's off" -- see
    bot/handlers/extra_links.py's _issue_now()).

    Does NOT generate a connection link/card — see this module's
    docstring for why that has to happen separately, after a resync. Call
    build_connection_card() for that, once run_sync() (if needed) has run.
    """
    leader = get_user(leader_username)
    if not leader:
        return None

    if existing_followers is None:
        existing_followers = get_followers(leader_username)

    new_username = next_follower_username(leader_username, existing_followers)

    try:
        add_user({
            "username": new_username,
            "password": leader.get("password"),
            "created_at": utcnow_naive().strftime("%Y-%m-%d"),
            "expires_at": leader.get("expires_at"),  # placeholder — link_user() below syncs it for real
            "status": leader.get("status", "active"),
            "telegram_id": None,
            "notified_days": [],
            "post_disable_notified": [],
            "pending_request": None,
        })
    except DuplicateUsernameError:
        return None

    link_user(new_username, leader_username)

    return new_username


def build_connection_card(username: str) -> str:
    """
    Builds the connection link (core.connect.primary_link) and formats the
    full instructions message for `username`.

    MUST be called AFTER a run_sync() that included this user: the Xray client
    is created by the sync, so calling it earlier gives a card without a link.
    See this module's docstring.

    Returns "" if username somehow doesn't exist (shouldn't happen if
    called right after issue_follower() succeeded).
    """
    user = get_user(username)
    if not user:
        return ""

    link = primary_link(username, user)
    return format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)


def leader_is_active(leader: dict) -> bool:
    """Whether issuing a new follower right now actually needs a
    the Xray client list resync — only true if the leader (and therefore the
    new follower, which syncs to the same status) is currently active."""
    from core.dates import is_expired
    return leader.get("status") == "active" and not is_expired(leader.get("expires_at"))
