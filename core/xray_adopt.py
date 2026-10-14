"""
Taking over a manually installed Xray stand with the bot: the part that needs
the panel's own modules (core.xray). Reading the stand and building the
database lives in core.manual_stand (standard library only, so it also runs
before the panel is installed).

build_plan()  = manual_stand.inspect_stand() + a simulation of what the first
                sync would do to the live Xray config (restart or not, which
                hand-made entries get re-labelled).
apply_plan()  = writes the planned xray.json under the panel's lock, after a
                backup of the old one.
"""
import os
import shutil
import time
from typing import Dict, Optional

from core import xray
from core.manual_stand import (  # noqa: F401  (re-exported for the CLI and tests)
    DEFAULT_PATHS,
    Plan,
    format_checks,
    format_plan,
    inspect_stand,
    parse_users_list,
    preflight,
    read_env_file,
    read_table,
)


def build_plan(paths: Optional[Dict[str, str]] = None, existing_store: Optional[Dict] = None,
               force: bool = False) -> Plan:
    plan = inspect_stand(paths, existing_store, force)
    if not plan.config or not plan.store.get("server"):
        return plan

    store, cfg = plan.store, plan.config
    desired = {n: c["uuid"] for n, c in store["clients"].items()
               if c.get("external") is True and c.get("enabled") is True and c.get("uuid")}
    try:
        merged, clients_changed = xray.merge_clients(cfg, desired)
        _, identity_changed = xray.apply_identity(merged, store["server"])
    except RuntimeError as e:
        plan.problems.append(str(e))
        return plan

    live = (xray._vless_inbound(cfg).get("settings") or {}).get("clients") or []
    desired_ids = set(desired.values())
    handmade = [c for c in live if not str(c.get("email", "")).startswith(xray.EMAIL_PREFIX)]
    config_ids = {c.get("id") for c in live}
    plan.effect = {
        "restart": bool(clients_changed or identity_changed),
        "relabeled": sorted(str(c.get("email")) for c in handmade if c.get("id") in desired_ids),
        "untouched": sorted(str(c.get("email") or c.get("id")) for c in handmade if c.get("id") not in desired_ids),
        "identity_changed": bool(identity_changed),
        "new_in_config": sorted(n for n, u in desired.items() if u not in config_ids),
    }
    return plan


def apply_plan(plan: Plan, backup_stamp: Optional[str] = None) -> Optional[str]:
    """Writes plan.store to xray.json under the lock. Returns the backup path (or None if there was no file)."""
    if not plan.ok:
        raise RuntimeError("в плане есть проблемы, запись отменена")
    with xray._lock():
        xray.load_store(strict=True)  # refuse to touch a corrupt file
        backup = None
        if os.path.exists(xray.XRAY_STORE_PATH):
            backup = f"{xray.XRAY_STORE_PATH}.pre-adopt-{backup_stamp or time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(xray.XRAY_STORE_PATH, backup)
            os.chmod(backup, 0o600)
        xray.save_store(plan.store)
    return backup
