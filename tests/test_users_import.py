"""Moving the "external" Xray people into the panel database: leaders, followers, paid terms."""
import contextlib
import importlib
import io
import json
import os
import re
from datetime import date

import pytest

from core import db as db_module
from core import payment, users_import, xray, xray_cli

TODAY = date(2026, 10, 14)
NAMES = ["main", "fabinc", "artyrarytunyan", "mk3oleg", "l2marlen", "boris", "elizaveta-odinokova", "l2marlen-2",
         "elizaveta-odinokova-2", "elizaveta-odinokova-3", "MKDaniil", "MKDaniil-2", "MKDaniil-3", "MK3Vinner",
         "DgEvgeniy"]


def uid(i):
    return f"{i:08d}-0000-4000-8000-000000000000"


def token(i):
    return f"{i:032x}"


def external_clients(names=NAMES):
    return {n: {"uuid": uid(i), "token": token(i), "enabled": True, "external": True} for i, n in enumerate(names, 1)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("XRAYPANEL_DB_PATH", str(tmp_path / "users.json"))
    importlib.reload(db_module)
    path = tmp_path / "xray.json"
    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(path))
    for name in ("XRAY_DOMAIN", "XRAY_EDGE_ADDR", "XRAY_ENC", "XRAY_PATH", "XRAY_PORT", "XRAY_AUTO", "SUB_DOMAIN", "SUB_DIR", "SUB_PUSH"):
        monkeypatch.delenv(name, raising=False)
    path.write_text(json.dumps({"version": 1, "server": {"decryption": "d", "encryption": "e", "domain": "cdn.test",
                                                         "edge_addr": "cdn.test", "port": 443, "path": "/p",
                                                         "padding_key": "k", "padding_placement": "cookie"},
                                "clients": external_clients()}))
    return path


PLAN = """\
# comment
username,expires_at,leader,password,rate
main,inf,,,
fabinc,2026-12-01,,,
artyrarytunyan,15.11.2026,,,
mk3oleg,2026-11-20,,,
l2marlen,2026-12-10,,,
l2marlen-2,,l2marlen,,
boris,2026-11-01,,secret99,
elizaveta-odinokova,2027-01-05,,,
elizaveta-odinokova-2,,elizaveta-odinokova,,
elizaveta-odinokova-3,,elizaveta-odinokova,,
MKDaniil,2026-12-25,,,
MKDaniil-2,,MKDaniil,,
MKDaniil-3,,MKDaniil,,
MK3Vinner,2026-10-30,,,250
DgEvgeniy,2026-11-11,,,
"""


def run_plan(text=PLAN, clients=None, db_names=(), allow_expired=False):
    rows, errors = users_import.parse_plan(text)
    plan = users_import.plan_import(rows, clients if clients is not None else external_clients(), set(db_names),
                                    today=TODAY, allow_expired=allow_expired)
    plan.problems[:0] = errors
    return plan


# ---------------- template ----------------

def test_template_guesses_leaders_from_numbered_names_and_skips_people_already_in_the_database():
    clients = external_clients()
    clients["panelguy"] = {"uuid": uid(99), "token": token(99), "enabled": True}              # not external
    text = users_import.build_template(clients, db_usernames={"boris"})
    rows, errors = users_import.parse_plan(text)
    assert errors == []
    names = [r.username for r in rows]
    assert "panelguy" not in names and "boris" not in names and len(names) == 14
    leaders = {r.username: r.leader for r in rows}
    assert leaders["l2marlen-2"] == "l2marlen" and leaders["MKDaniil-3"] == "MKDaniil"
    assert leaders["elizaveta-odinokova-2"] == "elizaveta-odinokova" and leaders["main"] is None
    assert names.index("l2marlen") + 1 == names.index("l2marlen-2")                           # followers sit under their leader
    assert all(r.has_expiry is False for r in rows)                                          # dates are for the admin to fill in


def test_a_numbered_name_without_an_existing_base_is_not_a_follower():
    text = users_import.build_template(external_clients(["solo-2", "other"]), set())
    assert {r.username: r.leader for r in users_import.parse_plan(text)[0]} == {"solo-2": None, "other": None}


# ---------------- parsing ----------------

@pytest.mark.parametrize("value,expected", [
    ("2026-12-01", (True, "2026-12-01")), ("01.12.2026", (True, "2026-12-01")), ("inf", (True, None)),
    ("∞", (True, None)), ("Безлимит", (True, None)), ("31.02.2026", (False, None)), ("tomorrow", (False, None)),
])
def test_dates(value, expected):
    assert users_import.parse_date(value) == expected


def test_parse_reports_bad_dates_and_rates_per_person():
    rows, errors = users_import.parse_plan("username,expires_at,rate\nanna,soon,\nbob,2026-12-01,abc\ncara,2026-12-01,150\n")
    assert [r.username for r in rows] == ["cara"] and rows[0].rate == 150
    assert len(errors) == 2 and "anna" in errors[0] and "bob" in errors[1]


def test_parse_needs_a_header_and_content():
    assert users_import.parse_plan("")[1] == ["файл пуст"]
    assert "заголовком" in users_import.parse_plan("anna,2026-12-01\n")[1][0]


# ---------------- planning ----------------

def test_the_full_plan_is_valid_and_counts_people():
    plan = run_plan()
    assert plan.ok, plan.problems
    assert "будет создано: ведущих 10, ведомых 5" in plan.info
    assert plan.warnings == []


@pytest.mark.parametrize("text,fragment", [
    ("username,expires_at,leader\nghost,2026-12-01,\n", "нет в xray.json"),
    ("username,expires_at,leader\nfabinc,,\n", "не указан expires_at"),
    ("username,expires_at,leader\nfabinc,2026-12-01,\nfabinc,2026-12-02,\n", "имя повторяется"),
    ("username,expires_at,leader\nl2marlen-2,,nobody\n", "ведущего 'nobody' нет"),
    ("username,expires_at,leader\nmain,inf,main\n", "самого себя"),
    ("username,expires_at,leader\nl2marlen,2026-12-01,\nl2marlen-2,,l2marlen\nMKDaniil,,l2marlen-2\n", "сам ведомый"),
    ("username,expires_at,leader\nfabinc,2026-10-01,\n", "уже прошёл"),
])
def test_problems_block_the_import(text, fragment):
    plan = run_plan(text)
    assert not plan.ok and any(fragment in p for p in plan.problems), plan.problems


def test_an_expired_date_needs_an_explicit_flag():
    text = "username,expires_at\nfabinc,2026-10-01\n"
    assert not run_plan(text).ok
    allowed = run_plan(text, allow_expired=True)
    assert allowed.ok and any("доступ будет отключён" in w for w in allowed.warnings)


def test_follower_dates_passwords_and_rates_are_ignored_with_a_warning():
    plan = run_plan("username,expires_at,leader,password,rate\nl2marlen,2026-12-01,,,\nl2marlen-2,2026-01-01,l2marlen,pw,500\n")
    assert plan.ok and len(plan.warnings) == 3


def test_people_already_in_the_database_are_skipped():
    plan = run_plan(db_names={"boris"})
    assert plan.ok and "boris: уже есть в базе, будет пропущен" in plan.info


# ---------------- applying ----------------

def apply(world_path):
    plan = run_plan()
    assert plan.ok, plan.problems
    return users_import.apply_import(plan)


def test_everyone_lands_in_the_database_with_followers_linked_to_their_leader(world):
    result = apply(world)
    users = {u["username"]: u for u in db_module.list_users()}
    assert sorted(users) == sorted(NAMES) and len(result["leaders"]) == 10 and len(result["followers"]) == 5

    assert users["l2marlen-2"]["linked_to"] == "l2marlen"
    assert [f["username"] for f in db_module.get_followers("elizaveta-odinokova")] == ["elizaveta-odinokova-2", "elizaveta-odinokova-3"]
    assert [f["username"] for f in db_module.get_followers("MKDaniil")] == ["MKDaniil-2", "MKDaniil-3"]
    assert db_module.get_followers("fabinc") == []


def test_paid_terms_are_set_and_followers_follow_their_leader(world):
    apply(world)
    users = {u["username"]: u for u in db_module.list_users()}
    assert users["fabinc"]["expires_at"] == "2026-12-01" and users["artyrarytunyan"]["expires_at"] == "2026-11-15"
    assert users["main"]["expires_at"] is None                                         # unlimited
    for follower, leader in (("l2marlen-2", "l2marlen"), ("MKDaniil-3", "MKDaniil"), ("elizaveta-odinokova-2", "elizaveta-odinokova")):
        assert users[follower]["expires_at"] == users[leader]["expires_at"] and users[follower]["status"] == "active"
        assert users[follower]["password"] == users[leader]["password"]
    assert users["boris"]["password"] == "secret99"


def test_generated_passwords_are_unique_readable_and_reported_once(world):
    result = apply(world)
    generated = {n: p for n, p in result["passwords"].items() if n != "boris"}
    assert len(generated) == 9 and len(set(generated.values())) == 9
    assert all(re.fullmatch(r"[a-hjkmnp-z2-9]{8}", p) for p in generated.values())
    assert db_module.get_user("fabinc")["password"] == result["passwords"]["fabinc"]


def test_xray_clients_keep_uuid_and_token_but_stop_being_external(world):
    apply(world)
    clients = xray.load_store()["clients"]
    original = external_clients()
    for name in NAMES:
        assert "external" not in clients[name]
        assert clients[name]["uuid"] == original[name]["uuid"] and clients[name]["token"] == original[name]["token"]


def test_nobody_is_asked_to_top_up_right_after_the_move(world):
    apply(world)
    assert payment.calc_monthly_price("elizaveta-odinokova") == (300, 2)               # 100 + 2 devices x 100
    assert db_module.get_user("elizaveta-odinokova")["last_renewal_rate"] == 300
    assert payment.calc_rate_gap("elizaveta-odinokova") is None
    assert payment.calc_rate_gap("l2marlen") is None and payment.calc_rate_gap("MKDaniil") is None


def test_an_explicit_rate_is_kept(world):
    apply(world)
    assert db_module.get_user("MK3Vinner")["last_renewal_rate"] == 250


def test_the_old_database_is_backed_up_and_a_second_run_changes_nothing(world, tmp_path):
    db_module.add_user({"username": "panelguy", "password": "x", "status": "active", "expires_at": None})
    result = apply(world)
    assert os.path.exists(result["backup"]) and "panelguy" not in {u["username"] for u in db_module.list_users()} - {"panelguy"}
    before = json.dumps(db_module.list_users(), sort_keys=True)
    second = users_import.apply_import(run_plan(db_names={u["username"] for u in db_module.list_users()}))
    assert second["leaders"] == [] and second["followers"] == [] and json.dumps(db_module.list_users(), sort_keys=True) == before


def test_a_plan_with_problems_is_never_applied(world):
    with pytest.raises(RuntimeError):
        users_import.apply_import(run_plan("username,expires_at\nghost,2026-12-01\n"))
    assert db_module.list_users() == []


# ---------------- access after the move ----------------

def test_after_the_move_access_follows_the_paid_terms(world, monkeypatch):
    apply(world)
    db_module.update_user("mk3oleg", expires_at="2020-01-01")                         # that term ran out
    applied = []
    monkeypatch.setattr(xray, "list_users", db_module.list_users)
    monkeypatch.setattr(xray, "_apply_config", lambda store, desired: applied.append(dict(desired)) or True)
    xray.sync_xray_from_users()
    desired = applied[0]
    original = external_clients()
    assert "mk3oleg" not in desired                                                    # expired: cut off at the sync
    assert desired["fabinc"] == original["fabinc"]["uuid"] and desired["l2marlen-2"] == original["l2marlen-2"]["uuid"]
    assert len(desired) == 14                                                          # same UUIDs as before for everyone else


# ---------------- the command line ----------------

def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = xray_cli.main(list(argv))
    return code, out.getvalue()


def test_cli_template_dry_run_and_apply(world, tmp_path):
    code, template = cli("users-template")
    assert code == 0 and template.splitlines()[3] == "username,expires_at,leader,password,rate"
    plan_file = tmp_path / "plan.csv"
    plan_file.write_text(PLAN)

    code, out = cli("import-users", str(plan_file))
    assert code == 0 and "Пробный запуск" in out and db_module.list_users() == []        # dry run writes nothing

    code, out = cli("import-users", str(plan_file), "--apply")
    assert code == 0 and "ведущих 10, ведомых 5" in out and "Пароли ведущих" in out and "boris: secret99" in out
    assert len(db_module.list_users()) == 15

    code, template = cli("users-template")
    assert template.count("\n") == 4 and "fabinc" not in template                        # nobody is left to move


def test_cli_refuses_a_broken_plan(world, tmp_path):
    plan_file = tmp_path / "plan.csv"
    plan_file.write_text("username,expires_at\nfabinc,eventually\n")
    code, out = cli("import-users", str(plan_file), "--apply")
    assert code == 1 and "непонятная дата" in out and db_module.list_users() == []
