"""
Owns: ExtraLinksPayment.waiting_receipt.

Client-initiated self-service request for additional devices.

One link = one device = core.payment.PRICE_PER_DEVICE (100 ₽) a month, and a
link is issued ONLY after the payment is made and the receipt is verified.
There is no free tier and no "pay later" path:

  1. The client picks how many devices to add and sees the amount due:
     count * PRICE_PER_DEVICE, just what is being added (NOT a recomputed
     whole-month total, see the note on that bug below). A pending_request of
     type "extra_links" is stored on the user record.
  2. The client sends a receipt. Gemini auto-checks it
     (core.auto_renewal.try_auto_verify_extra_links_payment) and, if the
     amount covers what was asked, the links are issued on the spot.
  3. Otherwise the receipt goes to the admin ("✅ Выдать" confirms the payment
     and issues, "💚 Без наценки" issues without raising the client's rate,
     "❌ Отклонить" issues nothing). The admin is the only person who can
     bypass the payment.

Paying changes the ongoing monthly RATE only, never expires_at: the new
rate takes effect at the client's next renewal.

BUG HISTORY: an earlier version charged
core.payment.calc_monthly_price_with_additional(), the client's WHOLE new
monthly total, as the amount to pay THIS transaction. That double-charged for
devices the client was already paying for. That function is now only used for
the informational "итого в месяц получится X₽" line, never as the charged
amount (see core.payment.quote_extra_devices). A second, unrelated bug from an
even earlier version prorated the charge by days remaining, which is
nonsensical for a brand-new request with no "already paid" period to
reconcile against.

Reuses the single pending_request slot on the user record: a client can't have
a renewal receipt AND an extra-devices request in flight at the same time (see
the guards in extra_links_start/extra_links_pick below). That is a deliberate
simplification: both are rare, short-lived, one-at-a-time asks.
"""
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, notify_bg, notify_client, run_sync
from bot.config import ADMIN_ID, bot
from bot.keyboards import client_menu
from bot.states import ExtraLinksPayment
from follower_issuance import build_connection_card, issue_follower, leader_is_active
from core.auto_renewal import try_auto_verify_extra_links_payment
from core.dates import utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel, notify_user
from core.payment import (
    DEVICES_PER_LINK,
    EXTRA_LINK_SURCHARGE,
    PAYMENT_LINK,
    PAYMENT_RECIPIENT,
    calc_monthly_price,
    devices_word,
    quote_extra_devices,
)

router = Router()
log = logging.getLogger(__name__)

MAX_EXTRA_LINKS = 4


async def _send_admin_review_card(caption: str, kb_rows: list, tg_id, file_id=None, is_photo=True) -> None:
    """
    Same "💬 Открыть чат в Telegram" tg://user?id=... deep-link pattern as
    bot/handlers/list_users.py's _send_user_card() — see that function's
    docstring for why this specific button sometimes gets rejected by
    Telegram (BUTTON_USER_INVALID / BUTTON_USER_PRIVACY_RESTRICTED) and
    why the fix is "retry once without that one button" rather than
    failing the whole card. Attaches the receipt (file_id) when one is
    given — the manual-review path only ever runs once a receipt exists.
    """
    rows = list(kb_rows)
    if tg_id:
        rows = rows + [[InlineKeyboardButton(text="💬 Открыть чат в Telegram", url=f"tg://user?id={tg_id}")]]

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    kb_no_chat = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        if file_id and is_photo:
            await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
        elif file_id:
            await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)
        else:
            await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
    except TelegramBadRequest as e:
        if tg_id and "BUTTON_USER_" in str(e):
            log.info("tg://user deep link rejected for telegram_id=%s (%s), resending without it", tg_id, e)
            if file_id and is_photo:
                await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb_no_chat)
            elif file_id:
                await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb_no_chat)
            else:
                await bot.send_message(ADMIN_ID, caption, reply_markup=kb_no_chat)
        else:
            raise


async def _issue_now(username: str, count: int, followers_snapshot: list) -> list:
    """Issues `count` new follower accounts immediately (no approval),
    following the exact same issue -> resync -> build-card ordering the
    admin-approval path uses (see follower_issuance.py's docstring for
    why that order matters). Returns the list of (username, card) pairs
    actually created — may be shorter than `count` if issue_follower()
    ever returns None (shouldn't happen for an existing leader)."""
    was_active = leader_is_active(get_user(username))

    created_usernames = []
    for _ in range(count):
        new_username = issue_follower(username, existing_followers=followers_snapshot)
        if not new_username:
            break
        created_usernames.append(new_username)
        followers_snapshot = followers_snapshot + [{"username": new_username}]

    if was_active and created_usernames:
        await run_sync()

    return [(u, build_connection_card(u)) for u in created_usernames]


def _device_button_label(n: int) -> str:
    """Compact label so MAX_EXTRA_LINKS buttons fit in one row: devices and what they cost now."""
    return f"{n} · {n * EXTRA_LINK_SURCHARGE}₽"


_TOPUP_BLOCKED = (
    "У вас есть неоплаченная доплата за уже выданные устройства — "
    "оплатите её («💳 Реквизиты для оплаты» → «💰 Доплатить»), прежде "
    "чем запрашивать новые."
)


@router.callback_query(F.data == "extralinks:start")
async def extra_links_start(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    if user.get("pending_request"):
        pending_type = (user.get("pending_request") or {}).get("type")
        if pending_type == "rate_topup":
            msg = _TOPUP_BLOCKED
        elif pending_type == "extra_links":
            msg = "У вас уже есть заявка на доп. устройства, ожидающая оплаты или проверки."
        else:
            msg = "У вас уже есть необработанный запрос — дождитесь ответа администратора."
        await call.answer(msg, show_alert=True)
        return

    followers = get_followers(user["username"])
    total_devices = (len(followers) + 1) * DEVICES_PER_LINK

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_device_button_label(n), callback_data=f"extralinks:req:{n}")
        for n in range(1, MAX_EXTRA_LINKS + 1)
    ]])

    await call.message.answer(
        "\n".join([
            "📱 Подключить ещё устройства",
            "",
            f"Сейчас подключено: {total_devices} {devices_word(total_devices)} (включая основное).",
            "",
            f"Одна ссылка — одно устройство, {EXTRA_LINK_SURCHARGE}₽/мес за каждое. "
            "Ссылка выдаётся после оплаты и проверки чека.",
            "",
            "Сколько устройств добавить?",
        ]),
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("extralinks:req:"))
async def extra_links_pick(call: CallbackQuery, state: FSMContext):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    try:
        count = int(call.data.split(":", 2)[2])
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_EXTRA_LINKS:
        await call.answer("Некорректное количество.", show_alert=True)
        return

    username = user["username"]
    pending = user.get("pending_request") or {}

    # Rate-topup gate: while the client owes money on devices they already
    # have (see bot/handlers/client_menu.py's "💰 Доплатить"), no NEW devices
    # are requested until that clears.
    if pending.get("type") == "rate_topup":
        await call.answer(_TOPUP_BLOCKED, show_alert=True)
        return

    if pending:
        await call.answer("У вас уже есть необработанный запрос.", show_alert=True)
        return

    quote = quote_extra_devices(username, count)
    update_user(username, pending_request={
        "type": "extra_links",
        "count": count,
        "amount_due": quote["amount_due"],
        "requested_at": utcnow_naive().isoformat(),
    })
    await state.set_state(ExtraLinksPayment.waiting_receipt)
    await state.update_data(epay_username=username)

    await call.message.answer(
        f"💰 Ещё {count} {devices_word(count)} — это {quote['amount_due']}₽ к вашей основной "
        f"подписке (итого в месяц получится {quote['new_rate']}₽ за {quote['total_devices']} "
        f"{devices_word(quote['total_devices'])}).\n\n"
        f"Переведите по ссылке ниже ещё {quote['amount_due']}₽\n\n"
        "Чек или скрин для проверки отправьте прямо сюда в чат ✅. "
        "Ссылка будет выдана после проверки чека.\n\n"
        f"{PAYMENT_LINK}\n{PAYMENT_RECIPIENT}"
    )
    await call.answer()


@router.message(ExtraLinksPayment.waiting_receipt, F.photo | F.document)
async def extra_links_payment_receipt(msg: Message, state: FSMContext):
    data = await state.get_data()
    username = data.get("epay_username")
    await state.clear()

    user = get_user(username)
    if not user:
        await msg.answer("Не удалось определить ваш аккаунт.", reply_markup=client_menu)
        return

    pending = user.get("pending_request") or {}
    if pending.get("type") != "extra_links":
        await msg.answer("Заявка уже не актуальна.", reply_markup=client_menu)
        return

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    pending = dict(pending)
    pending["receipt_file_id"] = file_id
    pending["receipt_is_photo"] = is_photo
    update_user(username, pending_request=pending)

    auto_applied = try_auto_verify_extra_links_payment(username)

    if auto_applied:
        await msg.answer("✅ Оплата подтверждена, устройства подключены — карточки выше.", reply_markup=client_menu)
        return

    count = pending.get("count", 1)
    amount_due = pending.get("amount_due")
    caption = (
        f"💰 Оплата доп. устройств от {username}\n"
        f"{count} {devices_word(count)}, ожидается ~{amount_due}₽ (доплата к текущей ставке)\n\n"
        f"«✅ Выдать» — подтвердить оплату и выдать.\n"
        f"«💚 Без наценки» — выдать бесплатно, без учёта наценки.\n"
        f"«❌ Отклонить» — не выдавать."
    )
    kb_rows = [
        [InlineKeyboardButton(text=f"✅ Выдать {count}", callback_data=f"exlreview:{username}:approve")],
        [InlineKeyboardButton(text="💚 Без наценки", callback_data=f"exlreview:{username}:approve_free")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"exlreview:{username}:reject")],
    ]
    await _send_admin_review_card(caption, kb_rows, user.get("telegram_id"), file_id=file_id, is_photo=is_photo)
    await notify_bg(log_to_channel, caption)

    await msg.answer("✅ Чек отправлен администратору на проверку.", reply_markup=client_menu)


@router.message(ExtraLinksPayment.waiting_receipt)
async def extra_links_payment_wrong_content(msg: Message):
    await msg.answer("Пришлите, пожалуйста, именно фото или файл чека.")


@router.callback_query(F.data.startswith("exlreview:"))
async def extra_links_review(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":")
    user = get_user(username)

    pending = (user or {}).get("pending_request") or {}
    if not user or pending.get("type") != "extra_links":
        await call.answer("Заявка уже обработана.", show_alert=True)
        try:
            await call.message.edit_caption(caption=(call.message.caption or call.message.text or "") + "\n\n⚠️ Уже обработано.")
        except Exception:
            pass
        return

    count = pending.get("count", 1)
    update_user(username, pending_request=None)

    if action == "reject":
        try:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ Отклонено")
        except Exception:
            pass

        if user.get("telegram_id"):
            await notify_client(
                bot, user["telegram_id"],
                "❌ Оплата/запрос на доп. устройства отклонён администратором.",
                clear_username=username
            )

        await call.answer("Отклонено")
        return

    # action == "approve" or "approve_free": issue -> resync -> build-card
    # ordering, via the shared _issue_now() helper. The only difference between the two is
    # whether these links count toward the monthly-price surcharge (see
    # core/payment.py's calc_monthly_price) — "approve_free" bumps
    # surcharge_exempt_links so these specific links never add to the
    # client's price, "approve" leaves it as-is so they do (and confirms
    # last_renewal_rate at the new total). Neither touches expires_at —
    # this is a rate change, not a renewal.
    created = await _issue_now(username, count, get_followers(username))
    waived = action == "approve_free"

    if waived and created:
        update_user(username, surcharge_exempt_links=(user.get("surcharge_exempt_links") or 0) + len(created))
    elif created:
        new_rate, _ = calc_monthly_price(username)
        update_user(username, last_renewal_rate=new_rate)

    status_note = "✅ Выдано {} из {}{}".format(
        len(created), count, " (без наценки)" if waived else ""
    )
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n{status_note}")
    except Exception:
        pass

    if user.get("telegram_id") and created:
        client_note = (
            f"✅ Администратор подключил вам ещё {len(created)} {devices_word(len(created))}"
            + (" без повышения тарифа:" if waived else (
                f" — оплата подтверждена, ставка теперь {calc_monthly_price(username)[0]}₽/мес:"
            ))
        )
        delivered = await notify_client(
            bot, user["telegram_id"],
            client_note,
            clear_username=username
        )
        if delivered:
            for _, card in created:
                try:
                    await bot.send_message(user["telegram_id"], card)
                except (TelegramBadRequest, TelegramForbiddenError):
                    log.warning("could not deliver a new connection card to %s", username)

    await notify_bg(
        log_to_channel,
        f"✅ Выдано {len(created)} доп. ссылок ({len(created)} {devices_word(len(created))}) для {username} "
        f"(запрошено {count})" + (", без наценки." if waived else ".")
    )

    await call.answer("Готово")
