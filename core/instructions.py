# Инструкции по подключению — правьте только этот файл, код бота трогать не нужно.
#
# Подключение идёт через страницу подключения (join.html на хосте подписок):
# на ней кнопки «Добавить подписку» и «Маршрутизация» для Incy, Happ и v2RayTun.
# Ссылки на магазины приложений заполните своими: пустая строка просто скрывает строку со ссылкой.

APP_LINK_ANDROID = ""   # например, ссылка на Incy в Google Play
APP_LINK_IOS = ""       # например, ссылка на Incy в App Store

FALLBACK_LINK_NOTE = (
    "вашей странице подключения (если под рукой нет, нажмите «🔗 Мои подключения» в меню)"
)

APPS_NOTE = "Подойдёт Incy, Happ или v2RayTun (ниже на примере Incy)."


def _install_line(store_link: str) -> str:
    return f"{store_link}\n\n" if store_link else "\n"


def render_android_instructions(link: str = None) -> str:
    """
    Interactive (in-bot) version. If `link` is given (the join page from
    core.connect.primary_link), it is embedded at the "open your page" step.
    """
    link_line = link if link else FALLBACK_LINK_NOTE

    return (
        "🤖 Подключение на Android\n\n"
        f"{APPS_NOTE}\n\n"
        "1. Установите приложение Incy (Google Play):\n"
        f"{_install_line(APP_LINK_ANDROID)}"
        f"2. Откройте страницу подключения в обычном браузере (не внутри мессенджера): {link_line}\n\n"
        "3. Нажмите «Добавить подписку»: ссылка откроется в приложении, сервер и маршрутизация "
        "(российские сайты и банки напрямую, остальное через VPN) добавятся сами.\n\n"
        "4. Включите подключение круглой кнопкой в приложении.\n\n"
        "Если что-то не получается — просто напишите мне (кнопка \"✉️ Написать "
        "администратору\"), помогу разобраться. 🙌"
    )


def render_ios_instructions(link: str = None) -> str:
    link_line = link if link else FALLBACK_LINK_NOTE

    return (
        "📱 Подключение на iPhone\n\n"
        f"{APPS_NOTE}\n\n"
        "1. Установите приложение Incy (App Store):\n"
        f"{_install_line(APP_LINK_IOS)}"
        f"2. Откройте страницу подключения в Safari (не внутри мессенджера): {link_line}\n\n"
        "3. Нажмите «Добавить подписку»: ссылка откроется в приложении, сервер и маршрутизация "
        "(российские сайты и банки напрямую, остальное через VPN) добавятся сами.\n\n"
        "4. Включите подключение круглой кнопкой в приложении (iOS попросит разрешить VPN-конфигурацию).\n\n"
        "Если что-то не получается — просто напишите мне (кнопка \"✉️ Написать "
        "администратору\"), помогу разобраться. 🙌"
    )


# Используется в РУЧНОМ режиме (когда админ сам копирует и отправляет карточку
# новому клиенту — часто ДО того, как у клиента появится доступ к Telegram,
# например через WhatsApp). Установку приложения админ в этом режиме уже
# объясняет отдельно сам, до отправки карточки — поэтому здесь только шаги
# ПОСЛЕ установки.
MANUAL_CONNECT_STEPS = (
    "📲 Как подключиться (после установки приложения):\n\n"
    "1. Откройте ссылку из карточки выше в обычном браузере\n\n"
    "2. Нажмите «Добавить подписку»: сервер и маршрутизация добавятся сами\n\n"
    "3. В приложении включите подключение круглой кнопкой."
)


# ---------------- ROUTING (RU sites go direct while the VPN is on) ----------------

def render_routing_instructions(link: str = None) -> str:
    link_line = f"\n\nВаша страница подключения:\n{link}" if link else ""
    return (
        "🇷🇺 Чтобы российские сайты и приложения (Госуслуги, банки, VK, Ozon, "
        "Wildberries и т. д.) работали как обычно, когда VPN включён, используйте "
        "маршрутизацию: трафик к российским адресам идёт напрямую, остальной через VPN.\n\n"
        "В Incy и Happ она приходит автоматически вместе с подпиской (и обновляется сама), "
        "отдельных действий не нужно. Если российские сайты всё же открываются через VPN, "
        "обновите подписку в приложении.\n\n"
        "В v2RayTun правила маршрутизации задаются вручную в настройках приложения: "
        "российские домены и IP (geosite:category-ru, geoip:ru) направьте в «Direct»."
        f"{link_line}"
    )


def render_bot_usage_instructions() -> str:
    """
    "Как пользоваться ботом" — an explanation of the client_menu buttons
    themselves (ℹ️/🔗/📖/💳/✉️), not a device-connection guide (that's
    render_android_instructions/render_ios_instructions above). Sent
    automatically right after a client's first successful link to the DB
    (see bot/handlers/start.py's onboard_trial_start/bind_by_card, and
    the invite-link flow in bot/handlers/leader_link.py), plus reachable
    on demand via the "🤖 Как пользоваться ботом" button both inside
    "📖 Инструкции" and right after the platform-specific setup steps
    (bot/keyboards.py's bot_usage_button_kb(), used in client_menu.py's
    howto:ios/howto:android).
    """
    return (
        "🤖 Как пользоваться ботом\n\n"
        "ℹ️ Мой статус — когда истекает доступ и сколько подключений активно.\n\n"
        "🔗 Мои подключения — ваши ссылки. Каждая ссылка — одно устройство; "
        "доп. устройство стоит 100₽/мес, ссылка выдаётся после оплаты и проверки чека.\n\n"
        "📖 Инструкции — три раздела: как подключить приложение (Android/iOS), "
        "как включить маршрутизацию (российские сайты напрямую), и это же руководство — "
        "на случай, если понадобится ещё раз.\n\n"
        "💳 Реквизиты для оплаты — здесь вы можете продлить ваше подключение.\n\n"
        "✉️ Написать администратору — напишите ваш вопрос и предложения по "
        "улучшению работы 👍🚀"
    )
