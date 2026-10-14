"""
Restoring the panel's database from a backup zip (what the bot's
"🗄 База данных → 📥 Загрузить БД" does). Pure functions, no aiogram: the
critical path of a reinstall is covered by tests/test_restore.py.

Only files listed in `files` (core.paths.BACKUP_FILES) are ever touched, and
only if they are present in the zip: a zip with just xray.json restores just
xray.json and leaves users.json and everything else of a fresh install alone.
"""
import io
import json
import logging
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List

from core import xray

log = logging.getLogger(__name__)


@dataclass
class RestoreResult:
    restored: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)   # present in the zip but rejected
    found: List[str] = field(default_factory=list)     # known files that were in the zip


def validate_users_json(content: bytes) -> bool:
    """A list of dicts, each with a username (plain JSON validity would accept `{}` and wipe users)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, list):
        return False
    return all(isinstance(item, dict) and item.get("username") for item in data)


def validate_member(arcname: str, content: bytes) -> bool:
    if arcname == "users.json":
        return validate_users_json(content)
    if arcname == "xray.json":
        return xray.store_is_valid(content)
    try:
        json.loads(content)
        return True
    except json.JSONDecodeError:
        return False


def restore_backup_zip(raw: bytes, files: Dict[str, str]) -> RestoreResult:
    """Raises zipfile.BadZipFile if `raw` is not a zip."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()
    result = RestoreResult(found=[a for a in files if a in names])

    # safety copy of whatever is on disk now, in case this restore is a mistake
    for dest in files.values():
        if os.path.exists(dest):
            try:
                shutil.copy(dest, dest + ".before_restore")
            except OSError as e:
                log.warning("pre-restore backup failed for %s: %s", dest, e)

    for arcname, dest in files.items():
        if arcname not in names:
            continue
        content = zf.read(arcname)
        if not validate_member(arcname, content):
            result.invalid.append(arcname)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)
        os.chmod(dest, 0o600)
        result.restored.append(arcname)
    return result
