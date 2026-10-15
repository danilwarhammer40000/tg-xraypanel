"""
Command line helper for the Xray backend (run on the server with the panel).

    cd /opt/xraypanel
    venv/bin/python -m core.xray_cli preflight                 # host checks before taking over a manual stand
    venv/bin/python -m core.xray_cli adopt [--apply] [--force] # read the manual stand, show / write xray.json
    venv/bin/python -m core.xray_cli export-db --out /root     # restorable zip for the bot (same as tools/export_manual_stand.py)
    venv/bin/python -m core.xray_cli import-list /root/users.list   # only add people from users.list
    venv/bin/python -m core.xray_cli users-template > /root/users-plan.csv   # table of "external" people to move into the database
    venv/bin/python -m core.xray_cli import-users /root/users-plan.csv [--apply] [--allow-expired]
    venv/bin/python -m core.xray_cli urls                      # join-page URLs of everyone (secret!)
    venv/bin/python -m core.xray_cli disable NAME | enable NAME # switch a person off/on (then: publish)
    venv/bin/python -m core.xray_cli publish                   # sync Xray + publish subscriptions now

People taken over from a manual stand keep their UUID and token (every URL
already handed out keeps working) and are stored as "external" clients: they
are not panel users, so users.json status/expiry does not apply to them.
Names starting with "_" (service users such as _probe) are skipped.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.getenv("XRAYPANEL_ENV", "/opt/xraypanel/.env"))  # must precede core.* imports (DB path)

from core import manual_stand as ms  # noqa: E402
from core import subscription as sub  # noqa: E402
from core import users_import, xray, xray_adopt  # noqa: E402
from core.xray_adopt import parse_users_list  # noqa: E402,F401  (kept importable from here)


def _paths(args) -> dict:
    return {key: getattr(args, key) for key in ms.DEFAULT_PATHS}


def cmd_preflight(args) -> int:
    checks = ms.preflight(_paths(args), install_dir=os.path.dirname(os.path.dirname(xray.XRAY_STORE_PATH)))
    print(ms.format_checks(checks))
    return 1 if any(level == "FAIL" for level, _ in checks) else 0


def cmd_adopt(args) -> int:
    existing = xray.load_store(strict=True)
    plan = xray_adopt.build_plan(_paths(args), existing, force=args.force)
    print(ms.format_plan(plan))
    if not plan.ok:
        print("\nИсправьте пункты ❌: ничего не записано.")
        return 1
    if not args.apply:
        print("\nПробный запуск: ничего не записано. Для записи добавьте --apply")
        return 0
    backup = xray_adopt.apply_plan(plan)
    print(f"\nxray.json записан{f', старый сохранён: {backup}' if backup else ''}.")
    print("Дальше: python -m core.xray_cli publish   (или кнопка синхронизации в боте)")
    return 0


def cmd_export_db(args) -> int:
    plan = ms.inspect_stand(_paths(args))
    print(ms.format_plan(plan))
    if not plan.ok:
        return 1
    result = ms.export_bundle(plan, _paths(args), args.out)
    print(f"\nБаза для бота: {result['zip']}\nКопия исходников: {result['originals']}")
    return 0


def cmd_import_list(args) -> int:
    with open(args.path) as f:
        rows = list(parse_users_list(f.read()))

    added, skipped, bad = [], [], []

    def go(clients):
        for name, uid, token in rows:
            if uid is None:
                bad.append((name, token))
            elif name in clients:
                skipped.append(name)
            else:
                clients[name] = {"uuid": uid, "token": token, "enabled": True, "external": True}
                added.append(name)
        return {}

    xray._mutate("import", go)  # takes the lock, refuses to touch a corrupt xray.json
    print(f"добавлено: {len(added)} {added}")
    print(f"уже были, не тронуты: {len(skipped)} {skipped}")
    for name, reason in bad:
        print(f"ПРОПУЩЕНО {name}: {reason}")
    if added:
        print("Дальше: python -m core.xray_cli publish")
    return 1 if bad else 0


def cmd_switch(args) -> int:
    """enable/disable NAME for any client, including "external" people who have no card in the bot."""
    on = args.cmd == "enable"
    missing = []

    def go(clients):
        if args.name not in clients:
            missing.append(args.name)
            return {}
        clients[args.name]["enabled"] = on
        return {}

    xray._mutate(args.name, go)
    if missing:
        print(f"нет такого клиента: {args.name}")
        return 1
    print(f"{args.name}: {'включён' if on else 'выключен'}. Применить: python -m core.xray_cli publish")
    return 0


def cmd_users_template(args) -> int:
    from core import db
    store = xray.load_store()
    sys.stdout.write(users_import.build_template(store["clients"], {u.get("username") for u in db.list_users()}))
    return 0


def cmd_import_users(args) -> int:
    from core import db
    with open(args.path, encoding="utf-8") as f:
        rows, errors = users_import.parse_plan(f.read())
    store = xray.load_store(strict=True)
    plan = users_import.plan_import(rows, store["clients"], {u.get("username") for u in db.list_users()},
                                    allow_expired=args.allow_expired)
    plan.problems[:0] = errors
    print(users_import.format_plan(plan))
    if not plan.ok:
        print("\nИсправьте пункты ❌: ничего не записано.")
        return 1
    if not args.apply:
        print("\nПробный запуск: ничего не записано. Для записи добавьте --apply")
        return 0
    result = users_import.apply_import(plan)
    print(f"\nГотово: ведущих {len(result['leaders'])}, ведомых {len(result['followers'])}.")
    if result["backup"]:
        print(f"Копия прежней базы: {result['backup']}")
    if result["passwords"]:
        print("\nПароли ведущих (для привязки Telegram у клиента: имя и пароль; никому, кроме клиента, не показывайте):")
        for name, password in result["passwords"].items():
            print(f"  {name}: {password}")
    print("\nДальше: python -m core.xray_cli publish   (доступ теперь следует срокам из базы)")
    return 0


def cmd_urls(args=None) -> int:
    store = xray.load_store()
    cfg = sub.sub_settings(store.get("subscription"), os.path.dirname(xray.XRAY_STORE_PATH))
    if not cfg["enabled"]:
        print('подписки не настроены: в xray.json нет "subscription"."domain"')
        return 1
    for name, client in sorted(store["clients"].items()):
        if not sub.valid_token(client.get("token")):
            continue
        state = "вкл" if client.get("enabled") is True else "выкл"
        kind = "внешний" if client.get("external") else "панель"
        print(f"{name} [{state}, {kind}]\n  {sub.join_url(cfg, client['token'])}")
    return 0


def cmd_publish(args=None) -> int:
    try:
        restarted = xray.sync_xray_from_users()
    except sub.SubscriptionError as e:
        print("Xray применён, но:", e)
        return 2
    print("Xray перезапущен" if restarted else "Xray без изменений", "- подписки опубликованы")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.xray_cli")
    commands = parser.add_subparsers(dest="cmd", required=True)

    def with_paths(p):
        for key, default in ms.DEFAULT_PATHS.items():
            p.add_argument(f"--{key}", default=default, help=f"по умолчанию {default}")
        return p

    with_paths(commands.add_parser("preflight", help="проверки хоста перед переходом с ручной установки"))
    adopt = with_paths(commands.add_parser("adopt", help="прочитать ручной стенд и записать xray.json"))
    adopt.add_argument("--apply", action="store_true", help="записать (без этого флага только показать план)")
    adopt.add_argument("--force", action="store_true", help="перезаписать уже существующие секции server/subscription")
    export = with_paths(commands.add_parser("export-db", help="собрать zip для восстановления через бота"))
    export.add_argument("--out", default="/root")
    imp = commands.add_parser("import-list", help="добавить людей из users.list")
    imp.add_argument("path")
    commands.add_parser("users-template", help="таблица внешних клиентов для переноса в базу панели")
    iu = commands.add_parser("import-users", help="перенести внешних клиентов в базу панели (сроки, ведомые)")
    iu.add_argument("path")
    iu.add_argument("--apply", action="store_true", help="записать (без этого флага только показать план)")
    iu.add_argument("--allow-expired", action="store_true", help="разрешить даты в прошлом (доступ будет отключён)")
    commands.add_parser("urls", help="показать ссылки подключения (секретные)")
    for name, text in (("enable", "включить человека"), ("disable", "выключить человека (его подписка перестанет открываться)")):
        commands.add_parser(name, help=text).add_argument("name")
    commands.add_parser("publish", help="синхронизировать Xray и опубликовать подписки сейчас")

    args = parser.parse_args(argv)
    return {"preflight": cmd_preflight, "adopt": cmd_adopt, "export-db": cmd_export_db,
            "import-list": cmd_import_list, "urls": cmd_urls, "publish": cmd_publish,
            "enable": cmd_switch, "disable": cmd_switch,
            "users-template": cmd_users_template, "import-users": cmd_import_users}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
