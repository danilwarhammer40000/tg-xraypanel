#!/usr/bin/env python3
"""
Профили маршрутизации для Incy и Happ + заголовки подписки (только python3, без зависимостей).

Запускать на ХОСТЕ ПОДПИСОК:

    python3 gen_routing.py --domain config.example.net [--mode ru|wl] [--reload]

Что делает:
  1. Пишет профили в /var/www/sub/routing/: incy-ru.json, incy-wl.json, happ-ru.json, happ-wl.json
     (режимы: ru = российские сайты и IP напрямую; wl = напрямую только белый список).
  2. Пишет кусок конфигурации nginx /etc/nginx/snippets/sub-routing-headers.conf с двумя
     заголовками, которые nginx добавляет к КАЖДОМУ ответу подписки:
       autorouting: incy://autorouting/onadd/https://<домен>/routing/incy-<режим>.json
       routing:     happ://routing/onadd/<base64 профиля Happ>
     Приложение само применяет профиль и скачивает гео-файлы при импорте и при каждом
     обновлении подписки: отдельная кнопка «Маршрутизация» людям не нужна.
     Incy берёт заголовок autorouting (у него наивысший приоритет, профиль обновляется сам),
     Happ берёт routing. Лишний для клиента заголовок игнорируется.

После изменения правил запустите скрипт заново (с --reload или вручную
`nginx -t && systemctl reload nginx`), и клиенты подтянут новый профиль при ближайшем
обновлении подписки.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

GEOIP = "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip/release/geoip.dat"
GEOSITE = "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geosite/release/geosite.dat"

# режим -> (название профиля, DirectSites, DirectIp). Название ОБЯЗАНО быть стабильным:
# профиль с тем же именем приложение обновляет, а не дублирует.
MODES = {
    "ru": ("РФ напрямую",
           ["geosite:private", "geosite:category-ru", "geosite:whitelist"],
           ["geoip:private", "geoip:direct"]),
    "wl": ("Белый список напрямую",
           ["geosite:private", "geosite:whitelist"],
           ["geoip:private", "geoip:whitelist"]),
}

DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


def build_profile(app: str, mode: str, now: int) -> dict:
    """Профиль маршрутизации: у Incy полный набор полей, у Happ только те, что описаны в его документации."""
    name, sites, ips = MODES[mode]
    profile = {
        "Name": name, "GlobalProxy": "true", "RemoteDns": "8.8.8.8", "DomesticDns": "77.88.8.8",
        "Geoipurl": GEOIP, "Geositeurl": GEOSITE, "DnsHosts": {},
        "DirectSites": sites, "DirectIp": ips, "ProxySites": [], "ProxyIp": [],
        "BlockSites": [], "BlockIp": [], "DomainStrategy": "IPIfNonMatch",
    }
    if app == "incy":
        profile.update({
            "UseChunkFiles": "true",
            "RemoteDNSType": "DoH", "RemoteDNSDomain": "https://8.8.8.8/dns-query", "RemoteDNSIP": "8.8.8.8",
            "DomesticDNSType": "DoH", "DomesticDNSDomain": "https://77.88.8.8/dns-query", "DomesticDNSIP": "77.88.8.8",
            "LastUpdated": str(now), "RouteOrder": "block-proxy-direct", "FakeDNS": "false",
        })
    return profile


def profile_bytes(profile: dict) -> bytes:
    return (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode()


def header_values(domain: str, mode: str, happ_profile: bytes) -> dict:
    """Значения заголовков подписки (без кавычек и $, чтобы они безопасно лежали в конфиге nginx)."""
    return {
        "autorouting": f"incy://autorouting/onadd/https://{domain}/routing/incy-{mode}.json",
        "routing": "happ://routing/onadd/" + base64.b64encode(happ_profile).decode(),
    }


def nginx_snippet(headers: dict) -> str:
    lines = ["# Создано gen_routing.py: не правьте руками.",
             "# Профиль маршрутизации приезжает вместе с подпиской: Incy читает autorouting, Happ читает routing."]
    lines += [f'add_header {name} "{value}" always;' for name, value in headers.items()]
    return "\n".join(lines) + "\n"


def write(path: str, data: bytes, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def generate(domain: str, mode: str, out_dir: str, snippet_path: str, now: int = None) -> dict:
    if not DOMAIN_RE.match(domain):
        raise ValueError(f"некорректный домен: {domain!r}")
    if mode not in MODES:
        raise ValueError(f"режим должен быть один из {sorted(MODES)}")
    now = int(time.time()) if now is None else now

    written = []
    happ_default = b""
    for app in ("incy", "happ"):
        for m in MODES:
            data = profile_bytes(build_profile(app, m, now))
            path = os.path.join(out_dir, f"{app}-{m}.json")
            write(path, data)
            written.append(path)
            if app == "happ" and m == mode:
                happ_default = data

    headers = header_values(domain, mode, happ_default)
    for value in headers.values():
        assert '"' not in value and "$" not in value and "\n" not in value
    write(snippet_path, nginx_snippet(headers).encode())
    return {"files": written, "snippet": snippet_path, "headers": headers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True, help="домен подписок (тот же, что в https://<домен>/s/<токен>)")
    parser.add_argument("--mode", default="ru", choices=sorted(MODES), help="какой режим отдавать вместе с подпиской (по умолчанию ru)")
    parser.add_argument("--out", default="/var/www/sub/routing")
    parser.add_argument("--snippet", default="/etc/nginx/snippets/sub-routing-headers.conf")
    parser.add_argument("--reload", action="store_true", help="после записи выполнить nginx -t и systemctl reload nginx")
    args = parser.parse_args(argv)

    try:
        result = generate(args.domain, args.mode, args.out, args.snippet)
    except ValueError as e:
        print("Ошибка:", e)
        return 1
    print(f"Профили записаны в {args.out}: {len(result['files'])} файла")
    print(f"Заголовки nginx: {result['snippet']} (режим {args.mode})")
    for name, value in result["headers"].items():
        print(f"  {name}: {value[:70]}{'...' if len(value) > 70 else ''}")

    if args.reload:
        for cmd in (["nginx", "-t"], ["systemctl", "reload", "nginx"]):
            code = subprocess.run(cmd).returncode
            if code != 0:
                print("Команда не прошла:", " ".join(cmd))
                return code
        print("nginx перезагружен.")
    else:
        print("Примените: nginx -t && systemctl reload nginx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
