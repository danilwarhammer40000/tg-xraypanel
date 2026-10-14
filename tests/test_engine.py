"""Xray is the only engine: automatic clients, what a client opens, client texts, no leftovers."""
import json
import os
import pathlib
import re

import pytest

from bot import formatting
from core import connect, instructions, service, xray
from core import subscription as sub

U1 = "11111111-1111-4111-8111-111111111111"
T1 = "a" * 32
SERVER = {
    "decryption": "mlkem768x25519plus.native.600s.PRIVATE",
    "encryption": "mlkem768x25519plus.native.0rtt.PUBLIC",
    "domain": "cdn.test", "edge_addr": "cdn.test", "port": 443,
    "path": "/static/js/chunk.js", "padding_key": "k5e9394", "padding_placement": "cookie",
}


def user(name, status="active", expires=None):
    return {"username": name, "status": status, "expires_at": expires}


@pytest.fixture
def store_file(monkeypatch, tmp_path):
    path = tmp_path / "xray.json"
    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(path))
    monkeypatch.setattr(connect, "XRAY_STORE_PATH", str(path))
    for name in ("XRAY_DOMAIN", "XRAY_EDGE_ADDR", "XRAY_ENC", "XRAY_PATH", "XRAY_PORT", "XRAY_AUTO",
                 "SUB_DOMAIN", "SUB_DIR", "SUB_PUSH"):
        monkeypatch.delenv(name, raising=False)
    return path


def write_store(path, clients=None, subscription=None, server=SERVER):
    data = {"version": 1, "server": server, "clients": clients or {}}
    if subscription is not None:
        data["subscription"] = subscription
    path.write_text(json.dumps(data))


# ---------------- automatic clients ----------------

def test_every_user_gets_a_client_with_uuid_and_token():
    clients = {}
    assert xray.provision_clients([user("a"), user("b", status="inactive"), {"status": "active"}], clients) is True
    assert sorted(clients) == ["a", "b"]                       # inactive users too: access is decided later
    for entry in clients.values():
        assert entry["enabled"] is True and sub.valid_token(entry["token"]) and len(entry["uuid"]) == 36
    assert clients["a"]["uuid"] != clients["b"]["uuid"] and clients["a"]["token"] != clients["b"]["token"]


def test_provisioning_never_touches_existing_clients_not_even_switched_off_ones():
    clients = {"a": {"uuid": U1, "enabled": False}}
    assert xray.provision_clients([user("a")], clients) is False
    assert clients == {"a": {"uuid": U1, "enabled": False}}


def test_xray_auto_zero_restores_the_manual_opt_in(monkeypatch):
    monkeypatch.setenv("XRAY_AUTO", "0")
    clients = {}
    assert xray.provision_clients([user("a")], clients) is False and clients == {}


def test_sync_provisions_everyone_but_only_active_unexpired_users_get_access(store_file, monkeypatch, tmp_path):
    write_store(store_file, subscription={"domain": "config.test"})
    users = [user("ok"), user("off", status="inactive"), user("late", expires="2020-01-01"), user("soon", expires="2999-01-01")]
    monkeypatch.setattr(xray, "list_users", lambda: users)
    applied = []
    monkeypatch.setattr(xray, "_apply_config", lambda store, desired: applied.append(dict(desired)) or True)

    xray.sync_xray_from_users()

    clients = xray.load_store()["clients"]
    assert sorted(clients) == ["late", "off", "ok", "soon"]
    assert sorted(applied[0]) == ["ok", "soon"]                              # fail closed on inactive and expired
    published = sorted(os.listdir(tmp_path / "sub-out"))
    assert published == sorted([clients["ok"]["token"], clients["soon"]["token"]])

    applied.clear()
    xray.sync_xray_from_users()
    assert xray.load_store()["clients"] == clients                           # idempotent: nothing is regenerated


# ---------------- what a client opens ----------------

def test_primary_link_is_the_join_page_when_subscriptions_are_configured(store_file):
    write_store(store_file, {"anna": {"uuid": U1, "enabled": True, "token": T1}}, {"domain": "config.test"})
    assert connect.primary_link("anna", user("anna")) == f"https://config.test/join.html#{T1}"
    assert connect.subscription_link("anna") == f"https://config.test/s/{T1}"


def test_primary_link_falls_back_to_the_raw_vless_link(store_file):
    write_store(store_file, {"anna": {"uuid": U1, "enabled": True, "token": T1}})
    link = connect.primary_link("anna", user("anna"))
    assert link.startswith(f"vless://{U1}@cdn.test:443?") and connect.subscription_link("anna") == ""


@pytest.mark.parametrize("clients", [{}, {"anna": {"uuid": U1, "enabled": False, "token": T1}}])
def test_primary_link_is_empty_when_nothing_is_ready(store_file, clients):
    write_store(store_file, clients, {"domain": "config.test"})
    assert connect.primary_link("anna", user("anna")) == ""


def test_primary_link_is_empty_when_the_engine_is_not_configured(store_file):
    write_store(store_file, {"anna": {"uuid": U1, "enabled": True, "token": T1}}, server={})
    assert connect.primary_link("anna", user("anna")) == ""


# ---------------- client texts ----------------

def test_card_for_a_join_page_link():
    text = formatting.format_connection_message("anna", "pw1", "2026-12-01", "https://config.test/join.html#" + T1)
    assert "👤 Username: anna" in text and "🔑 Password: pw1" in text and "⏳ Expires: 2026-12-01" in text
    assert "https://config.test/join.html#" + T1 in text and "Incy" in text


def test_card_for_a_raw_link_and_for_nothing_ready():
    assert "vless://x" in formatting.format_connection_message("a", "p", None, "vless://x")
    waiting = formatting.format_connection_message("a", "p", None, "")
    assert "⏳ Expires: ∞" in waiting and "готовится" in waiting


def test_new_card_is_still_recognised_and_parsed_as_a_card():
    text = formatting.format_full_instructions_message("anna", "pw1", None, "https://config.test/join.html#" + T1)
    assert formatting.looks_like_card(text)
    match = formatting.CARD_RE.search(text)
    assert match.group(1) == "anna" and match.group(2) == "pw1"
    assert instructions.MANUAL_CONNECT_STEPS in text


def test_instructions_describe_xray_apps_and_embed_the_link():
    link = "https://config.test/join.html#" + T1
    for text in (instructions.render_android_instructions(link), instructions.render_ios_instructions(link),
                 instructions.render_routing_instructions(link)):
        assert link in text and "TrustTunnel" not in text and "None" not in text
    assert "Incy" in instructions.render_android_instructions()
    assert instructions.FALLBACK_LINK_NOTE in instructions.render_ios_instructions()
    assert "Ваша страница подключения" not in instructions.render_routing_instructions()


def test_store_links_appear_only_when_configured(monkeypatch):
    monkeypatch.setattr(instructions, "APP_LINK_ANDROID", "https://play.example/incy")
    assert "https://play.example/incy" in instructions.render_android_instructions("x")
    monkeypatch.setattr(instructions, "APP_LINK_ANDROID", "")
    assert "https://" not in instructions.render_android_instructions()


# ---------------- service ----------------

def test_full_resync_is_just_the_xray_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(xray, "sync_xray_from_users", lambda: calls.append("xray"))
    service.full_resync_and_reload()
    assert calls == ["xray"] and service.safe_sync() == "OK"


def test_safe_sync_reports_errors(monkeypatch):
    def boom():
        raise RuntimeError("xray is down")
    monkeypatch.setattr(xray, "sync_xray_from_users", boom)
    assert service.safe_sync() == "ERROR: xray is down"


# ---------------- nothing of the old engines is left ----------------

LEFTOVERS = re.compile(r"trusttunnel|max_chat_id|MAX_BOT_TOKEN|max_bot|credentials\.toml|generate_link|send_max", re.I)


def test_no_trusttunnel_or_max_code_is_left():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if (not path.is_file() or parts[0] in ("tests", ".git", "venv", "__pycache__")
                or path.suffix not in (".py", ".sh", ".service", ".timer", ".txt", ".example")):
            continue
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if LEFTOVERS.search(line):
                offenders.append(f"{'/'.join(parts)}:{number}: {line.strip()[:80]}")
    assert offenders == []
