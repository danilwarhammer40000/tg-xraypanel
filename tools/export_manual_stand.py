#!/usr/bin/env python3
"""
Export the people of a MANUALLY installed Xray stand as a database the new
panel can restore. Needs nothing but python3: run it straight from the
unpacked repository, before installing anything.

    python3 tools/export_manual_stand.py --check          # host checks only
    python3 tools/export_manual_stand.py                  # show the plan, change nothing
    python3 tools/export_manual_stand.py --out /root      # ... and write the files

It writes two files (mode 0600) into --out:
  xraypanel_backup_<time>.zip         upload it in the bot: 🗄 База данных → 📥 Загрузить БД
  manual_stand_originals_<time>.tar.gz   the 4 original files, only as a safety net

BOTH CONTAIN THE SERVER'S PRIVATE KEY AND EVERY PERSON'S TOKEN: copy them off
the server to a private place and never post them anywhere.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import manual_stand as ms  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only check the host (services, conflicts, tools)")
    parser.add_argument("--out", help="write the zip and the originals tarball into this directory")
    for key, default in ms.DEFAULT_PATHS.items():
        parser.add_argument(f"--{key}", default=default, help=f"default: {default}")
    args = parser.parse_args(argv)
    paths = {key: getattr(args, key) for key in ms.DEFAULT_PATHS}

    if args.check:
        checks = ms.preflight(paths)
        print(ms.format_checks(checks))
        return 1 if any(level == "FAIL" for level, _ in checks) else 0

    plan = ms.inspect_stand(paths)
    print(ms.format_plan(plan))
    if not plan.ok:
        print("\nИсправьте пункты ❌ и запустите снова: ничего не записано.")
        return 1
    clients = [n for n, c in plan.store["clients"].items()]
    print(f"\nГотово к выгрузке: {len(clients)} человек: {clients}")

    if not args.out:
        print("Ничего не записано. Для выгрузки добавьте --out /root")
        return 0

    result = ms.export_bundle(plan, paths, args.out)
    print(f"\nБаза для бота:       {result['zip']}")
    print(f"Копия исходников:    {result['originals']}")
    print("Скопируйте оба файла с сервера в безопасное место. В них приватный ключ и токены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
