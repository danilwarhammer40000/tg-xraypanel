"""Builds a throwaway copy of a manually installed Xray stand (the guide's file layout)."""
import json
import os

DEC = "mlkem768x25519plus.native.600s.PRIVATEKEY"
ENC = "mlkem768x25519plus.native.0rtt.PUBLICKEY"
XPATH = "/static/js/chunk-vendors.86011696.js"
PADKEY = "k5e9394"

U_MAIN = "11111111-1111-4111-8111-111111111111"
U_PROBE = "99999999-9999-4999-8999-999999999999"
U_ANNA = "22222222-2222-4222-8222-222222222222"
T_MAIN = "a" * 32
T_PROBE = "9" * 32
T_ANNA = "b" * 32

DEFAULT_USERS = [("main", U_MAIN, T_MAIN), ("_probe", U_PROBE, T_PROBE), ("anna", U_ANNA, T_ANNA)]


def vless_inbound(users, path=XPATH, padkey=PADKEY, dec=DEC):
    return {
        "listen": "127.0.0.1", "port": 10085, "protocol": "vless",
        "settings": {"clients": [{"id": uid, "email": name} for name, uid, _ in users], "decryption": dec},
        "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": {
            "path": path, "mode": "packet-up", "xPaddingObfsMode": True, "xPaddingKey": padkey,
            "xPaddingPlacement": "cookie", "noSSEHeader": True, "uplinkDataPlacement": "header",
            "uplinkDataKey": "X-Upload-Token", "serverMaxHeaderBytes": 32768, "scMaxBufferedPosts": 100}},
    }


def make_stand(root, users=None, *, push="", sub_domain="config.test", env_path=XPATH, env_padkey=PADKEY,
               config_path=XPATH, config_padkey=PADKEY, dec=DEC, with_stats=False, endpoints=None):
    """Returns {"paths": {...}, "sub_dir": ...}. Files are written under `root`."""
    users = DEFAULT_USERS if users is None else users
    root = str(root)
    os.makedirs(root, exist_ok=True)
    www = os.path.join(root, "www")
    paths = {k: os.path.join(root, n) for k, n in
             (("config", "config.json"), ("env", "client-params.env"),
              ("endpoints", "endpoints.list"), ("users", "users.list"))}

    inbounds = [vless_inbound(users, config_path, config_padkey, dec)]
    cfg = {"log": {"loglevel": "warning"}, "inbounds": inbounds, "outbounds": [{"protocol": "freedom"}]}
    if with_stats:  # what the stats step adds: an extra dokodemo-door inbound, a policy and a routing rule
        inbounds.append({"tag": "api-in", "listen": "127.0.0.1", "port": 10086, "protocol": "dokodemo-door",
                         "settings": {"address": "127.0.0.1"}})
        cfg.update({"stats": {}, "api": {"tag": "api", "services": ["StatsService"]},
                    "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
                    "routing": {"rules": [{"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"}]}})
    with open(paths["config"], "w") as f:
        json.dump(cfg, f)

    with open(paths["env"], "w") as f:
        f.write(f"ENC={ENC}\nXPATH={env_path}\nPADKEY={env_padkey}\nSESSKEY=\nORIGIN_TOKEN=secret-origin-token\n"
                f"SUB_DOMAIN={sub_domain}\nSUB_DIR={www}\nSUB_PUSH={push}\nXMUX=0\nUPLINK_GET=1\n"
                'DNS_RESOLVERS="8.8.8.8 1.1.1.1"\nECS_SUBNETS=""\nMAX_IPS=2\n')
    with open(paths["endpoints"], "w") as f:
        f.write("# метка   домен-туннеля   режим\n")
        f.write("yandex-d  cdn.test  domain\n" if endpoints is None else endpoints)
    with open(paths["users"], "w") as f:
        f.write("".join(f"{name} {uid} {token}\n" for name, uid, token in users))
    return {"paths": paths, "sub_dir": os.path.join(www, "s"), "www": www}
