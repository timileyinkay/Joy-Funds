"""
Joy Funds - backend server
Automated fund doubling within 4 days of accepted funding.

Run:
    python app.py

Environment (optional):
    JOYFUNDS_ADMIN_EMAIL       (default: admin@joyfunds.com)
    JOYFUNDS_ADMIN_PASSWORD    (default: ChangeMe-Admin-123!)
    JOYFUNDS_PORT              (default: 8000)
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")

ADMIN_EMAIL = os.environ.get("JOYFUNDS_ADMIN_EMAIL", "admin@joyfunds.com")
ADMIN_PASSWORD = os.environ.get("JOYFUNDS_ADMIN_PASSWORD", "ChangeMe-Admin-123!")
PORT = int(os.environ.get("JOYFUNDS_PORT") or os.environ.get("PORT", "8000"))

# Doubling settings
DOUBLING_DELAY_SECONDS = 4 * 24 * 60 * 60   # 4 days
NEW_DEPOSIT_COOLDOWN_SECONDS = 5 * 24 * 60 * 60  # 5 days between deposit requests
# Progressive growth ladder:
# ₦500 -> ₦2,000
# ₦2,000 -> ₦5,000
# ₦5,000 -> ₦20,000
# ₦20,000 -> ₦90,000
# Larger deposits continue on the same upward path.
DOUBLING_MULTIPLIER = 5

# Deposit account shown to members — hardcoded, no env vars required.
DEPOSIT_BANK = "PalmPay"
DEPOSIT_ACCOUNT_NAME = "OLUWATOBILOBA SHERIFFDEEN KEHINDE OFFICAIL"
DEPOSIT_ACCOUNT_NUMBER = "9073277430"

# Session cookie names
USER_COOKIE = "joyfunds_session"
ADMIN_COOKIE = "joyfunds_admin"

# In-memory sessions
SESSION_LOCK = threading.Lock()
USER_SESSIONS = {}    # token -> email
ADMIN_SESSIONS = {}   # token -> True

# Static files this server will serve
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/calendar.html": ("calendar.html", "text/html; charset=utf-8"),
    "/dashboard.html": ("dashboard.html", "text/html; charset=utf-8"),
    "/investment.html": ("investment.html", "text/html; charset=utf-8"),
    "/withdrawal.html": ("withdrawal.html", "text/html; charset=utf-8"),
    "/login.html": ("login.html", "text/html; charset=utf-8"),
    "/signup.html": ("signup.html", "text/html; charset=utf-8"),
    "/admin.html": ("admin.html", "text/html; charset=utf-8"),
    "/admin-login.html": ("admin-login.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/script.js": ("script.js", "application/javascript; charset=utf-8"),
}


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256)
# ---------------------------------------------------------------------------

PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return hmac.compare_digest(check, digest)


def normalize_growth_status(status):
    """Convert older growth labels to the current 4-day language."""
    status_text = str(status or "")
    if status_text.lower().startswith("pending doubling"):
        return "Pending 4-day growth"
    return status_text


def migrate_user_passwords():
    """Ensure all users have the required fields and hashed passwords."""
    users = load_users()
    changed = False
    for email, user in users.items():
        if "balance_ngn" not in user:
            user["balance_ngn"] = 0
            changed = True
        if "investments" not in user:
            user["investments"] = []
            changed = True
        if "withdrawals" not in user:
            user["withdrawals"] = []
            changed = True
        if "ledger" not in user:
            user["ledger"] = []
            changed = True
        for inv in user.get("investments", []):
            old_status = inv.get("status")
            if old_status and str(old_status).lower().startswith("pending doubling"):
                inv["status"] = "Pending 4-day growth"
                changed = True
        pwd = user.get("password", "")
        if pwd and not pwd.startswith("pbkdf2_sha256$"):
            user["password"] = hash_password(pwd)
            changed = True
        if not user.get("created_at"):
            user["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            changed = True
    if changed:
        save_users(users)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_user_session(email: str) -> str:
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        USER_SESSIONS[token] = email
    return token


def create_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        ADMIN_SESSIONS[token] = True
    return token


def get_session_user(handler) -> str | None:
    token = handler.get_cookie(USER_COOKIE)
    if not token:
        return None
    with SESSION_LOCK:
        return USER_SESSIONS.get(token)


def is_admin_session(handler) -> bool:
    token = handler.get_cookie(ADMIN_COOKIE)
    if not token:
        return False
    with SESSION_LOCK:
        return bool(ADMIN_SESSIONS.get(token))


# ---------------------------------------------------------------------------
# Automatic doubling
# ---------------------------------------------------------------------------

def get_growth_total(amount: int) -> int:
    """Return the net total after the next progressive growth level."""
    amount = max(0, int(amount))
    if amount <= 500:
        return 2000
    if amount <= 2000:
        return 5000
    if amount <= 5000:
        return 20000
    if amount <= 20000:
        return 90000
    return amount * 5


def get_doubling_multiplier(amount: int) -> float:
    """Return the conversion multiplier for the current deposit level."""
    amount = max(0, int(amount))
    if amount <= 500:
        return 4
    if amount <= 2000:
        return 2.5
    if amount <= 5000:
        return 4
    if amount <= 20000:
        return 4.5
    return 5.0


def process_pending_doubling(user: dict) -> bool:
    """
    For every deposit that has been accepted (funded) but not yet doubled,
    check if 4 days have passed. If so, add the growth amount to the balance.
    Returns True if the user record changed.
    """
    now = time.time()
    changed = False
    investments = user.get("investments", [])
    ledger = user.setdefault("ledger", [])

    for inv in investments:
        if inv.get("doubled"):
            continue
        funded_at = inv.get("funded_at")
        if not funded_at:
            continue
        try:
            funded_ts = datetime.strptime(funded_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue

        if now - funded_ts >= DOUBLING_DELAY_SECONDS:
            original = int(inv.get("amount", 0))
            growth_total = get_growth_total(original)
            increase = growth_total - original
            user["balance_ngn"] = int(user.get("balance_ngn", 0)) + increase
            inv["doubled"] = True
            inv["status"] = "Doubled"
            inv["doubled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            inv["increase_ngn"] = increase
            inv["doubling_multiplier"] = get_doubling_multiplier(original)

            ledger.append({
                "type": "Fund doubling",
                "amount": increase,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": f"Growth level on ₦{original:,} deposited on {inv.get('date', '—')} (₦{growth_total:,} total)",
                "audit_reference": f"AUTO-DOUBLE-{inv.get('id', '')}",
            })
            changed = True

    return changed


def process_all_doubling():
    """Check every user and apply any due doublings."""
    users = load_users()
    changed = False
    for user in users.values():
        if process_pending_doubling(user):
            changed = True
    if changed:
        save_users(users)


def get_deposit_cooldown(user: dict, now: datetime | None = None) -> dict:
    """Return the latest deposit time and when the next deposit is allowed."""
    investments = user.get("investments", [])
    dated_investments = [inv for inv in investments if inv.get("date")]
    if not dated_investments:
        return {"last_deposit_at": "", "next_deposit_at": "", "deposit_wait_seconds": 0}

    latest = max(dated_investments, key=lambda inv: str(inv.get("date", "")))
    try:
        deposited_at = datetime.strptime(latest["date"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, TypeError, ValueError):
        return {"last_deposit_at": "", "next_deposit_at": "", "deposit_wait_seconds": 0}

    current_time = now or datetime.now(timezone.utc)
    next_deposit_at = deposited_at + timedelta(seconds=NEW_DEPOSIT_COOLDOWN_SECONDS)
    wait_seconds = max(0, int((next_deposit_at - current_time).total_seconds()))
    return {
        "last_deposit_at": deposited_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_deposit_at": next_deposit_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deposit_wait_seconds": wait_seconds,
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def public_user_profile(user: dict) -> dict:
    """Profile returned to the member themselves."""
    investments = user.get("investments", [])
    withdrawals = user.get("withdrawals", [])
    ledger = user.get("ledger", [])

    activity = []
    transactions = []
    pending_doubling = []
    pending_total = 0

    for inv in investments:
        amount = int(inv.get("amount", 0))
        doubled = bool(inv.get("doubled"))
        status = normalize_growth_status(inv.get("status", "Pending 4-day growth"))
        increase = int(inv.get("increase_ngn", 0)) if doubled else 0
        if not doubled and inv.get("funded_at"):
            pending_total += amount
            try:
                funded_at = datetime.strptime(inv["funded_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                due_at = funded_at + timedelta(seconds=DOUBLING_DELAY_SECONDS)
                pending_doubling.append({
                    "amount_ngn": amount,
                    "due_at": due_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            except (KeyError, ValueError):
                pass

        if not inv.get("funded_at") and not inv.get("accepted_at"):
            transactions.append({
                "type": "Deposit",
                "amount_ngn": amount,
                "date": inv.get("date", ""),
                "status": inv.get("status", "Pending admin review"),
                "reference": inv.get("transfer_reference", ""),
            })

        # Keep reviewed deposits in storage/admin history, but remove them from
        # the member dashboard's pending activity list after accept or decline.
        if (
            inv.get("funded_at")
            or inv.get("accepted_at")
            or str(inv.get("status", "")).lower().startswith("declined")
        ):
            continue

        activity.append({
            "type": "Deposit",
            "amount_ngn": amount,
            "increase_ngn": increase,
            "date": inv.get("date", ""),
            "status": status,
            "doubled": doubled,
        })

    for wd in withdrawals:
        status = wd.get("status", "Pending admin review")
        if not str(status).lower().startswith(("paid", "rejected")):
            activity.append({
                "type": "Withdrawal request",
                "amount_ngn": int(wd.get("amount", 0)),
                "increase_ngn": 0,
                "date": wd.get("date", ""),
                "status": status,
                "doubled": False,
            })
            transactions.append({
                "type": "Withdrawal request",
                "amount_ngn": -int(wd.get("amount", 0)),
                "date": wd.get("date", ""),
                "status": status,
                "reference": "",
            })

    for entry in ledger:
        transactions.append({
            "type": entry.get("type", "Account transaction"),
            "amount_ngn": int(entry.get("amount", 0)),
            "date": entry.get("date", ""),
            "status": entry.get("status", "Completed"),
            "reference": entry.get("audit_reference", ""),
            "note": entry.get("note", ""),
        })

    activity.sort(key=lambda x: x.get("date", ""), reverse=True)
    transactions.sort(key=lambda x: x.get("date", ""), reverse=True)
    pending_doubling.sort(key=lambda x: x["due_at"])
    deposit_cooldown = get_deposit_cooldown(user)

    return {
        "full_name": user.get("full_name", ""),
        "email": user.get("email", ""),
        "created_at": user.get("created_at", ""),
        "balance_ngn": int(user.get("balance_ngn", 0)),
        "private_messages": user.get("private_messages", []),
        **deposit_cooldown,
        "pending_doubling_ngn": pending_total,
        "pending_doubling": pending_doubling,
        "activity": activity,
        "transactions": transactions,
    }


def admin_user_record(email: str, user: dict) -> dict:
    """Profile shown only to an authenticated admin, including payout details."""
    investments = user.get("investments", [])
    withdrawals = user.get("withdrawals", [])

    activity = []
    for inv in investments:
        activity.append({
            "id": inv.get("id", ""),
            "type": "Deposit",
            "amount_ngn": int(inv.get("amount", 0)),
            "date": inv.get("date", ""),
            "status": normalize_growth_status(inv.get("status", "Pending admin review")),
            "doubled": bool(inv.get("doubled")),
            "funded_at": inv.get("funded_at", ""),
            "accepted_at": inv.get("accepted_at", ""),
            "sender_name": inv.get("sender_name", ""),
            "transfer_reference": inv.get("transfer_reference", ""),
            "increase_ngn": int(inv.get("increase_ngn", 0)),
        })
    for wd in withdrawals:
        withdrawal_activity = {
            "id": wd.get("id", ""),
            "type": "Withdrawal request",
            "amount_ngn": int(wd.get("amount", 0)),
            "date": wd.get("date", ""),
            "status": wd.get("status", "Pending admin review"),
            "note": wd.get("note", ""),
        }
        payout_details = {
            "bank_name": wd.get("payout_bank_name"),
            "account_name": wd.get("payout_account_name"),
            "account_number": wd.get("payout_account_number"),
        }
        withdrawal_activity.update({
            key: value for key, value in payout_details.items() if value
        })
        activity.append(withdrawal_activity)

    activity.sort(key=lambda x: x.get("date", ""), reverse=True)

    return {
        "full_name": user.get("full_name", ""),
        "email": email,
        "created_at": user.get("created_at", ""),
        "balance_ngn": int(user.get("balance_ngn", 0)),
        "activity": activity,
        "private_messages": user.get("private_messages", []),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class JoyFundsHandler(BaseHTTPRequestHandler):
    server_version = "JoyFunds/1.0"

    # -------- helpers --------

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def get_cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        if name in jar:
            return jar[name].value
        return None

    def send_security_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, status, payload, cookies=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookies:
            for c in cookies:
                self.send_header("Set-Cookie", c)
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename, content_type):
        path = os.path.join(BASE_DIR, filename)
        if not os.path.isfile(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def make_session_cookie(self, name, token, max_age=60 * 60 * 24 * 30):
        return (
            f"{name}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={max_age}"
        )

    def clear_cookie(self, name):
        return f"{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    # -------- routing --------

    def do_GET(self):
        path = urlparse(self.path).path

        if path in STATIC_FILES:
            filename, ctype = STATIC_FILES[path]
            self.send_static(filename, ctype)
            return

        if path == "/api/me":
            self.handle_me()
            return

        if path == "/api/payment-details":
            self.handle_payment_details()
            return

        if path == "/api/admin/users":
            self.handle_admin_users()
            return

        if path == "/api/admin/me":
            self.handle_admin_me()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/signup":
            self.handle_signup()
            return

        if path == "/api/login":
            self.handle_login()
            return

        if path == "/api/logout":
            self.handle_logout()
            return

        if path == "/api/admin/login":
            self.handle_admin_login()
            return

        if path == "/api/admin/logout":
            self.handle_admin_logout()
            return

        if path == "/api/invest":
            self.handle_invest()
            return

        if path == "/api/withdraw":
            self.handle_withdraw()
            return

        if path == "/api/admin/requests/action":
            self.handle_admin_action()
            return

        if path == "/api/admin/user/edit":
            self.handle_admin_user_edit()
            return

        if path == "/api/admin/user/balance":
            self.handle_admin_user_balance()
            return
        
        if path == "/api/admin/user/message":
            self.handle_admin_user_message()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    # -------- member auth --------

    def handle_signup(self):
        form = self.read_form()
        full_name = (form.get("full_name") or "").strip()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""

        if not full_name or not email or not password:
            self.send_json(400, {"success": False, "message": "All fields are required."})
            return
        if len(password) < 12 or len(password) > 128:
            self.send_json(400, {"success": False, "message": "Password must be between 12 and 128 characters."})
            return
        if "@" not in email or "." not in email.split("@")[-1]:
            self.send_json(400, {"success": False, "message": "Please enter a valid email."})
            return

        users = load_users()
        if email in users:
            self.send_json(409, {"success": False, "message": "An account with this email already exists."})
            return

        users[email] = {
            "full_name": full_name,
            "email": email,
            "password": hash_password(password),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "balance_ngn": 0,
            "investments": [],
            "withdrawals": [],
            "ledger": [],
            "private_messages": [],
        }
        save_users(users)

        token = create_user_session(email)
        cookie = self.make_session_cookie(USER_COOKIE, token)
        self.send_json(
            200,
            {"success": True, "message": "Account created.", "redirect": "dashboard.html"},
            cookies=[cookie],
        )

    def handle_login(self):
        form = self.read_form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""

        users = load_users()
        user = users.get(email)
        if not user or not verify_password(password, user.get("password", "")):
            self.send_json(401, {"success": False, "message": "Invalid email or password."})
            return

        token = create_user_session(email)
        cookie = self.make_session_cookie(USER_COOKIE, token)
        self.send_json(
            200,
            {"success": True, "message": "Logged in.", "redirect": "dashboard.html"},
            cookies=[cookie],
        )

    def handle_logout(self):
        cookie = self.clear_cookie(USER_COOKIE)
        self.send_json(200, {"success": True}, cookies=[cookie])

    def handle_me(self):
        email = get_session_user(self)
        if not email:
            self.send_json(401, {"success": False, "message": "Not logged in."})
            return

        users = load_users()
        user = users.get(email)
        if not user:
            self.send_json(401, {"success": False, "message": "Account not found."})
            return

        if process_pending_doubling(user):
            users[email] = user
            save_users(users)

        self.send_json(200, public_user_profile(user))

    # -------- deposit / invest --------

    def handle_payment_details(self):
        email = get_session_user(self)
        if not email:
            self.send_json(401, {"success": False, "message": "Not logged in."})
            return
        self.send_json(200, {
            "success": True,
            "bank": DEPOSIT_BANK,
            "account_name": DEPOSIT_ACCOUNT_NAME,
            "account_number": DEPOSIT_ACCOUNT_NUMBER,
        })

    def handle_invest(self):
        email = get_session_user(self)
        if not email:
            self.send_json(401, {"success": False, "message": "Not logged in."})
            return

        form = self.read_form()
        try:
            amount = int(float(form.get("amount", "0")))
        except ValueError:
            amount = 0
        sender_name = (form.get("sender_name") or "").strip()
        transfer_reference = (form.get("transfer_reference") or "").strip()

        if amount <= 0:
            self.send_json(400, {"success": False, "message": "Enter a valid amount."})
            return
        if not sender_name:
            self.send_json(400, {"success": False, "message": "Enter the name on the sending account."})
            return

        users = load_users()
        user = users.get(email)
        if not user:
            self.send_json(401, {"success": False, "message": "Account not found."})
            return

        cooldown = get_deposit_cooldown(user)
        if cooldown["deposit_wait_seconds"] > 0:
            self.send_json(429, {
                "success": False,
                "message": "You can make your next deposit five days after your last deposit request.",
                "next_deposit_at": cooldown["next_deposit_at"],
                "deposit_wait_seconds": cooldown["deposit_wait_seconds"],
            })
            return

        accepted_deposits = [
            inv for inv in user.get("investments", [])
            if inv.get("funded_at") or inv.get("accepted_at")
        ]
        if accepted_deposits:
            latest_deposit = max(
                accepted_deposits,
                key=lambda inv: inv.get("accepted_at") or inv.get("funded_at") or "",
            )
            minimum_amount = int(latest_deposit.get("amount", 0))
        else:
            minimum_amount = 500
        if amount < minimum_amount:
            self.send_json(400, {
                "success": False,
                "message": f"Enter the same amount as your last accepted deposit or more (minimum ₦{minimum_amount:,}).",
            })
            return

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        request_id = secrets.token_hex(8)

        # NOTE: no funded_at yet — admin must accept before doubling starts
        deposit = {
            "id": request_id,
            "amount": amount,
            "sender_name": sender_name,
            "transfer_reference": transfer_reference,
            "date": now_iso,
            "funded_at": "",
            "doubled": False,
            "increase_ngn": 0,
            "status": "Pending admin review",
        }
        user.setdefault("investments", []).append(deposit)
        users[email] = user
        save_users(users)

        self.send_json(201, {
            "success": True,
            "request_id": request_id,
            "message": (
                "Your transaction has been added to pending. "
                "It will be reviewed and your funds will double automatically within 4 days of acceptance."
            ),
        })

    # -------- withdrawals --------

    def handle_withdraw(self):
        email = get_session_user(self)
        if not email:
            self.send_json(401, {"success": False, "message": "Not logged in."})
            return

        form = self.read_form()
        try:
            amount = int(float(form.get("amount", "0")))
        except ValueError:
            amount = 0
        note = (form.get("note") or "").strip()[:500]
        bank_name = (form.get("bank_name") or "").strip()[:100]
        account_name = " ".join((form.get("account_name") or "").split())[:120]
        account_number = (form.get("account_number") or "").strip()

        if amount <= 0 or amount > 100_000_000:
            self.send_json(400, {"success": False, "message": "Enter a valid withdrawal amount."})
            return
        if not bank_name:
            self.send_json(400, {"success": False, "message": "Enter the bank name for the payout."})
            return
        if not account_name:
            self.send_json(400, {"success": False, "message": "Enter the beneficiary account name."})
            return
        if not (account_number.isdigit() and 10 <= len(account_number) <= 20):
            self.send_json(400, {"success": False, "message": "Enter a valid account number (10–20 digits)."})
            return

        users = load_users()
        user = users.get(email)
        if not user:
            self.send_json(401, {"success": False, "message": "Account not found."})
            return

        verified_deposit_names = {
            " ".join(str(inv.get("sender_name", "")).split()).casefold()
            for inv in user.get("investments", [])
            if inv.get("funded_at") or inv.get("accepted_at")
            if str(inv.get("sender_name", "")).strip()
        }
        allowed_names = verified_deposit_names or {
            " ".join(str(user.get("full_name", "")).split()).casefold()
        }
        if " ".join(account_name.split()).casefold() not in allowed_names:
            self.send_json(400, {
                "success": False,
                "message": "The beneficiary account name must match an accepted deposit name, or your profile name if no deposit has been accepted.",
            })
            return

        if amount > int(user.get("balance_ngn", 0)):
            self.send_json(409, {
                "success": False,
                "message": "Withdrawal amount is greater than your available balance.",
            })
            return

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        request_id = secrets.token_hex(8)

        user.setdefault("withdrawals", []).append({
            "id": request_id,
            "amount": amount,
            "note": note,
            "payout_bank_name": bank_name,
            "payout_account_name": account_name,
            "payout_account_number": account_number,
            "date": now_iso,
            "status": "Pending admin review",
        })
        user["balance_ngn"] = int(user.get("balance_ngn", 0)) - amount

        users[email] = user
        save_users(users)

        self.send_json(201, {
            "success": True,
            "request_id": request_id,
            "message": (
                "Your withdrawal request has been added to pending. "
                "It will be reviewed and processed by the Government."
            ),
        })

    # -------- admin auth --------

    def handle_admin_login(self):
        form = self.read_form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""

        if email != ADMIN_EMAIL.lower() or password != ADMIN_PASSWORD:
            self.send_json(401, {"success": False, "message": "Invalid admin credentials."})
            return

        token = create_admin_session()
        cookie = self.make_session_cookie(ADMIN_COOKIE, token, max_age=60 * 60 * 8)
        self.send_json(
            200,
            {"success": True, "message": "Admin logged in.", "redirect": "admin.html"},
            cookies=[cookie],
        )

    def handle_admin_logout(self):
        cookie = self.clear_cookie(ADMIN_COOKIE)
        self.send_json(200, {"success": True}, cookies=[cookie])

    def handle_admin_me(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Not an admin session."})
            return
        self.send_json(200, {"success": True, "email": ADMIN_EMAIL})

    # -------- admin data --------

    def handle_admin_users(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Admin login required."})
            return

        process_all_doubling()

        users = load_users()
        records = [admin_user_record(email, u) for email, u in users.items()]
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        self.send_json(200, {"success": True, "users": records})

    # -------- admin: edit user --------

    def handle_admin_user_edit(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Admin login required."})
            return

        form = self.read_form()
        current_email = (form.get("current_email") or "").strip().lower()
        full_name = (form.get("full_name") or "").strip()
        new_email = (form.get("email") or "").strip().lower()

        if not current_email or not full_name or not new_email:
            self.send_json(400, {"success": False, "message": "Name and email are required."})
            return

        users = load_users()
        if current_email not in users:
            self.send_json(404, {"success": False, "message": "User not found."})
            return

        if new_email != current_email and new_email in users:
            self.send_json(409, {"success": False, "message": "Another account already uses that email."})
            return

        user = users.pop(current_email)
        user["full_name"] = full_name
        user["email"] = new_email
        users[new_email] = user
        save_users(users)

        self.send_json(200, {"success": True, "message": "User profile updated."})
    
    def handle_admin_user_message(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Admin login required."})
            return

        form = self.read_form()
        email = (form.get("email") or "").strip().lower()
        message = (form.get("message") or "").strip()
        if not email or not message:
            self.send_json(400, {"success": False, "message": "Choose a user and enter a message."})
            return
        if len(message) > 2000:
            self.send_json(400, {"success": False, "message": "Messages must be 2,000 characters or fewer."})
            return

        users = load_users()
        user = users.get(email)
        if not user:
            self.send_json(404, {"success": False, "message": "User not found."})
            return

        private_message = {
            "id": secrets.token_hex(8),
            "sender": "Admin",
            "message": message,
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        user.setdefault("private_messages", []).append(private_message)
        users[email] = user
        save_users(users)
        self.send_json(201, {"success": True, "message": "Private message sent to this user."})

    # -------- admin: add / subtract balance --------

    def handle_admin_user_balance(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Admin login required."})
            return

        form = self.read_form()
        email = (form.get("email") or "").strip().lower()
        mode = (form.get("mode") or "").strip().lower()
        note = (form.get("note") or "").strip()[:200]
        try:
            amount = int(float(form.get("amount", "0")))
        except ValueError:
            amount = 0

        if mode not in ("add", "subtract") or amount <= 0 or not email:
            self.send_json(400, {"success": False, "message": "Invalid balance adjustment."})
            return

        users = load_users()
        user = users.get(email)
        if not user:
            self.send_json(404, {"success": False, "message": "User not found."})
            return

        if mode == "add":
            user["balance_ngn"] = int(user.get("balance_ngn", 0)) + amount
            ledger_amount = amount
            ledger_type = "Admin credit"
        else:
            if amount > int(user.get("balance_ngn", 0)):
                self.send_json(409, {
                    "success": False,
                    "message": "Cannot subtract more than the user's current balance.",
                })
                return
            user["balance_ngn"] = int(user.get("balance_ngn", 0)) - amount
            ledger_amount = -amount
            ledger_type = "Admin debit"

        user.setdefault("ledger", []).append({
            "type": ledger_type,
            "amount": ledger_amount,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": note,
            "audit_reference": f"ADMIN-{ledger_type.upper().replace(' ', '-')}-{secrets.token_hex(4)}",
        })

        users[email] = user
        save_users(users)

        self.send_json(200, {
            "success": True,
            "message": f"Balance {'increased' if mode == 'add' else 'reduced'} by ₦{amount:,}.",
            "balance_ngn": int(user.get("balance_ngn", 0)),
        })

    # -------- admin: accept / decline / withdraw actions --------

    def handle_admin_action(self):
        if not is_admin_session(self):
            self.send_json(401, {"success": False, "message": "Admin login required."})
            return

        form = self.read_form()
        request_id = (form.get("request_id") or "").strip()
        action = (form.get("action") or "").strip()
        audit_reference = (form.get("audit_reference") or "").strip()

        if not request_id or not action:
            self.send_json(400, {
                "success": False,
                "message": "request_id and action are required.",
            })
            return

        users = load_users()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        target_email = None
        target_user = None
        for email, user in users.items():
            for inv in user.get("investments", []):
                if inv.get("id") == request_id:
                    target_email, target_user = email, user
                    break
            if target_user:
                break
            for wd in user.get("withdrawals", []):
                if wd.get("id") == request_id:
                    target_email, target_user = email, user
                    break
            if target_user:
                break

        if not target_user:
            self.send_json(404, {"success": False, "message": "Request not found."})
            return

        # ---- Deposit accepted ----
        if action == "accept_deposit":
            credited_amount = 0
            for inv in target_user.get("investments", []):
                if inv.get("id") != request_id:
                    continue
                if inv.get("doubled"):
                    self.send_json(409, {"success": False, "message": "This deposit has already been doubled."})
                    return
                if inv.get("funded_at") or inv.get("accepted_at"):
                    self.send_json(409, {"success": False, "message": "This deposit has already been accepted."})
                    return
                if str(inv.get("status", "")).lower().startswith("declined"):
                    self.send_json(409, {"success": False, "message": "This deposit was already declined."})
                    return

                credited_amount = int(inv.get("amount", 0))
                target_user["balance_ngn"] = int(target_user.get("balance_ngn", 0)) + credited_amount
                inv["funded_at"] = now_iso
                inv["status"] = "Pending 4-day growth"
                if audit_reference:
                    inv["admin_reference"] = audit_reference
                inv["accepted_at"] = now_iso

                ledger_entry = {
                    "type": "Deposit accepted",
                    "amount": credited_amount,
                    "date": now_iso,
                    "note": f"Deposit of ₦{credited_amount:,} accepted and funded.",
                }
                if audit_reference:
                    ledger_entry["audit_reference"] = audit_reference
                target_user.setdefault("ledger", []).append(ledger_entry)
                break
            else:
                self.send_json(404, {"success": False, "message": "Deposit not found."})
                return

            process_pending_doubling(target_user)
            users[target_email] = target_user
            save_users(users)

            self.send_json(200, {
                "success": True,
                "message": f"Deposit accepted. ₦{credited_amount:,} credited. Doubling will complete within 4 days.",
                "balance_ngn": int(target_user.get("balance_ngn", 0)),
            })
            return

        # ---- Deposit declined ----
        if action == "decline_deposit":
            for inv in target_user.get("investments", []):
                if inv.get("id") != request_id:
                    continue
                if inv.get("doubled"):
                    self.send_json(409, {"success": False, "message": "Cannot decline a doubled deposit."})
                    return
                if inv.get("funded_at") or inv.get("accepted_at"):
                    self.send_json(409, {"success": False, "message": "Cannot decline a deposit that was already accepted."})
                    return
                if str(inv.get("status", "")).lower().startswith("declined"):
                    self.send_json(409, {"success": False, "message": "Already declined."})
                    return
                inv["status"] = "Declined"
                inv["declined_at"] = now_iso
                if audit_reference:
                    inv["admin_reference"] = audit_reference
                break
            else:
                self.send_json(404, {"success": False, "message": "Deposit not found."})
                return

            users[target_email] = target_user
            save_users(users)

            self.send_json(200, {"success": True, "message": "Deposit declined. No funds were credited."})
            return

        # ---- Withdrawal paid ----
        if action == "mark_withdrawal_paid":
            for wd in target_user.get("withdrawals", []):
                if wd.get("id") != request_id:
                    continue
                if wd.get("status", "").startswith("Paid"):
                    self.send_json(409, {"success": False, "message": "Already paid."})
                    return
                wd["status"] = "Paid"
                wd["paid_at"] = now_iso
                if audit_reference:
                    wd["audit_reference"] = audit_reference
                ledger_entry = {
                    "type": "Withdrawal paid",
                    "amount": -int(wd.get("amount", 0)),
                    "date": now_iso,
                }
                if audit_reference:
                    ledger_entry["audit_reference"] = audit_reference
                target_user.setdefault("ledger", []).append(ledger_entry)
                break
            else:
                self.send_json(404, {"success": False, "message": "Withdrawal not found."})
                return

            users[target_email] = target_user
            save_users(users)
            self.send_json(200, {"success": True, "message": "Withdrawal marked as paid."})
            return

        # ---- Withdrawal rejected (refund) ----
        if action == "reject_withdrawal":
            for wd in target_user.get("withdrawals", []):
                if wd.get("id") != request_id:
                    continue
                if wd.get("status", "").startswith("Paid"):
                    self.send_json(409, {"success": False, "message": "Already paid, cannot reject."})
                    return
                if wd.get("status", "").startswith("Rejected"):
                    self.send_json(409, {"success": False, "message": "Already rejected."})
                    return
                wd["status"] = "Rejected"
                wd["rejected_at"] = now_iso
                if audit_reference:
                    wd["audit_reference"] = audit_reference
                target_user["balance_ngn"] = int(target_user.get("balance_ngn", 0)) + int(wd.get("amount", 0))
                ledger_entry = {
                    "type": "Withdrawal refunded",
                    "amount": int(wd.get("amount", 0)),
                    "date": now_iso,
                }
                if audit_reference:
                    ledger_entry["audit_reference"] = audit_reference
                target_user.setdefault("ledger", []).append(ledger_entry)
                break
            else:
                self.send_json(404, {"success": False, "message": "Withdrawal not found."})
                return

            users[target_email] = target_user
            save_users(users)
            self.send_json(200, {"success": True, "message": "Withdrawal rejected. Funds refunded."})
            return

        self.send_json(400, {"success": False, "message": f"Unknown action: {action}"})


# ---------------------------------------------------------------------------
# Background doubling worker
# ---------------------------------------------------------------------------

def doubling_worker():
    """Every 60 seconds, apply any due doublings across all users."""
    while True:
        try:
            process_all_doubling()
        except Exception as exc:
            print(f"[doubling_worker] error: {exc}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    migrate_user_passwords()
    process_all_doubling()

    worker = threading.Thread(target=doubling_worker, daemon=True)
    worker.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), JoyFundsHandler)
    print(f"Joy Funds server running on http://127.0.0.1:{PORT}")
    print(f"Admin email: {ADMIN_EMAIL}")
    print("(Change the admin password with JOYFUNDS_ADMIN_PASSWORD before going live.)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()