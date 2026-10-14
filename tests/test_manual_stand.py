import io
import json
import os
import tarfile
import zipfile

import pytest

from core import manual_stand as ms
from tests.stand_helpers import (DEC, ENC, PADKEY, T_ANNA, T_MAIN, U_ANNA, U_MAIN, XPATH, make_stand)


def plan_for(tmp_path, **kw):
    stand = make_stand(tmp_path / "stand", **kw)
    return stand, ms.inspect_stand(stand["paths"])


# ---------------- reading the stand ----------------

def test_inspect_builds_server_subscription_and_people(tmp_path):
    stand, plan = plan_for(tmp_path)
    assert plan.ok, plan.problems
    store = plan.store
    assert store["server"] == {"decryption": DEC, "encryption": ENC, "domain": "cdn.test", "edge_addr": "cdn.test",
                               "port": 443, "path": XPATH, "padding_key": PADKEY, "padding_placement": "cookie"}
    assert store["subscription"]["domain"] == "config.test"
    assert store["subscription"]["dir"] == stand["sub_dir"]            # same host: files go straight to nginx's folder
    assert "push" not in store["subscription"]
    assert store["subscription"]["endpoints"] == [{"label": "yandex-d", "host": "cdn.test", "mode": "domain"}]
    assert store["subscription"]["uplink_get"] is True and store["subscription"]["xmux"] is False
    assert store["subscription"]["max_ips"] == 2
    assert store["subscription"]["dns_resolvers"] == ["8.8.8.8", "1.1.1.1"]


def test_people_keep_uuid_and_token_and_service_users_are_skipped(tmp_path):
    _, plan = plan_for(tmp_path)
    assert plan.store["clients"] == {
        "main": {"uuid": U_MAIN, "token": T_MAIN, "enabled": True, "external": True},
        "anna": {"uuid": U_ANNA, "token": T_ANNA, "enabled": True, "external": True},
    }


def test_secrets_the_panel_does_not_need_are_not_copied(tmp_path):
    _, plan = plan_for(tmp_path)
    assert "secret-origin-token" not in json.dumps(plan.store)


def test_push_mode_keeps_the_target_and_no_local_dir(tmp_path):
    _, plan = plan_for(tmp_path, push="sub@203.0.113.7:/var/www/sub/s/")
    assert plan.ok
    assert plan.store["subscription"]["push"] == ["sub@203.0.113.7:/var/www/sub/s/"]
    assert "dir" not in plan.store["subscription"]


def test_the_first_vless_inbound_is_read_even_with_a_stats_inbound_next_to_it(tmp_path):
    _, plan = plan_for(tmp_path, with_stats=True)
    assert plan.ok and plan.store["server"]["decryption"] == DEC


@pytest.mark.parametrize("kwargs,fragment", [
    ({"env_path": "/other/path.js"}, "path не совпадает"),
    ({"env_padkey": "zzz"}, "ключ padding не совпадает"),
    ({"sub_domain": ""}, "SUB_DOMAIN"),
    ({"dec": "none"}, "decryption"),
    ({"endpoints": "# nothing\n"}, "endpoints.list"),
])
def test_problems_block_the_export(tmp_path, kwargs, fragment):
    stand, plan = plan_for(tmp_path, **kwargs)
    assert not plan.ok and any(fragment in p for p in plan.problems), plan.problems
    with pytest.raises(RuntimeError):
        ms.export_bundle(plan, stand["paths"], str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_a_config_without_a_vless_inbound_is_a_problem(tmp_path):
    stand = make_stand(tmp_path / "stand")
    with open(stand["paths"]["config"], "w") as f:
        json.dump({"inbounds": [{"protocol": "dokodemo-door"}]}, f)
    plan = ms.inspect_stand(stand["paths"])
    assert not plan.ok and "VLESS" in plan.problems[0]


def test_bad_and_duplicate_lines_in_users_list_are_reported(tmp_path):
    stand = make_stand(tmp_path / "stand")
    with open(stand["paths"]["users"], "a") as f:
        f.write(f"dup {U_ANNA} {'c' * 32}\nbroken {U_MAIN}\nbadid nope {'d' * 32}\nbadtoken {U_ANNA[:-1]}0 xyz\n")
    plan = ms.inspect_stand(stand["paths"])
    text = "\n".join(plan.problems)
    assert "повторяется UUID (dup)" in text and "три колонки" in text
    assert "UUID некорректный" in text and "токен должен быть" in text


def test_an_existing_different_section_needs_force(tmp_path):
    stand = make_stand(tmp_path / "stand")
    existing = ms.empty_store()
    existing["server"] = {"decryption": "x", "encryption": "y", "domain": "old.test"}
    plan = ms.inspect_stand(stand["paths"], existing)
    assert not plan.ok and any('секция "server"' in p for p in plan.problems)

    forced = ms.inspect_stand(stand["paths"], existing, force=True)
    assert forced.ok and forced.store["server"]["domain"] == "cdn.test"


def test_existing_people_are_not_overwritten(tmp_path):
    stand = make_stand(tmp_path / "stand")
    existing = ms.empty_store()
    existing["clients"]["anna"] = {"uuid": "33333333-3333-4333-8333-333333333333", "token": "e" * 32, "enabled": True}
    plan = ms.inspect_stand(stand["paths"], existing)
    assert plan.store["clients"]["anna"]["token"] == "e" * 32          # untouched
    assert "main" in plan.store["clients"]


def test_the_input_store_is_not_mutated(tmp_path):
    stand = make_stand(tmp_path / "stand")
    existing = ms.empty_store()
    ms.inspect_stand(stand["paths"], existing)
    assert existing == ms.empty_store()


# ---------------- export ----------------

def test_export_writes_a_restorable_zip_and_an_originals_tarball_with_private_modes(tmp_path):
    stand, plan = plan_for(tmp_path)
    result = ms.export_bundle(plan, stand["paths"], str(tmp_path / "out"), stamp="20261011_120000")

    assert os.path.basename(result["zip"]) == "xraypanel_backup_20261011_120000.zip"
    for path in result.values():
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    with zipfile.ZipFile(result["zip"]) as zf:
        assert zf.namelist() == ["xray.json"]                           # ONLY xray.json: nothing else gets overwritten
        assert json.loads(zf.read("xray.json")) == plan.store

    with tarfile.open(result["originals"]) as tar:
        assert sorted(tar.getnames()) == ["client-params.env", "config.json", "endpoints.list", "users.list"]


def test_backup_zip_bytes_are_valid_in_memory(tmp_path):
    _, plan = plan_for(tmp_path)
    data = ms.build_backup_zip(plan.store)
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["xray.json"]


# ---------------- preflight ----------------

def fake_run(overrides=None):
    answers = {
        ("systemctl", "is-active", "xray"): (0, "active"),
        ("systemctl", "is-active", "nginx"): (0, "active"),
        ("nginx", "-t"): (0, "ok"),
        ("systemctl", "list-timers", "--all", "--no-pager", "--no-legend"): (0, ""),
    }
    answers.update(overrides or {})

    def run(cmd):
        if cmd[1:3] == ["run", "-test"]:
            return answers.get(("xray-test",), (0, "Configuration OK"))
        return answers.get(tuple(cmd), (0, ""))
    return run


def levels(checks):
    return {level for level, _ in checks}


def run_preflight(stand, tmp_path, overrides=None, which=lambda name: "/usr/bin/" + name, cron_files=()):
    return ms.preflight(stand["paths"], run=fake_run(overrides), which=which,
                        install_dir=str(tmp_path / "none"), cron_files=list(cron_files))


def test_preflight_on_a_healthy_stand_has_no_failures(tmp_path):
    stand = make_stand(tmp_path / "stand")
    checks = run_preflight(stand, tmp_path)
    assert "FAIL" not in levels(checks) and "WARN" not in levels(checks)


def test_preflight_warns_about_the_old_generator_timer_and_cron(tmp_path):
    stand = make_stand(tmp_path / "stand")
    cron = tmp_path / "crontab"
    cron.write_text("*/15 * * * * root python3 /root/gen-sub.py\n")
    timers = {("systemctl", "list-timers", "--all", "--no-pager", "--no-legend"):
              (0, "Sun 12:00 5min left gen-sub.timer gen-sub.service")}
    checks = run_preflight(stand, tmp_path, timers, cron_files=[str(cron)])
    warns = [text for level, text in checks if level == "WARN"]
    assert len(warns) == 2 and "gen-sub.timer" in warns[0] and "cron" in warns[1]


def test_preflight_comments_in_cron_are_ignored(tmp_path):
    stand = make_stand(tmp_path / "stand")
    cron = tmp_path / "crontab"
    cron.write_text("# */15 * * * * root python3 /root/gen-sub.py\n")
    assert "WARN" not in levels(run_preflight(stand, tmp_path, cron_files=[str(cron)]))


def test_preflight_fails_on_a_broken_nginx_inactive_xray_or_bad_config(tmp_path):
    stand = make_stand(tmp_path / "stand")
    checks = run_preflight(stand, tmp_path, {
        ("nginx", "-t"): (1, "emerg: bad directive"),
        ("systemctl", "is-active", "xray"): (3, "inactive"),
        ("xray-test",): (1, "bad json"),
    })
    fails = [text for level, text in checks if level == "FAIL"]
    assert len(fails) == 3
    assert any("nginx -t" in t for t in fails) and any("xray не активен" in t for t in fails)
    assert any("xray run -test" in t for t in fails)


def test_preflight_requires_dig_for_resolve_and_rsync_for_push(tmp_path):
    stand = make_stand(tmp_path / "stand", push="sub@h:/s/", endpoints="yandex cdn.test resolve\n")
    checks = run_preflight(stand, tmp_path, which=lambda name: None if name in ("dig", "rsync") else "/bin/" + name)
    fails = [text for level, text in checks if level == "FAIL"]
    assert any("dig" in t for t in fails) and any("rsync" in t for t in fails)


def test_preflight_reports_missing_stand_files(tmp_path):
    stand = make_stand(tmp_path / "stand")
    os.remove(stand["paths"]["users"])
    checks = run_preflight(stand, tmp_path)
    assert any(level == "FAIL" and "users.list" in text for level, text in checks)
