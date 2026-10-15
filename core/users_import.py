"""
Moving people who exist only as "external" Xray clients (taken over from a
manual stand, see core/xray_adopt.py) into the panel's user database, so the
panel can manage them: followers linked to their leader and paid terms set.

Workflow (python -m core.xray_cli):

    users-template > /root/users-plan.csv     # a table of everyone, leaders guessed from "-2", "-3" names
    (fill in expires_at for every leader, check the leader column)
    import-users /root/users-plan.csv         # dry run: what would happen, nothing is written
    import-users /root/users-plan.csv --apply

Plan file (CSV, "#" lines are comments):

    username,expires_at,leader,password,rate
    anna,2026-12-01,,,
    anna-2,,anna,,
    boss,inf,,,

  username    must already exist in xray.json: UUID and subscription token are kept,
              so every link already handed out keeps working
  expires_at  YYYY-MM-DD or DD.MM.YYYY, or "inf" for unlimited. Required for leaders;
              a follower always takes the leader's date and status
  leader      empty for a leader, otherwise the leader's username
  password    empty = generated (leaders); followers share the leader's password
  rate        what the leader's current paid term was paid at, rub/month; empty = the
              current price from core.payment (so nobody is asked to top up right after the move)

After the import the people stop being "external": status and expiry from users.json
decide their access, exactly as for anyone else.
"""
import csv
import io
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from core import xray
from core.dates import parse_expiry, utcnow_naive

HEADER = ["username", "expires_at", "leader", "password", "rate"]
UNLIMITED = {"inf", "infinite", "unlimited", "∞", "-", "безлимит", "бессрочно"}
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (0/o, 1/l/i)
_FOLLOWER_NAME = re.compile(r"^(.+)-(\d+)$")


@dataclass
class Row:
    username: str
    expires_at: Optional[str] = None     # ISO date or None = unlimited
    has_expiry: bool = False             # False: the column was empty
    leader: Optional[str] = None
    password: Optional[str] = None
    rate: Optional[int] = None


@dataclass
class ImportPlan:
    rows: List[Row] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ---------------- template ----------------

def infer_leader(name: str, names) -> Optional[str]:
    m = _FOLLOWER_NAME.match(name)
    return m.group(1) if m and m.group(1) in names else None


def _sort_key(name: str, names):
    leader = infer_leader(name, names) or name
    m = _FOLLOWER_NAME.match(name)
    return (leader.lower(), int(m.group(2)) if infer_leader(name, names) else 0)


def build_template(clients: Dict[str, Dict], db_usernames) -> str:
    """CSV with every external Xray client that is not in the panel database yet."""
    pending = sorted((n for n, c in clients.items() if c.get("external") is True and n not in db_usernames),
                     key=lambda n: _sort_key(n, set(clients)))
    names = set(pending)
    out = io.StringIO()
    out.write("# Заполните expires_at у каждого ведущего: ГГГГ-ММ-ДД, ДД.ММ.ГГГГ или inf (безлимит).\n")
    out.write("# Колонка leader угадана по именам вида имя-2, имя-3: проверьте её.\n")
    out.write("# У ведомых дату оставьте пустой: они берут дату и статус у ведущего.\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER)
    for name in pending:
        writer.writerow([name, "", infer_leader(name, names) or "", "", ""])
    return out.getvalue()


# ---------------- parsing ----------------

def parse_date(value: str) -> Tuple[bool, Optional[str]]:
    """(ok, iso). ok=False on garbage; iso=None means unlimited."""
    value = value.strip()
    if value.lower() in UNLIMITED:
        return True, None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return True, datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return False, None


def parse_plan(text: str) -> Tuple[List[Row], List[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    errors: List[str] = []
    if not lines:
        return [], ["файл пуст"]
    reader = csv.reader(lines, skipinitialspace=True)
    header = [h.strip().lower() for h in next(reader)]
    if "username" not in header:
        return [], [f"первая строка должна быть заголовком: {','.join(HEADER)}"]
    col = {name: header.index(name) for name in HEADER if name in header}

    def cell(record, name):
        i = col.get(name)
        return record[i].strip() if i is not None and i < len(record) else ""

    rows = []
    for record in reader:
        name = cell(record, "username")
        if not name:
            continue
        row = Row(username=name, leader=cell(record, "leader") or None, password=cell(record, "password") or None)
        raw_date = cell(record, "expires_at")
        if raw_date:
            ok, iso = parse_date(raw_date)
            if not ok:
                errors.append(f"{name}: непонятная дата {raw_date!r} (ГГГГ-ММ-ДД, ДД.ММ.ГГГГ или inf)")
                continue
            row.expires_at, row.has_expiry = iso, True
        raw_rate = cell(record, "rate")
        if raw_rate:
            if not raw_rate.isdigit() or not 1 <= int(raw_rate) <= 100000:
                errors.append(f"{name}: rate должен быть целым числом рублей, а не {raw_rate!r}")
                continue
            row.rate = int(raw_rate)
        rows.append(row)
    return rows, errors


# ---------------- planning ----------------

def plan_import(rows: List[Row], clients: Dict[str, Dict], db_usernames, today: Optional[date] = None,
                allow_expired: bool = False) -> ImportPlan:
    plan = ImportPlan(rows=rows)
    today = today or utcnow_naive().date()
    by_name: Dict[str, Row] = {}

    for row in rows:
        if row.username in by_name:
            plan.problems.append(f"{row.username}: имя повторяется в файле")
        by_name[row.username] = row

    for row in rows:
        name = row.username
        if name not in clients:
            plan.problems.append(f"{name}: нет в xray.json. Проверьте имя (клиента нужно сначала завести: adopt или бот)")
        elif clients[name].get("external") is not True and name not in db_usernames:
            plan.warnings.append(f"{name}: уже управляется панелью (не внешний клиент), запись в базе будет создана")

        if name in db_usernames:
            plan.info.append(f"{name}: уже есть в базе, будет пропущен")
            continue

        if row.leader:
            if row.leader == name:
                plan.problems.append(f"{name}: не может быть ведомым самого себя")
            elif row.leader not in by_name and row.leader not in db_usernames:
                plan.problems.append(f"{name}: ведущего {row.leader!r} нет ни в файле, ни в базе")
            elif row.leader in by_name and by_name[row.leader].leader:
                plan.problems.append(f"{name}: ведущий {row.leader} сам ведомый (цепочки не поддерживаются)")
            if row.has_expiry:
                plan.warnings.append(f"{name}: дата ведомого игнорируется, берётся дата ведущего")
            if row.password:
                plan.warnings.append(f"{name}: пароль ведомого игнорируется, берётся пароль ведущего")
            if row.rate:
                plan.warnings.append(f"{name}: rate задаётся у ведущего, у ведомого он игнорируется")
            continue

        if not row.has_expiry:
            plan.problems.append(f"{name}: не указан expires_at (дата или inf)")
        elif row.expires_at and parse_expiry(row.expires_at).date() < today:
            message = f"{name}: срок {row.expires_at} уже прошёл, доступ будет отключён"
            (plan.warnings if allow_expired else plan.problems).append(
                message + ("" if allow_expired else " (добавьте --allow-expired, если так и нужно)"))

    leaders = [r for r in rows if not r.leader and r.username not in db_usernames]
    followers = [r for r in rows if r.leader and r.username not in db_usernames]
    plan.info.append(f"будет создано: ведущих {len(leaders)}, ведомых {len(followers)}")
    return plan


def format_plan(plan: ImportPlan) -> str:
    lines = [f"  • {t}" for t in plan.info]
    lines += [f"⚠️  {t}" for t in plan.warnings]
    lines += [f"❌ {t}" for t in plan.problems]
    followers: Dict[str, List[str]] = {}
    for r in plan.rows:
        if r.leader:
            followers.setdefault(r.leader, []).append(r.username)
    for r in plan.rows:
        if not r.leader:
            until = "безлимит" if r.has_expiry and r.expires_at is None else (r.expires_at or "?")
            tail = f", ведомые: {', '.join(followers[r.username])}" if r.username in followers else ""
            lines.append(f"  {r.username}: до {until}{tail}")
    return "\n".join(lines)


# ---------------- applying ----------------

def generate_password(length: int = 8) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def apply_import(plan: ImportPlan) -> Dict:
    """Writes the users into users.json and marks them as panel-managed in xray.json. Returns a summary."""
    if not plan.ok:
        raise RuntimeError("в плане есть проблемы, запись отменена")

    from core import db
    from core.payment import calc_monthly_price

    existing = {u.get("username") for u in db.list_users()}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = None
    if os.path.exists(db.DB_PATH):
        backup = f"{db.DB_PATH}.pre-import-{stamp}"
        shutil.copy2(db.DB_PATH, backup)
        os.chmod(backup, 0o600)

    today = utcnow_naive().strftime("%Y-%m-%d")
    passwords: Dict[str, str] = {}
    created_leaders, created_followers, linked = [], [], []

    def record(name, password, expires_at):
        return {"username": name, "password": password, "created_at": today, "expires_at": expires_at,
                "status": "active", "telegram_id": None, "notified_days": [], "post_disable_notified": [],
                "pending_request": None}

    for row in (r for r in plan.rows if not r.leader and r.username not in existing):
        password = row.password or generate_password()
        db.add_user(record(row.username, password, row.expires_at))
        passwords[row.username] = password
        created_leaders.append(row.username)

    for row in (r for r in plan.rows if r.leader and r.username not in existing):
        leader = db.get_user(row.leader)
        db.add_user(record(row.username, leader.get("password"), leader.get("expires_at")))
        db.link_user(row.username, row.leader)
        created_followers.append(row.username)
        linked.append((row.username, row.leader))

    # The current paid term counts as paid at today's price, so nobody is asked to top up the moment the move is done.
    for row in (r for r in plan.rows if not r.leader and r.username in created_leaders):
        price, _ = calc_monthly_price(row.username)
        db.update_user(row.username, last_renewal_rate=row.rate or price)

    imported = created_leaders + created_followers

    def go(clients):
        for name in imported:
            if name in clients:
                clients[name].pop("external", None)
        return {}

    xray._mutate("import-users", go)
    return {"leaders": created_leaders, "followers": created_followers, "linked": linked,
            "passwords": passwords, "backup": backup}
