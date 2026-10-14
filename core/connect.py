"""
What a client opens to connect. Xray is the engine, so the answer comes from
xray.json: the join page (https://<subscription host>/join.html#<token>, with
one-tap buttons for Incy / Happ / v2RayTun) when subscriptions are configured,
otherwise the single raw vless:// link, otherwise "" (nothing ready yet).
"""
import os
from typing import Optional

from core import subscription as sub
from core import xray
from core.db import get_user
from core.paths import XRAY_STORE_PATH


def primary_link(username: str, user: Optional[dict] = None) -> str:
    user = user if user is not None else get_user(username)
    store = xray.load_store()
    state = xray.client_state(user or {"username": username}, store)
    if not state["configured"] or not state["enabled"]:
        return ""

    cfg = sub.sub_settings(store.get("subscription"), os.path.dirname(XRAY_STORE_PATH))
    token = state.get("token")
    if cfg["enabled"] and sub.valid_token(token):
        return sub.join_url(cfg, token)
    return xray.build_link(username, state["uuid"], store)


def subscription_link(username: str) -> str:
    """The plain subscription URL (for apps that take a URL), or "" when subscriptions are not configured."""
    store = xray.load_store()
    client = store["clients"].get(username) or {}
    cfg = sub.sub_settings(store.get("subscription"), os.path.dirname(XRAY_STORE_PATH))
    if cfg["enabled"] and sub.valid_token(client.get("token")):
        return sub.subscription_url(cfg, client["token"])
    return ""
