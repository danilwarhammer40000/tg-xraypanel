import io
import json
import os
import stat
import zipfile

import pytest

from core import restore, xray

U1 = "11111111-1111-4111-8111-111111111111"
T1 = "a" * 32
GOOD_STORE = {"version": 1, "server": {"domain": "cdn.test"},
              "clients": {"main": {"uuid": U1, "token": T1, "enabled": True, "external": True}}}


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content if isinstance(content, (str, bytes)) else json.dumps(content))
    return buf.getvalue()


def files_in(tmp_path):
    return {"users.json": str(tmp_path / "users.json"), "xray.json": str(tmp_path / "xray.json"),
            "settings.json": str(tmp_path / "settings.json")}


# ---------------- store shape check ----------------

@pytest.mark.parametrize("content", [
    "{}", "[]", '"hello"', "not json", b"", "null",
    json.dumps({"clients": []}),
    json.dumps({"clients": {"a": "x"}}),
    json.dumps({"clients": {"a": {"uuid": "nope"}}}),
    json.dumps({"clients": {"a": {"uuid": U1, "token": "short"}}}),
    json.dumps({"server": [], "clients": {}}),
    json.dumps({"clients": {}, "subscription": []}),
])
def test_store_is_valid_rejects_garbage(content):
    assert xray.store_is_valid(content) is False


@pytest.mark.parametrize("content", [
    json.dumps(GOOD_STORE),
    json.dumps({"clients": {}}),                                         # an empty but real store
    json.dumps({"server": {}, "clients": {"a": {"uuid": U1, "enabled": True}}}),   # legacy entry without a token
])
def test_store_is_valid_accepts_real_stores(content):
    assert xray.store_is_valid(content) is True


# ---------------- restoring ----------------

def test_a_zip_with_only_xray_json_leaves_every_other_file_alone(tmp_path):
    files = files_in(tmp_path)
    (tmp_path / "users.json").write_text('[{"username": "panelguy"}]')
    (tmp_path / "settings.json").write_text('{"keep": true}')

    result = restore.restore_backup_zip(make_zip({"xray.json": GOOD_STORE}), files)

    assert result.restored == ["xray.json"] and result.invalid == []
    assert json.loads((tmp_path / "xray.json").read_text()) == GOOD_STORE
    assert (tmp_path / "users.json").read_text() == '[{"username": "panelguy"}]'
    assert (tmp_path / "settings.json").read_text() == '{"keep": true}'
    assert stat.S_IMODE(os.stat(tmp_path / "xray.json").st_mode) == 0o600


def test_current_files_are_copied_aside_before_anything_is_overwritten(tmp_path):
    files = files_in(tmp_path)
    (tmp_path / "xray.json").write_text('{"clients": {}}')
    restore.restore_backup_zip(make_zip({"xray.json": GOOD_STORE}), files)
    assert (tmp_path / "xray.json.before_restore").read_text() == '{"clients": {}}'


def test_an_invalid_xray_json_is_skipped_and_the_current_one_survives(tmp_path):
    files = files_in(tmp_path)
    (tmp_path / "xray.json").write_text(json.dumps(GOOD_STORE))
    result = restore.restore_backup_zip(make_zip({"xray.json": "{}"}), files)
    assert result.restored == [] and result.invalid == ["xray.json"] and result.found == ["xray.json"]
    assert json.loads((tmp_path / "xray.json").read_text()) == GOOD_STORE


def test_users_json_must_be_a_list_of_users(tmp_path):
    files = files_in(tmp_path)
    result = restore.restore_backup_zip(make_zip({"users.json": "{}", "settings.json": '{"a": 1}'}), files)
    assert result.invalid == ["users.json"] and result.restored == ["settings.json"]
    assert not (tmp_path / "users.json").exists()

    result = restore.restore_backup_zip(make_zip({"users.json": [{"username": "bob"}]}), files)
    assert result.restored == ["users.json"]


def test_unknown_files_in_the_zip_are_ignored(tmp_path):
    result = restore.restore_backup_zip(make_zip({"evil.sh": "rm -rf /", "../x.json": "{}"}), files_in(tmp_path))
    assert result.restored == [] and result.found == []
    assert os.listdir(tmp_path) == []


def test_not_a_zip_raises(tmp_path):
    with pytest.raises(zipfile.BadZipFile):
        restore.restore_backup_zip(b"definitely not a zip", files_in(tmp_path))


def test_destination_directory_is_created(tmp_path):
    files = {"xray.json": str(tmp_path / "fresh" / "data" / "xray.json")}
    result = restore.restore_backup_zip(make_zip({"xray.json": GOOD_STORE}), files)
    assert result.restored == ["xray.json"] and os.path.exists(files["xray.json"])


# ---------------- archives that were re-zipped on another system ----------------

@pytest.mark.parametrize("member", [
    "backup/xray.json",                                  # unpacked and re-zipped inside a folder
    "xraypanel_backup_20261014_123646/xray.json",
    "./xray.json",
    "XRAY.JSON",                                         # case is ignored
    "a\\b\\xray.json",                                  # Windows separators
])
def test_the_file_is_found_by_name_wherever_it_sits_in_the_archive(tmp_path, member):
    files = files_in(tmp_path)
    result = restore.restore_backup_zip(make_zip({member: GOOD_STORE}), files)
    assert result.restored == ["xray.json"] and result.invalid == []
    assert json.loads((tmp_path / "xray.json").read_text()) == GOOD_STORE
    assert os.listdir(tmp_path) == ["xray.json"]            # always the fixed destination, never the archive's path


def test_macos_metadata_and_folder_entries_are_ignored(tmp_path):
    files = files_in(tmp_path)
    members = {"__MACOSX/backup/._xray.json": "garbage", "backup/": "", "backup/._xray.json": "garbage",
               "backup/xray.json": GOOD_STORE}
    result = restore.restore_backup_zip(make_zip(members), files)
    assert result.restored == ["xray.json"] and result.invalid == []


def test_the_shallowest_copy_wins(tmp_path):
    other = {"clients": {"x": {"uuid": U1, "enabled": True}}}
    result = restore.restore_backup_zip(make_zip({"deep/er/xray.json": other, "xray.json": GOOD_STORE}), files_in(tmp_path))
    assert result.restored == ["xray.json"]
    assert json.loads((tmp_path / "xray.json").read_text()) == GOOD_STORE


def test_the_result_lists_what_the_archive_contains(tmp_path):
    result = restore.restore_backup_zip(make_zip({"notes.txt": "hi", "photos/a.png": "x"}), files_in(tmp_path))
    assert result.restored == [] and result.members == ["notes.txt", "photos/a.png"]


def test_an_archive_cannot_write_outside_the_fixed_destinations(tmp_path):
    files = {"xray.json": str(tmp_path / "data" / "xray.json")}
    restore.restore_backup_zip(make_zip({"../../etc/xray.json": GOOD_STORE, "/abs/path/xray.json": GOOD_STORE}), files)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["xray.json"]
    assert (tmp_path / "data" / "xray.json").exists()
