import contextlib
import io
import json

import pytest

from core import xray, xray_cli

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
T1 = "a" * 32
T2 = "b" * 32
SERVER = {"decryption": "d", "encryption": "e", "domain": "cdn.test"}


@pytest.fixture
def store_file(monkeypatch, tmp_path):
    path = tmp_path / "xray.json"
    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(path))
    for name in ("SUB_DOMAIN", "SUB_DIR", "SUB_PUSH"):
        monkeypatch.delenv(name, raising=False)
    return path


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = xray_cli.main(list(argv))
    return code, out.getvalue()


def test_parse_skips_comments_service_users_and_reports_bad_lines():
    text = f"""
# name uuid token
main {U1} {T1}
_probe {U2} {T2}
broken {U1}
badid not-a-uuid {T1}
badtoken {U1} xyz
anna {U2} {T2}   # trailing comment
"""
    rows = list(xray_cli.parse_users_list(text))
    assert [(r[0], r[1] is not None) for r in rows] == [
        ("main", True), ("broken", False), ("badid", False), ("badtoken", False), ("anna", True)]
    assert rows[0] == ("main", U1, T1) and rows[4] == ("anna", U2, T2)


def test_import_keeps_uuid_and_token_marks_external_and_never_overwrites(store_file, tmp_path):
    store_file.write_text(json.dumps({"version": 1, "server": SERVER,
                                      "clients": {"anna": {"uuid": U1, "enabled": True, "token": T1}}}))
    users_list = tmp_path / "users.list"
    users_list.write_text(f"main {U1} {T1}\nanna {U2} {T2}\nbad x y\n")

    code, out = run("import-list", str(users_list))
    clients = xray.load_store()["clients"]
    assert code == 1                                                    # one bad line reported
    assert clients["main"] == {"uuid": U1, "token": T1, "enabled": True, "external": True}
    assert clients["anna"]["uuid"] == U1 and clients["anna"]["token"] == T1   # existing entry untouched
    assert "ПРОПУЩЕНО bad" in out and xray.load_store()["server"] == SERVER


def test_import_refuses_a_corrupt_store(store_file, tmp_path):
    store_file.write_text("{ not json")
    users_list = tmp_path / "users.list"
    users_list.write_text(f"main {U1} {T1}\n")
    with pytest.raises(xray.StoreError):
        xray_cli.main(["import-list", str(users_list)])
    assert store_file.read_text() == "{ not json"


def test_urls_lists_join_pages_only_when_subscriptions_are_configured(store_file):
    clients = {"main": {"uuid": U1, "token": T1, "enabled": True, "external": True},
               "off": {"uuid": U2, "token": T2, "enabled": False}}
    store_file.write_text(json.dumps({"version": 1, "server": SERVER, "clients": clients}))
    code, out = run("urls")
    assert code == 1 and "не настроены" in out

    store_file.write_text(json.dumps({"version": 1, "server": SERVER, "clients": clients,
                                      "subscription": {"domain": "config.test"}}))
    code, out = run("urls")
    assert code == 0
    assert f"https://config.test/join.html#{T1}" in out and "main [вкл, внешний]" in out
    assert "off [выкл, панель]" in out
