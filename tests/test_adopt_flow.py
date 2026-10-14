"""
The whole migration, end to end, on throwaway files:

  manual stand --export--> zip --(panel reinstalled, data wiped)--> bot restore --> first sync

What must hold: the people keep their UUIDs and tokens (so every URL already
handed out still works), hand-made copies are replaced instead of duplicated,
service users are left alone, nothing else of the fresh panel is touched.
"""
import base64
import contextlib
import io
import json
import os
from urllib.parse import urlparse

import pytest

from core import manual_stand, restore, xray, xray_cli
from tests.stand_helpers import (DEC, T_ANNA, T_MAIN, T_PROBE, U_ANNA, U_MAIN, U_PROBE, make_stand)


@pytest.fixture
def world(tmp_path, monkeypatch):
    stand = make_stand(tmp_path / "stand", with_stats=True)

    data = tmp_path / "panel" / "data"          # the freshly installed panel's data dir
    data.mkdir(parents=True)
    (data / "users.json").write_text(json.dumps([{"username": "panelguy", "status": "active", "expires_at": None}]))

    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(data / "xray.json"))
    monkeypatch.setenv("XRAY_CONFIG_PATH", stand["paths"]["config"])
    for name in ("XRAY_DOMAIN", "XRAY_EDGE_ADDR", "XRAY_ENC", "XRAY_PATH", "XRAY_PORT", "SUB_DOMAIN", "SUB_DIR", "SUB_PUSH"):
        monkeypatch.delenv(name, raising=False)

    restarts = []
    monkeypatch.setattr(xray, "_validate", lambda path: None)             # no xray binary here
    monkeypatch.setattr(xray, "_service_state", lambda: "active")
    monkeypatch.setattr(xray, "_restart_and_check", lambda: restarts.append(1))
    monkeypatch.setattr(xray, "list_users", lambda: json.loads((data / "users.json").read_text()))
    return {"stand": stand, "data": data, "restarts": restarts, "tmp": tmp_path}


def live_clients(stand):
    with open(stand["paths"]["config"]) as f:
        cfg = json.load(f)
    return cfg["inbounds"][0]["settings"]["clients"]


def subscription_links(stand, token):
    with open(os.path.join(stand["sub_dir"], token)) as f:
        return base64.b64decode(f.read()).decode().split("\n")


def test_manual_stand_to_panel_roundtrip(world):
    stand, data, tmp = world["stand"], world["data"], world["tmp"]

    # 1. on the OLD host, before the panel exists: pure standard-library export
    plan = manual_stand.inspect_stand(stand["paths"])
    assert plan.ok, plan.problems
    bundle = manual_stand.export_bundle(plan, stand["paths"], str(tmp / "out"))

    # 2. the panel was reinstalled: its data dir knows nothing about the people
    assert not (data / "xray.json").exists()

    # 3. the admin uploads the zip in the bot ("🗄 База данных → 📥 Загрузить БД")
    files = {"users.json": str(data / "users.json"), "xray.json": str(data / "xray.json")}
    with open(bundle["zip"], "rb") as f:
        result = restore.restore_backup_zip(f.read(), files)
    assert result.restored == ["xray.json"] and result.invalid == []
    assert json.loads((data / "users.json").read_text())[0]["username"] == "panelguy"   # untouched

    # 4. the bot runs its sync: the first one re-labels the hand-made entries
    assert xray.sync_xray_from_users() is True
    assert len(world["restarts"]) == 1

    clients = {c["email"]: c["id"] for c in live_clients(stand)}
    # Xray is the engine: the panel's own user gets a client automatically (fresh UUID + token) ...
    panel_uuid = clients.pop("tp:panelguy")
    assert panel_uuid not in (U_MAIN, U_ANNA, U_PROBE)
    panel_token = xray.load_store()["clients"]["panelguy"]["token"]
    assert os.path.exists(os.path.join(stand["sub_dir"], panel_token))
    # ... and the taken-over people are re-labelled without duplicates, _probe is kept
    assert clients == {"_probe": U_PROBE, "tp:main": U_MAIN, "tp:anna": U_ANNA}
    with open(stand["paths"]["config"]) as f:
        cfg = json.load(f)
    assert cfg["inbounds"][0]["settings"]["decryption"] == DEC                           # the live key is intact
    assert [i["tag"] for i in cfg["inbounds"] if "tag" in i] == ["api-in"]               # stats inbound untouched
    assert cfg["routing"]["rules"][0]["inboundTag"] == ["api-in"]

    # 5. the SAME tokens now point at files the panel wrote: old URLs keep working
    for token, uid in ((T_MAIN, U_MAIN), (T_ANNA, U_ANNA)):
        links = subscription_links(stand, token)
        assert links and all(l.startswith(f"vless://{uid}@cdn.test:443?") for l in links)
        assert "extra=" in links[0]
    assert not os.path.exists(os.path.join(stand["sub_dir"], T_PROBE))                    # service user: no subscription

    # 6. nothing more to do: a second sync neither restarts Xray nor rewrites files
    before = os.stat(os.path.join(stand["sub_dir"], T_MAIN)).st_mtime_ns
    assert xray.sync_xray_from_users() is False
    assert len(world["restarts"]) == 1
    assert os.stat(os.path.join(stand["sub_dir"], T_MAIN)).st_mtime_ns == before


def test_revoking_a_taken_over_person_removes_the_subscription_and_the_uuid(world):
    stand, data = world["stand"], world["data"]
    plan = manual_stand.inspect_stand(stand["paths"])
    xray.save_store(plan.store)
    xray.sync_xray_from_users()

    xray._mutate("anna", lambda clients: clients["anna"].update(enabled=False) or {})
    xray.sync_xray_from_users()

    assert "tp:anna" not in {c["email"] for c in live_clients(stand)}
    assert not os.path.exists(os.path.join(stand["sub_dir"], T_ANNA))
    assert os.path.exists(os.path.join(stand["sub_dir"], T_MAIN))


def test_adopt_cli_dry_run_then_apply_writes_xray_json_and_a_backup(world):
    stand, data = world["stand"], world["data"]
    args = ["adopt"] + [f"--{k}={v}" for k, v in stand["paths"].items()]

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert xray_cli.main(args) == 0
    assert "перезапуск: ДА" in out.getvalue() and "tp:<имя>" in out.getvalue()
    assert "['_probe']" in out.getvalue()                                                 # untouched entries are listed
    assert not (data / "xray.json").exists()                                              # dry run wrote nothing

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert xray_cli.main(args + ["--apply"]) == 0
    store = json.loads((data / "xray.json").read_text())
    assert sorted(store["clients"]) == ["anna", "main"] and store["server"]["decryption"] == DEC

    # a second apply must not silently overwrite: the section exists and equals, people exist: fine and backed up
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert xray_cli.main(args + ["--apply"]) == 0
    assert any(n.startswith("xray.json.pre-adopt-") for n in os.listdir(data))


def test_adopt_cli_refuses_to_apply_a_plan_with_problems(world):
    stand, data = world["stand"], world["data"]
    broken = make_stand(world["tmp"] / "broken", env_path="/wrong.js")
    args = ["adopt", "--apply"] + [f"--{k}={v}" for k, v in broken["paths"].items()]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert xray_cli.main(args) == 1
    assert "path не совпадает" in out.getvalue() and not (data / "xray.json").exists()


def test_cli_disable_and_enable_switch_a_taken_over_person(world):
    stand = world["stand"]
    plan = manual_stand.inspect_stand(stand["paths"])
    xray.save_store(plan.store)
    xray.sync_xray_from_users()

    def cli(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = xray_cli.main(list(argv))
        return code, out.getvalue()

    assert cli("disable", "anna")[0] == 0
    assert cli("publish")[0] == 0
    assert not os.path.exists(os.path.join(stand["sub_dir"], T_ANNA))
    assert "tp:anna" not in {c["email"] for c in live_clients(stand)}

    assert cli("enable", "anna")[0] == 0
    assert cli("publish")[0] == 0
    assert os.path.exists(os.path.join(stand["sub_dir"], T_ANNA))              # the same token again
    assert {"tp:anna": U_ANNA}.items() <= {c["email"]: c["id"] for c in live_clients(stand)}.items()

    code, text = cli("disable", "nobody")
    assert code == 1 and "нет такого клиента" in text
