"""The bot's "📥 Загрузить БД" handler, driven with fake Telegram objects."""
import asyncio
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("ADMIN_ID", "1")

from bot.handlers import database  # noqa: E402

U1 = "11111111-1111-4111-8111-111111111111"
STORE = {"version": 1, "server": {"domain": "cdn.test"},
         "clients": {"main": {"uuid": U1, "token": "a" * 32, "enabled": True, "external": True}}}


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, json.dumps(content) if not isinstance(content, str) else content)
    return buf.getvalue()


class Msg:
    def __init__(self, file_name="xraypanel_backup_x.zip"):
        self.sent, self.from_user = [], SimpleNamespace(id=1)
        self.document = SimpleNamespace(file_name=file_name, file_id="f")

    async def answer(self, text, **kw):
        self.sent.append(text)


class State:
    async def clear(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = SimpleNamespace(syncs=[], raw=b"")
    monkeypatch.setattr(database, "BACKUP_FILES", {"users.json": str(tmp_path / "users.json"),
                                                   "xray.json": str(tmp_path / "xray.json")})

    class Bot:
        async def get_file(self, file_id):
            return SimpleNamespace(file_path="p")

        async def download_file(self, path):
            return io.BytesIO(e.raw)

    async def run_sync():
        e.syncs.append(1)

    monkeypatch.setattr(database, "bot", Bot())
    monkeypatch.setattr(database, "run_sync", run_sync)
    e.dir = tmp_path
    return e


def upload(env, raw, **kw):
    env.raw = raw
    msg = Msg(**kw)
    asyncio.run(database.db_import_apply(msg, State()))
    return msg.sent


def test_a_plain_export_restores_and_syncs(env):
    sent = upload(env, make_zip({"xray.json": STORE}))
    assert sent[0].startswith("✅ Восстановлено: xray.json") and sent[-1].startswith("✅ Готово")
    assert json.loads((env.dir / "xray.json").read_text()) == STORE and env.syncs == [1]


def test_an_archive_re_zipped_inside_a_folder_restores_too(env):
    sent = upload(env, make_zip({"xraypanel_backup_20261014_123646/xray.json": STORE}))
    assert sent[0].startswith("✅ Восстановлено: xray.json") and env.syncs == [1]


def test_nothing_found_tells_what_the_archive_actually_contains(env):
    sent = upload(env, make_zip({"notes.txt": "hi", "pics/a.png": "x"}))
    assert len(sent) == 1 and "не нашлось ни одного нужного файла" in sent[0]
    assert "В вашем архиве лежит: notes.txt, pics/a.png" in sent[0] and env.syncs == []
    assert not (env.dir / "xray.json").exists()


def test_a_broken_xray_json_is_reported_and_not_written(env):
    sent = upload(env, make_zip({"xray.json": "{}"}))
    assert sent[0].startswith("⚠️ xray.json") and "не нашлось" in sent[1]
    assert not (env.dir / "xray.json").exists() and env.syncs == []


def test_not_a_zip_and_not_a_zip_name(env):
    assert upload(env, b"nope")[0] == "Файл повреждён или это не zip-архив."
    assert upload(env, make_zip({"xray.json": STORE}), file_name="backup.tar.gz")[0] == "Это не .zip файл. Загрузка отменена."
