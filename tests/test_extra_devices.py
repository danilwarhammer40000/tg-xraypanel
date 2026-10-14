"""
One link = one device = 100 rub/month; a link is issued ONLY after the payment is
made and the receipt is verified (or by the admin). Pricing first, then the real
handlers driven with fake Telegram objects.
"""
import asyncio
import importlib
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("ADMIN_ID", "1")

from core import db as db_module  # noqa: E402
from core import payment  # noqa: E402
from bot.handlers import extra_links  # noqa: E402
from follower_issuance import FREE_EXTRA_LINKS  # noqa: E402

ADMIN = 1
CLIENT_TG = 555


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("XRAYPANEL_DB_PATH", str(tmp_path / "users.json"))
    importlib.reload(db_module)
    db_module.add_user({"username": "anna", "password": "pw", "telegram_id": CLIENT_TG,
                        "status": "active", "expires_at": "2099-01-01"})
    return db_module


def add_follower(db, name, leader="anna"):
    db.add_user({"username": name, "password": "pw", "status": "active", "expires_at": "2099-01-01"})
    db.link_user(name, leader)


# ---------------- pricing ----------------

def test_constants_one_link_is_one_device_at_100_and_nothing_is_free():
    assert payment.PRICE_PER_DEVICE == 100 and payment.DEVICES_PER_LINK == 1
    assert payment.BASE_PRICE_PER_MONTH == 100 and payment.EXTRA_LINK_SURCHARGE == 100
    assert FREE_EXTRA_LINKS == 0


def test_monthly_price_is_100_per_device(db):
    assert payment.calc_monthly_price("anna") == (100, 0)
    add_follower(db, "anna-2")
    assert payment.calc_monthly_price("anna") == (200, 1)
    add_follower(db, "anna-3")
    assert payment.calc_monthly_price("anna") == (300, 2)


def test_an_admin_waiver_removes_that_device_from_the_price(db):
    add_follower(db, "anna-2")
    add_follower(db, "anna-3")
    db.update_user("anna", surcharge_exempt_links=1)
    assert payment.calc_monthly_price("anna") == (200, 1)


def test_a_pinned_custom_rate_replaces_the_automatic_price(db):
    add_follower(db, "anna-2")
    db.update_user("anna", custom_rate=150)
    assert payment.calc_monthly_price("anna") == (150, 0)


def test_quote_charges_only_the_marginal_cost(db):
    add_follower(db, "anna-2")                      # already pays 200/mo for 2 devices
    quote = payment.quote_extra_devices("anna", 2)
    assert quote == {"amount_due": 200, "new_rate": 400, "total_devices": 4}


@pytest.mark.parametrize("n,word", [(1, "устройство"), (2, "устройства"), (4, "устройства"), (5, "устройств"),
                                    (11, "устройств"), (12, "устройств"), (21, "устройство"), (22, "устройства"),
                                    (111, "устройств")])
def test_russian_plurals(n, word):
    assert payment.devices_word(n) == word


def test_payment_info_counts_devices_not_pairs(db):
    add_follower(db, "anna-2")
    add_follower(db, "anna-3")
    text = payment.render_payment_info_for_user("anna")
    assert "300руб/мес" in text and "3 устройства (100₽/мес за каждое)" in text
    assert "6 устройств" not in text


# ---------------- fakes ----------------

class FakeMessage:
    def __init__(self):
        self.sent, self.edited, self.caption, self.text = [], None, "admin card", None

    async def answer(self, text, **kw):
        self.sent.append((text, kw))

    async def edit_caption(self, caption=None, **kw):
        self.edited = caption


class FakeCall:
    def __init__(self, data, tg_id=CLIENT_TG):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=tg_id), FakeMessage(), []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append((text, show_alert))


class FakeState:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)
    async def clear(self): self.state, self.data = None, {}


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(("msg", chat_id, text))
    async def send_photo(self, chat_id, photo=None, caption=None, **kw): self.sent.append(("photo", chat_id, caption, kw.get("reply_markup")))
    async def send_document(self, chat_id, document=None, caption=None, **kw): self.sent.append(("doc", chat_id, caption, kw.get("reply_markup")))


@pytest.fixture
def world(db, monkeypatch):
    w = SimpleNamespace(bot=FakeBot(), syncs=[], logs=[], notes=[], auto=False)

    async def run_sync(): w.syncs.append(1)
    async def notify_bg(fn, *a, **k): w.logs.append(a)
    async def notify_client(bot, tg, text, **k):
        w.notes.append(text)
        return True

    monkeypatch.setattr(extra_links, "bot", w.bot)
    monkeypatch.setattr(extra_links, "run_sync", run_sync)
    monkeypatch.setattr(extra_links, "notify_bg", notify_bg)
    monkeypatch.setattr(extra_links, "notify_client", notify_client)
    monkeypatch.setattr(extra_links, "build_connection_card", lambda u: f"CARD:{u}")
    monkeypatch.setattr(extra_links, "try_auto_verify_extra_links_payment", lambda u: w.auto)
    return w


def run(coro):
    return asyncio.run(coro)


def followers(db):
    return [f["username"] for f in db.get_followers("anna")]


# ---------------- the request screens ----------------

def test_start_screen_says_one_link_one_device_and_shows_prices_on_the_buttons(db, world):
    call = FakeCall("extralinks:start")
    run(extra_links.extra_links_start(call))
    text, kw = call.message.sent[0]
    assert "Сейчас подключено: 1 устройство" in text
    assert "Одна ссылка — одно устройство, 100₽/мес за каждое" in text
    assert "после оплаты и проверки чека" in text
    assert "🆓" not in text and "бесплатно" not in text.lower()
    labels = [b.text for b in kw["reply_markup"].inline_keyboard[0]]
    assert labels == ["1 · 100₽", "2 · 200₽", "3 · 300₽", "4 · 400₽"]


def test_start_screen_counts_existing_devices(db, world):
    add_follower(db, "anna-2")
    call = FakeCall("extralinks:start")
    run(extra_links.extra_links_start(call))
    assert "Сейчас подключено: 2 устройства" in call.message.sent[0][0]


@pytest.mark.parametrize("pending,fragment", [
    ({"type": "rate_topup"}, "неоплаченная доплата"),
    ({"type": "extra_links"}, "заявка на доп. устройства"),
    ({"type": "renewal"}, "необработанный запрос"),
])
def test_start_is_blocked_by_any_pending_request(db, world, pending, fragment):
    db.update_user("anna", pending_request=pending)
    call = FakeCall("extralinks:start")
    run(extra_links.extra_links_start(call))
    assert call.message.sent == [] and fragment in call.alerts[0][0] and call.alerts[0][1] is True


# ---------------- nothing is issued before payment ----------------

def test_picking_devices_asks_for_payment_and_issues_nothing(db, world):
    call, state = FakeCall("extralinks:req:2"), FakeState()
    run(extra_links.extra_links_pick(call, state))

    pending = db.get_user("anna")["pending_request"]
    assert pending["type"] == "extra_links" and pending["count"] == 2 and pending["amount_due"] == 200
    assert followers(db) == [] and world.syncs == []                   # NO link before the money
    assert state.data == {"epay_username": "anna"} and state.state is not None

    text = call.message.sent[0][0]
    assert "Ещё 2 устройства — это 200₽" in text
    assert "итого в месяц получится 300₽ за 3 устройства" in text
    assert "Ссылка будет выдана после проверки чека" in text
    assert payment.PAYMENT_LINK in text


@pytest.mark.parametrize("data", ["extralinks:req:0", "extralinks:req:5", "extralinks:req:-1", "extralinks:req:99",
                                  "extralinks:req:abc"])
def test_out_of_range_counts_are_rejected(db, world, data):
    call, state = FakeCall(data), FakeState()
    run(extra_links.extra_links_pick(call, state))
    assert call.alerts == [("Некорректное количество.", True)]
    assert db.get_user("anna").get("pending_request") is None and state.state is None


@pytest.mark.parametrize("pending", [{"type": "rate_topup"}, {"type": "extra_links", "count": 1}, {"type": "x"}])
def test_a_second_request_cannot_start_while_one_is_pending(db, world, pending):
    db.update_user("anna", pending_request=pending)
    call, state = FakeCall("extralinks:req:1"), FakeState()
    run(extra_links.extra_links_pick(call, state))
    assert call.alerts and call.alerts[0][1] is True and call.message.sent == []
    assert db.get_user("anna")["pending_request"] == pending and followers(db) == []


def make_receipt_message(file_id="FILE1"):
    msg = FakeMessage()
    msg.photo, msg.document = [SimpleNamespace(file_id=file_id)], None
    return msg


def start_request(db, world, count=1):
    run(extra_links.extra_links_pick(FakeCall(f"extralinks:req:{count}"), FakeState()))


def test_an_unverified_receipt_goes_to_the_admin_and_still_issues_nothing(db, world):
    start_request(db, world, 2)
    state = FakeState()
    state.data = {"epay_username": "anna"}
    msg = make_receipt_message()
    msg.answer = FakeMessage().answer
    msg.sent = []

    async def answer(text, **kw): msg.sent.append(text)
    msg.answer = answer
    run(extra_links.extra_links_payment_receipt(msg, state))

    pending = db.get_user("anna")["pending_request"]
    assert pending["receipt_file_id"] == "FILE1" and pending["receipt_is_photo"] is True
    assert followers(db) == [] and world.syncs == []                      # still nothing: waiting for the admin
    kind, chat, caption, markup = world.bot.sent[0]
    assert chat == ADMIN and "2 устройства, ожидается ~200₽" in caption
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if getattr(b, "callback_data", None)]
    assert callbacks == ["exlreview:anna:approve", "exlreview:anna:approve_free", "exlreview:anna:reject"]
    assert msg.sent == ["✅ Чек отправлен администратору на проверку."]


def test_a_receipt_the_ai_verifies_is_handled_without_the_admin(db, world):
    start_request(db, world, 1)
    world.auto = True                                       # core.auto_renewal issued the link itself
    state = FakeState()
    state.data = {"epay_username": "anna"}
    msg = make_receipt_message()
    out = []

    async def answer(text, **kw): out.append(text)
    msg.answer = answer
    run(extra_links.extra_links_payment_receipt(msg, state))
    assert out == ["✅ Оплата подтверждена, устройства подключены — карточки выше."]
    assert world.bot.sent == []                             # no admin card


def test_a_receipt_without_a_matching_request_is_ignored(db, world):
    state = FakeState()
    state.data = {"epay_username": "anna"}
    msg = make_receipt_message()
    out = []

    async def answer(text, **kw): out.append(text)
    msg.answer = answer
    run(extra_links.extra_links_payment_receipt(msg, state))
    assert out == ["Заявка уже не актуальна."] and world.bot.sent == []


# ---------------- the admin decides ----------------

def prepare_review(db, world, count=2):
    start_request(db, world, count)
    pending = dict(db.get_user("anna")["pending_request"], receipt_file_id="FILE1", receipt_is_photo=True)
    db.update_user("anna", pending_request=pending)


def review(action, tg=ADMIN):
    call = FakeCall(f"exlreview:anna:{action}", tg)
    run(extra_links.extra_links_review(call))
    return call


def test_approve_issues_one_link_per_device_and_raises_the_rate(db, world):
    prepare_review(db, world, 2)
    call = review("approve")

    assert followers(db) == ["anna-2", "anna-3"]                       # 2 devices -> 2 links, not 1 pair
    assert db.get_user("anna").get("pending_request") is None
    assert payment.calc_monthly_price("anna") == (300, 2)
    assert db.get_user("anna")["last_renewal_rate"] == 300             # next receipt is judged at the new rate
    assert world.syncs == [1]                                          # synced BEFORE the cards were built
    assert "✅ Выдано 2 из 2" in call.message.edited
    assert world.notes[0] == "✅ Администратор подключил вам ещё 2 устройства — оплата подтверждена, ставка теперь 300₽/мес:"
    cards = [item for item in world.bot.sent if item[0] == "msg"]
    assert [c[2] for c in cards] == ["CARD:anna-2", "CARD:anna-3"] and all(c[1] == CLIENT_TG for c in cards)


def test_approve_without_markup_issues_but_keeps_the_rate(db, world):
    prepare_review(db, world, 1)
    review("approve_free")
    assert followers(db) == ["anna-2"]
    assert payment.calc_monthly_price("anna") == (100, 0)              # the admin waived the surcharge
    assert db.get_user("anna")["surcharge_exempt_links"] == 1
    assert "без повышения тарифа" in world.notes[0]


def test_reject_issues_nothing_and_clears_the_request(db, world):
    prepare_review(db, world, 2)
    call = review("reject")
    assert followers(db) == [] and db.get_user("anna").get("pending_request") is None
    assert "Отклонено" in call.message.edited and world.syncs == []
    assert world.notes == ["❌ Оплата/запрос на доп. устройства отклонён администратором."]


def test_only_the_admin_can_approve(db, world):
    prepare_review(db, world, 1)
    review("approve", tg=CLIENT_TG)
    assert followers(db) == [] and db.get_user("anna")["pending_request"]["type"] == "extra_links"


def test_a_request_that_was_already_processed_cannot_be_approved_twice(db, world):
    prepare_review(db, world, 1)
    review("approve")
    call = review("approve")
    assert followers(db) == ["anna-2"] and call.alerts[0] == ("Заявка уже обработана.", True)


# ---------------- the old paths are really gone ----------------

def test_no_free_tier_and_no_pay_later_handlers_remain():
    for name in ("_free_remaining", "_remaining_days", "extra_links_trial_confirm", "extra_links_trial_cancel"):
        assert not hasattr(extra_links, name)
    assert not hasattr(payment, "SHORT_REMAINING_THRESHOLD_DAYS")


# ---------------- the AI receipt check (core.auto_renewal) ----------------

from core import auto_renewal  # noqa: E402


@pytest.fixture
def auto(db, monkeypatch):
    """A pending extra_links request for 2 devices (200 rub) with a receipt attached; the engine and Telegram are faked."""
    import follower_issuance
    a = SimpleNamespace(extraction={"readable": True, "confidence": 0.95, "amount": 200}, syncs=[], told=[], admin=[])

    db.update_user("anna", pending_request={"type": "extra_links", "count": 2, "amount_due": 200,
                                            "receipt_file_id": "F1", "receipt_is_photo": True})
    monkeypatch.setattr(auto_renewal, "_fetch_receipt_file", lambda pending: (b"img", "image/jpeg", "F1", True))
    monkeypatch.setattr("core.gemini_client.extract_receipt_data", lambda b, m: a.extraction)
    monkeypatch.setattr(auto_renewal, "safe_sync", lambda: a.syncs.append(1) or "OK")
    monkeypatch.setattr(follower_issuance, "build_connection_card", lambda u: f"CARD:{u}")
    monkeypatch.setattr(auto_renewal, "notify_user", lambda user, text: a.told.append(text))
    monkeypatch.setattr(auto_renewal, "send_message", lambda chat, text: a.told.append(text))
    monkeypatch.setattr(auto_renewal, "notify_admin", lambda text: a.admin.append(text))
    return a


def test_a_verified_receipt_issues_one_link_per_device(db, auto):
    assert auto_renewal.try_auto_verify_extra_links_payment("anna") is True
    assert followers(db) == ["anna-2", "anna-3"]
    assert db.get_user("anna").get("pending_request") is None
    assert payment.calc_monthly_price("anna") == (300, 2) and db.get_user("anna")["last_renewal_rate"] == 300
    assert auto.syncs == [1]
    assert auto.told[0] == "✅ Оплата получена — подключено ещё 2 устройства. Ставка теперь 300₽/мес (спишется со следующего продления):"
    assert auto.told[1:] == ["CARD:anna-2", "CARD:anna-3"]
    assert "2 устройства" in auto.admin[0]


@pytest.mark.parametrize("extraction", [
    {"readable": True, "confidence": 0.95, "amount": 199},        # short by a rouble
    {"readable": True, "confidence": 0.95, "amount": 100},        # paid for one device, asked for two
    {"readable": True, "confidence": 0.95, "amount": 401},        # more than double: wrong receipt
    {"readable": True, "confidence": 0.5, "amount": 200},         # unsure what it read
    {"readable": False, "confidence": 0.99, "amount": 200},       # not a payment document
    {"readable": True, "confidence": 0.95, "amount": None},
    {"readable": True, "confidence": 0.95, "amount": "200"},
])
def test_anything_else_goes_to_the_admin_and_issues_nothing(db, auto, extraction):
    auto.extraction = extraction
    assert auto_renewal.try_auto_verify_extra_links_payment("anna") is False
    assert followers(db) == [] and auto.syncs == [] and auto.told == []
    assert db.get_user("anna")["pending_request"]["type"] == "extra_links"      # still waiting for review


def test_no_receipt_no_issue(db, auto):
    pending = dict(db.get_user("anna")["pending_request"])
    pending.pop("receipt_file_id")
    db.update_user("anna", pending_request=pending)
    assert auto_renewal.try_auto_verify_extra_links_payment("anna") is False and followers(db) == []
