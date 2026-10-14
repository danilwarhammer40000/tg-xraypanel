import base64
import json
import os
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from core import subscription as sub
from core import xray

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
T1 = "a" * 32
T2 = "b" * 32

SERVER = {
    "decryption": "mlkem768x25519plus.native.600s.PRIVATE",
    "encryption": "mlkem768x25519plus.native.0rtt.PUBLIC",
    "domain": "cdn.test",
    "edge_addr": "198.51.100.9",
    "port": 443,
    "path": "/static/js/chunk.js",
    "padding_key": "k5e9394",
    "padding_placement": "cookie",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SUB_DOMAIN", "SUB_DIR", "SUB_PUSH"):
        monkeypatch.delenv(name, raising=False)


def cfg_for(tmp_path, **sub_conf):
    conf = {"domain": "config.test"}
    conf.update(sub_conf)
    return sub.sub_settings(conf, str(tmp_path))


def decode(path):
    return base64.b64decode(open(path).read()).decode().split("\n")


# ---------------- settings ----------------

def test_disabled_without_a_domain(tmp_path):
    assert sub.sub_settings({}, str(tmp_path))["enabled"] is False
    assert sub.sub_settings(None, str(tmp_path))["enabled"] is False
    assert cfg_for(tmp_path)["enabled"] is True


def test_default_dir_lives_next_to_the_store(tmp_path):
    assert cfg_for(tmp_path)["dir"] == os.path.join(str(tmp_path), "sub-out")


def test_env_overrides_and_push_accepts_a_string_or_list(tmp_path, monkeypatch):
    assert cfg_for(tmp_path, push=["a:/x/", "b:/y/"])["push"] == ["a:/x/", "b:/y/"]
    monkeypatch.setenv("SUB_PUSH", "c:/z/ d:/w/")
    monkeypatch.setenv("SUB_DOMAIN", "env.test")
    monkeypatch.setenv("SUB_DIR", "/tmp/elsewhere")
    cfg = cfg_for(tmp_path, push=["a:/x/"])
    assert cfg["push"] == ["c:/z/", "d:/w/"] and cfg["domain"] == "env.test" and cfg["dir"] == "/tmp/elsewhere"


def test_incomplete_endpoints_are_dropped_and_bad_max_ips_falls_back(tmp_path):
    cfg = cfg_for(tmp_path, max_ips="x", endpoints=[
        {"label": "ok", "host": "h.test", "mode": "domain"},
        {"label": "", "host": "h.test", "mode": "domain"},
        {"label": "nomode", "host": "h.test"},
        "garbage",
    ])
    assert [e["label"] for e in cfg["endpoints"]] == ["ok"] and cfg["max_ips"] == 2


def test_urls_keep_the_token_in_the_fragment_for_the_join_page(tmp_path):
    cfg = cfg_for(tmp_path)
    assert sub.subscription_url(cfg, T1) == f"https://config.test/s/{T1}"
    assert sub.join_url(cfg, T1) == f"https://config.test/join.html#{T1}"


# ---------------- tokens ----------------

def test_tokens_are_32_hex_and_unique():
    a, b = sub.new_token(), sub.new_token()
    assert sub.valid_token(a) and sub.valid_token(b) and a != b
    for bad in (None, "", "short", "G" * 32, "A" * 32, T1 + "0", 123):
        assert sub.valid_token(bad) is False


def test_ensure_tokens_only_touches_clients_that_need_one():
    clients = {"a": {"uuid": U1}, "b": {"uuid": U2, "token": T2}, "c": {"enabled": True}, "d": {"uuid": U1, "token": "bad"}}
    assert sub.ensure_tokens(clients) is True
    assert sub.valid_token(clients["a"]["token"]) and clients["b"]["token"] == T2
    assert "token" not in clients["c"] and sub.valid_token(clients["d"]["token"])
    assert sub.ensure_tokens(clients) is False


# ---------------- links ----------------

def test_extra_defaults_and_options(tmp_path):
    base = sub.build_extra(SERVER, cfg_for(tmp_path))
    assert base == {"scMaxEachPostBytes": 65536, "xPaddingObfsMode": True,
                    "xPaddingKey": "k5e9394", "xPaddingPlacement": "cookie"}

    full = sub.build_extra(SERVER, cfg_for(tmp_path, sess_key="sk", xmux=True, uplink_get=True))
    assert full["sessionKey"] == "sk" and full["sessionPlacement"] == "cookie"
    assert full["xmux"]["maxConcurrency"] == "16-32"
    assert full["uplinkHTTPMethod"] == "GET" and full["scMaxEachPostBytes"] == 16384


def test_make_link_shape(tmp_path):
    extra = sub.build_extra(SERVER, cfg_for(tmp_path))
    link = sub.make_link(U1, "1.2.3.4", "cdn.test", SERVER, extra, "yandex ip1")
    parsed = urlparse(link)
    q = parse_qs(parsed.query)
    assert parsed.scheme == "vless" and parsed.username == U1
    assert parsed.hostname == "1.2.3.4" and parsed.port == 443
    assert q["sni"] == ["cdn.test"] and q["host"] == ["cdn.test"] and q["alpn"] == ["h2"]
    assert q["type"] == ["xhttp"] and q["mode"] == ["packet-up"] and q["path"] == ["/static/js/chunk.js"]
    assert q["encryption"] == [SERVER["encryption"]]
    assert json.loads(q["extra"][0])["xPaddingKey"] == "k5e9394"
    assert unquote(parsed.fragment) == "yandex ip1"


# ---------------- endpoints ----------------

def eps(*rows):
    return [{"label": l, "host": h, "mode": m} for l, h, m in rows]


def test_endpoint_modes_domain_fixed_ips_and_unknown(tmp_path):
    cfg = cfg_for(tmp_path)
    addrs = sub.resolve_endpoints(
        eps(("d", "h.test", "domain"), ("pool", "h.test", "1.1.1.1,2.2.2.2"), ("bad", "h.test", "whatever")),
        cfg, {}, dig=lambda *a: [])
    assert addrs == {"d": [("dom", "h.test")], "pool": [("ip1", "1.1.1.1"), ("ip2", "2.2.2.2")]}


def test_resolve_mode_caps_ips_remembers_them_and_reuses_them_when_dns_is_silent(tmp_path):
    cfg = cfg_for(tmp_path, max_ips=2, dns_resolvers=["r1", "r2"])
    state = {}
    answers = {"r1": ["9.9.9.1", "9.9.9.2", "9.9.9.3"], "r2": ["9.9.9.1"]}
    addrs = sub.resolve_endpoints(eps(("y", "h.test", "resolve")), cfg, state,
                                  dig=lambda host, resolver, subnet: answers[resolver])
    assert addrs["y"] == [("ip1", "9.9.9.1"), ("ip2", "9.9.9.2")]      # most common first, capped
    assert state == {"h.test": ["9.9.9.1", "9.9.9.2"]}

    again = sub.resolve_endpoints(eps(("y", "h.test", "resolve")), cfg, state, dig=lambda *a: [])
    assert again["y"] == [("ip1", "9.9.9.1"), ("ip2", "9.9.9.2")]      # previous IPs

    last_resort = sub.resolve_endpoints(eps(("y", "h.test", "resolve")), cfg, {}, dig=lambda *a: [])
    assert last_resort["y"] == [("dom", "h.test")]                     # never empty


def test_resolve_passes_every_ecs_subnet_to_dig(tmp_path):
    cfg = cfg_for(tmp_path, dns_resolvers=["r1"], ecs_subnets=["10.0.0.0/24", "10.1.0.0/24"])
    seen = []
    sub.resolve_endpoints(eps(("y", "h.test", "resolve")), cfg, {},
                          dig=lambda host, resolver, subnet: seen.append(subnet) or [])
    assert seen == ["10.0.0.0/24", "10.1.0.0/24"]


def test_default_endpoint_comes_from_the_server_section(tmp_path):
    cfg = cfg_for(tmp_path)
    endpoints = sub.default_endpoints(SERVER)
    addrs = sub.resolve_endpoints(endpoints, cfg, {})
    links = sub.build_links(U1, endpoints, addrs, SERVER, sub.build_extra(SERVER, cfg))
    assert len(links) == 1
    parsed = urlparse(links[0])
    assert parsed.hostname == "198.51.100.9" and parse_qs(parsed.query)["sni"] == ["cdn.test"]
    assert unquote(parsed.fragment) == "xray"


# ---------------- publish ----------------

def run_publish(tmp_path, clients, desired, run=None, **sub_conf):
    conf = {"domain": "config.test", "endpoints": [
        {"label": "y", "host": "cdn.test", "mode": "domain"},
        {"label": "pool", "host": "cdn.test", "mode": "1.1.1.1,2.2.2.2"},
    ]}
    conf.update(sub_conf)
    kwargs = {"dig": lambda *a: []}
    if run:
        kwargs["run"] = run
    return sub.publish(clients, desired, SERVER, conf, str(tmp_path), **kwargs)


def test_disabled_publishes_nothing(tmp_path):
    res = sub.publish({"a": {"token": T1}}, {"a": U1}, SERVER, {}, str(tmp_path))
    assert res["enabled"] is False
    assert os.listdir(tmp_path) == []


def test_one_file_per_client_with_a_link_per_endpoint_address(tmp_path):
    clients = {"a": {"uuid": U1, "token": T1}, "b": {"uuid": U2, "token": T2}}
    res = run_publish(tmp_path, clients, {"a": U1, "b": U2})
    assert res == {"enabled": True, "written": 2, "removed": 0, "kept": 2}

    links = decode(tmp_path / "sub-out" / T1)
    assert len(links) == 3                                   # domain + two fixed IPs
    assert all(l.startswith(f"vless://{U1}@") for l in links)
    assert [unquote(urlparse(l).fragment) for l in links] == ["y-dom", "pool-ip1", "pool-ip2"]
    assert decode(tmp_path / "sub-out" / T2)[0].startswith(f"vless://{U2}@")
    assert oct(os.stat(tmp_path / "sub-out" / T1).st_mode & 0o777) == "0o644"


def test_second_run_rewrites_nothing(tmp_path):
    clients = {"a": {"uuid": U1, "token": T1}}
    run_publish(tmp_path, clients, {"a": U1})
    assert run_publish(tmp_path, clients, {"a": U1})["written"] == 0


def test_a_changed_uuid_rewrites_the_file(tmp_path):
    clients = {"a": {"uuid": U1, "token": T1}}
    run_publish(tmp_path, clients, {"a": U1})
    assert run_publish(tmp_path, clients, {"a": U2})["written"] == 1


def test_revoked_clients_lose_their_file_but_unrelated_files_stay(tmp_path):
    clients = {"a": {"uuid": U1, "token": T1}, "b": {"uuid": U2, "token": T2}}
    run_publish(tmp_path, clients, {"a": U1, "b": U2})
    (tmp_path / "sub-out" / "README").write_text("not a token")

    res = run_publish(tmp_path, clients, {"a": U1})          # b expired / switched off
    assert res["removed"] == 1
    assert sorted(os.listdir(tmp_path / "sub-out")) == ["README", T1]


def test_rotated_token_kills_the_old_url(tmp_path):
    clients = {"a": {"uuid": U1, "token": T1}}
    run_publish(tmp_path, clients, {"a": U1})
    clients["a"]["token"] = T2
    run_publish(tmp_path, clients, {"a": U1})
    assert os.listdir(tmp_path / "sub-out") == [T2]


def test_client_without_a_valid_token_is_skipped(tmp_path):
    res = run_publish(tmp_path, {"a": {"uuid": U1}}, {"a": U1})
    assert res["kept"] == 0 and os.listdir(tmp_path / "sub-out") == []


def test_publish_uses_the_default_endpoint_when_none_are_configured(tmp_path):
    res = sub.publish({"a": {"token": T1}}, {"a": U1}, SERVER, {"domain": "config.test"}, str(tmp_path))
    assert res["written"] == 1
    assert len(decode(tmp_path / "sub-out" / T1)) == 1


def test_publish_pushes_the_folder_with_rsync_to_every_target(tmp_path):
    calls = []

    class Done:
        returncode, stderr = 0, ""

    def run(cmd, **kw):
        calls.append(cmd)
        return Done()

    run_publish(tmp_path, {"a": {"uuid": U1, "token": T1}}, {"a": U1}, run=run,
                push=["sub@host:/var/www/sub/s/", "/local/dir/"])
    assert len(calls) == 2
    assert calls[0][:3] == ["rsync", "-a", "--delete"] and "BatchMode=yes" in calls[0][4]
    assert calls[0][-2] == str(tmp_path / "sub-out") + "/" and calls[0][-1] == "sub@host:/var/www/sub/s/"
    assert calls[1][-1] == "/local/dir/"


def test_failed_push_raises_after_the_files_were_written(tmp_path):
    class Failed:
        returncode, stderr = 23, "permission denied"

    with pytest.raises(sub.SubscriptionError) as err:
        run_publish(tmp_path, {"a": {"uuid": U1, "token": T1}}, {"a": U1},
                    run=lambda cmd, **kw: Failed(), push=["sub@host:/s/"])
    assert "sub@host:/s/" in str(err.value) and "permission denied" in str(err.value)
    assert os.listdir(tmp_path / "sub-out") == [T1]           # local state is already correct


def test_missing_rsync_is_reported_not_crashed(tmp_path):
    def run(cmd, **kw):
        raise FileNotFoundError("rsync")

    with pytest.raises(sub.SubscriptionError):
        run_publish(tmp_path, {"a": {"uuid": U1, "token": T1}}, {"a": U1}, run=run, push=["h:/s/"])


# ---------------- integration with core.xray ----------------

@pytest.fixture
def store_file(monkeypatch, tmp_path):
    path = tmp_path / "xray.json"
    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(path))
    for name in ("XRAY_DOMAIN", "XRAY_EDGE_ADDR", "XRAY_ENC", "XRAY_PATH", "XRAY_PORT"):
        monkeypatch.delenv(name, raising=False)
    return path


def write_store(path, clients=None, subscription=None):
    data = {"version": 1, "server": SERVER, "clients": clients or {}}
    if subscription is not None:
        data["subscription"] = subscription
    path.write_text(json.dumps(data))


def test_subscription_section_survives_load_and_save(store_file):
    write_store(store_file, subscription={"domain": "config.test", "push": ["h:/s/"]})
    store = xray.load_store()
    assert store["subscription"] == {"domain": "config.test", "push": ["h:/s/"]}
    xray.save_store(store)
    assert xray.load_store()["subscription"] == store["subscription"]
    assert xray.load_store()["server"] == SERVER


def test_external_clients_ignore_users_json_but_respect_the_switch():
    clients = {
        "main": {"uuid": U1, "enabled": True, "external": True},
        "off": {"uuid": U2, "enabled": False, "external": True},
        "ghost": {"uuid": U2, "enabled": True},                    # not external, no panel user
    }
    assert xray.compute_desired([], clients, today=None) == {"main": U1}


def test_external_client_does_not_override_a_panel_user_of_the_same_name():
    users = [{"username": "main", "status": "active", "expires_at": None}]
    clients = {"main": {"uuid": U1, "enabled": True, "external": True}}
    assert xray.compute_desired(users, clients) == {"main": U1}


def test_block_shows_the_join_page_and_subscription_url_when_configured(store_file):
    write_store(store_file, clients={"a": {"uuid": U1, "enabled": True, "token": T1}},
                subscription={"domain": "config.test"})
    block = xray.xray_block({"username": "a", "status": "active", "expires_at": None})
    assert f"https://config.test/join.html#{T1}" in block and f"https://config.test/s/{T1}" in block
    assert "vless://" not in block


def test_block_falls_back_to_the_raw_link_without_a_subscription(store_file):
    write_store(store_file, clients={"a": {"uuid": U1, "enabled": True, "token": T1}})
    block = xray.xray_block({"username": "a", "status": "active", "expires_at": None})
    assert "<code>vless://" in block and "join.html" not in block


def test_sync_assigns_tokens_and_publishes_only_the_active(store_file, monkeypatch, tmp_path):
    write_store(
        store_file,
        clients={"a": {"uuid": U1, "enabled": True},           # legacy entry without a token
                 "b": {"uuid": U2, "enabled": False, "token": T2}},
        subscription={"domain": "config.test"},
    )
    users = [{"username": "a", "status": "active", "expires_at": None},
             {"username": "b", "status": "active", "expires_at": None}]
    monkeypatch.setattr(xray, "list_users", lambda: users)
    monkeypatch.setattr(xray, "_apply_config", lambda store, desired: False)

    assert xray.sync_xray_from_users() is False

    token = xray.load_store()["clients"]["a"]["token"]
    assert sub.valid_token(token)                              # persisted, so the URL is stable
    assert os.listdir(tmp_path / "sub-out") == [token]         # b is switched off: no file

    assert xray.sync_xray_from_users() is False                # idempotent
    assert xray.load_store()["clients"]["a"]["token"] == token


def test_sync_without_a_subscription_section_writes_no_files(store_file, monkeypatch, tmp_path):
    write_store(store_file, clients={"a": {"uuid": U1, "enabled": True}})
    monkeypatch.setattr(xray, "list_users", lambda: [{"username": "a", "status": "active", "expires_at": None}])
    monkeypatch.setattr(xray, "_apply_config", lambda store, desired: False)
    xray.sync_xray_from_users()
    assert not (tmp_path / "sub-out").exists()


def test_sync_raises_the_subscription_error_last(store_file, monkeypatch, tmp_path):
    write_store(store_file, clients={"a": {"uuid": U1, "enabled": True, "token": T1}},
                subscription={"domain": "config.test", "push": ["h:/s/"]})
    monkeypatch.setattr(xray, "list_users", lambda: [{"username": "a", "status": "active", "expires_at": None}])
    applied = []
    monkeypatch.setattr(xray, "_apply_config", lambda store, desired: applied.append(dict(desired)) or True)

    class Failed:
        returncode, stderr = 1, "no route"

    monkeypatch.setattr(sub.subprocess, "run", lambda *a, **kw: Failed())
    with pytest.raises(sub.SubscriptionError):
        xray.sync_xray_from_users()
    assert applied == [{"a": U1}]                              # Xray was already updated
