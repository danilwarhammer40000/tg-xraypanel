"""
Subscription publishing for the Xray backend (core/xray.py).

Instead of one raw vless:// link per user, every Xray client gets a secret
SUBSCRIPTION TOKEN. For each token this module writes a file

    <dir>/<token>            base64 of newline-separated vless:// links

(one link per CDN entry point, see "endpoints" below). The files are served
as /s/<token> by nginx on a separate subscription host and are delivered there
with rsync ("push"), so the subscription keeps working even when the VPN
server itself (its port 443 is usually taken) cannot host it.

Configuration lives in xray.json under "subscription" (so it rides along in
the bot's backup) and can be overridden by environment variables:

    {
      "domain":        "config.example.net",      # SUB_DOMAIN  (required to enable)
      "dir":           "/opt/xraypanel/data/sub-out",   # SUB_DIR  (optional)
      "push":          ["sub@host:/var/www/sub/s/"],     # SUB_PUSH (space separated)
      "endpoints": [
        {"label": "yandex",   "host": "cdn.example.net", "mode": "resolve"},
        {"label": "yandex-d", "host": "cdn.example.net", "mode": "domain"},
        {"label": "pool",     "host": "cdn.example.net", "mode": "1.2.3.4,5.6.7.8"}
      ],
      "max_ips": 2, "dns_resolvers": ["8.8.8.8", "1.1.1.1"], "ecs_subnets": [],
      "xmux": false, "uplink_get": false, "sess_key": ""
    }

Entry point modes: "domain" (the domain itself goes into the link), "resolve"
(CDN node IPs found with dig, last known IPs are reused if DNS is silent, the
domain is the last resort so an EMPTY subscription is never published) or a
fixed IPv4 / comma-separated list. Without "endpoints" a single entry built
from the server section of xray.json (edge_addr + domain) is used.

Nothing here touches the Xray config; core/xray.py decides WHO gets a
subscription (`desired`) and calls publish().
"""
import base64
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
STATE_NAME = "xray_sub_state.json"


class SubscriptionError(RuntimeError):
    """Subscription files could not be delivered to the subscription host."""


# ---------------- tokens and settings ----------------

def new_token() -> str:
    return secrets.token_hex(16)


def valid_token(token) -> bool:
    return isinstance(token, str) and bool(TOKEN_RE.match(token))


def ensure_tokens(clients: Dict[str, Dict]) -> bool:
    """Gives every client that has a UUID but no valid token one. True if changed."""
    changed = False
    for client in clients.values():
        if client.get("uuid") and not valid_token(client.get("token")):
            client["token"] = new_token()
            changed = True
    return changed


def _split(value) -> List[str]:
    if isinstance(value, str):
        return value.split()
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def sub_settings(sub: Optional[Dict], base_dir: str, environ=None) -> Dict:
    """Normalised settings: xray.json["subscription"] + env overrides."""
    environ = os.environ if environ is None else environ
    sub = sub if isinstance(sub, dict) else {}

    def env(name: str) -> str:
        return (environ.get(name) or "").strip()

    domain = env("SUB_DOMAIN") or str(sub.get("domain") or "").strip()
    directory = env("SUB_DIR") or str(sub.get("dir") or "").strip()
    push = _split(env("SUB_PUSH")) or _split(sub.get("push"))

    endpoints = []
    for ep in sub.get("endpoints") or []:
        if isinstance(ep, dict) and ep.get("label") and ep.get("host") and ep.get("mode"):
            endpoints.append({k: str(ep[k]).strip() for k in ("label", "host", "mode")})

    try:
        max_ips = max(1, int(sub.get("max_ips") or 2))
    except (TypeError, ValueError):
        max_ips = 2

    return {
        "enabled": bool(domain),
        "domain": domain,
        "dir": directory or os.path.join(base_dir, "sub-out"),
        "push": push,
        "endpoints": endpoints,
        "max_ips": max_ips,
        "dns_resolvers": _split(sub.get("dns_resolvers")) or ["8.8.8.8", "1.1.1.1"],
        "ecs_subnets": _split(sub.get("ecs_subnets")),
        "xmux": sub.get("xmux") is True,
        "uplink_get": sub.get("uplink_get") is True,
        "sess_key": str(sub.get("sess_key") or "").strip(),
    }


def subscription_url(cfg: Dict, token: str) -> str:
    return f"https://{cfg['domain']}/s/{token}"


def join_url(cfg: Dict, token: str) -> str:
    """Page with one-tap buttons (join.html on the subscription host); the token stays in the #fragment."""
    return f"https://{cfg['domain']}/join.html#{token}"


# ---------------- link building ----------------

def build_extra(server: Dict, cfg: Dict) -> Dict:
    """The client-side XHTTP "extra" block. `server` is xray.settings()."""
    extra: Dict = {
        "scMaxEachPostBytes": 65536,
        "xPaddingObfsMode": True,
        "xPaddingKey": server["padding_key"],
        "xPaddingPlacement": server["padding_placement"],
    }
    if cfg["sess_key"]:
        extra.update({"sessionPlacement": "cookie", "sessionKey": cfg["sess_key"]})
    if cfg["xmux"]:
        extra["xmux"] = {"maxConcurrency": "16-32", "hMaxRequestTimes": "600-900",
                         "hMaxReusableSecs": "900-1800", "hKeepAlivePeriod": 25}
    if cfg["uplink_get"]:
        extra.update({"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
                      "uplinkDataKey": "X-Upload-Token", "uplinkChunkSize": 4096,
                      "scMaxEachPostBytes": 16384, "scMinPostsIntervalMs": "10"})
    return extra


def make_link(xray_uuid: str, addr: str, host: str, server: Dict, extra: Dict, remark: str) -> str:
    q = lambda s: quote(str(s), safe="")  # noqa: E731
    return (
        f"vless://{xray_uuid}@{addr}:{server['port']}"
        f"?encryption={q(server['encryption'])}&security=tls&sni={host}&fp=chrome&alpn=h2"
        f"&type=xhttp&host={host}&path={q(server['path'])}&mode=packet-up"
        f"&extra={q(json.dumps(extra, separators=(',', ':')))}"
        f"#{q(remark)}"
    )


# ---------------- entry points ----------------

def _dig(host: str, resolver: str, subnet: Optional[str]) -> List[str]:
    cmd = ["dig", "+short", "+time=3", "+tries=1"]
    if subnet:
        cmd.append(f"+subnet={subnet}")
    cmd += [f"@{resolver}", "A", host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if IPV4_RE.match(ln.strip())]


def _resolve(host: str, cfg: Dict, dig: Callable) -> List[str]:
    seen: Counter = Counter()
    for resolver in cfg["dns_resolvers"]:
        for subnet in (cfg["ecs_subnets"] or [None]):
            for ip in dig(host, resolver, subnet):
                seen[ip] += 1
    return [ip for ip, _ in seen.most_common()]


def default_endpoints(server: Dict) -> List[Dict]:
    """One entry from xray.json["server"] when no endpoints are configured."""
    return [{"label": "", "host": server["domain"], "mode": "literal:" + str(server["edge_addr"])}]


def resolve_endpoints(endpoints: List[Dict], cfg: Dict, state: Dict,
                      dig: Callable = _dig) -> Dict[str, List[Tuple[str, str]]]:
    """
    label -> [(remark suffix, address)]. Updates `state` (last known IPs per
    host) in place; the caller persists it.
    """
    addrs: Dict[str, List[Tuple[str, str]]] = {}
    for ep in endpoints:
        label, host, mode = ep["label"], ep["host"], ep["mode"]
        if mode.startswith("literal:"):
            addrs[label] = [("", mode[len("literal:"):])]
        elif mode == "domain":
            addrs[label] = [("dom", host)]
        elif mode == "resolve":
            ips = _resolve(host, cfg, dig)[:cfg["max_ips"]]
            if ips:
                state[host] = ips
            else:
                ips = list(state.get(host) or [])
                log.warning("subscription: DNS gave nothing for %s, using last known %s", host, ips)
            addrs[label] = [(f"ip{i + 1}", ip) for i, ip in enumerate(ips)] or [("dom", host)]
        elif all(IPV4_RE.match(x) for x in mode.split(",")):
            addrs[label] = [(f"ip{i + 1}", ip) for i, ip in enumerate(mode.split(","))]
        else:
            log.warning("subscription: unknown endpoint mode %r (label %s), skipped", mode, label)
    return addrs


def build_links(xray_uuid: str, endpoints: List[Dict], addrs: Dict, server: Dict, extra: Dict) -> List[str]:
    links = []
    for ep in endpoints:
        for suffix, addr in addrs.get(ep["label"], []):
            remark = "-".join(p for p in (ep["label"], suffix) if p) or "xray"
            links.append(make_link(xray_uuid, addr, ep["host"], server, extra, remark))
    return links


def encode_body(links: List[str]) -> str:
    return base64.b64encode("\n".join(links).encode()).decode()


# ---------------- files ----------------

def _write_atomic(path: str, text: str, mode: int) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_state(path: str) -> Dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _push(cfg: Dict, run: Callable) -> List[str]:
    errors = []
    for target in cfg["push"]:
        try:
            result = run(
                ["rsync", "-a", "--delete", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
                 cfg["dir"].rstrip("/") + "/", target],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                errors.append(f"{target}: rsync код {result.returncode} {(result.stderr or '').strip()[-200:]}")
        except Exception as e:  # missing rsync, timeout, ...
            errors.append(f"{target}: {e}")
    return errors


def publish(clients: Dict[str, Dict], desired: Dict[str, str], server: Dict, sub: Optional[Dict],
            base_dir: str, *, dig: Callable = _dig, run: Callable = subprocess.run,
            environ=None) -> Dict:
    """
    Writes one file per client in `desired` (username -> uuid), deletes files
    of everyone else, then pushes the folder to the subscription host(s).

    Returns {"enabled", "written", "removed", "kept"}. Raises SubscriptionError
    when files were written locally but a push failed.
    """
    cfg = sub_settings(sub, base_dir, environ)
    if not cfg["enabled"]:
        return {"enabled": False, "written": 0, "removed": 0, "kept": 0}

    endpoints = cfg["endpoints"] or default_endpoints(server)
    state_path = os.path.join(base_dir, STATE_NAME)
    state = _load_state(state_path)
    addrs = resolve_endpoints(endpoints, cfg, state, dig)
    extra = build_extra(server, cfg)

    os.makedirs(cfg["dir"], exist_ok=True)
    os.chmod(cfg["dir"], 0o755)

    keep, written = set(), 0
    for username, xray_uuid in desired.items():
        token = (clients.get(username) or {}).get("token")
        if not valid_token(token):
            log.warning("subscription: %s has no valid token, skipped", username)
            continue
        links = build_links(xray_uuid, endpoints, addrs, server, extra)
        if not links:
            continue  # never publish an empty subscription
        body = encode_body(links)
        path = os.path.join(cfg["dir"], token)
        try:
            with open(path) as f:
                same = f.read() == body
        except OSError:
            same = False
        if not same:
            _write_atomic(path, body, 0o644)
            written += 1
        keep.add(token)

    removed = 0
    for name in os.listdir(cfg["dir"]):
        if valid_token(name) and name not in keep:
            os.remove(os.path.join(cfg["dir"], name))
            removed += 1

    _write_atomic(state_path, json.dumps(state), 0o600)

    errors = _push(cfg, run)
    if errors:
        raise SubscriptionError("подписки не доставлены на хост: " + "; ".join(errors))
    log.info("subscription: %d written, %d removed, %d kept", written, removed, len(keep))
    return {"enabled": True, "written": written, "removed": removed, "kept": len(keep)}
