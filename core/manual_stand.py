"""
Reading a MANUALLY installed Xray stand (the "guide" layout) and turning it
into the panel's database. Standard library only on purpose: it must run on
the old host BEFORE the panel (and its dependencies) is installed.

A hand-made install keeps its state in four places:

    /usr/local/etc/xray/config.json   server inbound: decryption key, XHTTP path/padding, clients
    /root/client-params.env           ENC (client key), XPATH, PADKEY, SESSKEY, XMUX, UPLINK_GET, SUB_*
    /root/endpoints.list              CDN entry points: label  host  mode
    /root/users.list                  people: name  uuid  token

inspect_stand() reads them and prepares the panel's xray.json WITHOUT writing
anything: the server section (so the live key keeps working), the
"subscription" section, and every person as an "external" client with the SAME
UUID and token, so every URL already handed out keeps working after the move.
It lists problems that must be fixed first (for example a path in
client-params.env that differs from the one the server really uses).

build_backup_zip() packs the result in the format the bot's
"🗄 База данных → 📥 Загрузить БД" accepts, so the database can be restored
through the bot after the panel is (re)installed.

preflight() checks the host for things that would conflict or fail.

Command line: tools/export_manual_stand.py (works without installing anything)
or `python -m core.xray_cli preflight|adopt|export-db`.
"""
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import uuid as uuidlib
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core import subscription as sub

STORE_VERSION = 1
BACKUP_NAME = "xray.json"   # the name the bot's restore flow looks for (core.paths.BACKUP_FILES)

DEFAULT_PATHS = {
    "config": "/usr/local/etc/xray/config.json",
    "env": "/root/client-params.env",
    "endpoints": "/root/endpoints.list",
    "users": "/root/users.list",
}


def empty_store() -> Dict:
    return {"version": STORE_VERSION, "server": {}, "subscription": {}, "clients": {}}


# ---------------- readers ----------------

def read_env_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_table(path: str) -> List[List[str]]:
    rows: List[List[str]] = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    rows.append(line.split())
    return rows


def parse_users_list(text: str):
    """Yields (name, uuid, token) for valid lines, (name, None, reason) for bad ones. "_names" are skipped."""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        name = parts[0]
        if name.startswith("_"):
            continue
        if len(parts) < 3:
            yield name, None, "нужно три колонки: имя uuid токен"
            continue
        try:
            uid = str(uuidlib.UUID(parts[1]))
        except ValueError:
            yield name, None, "UUID некорректный"
            continue
        if not sub.valid_token(parts[2]):
            yield name, None, "токен должен быть 32 символа 0-9a-f"
            continue
        yield name, uid, parts[2]


def vless_inbound(cfg: Dict) -> Dict:
    for inbound in cfg.get("inbounds") or []:
        if isinstance(inbound, dict) and inbound.get("protocol") == "vless":
            return inbound
    raise RuntimeError("в конфиге Xray нет VLESS-входа")


# ---------------- the plan ----------------

@dataclass
class Plan:
    store: Dict = field(default_factory=empty_store)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    effect: Dict = field(default_factory=dict)
    config: Dict = field(default_factory=dict)   # the live Xray config that was read

    @property
    def ok(self) -> bool:
        return not self.problems


def _one(value: Optional[str]) -> bool:
    return (value or "").strip() == "1"


def inspect_stand(paths: Optional[Dict[str, str]] = None, existing_store: Optional[Dict] = None,
                  force: bool = False) -> Plan:
    paths = {**DEFAULT_PATHS, **(paths or {})}
    plan = Plan()
    store = json.loads(json.dumps(existing_store if existing_store is not None else empty_store()))
    plan.store = store

    # --- server config.json ---
    try:
        with open(paths["config"]) as f:
            cfg = json.load(f)
        inbound = vless_inbound(cfg)
    except (OSError, json.JSONDecodeError, RuntimeError) as e:
        plan.problems.append(f"не удалось прочитать VLESS-вход из {paths['config']}: {e}")
        return plan
    plan.config = cfg

    dec = (inbound.get("settings") or {}).get("decryption") or ""
    xh = (inbound.get("streamSettings") or {}).get("xhttpSettings") or {}
    if not dec or dec == "none":
        plan.problems.append("в конфиге Xray нет decryption (VLESS-шифрования): панель работает только с ним")

    # --- client-params.env ---
    try:
        env = read_env_file(paths["env"])
    except OSError as e:
        plan.problems.append(f"не удалось прочитать {paths['env']}: {e}")
        return plan

    enc, xpath, padkey = env.get("ENC", ""), env.get("XPATH", ""), env.get("PADKEY", "")
    for name, value in (("ENC", enc), ("XPATH", xpath), ("PADKEY", padkey)):
        if not value:
            plan.problems.append(f"в {paths['env']} не задан {name}")

    if xpath and xh.get("path") != xpath:
        plan.problems.append(f"path не совпадает: в client-params.env {xpath!r}, в конфиге Xray {xh.get('path')!r}")
    if padkey and xh.get("xPaddingKey") != padkey:
        plan.problems.append(f"ключ padding не совпадает: в client-params.env {padkey!r}, в конфиге Xray {xh.get('xPaddingKey')!r}")
    placement = xh.get("xPaddingPlacement") or "cookie"
    if placement != "cookie":
        plan.warnings.append(f"xPaddingPlacement в конфиге {placement!r}: ссылки подписки используют это же значение")
    if xh.get("mode") not in (None, "packet-up"):
        plan.warnings.append(f"mode в конфиге {xh.get('mode')!r}, а панель и ссылки используют packet-up")
    if enc and dec and enc.split(".")[:2] != dec.split(".")[:2]:
        plan.warnings.append("ENC и decryption выглядят как пара из разных вариантов vlessenc (не совпадают первые две части)")

    # --- endpoints.list ---
    endpoints, hosts = [], []
    for row in read_table(paths["endpoints"]):
        if len(row) < 3:
            plan.warnings.append(f"строка endpoints.list пропущена (нужно 3 колонки): {' '.join(row)}")
            continue
        endpoints.append({"label": row[0], "host": row[1], "mode": row[2]})
        hosts.append(row[1])
    if not endpoints:
        plan.problems.append(f"{paths['endpoints']} пуст или не найден: нужен хотя бы один вход CDN")
    domain = hosts[0] if hosts else ""
    if len(set(hosts)) > 1:
        plan.warnings.append(f"в endpoints.list несколько доменов {sorted(set(hosts))}: в server.domain попадёт первый ({domain})")

    # --- subscription ---
    sub_domain = env.get("SUB_DOMAIN", "")
    if not sub_domain:
        plan.problems.append("в client-params.env пустой SUB_DOMAIN: без него нечего выдавать людям")
    push = env.get("SUB_PUSH", "").split()
    subscription: Dict = {
        "domain": sub_domain,
        "endpoints": endpoints,
        "xmux": _one(env.get("XMUX")),
        "uplink_get": _one(env.get("UPLINK_GET")),
        "sess_key": env.get("SESSKEY", ""),
    }
    try:
        subscription["max_ips"] = max(1, int(env.get("MAX_IPS") or 2))
    except ValueError:
        subscription["max_ips"] = 2
    if env.get("DNS_RESOLVERS"):
        subscription["dns_resolvers"] = env["DNS_RESOLVERS"].split()
    if env.get("ECS_SUBNETS"):
        subscription["ecs_subnets"] = env["ECS_SUBNETS"].split()
    if push:
        subscription["push"] = push
        plan.info.append(f"подписки уйдут на хост по rsync: {' '.join(push)}")
    else:
        directory = (env.get("SUB_DIR") or "").rstrip("/")
        if not directory:
            plan.problems.append("нет ни SUB_PUSH, ни SUB_DIR: непонятно, куда писать файлы подписок")
        else:
            subscription["dir"] = directory + "/s"
            plan.info.append(f"хост подписок на этом же сервере: файлы пишутся прямо в {directory}/s")
            if directory == "/root/sub-out":
                plan.warnings.append("SUB_DIR=/root/sub-out: nginx этот каталог не отдаёт; нужен каталог из root вашего nginx (например /var/www/sub)")

    # --- store: server + subscription ---
    new_server = {"decryption": dec, "encryption": enc, "domain": domain, "edge_addr": domain,
                  "port": 443, "path": xpath, "padding_key": padkey, "padding_placement": placement}
    for section, new in (("server", new_server), ("subscription", subscription)):
        current = store.get(section) or {}
        if current and current != new and not force:
            plan.problems.append(f'в xray.json уже есть секция "{section}" и она отличается: проверьте или запустите с --force')
        else:
            store[section] = new

    # --- people ---
    try:
        with open(paths["users"]) as f:
            rows = list(parse_users_list(f.read()))
    except OSError as e:
        plan.problems.append(f"не удалось прочитать {paths['users']}: {e}")
        rows = []

    seen = {"имя": set(), "UUID": set(), "токен": set()}
    added, kept = [], []
    for name, uid, token in rows:
        if uid is None:
            plan.problems.append(f"users.list, {name}: {token}")
            continue
        for kind, value in (("имя", name), ("UUID", uid), ("токен", token)):
            if value in seen[kind]:
                plan.problems.append(f"users.list: повторяется {kind} ({name})")
            seen[kind].add(value)
        if name in store["clients"]:
            kept.append(name)
        else:
            store["clients"][name] = {"uuid": uid, "token": token, "enabled": True, "external": True}
            added.append(name)
    plan.info.append(f"люди: добавлено {len(added)} {added}; уже были в xray.json {len(kept)} {kept}")
    return plan


def format_plan(plan: Plan) -> str:
    lines = [f"  • {text}" for text in plan.info]
    e = plan.effect
    if e:
        lines.append("Первая синхронизация сделает с живым конфигом Xray:")
        lines.append(f"  • перезапуск: {'ДА (соединения оборвутся примерно на секунду)' if e['restart'] else 'нет'}")
        if e["relabeled"]:
            lines.append(f"  • те же UUID получат метку tp:<имя>, ручные копии заменятся: {e['relabeled']}")
        if e["new_in_config"]:
            lines.append(f"  • UUID из users.list, которых нет в конфиге, будут добавлены: {e['new_in_config']}")
        if e["untouched"]:
            lines.append(f"  • не затрагиваются (служебные и ручные записи): {e['untouched']}")
        if e["identity_changed"]:
            lines.append("  • параметры входа (path/padding) будут приведены к xray.json")
    lines += [f"⚠️  {text}" for text in plan.warnings]
    lines += [f"❌ {text}" for text in plan.problems]
    return "\n".join(lines)


# ---------------- export ----------------

def store_json(store: Dict) -> bytes:
    return (json.dumps(store, indent=2, ensure_ascii=False) + "\n").encode()


def build_backup_zip(store: Dict) -> bytes:
    """
    A zip the bot accepts under "🗄 База данных → 📥 Загрузить БД". It holds
    ONLY xray.json: the restore flow skips files that are not in the archive,
    so users.json and the other panel files of a fresh install stay untouched.
    The file contains the server's private VLESS key: keep it private.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(BACKUP_NAME, store_json(store))
    return buf.getvalue()


def build_originals_tar(paths: Dict[str, str]) -> bytes:
    """tar.gz with the four original files of the manual stand: a safety net, not read by the bot."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for key in ("config", "env", "endpoints", "users"):
            path = paths[key]
            if os.path.exists(path):
                tar.add(path, arcname=os.path.basename(path))
    return buf.getvalue()


def write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def export_bundle(plan: Plan, paths: Dict[str, str], out_dir: str, stamp: Optional[str] = None) -> Dict[str, str]:
    """Writes the restorable zip and the originals tarball into out_dir (mode 0600). Returns their paths."""
    if not plan.ok:
        raise RuntimeError("в плане есть проблемы, выгрузка отменена")
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"xraypanel_backup_{stamp}.zip")
    tar_path = os.path.join(out_dir, f"manual_stand_originals_{stamp}.tar.gz")
    write_private(zip_path, build_backup_zip(plan.store))
    write_private(tar_path, build_originals_tar({**DEFAULT_PATHS, **paths}))
    return {"zip": zip_path, "originals": tar_path}


# ---------------- preflight ----------------

Check = Tuple[str, str]  # (level: OK | WARN | FAIL | INFO, text)


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _grep_files(pattern: str, files: List[str]) -> List[str]:
    hits = []
    for path in files:
        try:
            with open(path) as f:
                if any(pattern in line and not line.lstrip().startswith("#") for line in f):
                    hits.append(path)
        except OSError:
            pass
    return hits


def _cron_d() -> List[str]:
    try:
        return [os.path.join("/etc/cron.d", n) for n in os.listdir("/etc/cron.d")]
    except OSError:
        return []


def preflight(paths: Optional[Dict[str, str]] = None, run: Callable = _run,
              which: Callable = shutil.which, install_dir: str = "/opt/xraypanel",
              cron_files: Optional[List[str]] = None) -> List[Check]:
    paths = {**DEFAULT_PATHS, **(paths or {})}
    out: List[Check] = []

    binary = os.getenv("XRAY_BIN", "/usr/local/bin/xray")
    if which(binary) or os.path.exists(binary):
        rc, text = run([binary, "run", "-test", "-config", paths["config"]])
        out.append(("OK", "конфиг Xray проходит проверку") if rc == 0
                   else ("FAIL", f"xray run -test не прошёл: {text[-200:]}"))
    else:
        out.append(("FAIL", f"не найден бинарник Xray ({binary})"))

    for unit in ("xray", "nginx"):
        rc, text = run(["systemctl", "is-active", unit])
        out.append(("OK", f"{unit} работает") if text.strip() == "active"
                   else ("FAIL", f"{unit} не активен ({text.strip() or rc})"))
    rc, text = run(["nginx", "-t"])
    out.append(("OK", "nginx -t без ошибок") if rc == 0 else ("FAIL", f"nginx -t: {text[-200:]}"))

    for label, key in (("client-params.env", "env"), ("users.list", "users"), ("endpoints.list", "endpoints")):
        out.append(("OK", f"{label} найден") if os.path.exists(paths[key]) else ("FAIL", f"нет файла {paths[key]}"))

    env: Dict[str, str] = {}
    try:
        env = read_env_file(paths["env"])
    except OSError:
        pass
    modes = [r[2] for r in read_table(paths["endpoints"]) if len(r) >= 3]
    if "resolve" in modes:
        out.append(("OK", "dig найден") if which("dig") else ("FAIL", "нужен dig для режима resolve: apt install dnsutils"))
    if env.get("SUB_PUSH", "").strip():
        out.append(("OK", "rsync найден") if which("rsync") else ("FAIL", "нужен rsync для SUB_PUSH: apt install rsync"))

    # old automations that would fight the bot over the same files
    rc, timers = run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    if any("gen-sub" in ln for ln in timers.splitlines()):
        out.append(("WARN", "работает таймер gen-sub: он удалит файлы подписок, созданные ботом. "
                            "Отключите: systemctl disable --now gen-sub.timer"))
    else:
        out.append(("OK", "таймера gen-sub нет"))
    cron = _grep_files("gen-sub.py", cron_files if cron_files is not None
                       else ["/etc/crontab", "/var/spool/cron/crontabs/root"] + _cron_d())
    if cron:
        out.append(("WARN", f"gen-sub.py есть в cron ({', '.join(cron)}): уберите, иначе он будет перетирать подписки"))
    out.append(("INFO", "не запускайте xray-users.sh после перехода на бота: он перезаписывает список клиентов целиком"))


    out.append(("OK", f"панель установлена ({install_dir})") if os.path.isdir(install_dir)
               else ("INFO", f"{install_dir} ещё нет: сначала выгрузите базу, затем установите панель (install.sh)"))
    return out


def format_checks(checks: List[Check]) -> str:
    icons = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}
    return "\n".join(f"{icons[level]} {text}" for level, text in checks)
