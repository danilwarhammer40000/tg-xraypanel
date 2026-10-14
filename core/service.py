import logging

from core.db import list_users  # noqa: F401  (kept importable from here)

log = logging.getLogger(__name__)


# ---------------- FULL SYNC ----------------

def full_resync_and_reload() -> None:
    """
    Makes the engine match users.json: Xray's client list, then the
    subscription files (core.xray.sync_xray_from_users does both). A no-op
    until the server section of data/xray.json exists.
    """
    from core.xray import sync_xray_from_users
    sync_xray_from_users()


def mark_user_inactive(username: str) -> None:
    # Purely a logical marker: the live client list is rebuilt from users.json
    # on every sync, so there is nothing else to do here. Kept as a named
    # no-op so call sites stay self-documenting about *why* a user is being
    # disabled.
    log.debug("mark_user_inactive(%s): no-op, clients rebuilt on next sync", username)


# ---------------- SAFE SYNC ----------------

def safe_sync() -> str:
    try:
        full_resync_and_reload()
        return "OK"
    except Exception as e:
        log.exception("sync failed")
        return f"ERROR: {e}"
