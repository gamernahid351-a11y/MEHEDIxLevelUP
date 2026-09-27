# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Web Dashboard Server
Admin + User system with per-user isolation, limits and expiry
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

from user_manager import (
    create_user, validate_user, get_user, add_account_to_user,
    remove_account_from_user, list_all_users, delete_user, set_account_paused,
    extend_user_time, get_admin_credentials, is_user_expired
)

# ==================== GLOBAL BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_links: Dict[str, str] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str,
                         level: int, exp: int, likes: int = 0,
                         owner: Optional[str] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "display_uid": uid_str,
                "actual_uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "owner": owner,
                "added_at": time.time()
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            if owner:
                acc["owner"] = owner
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP!",
                         "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()


# ==================== TEMPLATE LOADER ====================
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def load_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<h1>{name} not found</h1>"


# ==================== SESSION MANAGEMENT ====================
_sessions: Dict[str, Dict[str, Any]] = {}
_session_lock = asyncio.Lock()
SESSION_TTL = 86400


def _gen_session_id() -> str:
    import secrets
    return secrets.token_urlsafe(32)


async def create_session(username: str, is_admin: bool = False) -> str:
    async with _session_lock:
        sid = _gen_session_id()
        _sessions[sid] = {
            "username": username,
            "is_admin": is_admin,
            "created_at": time.time()
        }
        return sid


async def get_session(sid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    async with _session_lock:
        s = _sessions.get(sid)
        if not s:
            return None
        if time.time() - s["created_at"] > SESSION_TTL:
            del _sessions[sid]
            return None
        return s


async def destroy_session(sid: Optional[str]):
    if not sid:
        return
    async with _session_lock:
        _sessions.pop(sid, None)


def _get_sid(request: web.Request) -> Optional[str]:
    return request.cookies.get("sid")


def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"status": "error", "error": msg}, status=status)


# ==================== POPUP CONFIG ====================
POPUP_CONFIG_FILE = "popup_config.json"

DEFAULT_POPUP = {
    "enabled": True,
    "header": "Important Update",
    "title": "IND Server Maintenance",
    "message": "Due to game restrictions in IND server, our Level Up system can only farm 5K exp per day. So we decided to close IND server until our devs fix this. We will be back online for IND server soon.",
    "button_text": "Chat Now",
    "button_link": "https://t.me/",
    "icon_class": "fa-solid fa-circle-check",
    "icon_color": "#22c55e",
    "updated_at": 0
}


def _load_popup_config() -> Dict[str, Any]:
    if not os.path.exists(POPUP_CONFIG_FILE):
        return dict(DEFAULT_POPUP)
    try:
        with open(POPUP_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = dict(DEFAULT_POPUP)
        cfg.update(data or {})
        return cfg
    except Exception:
        return dict(DEFAULT_POPUP)


def _save_popup_config(cfg: Dict[str, Any]):
    try:
        tmp = POPUP_CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, POPUP_CONFIG_FILE)
    except Exception as e:
        print(f"[POPUP] Save error: {e}")


async def api_public_popup(request: web.Request) -> web.Response:
    cfg = _load_popup_config()
    return web.json_response({"status": "ok", "popup": cfg})


async def api_admin_get_popup(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    cfg = _load_popup_config()
    return web.json_response({"status": "ok", "popup": cfg})


async def api_admin_save_popup(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        cfg = _load_popup_config()

        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
        if "header" in data:
            cfg["header"] = str(data["header"])
        if "title" in data:
            cfg["title"] = str(data["title"])
        if "message" in data:
            cfg["message"] = str(data["message"])
        if "button_text" in data:
            cfg["button_text"] = str(data["button_text"])
        if "button_link" in data:
            cfg["button_link"] = str(data["button_link"])
        if "icon_class" in data:
            cfg["icon_class"] = str(data["icon_class"])
        if "icon_color" in data:
            cfg["icon_color"] = str(data["icon_color"])

        cfg["updated_at"] = int(time.time())

        _save_popup_config(cfg)
        return web.json_response({"status": "ok", "popup": cfg})
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== PAGE ROUTES ====================

async def handle_logo(request: web.Request) -> web.Response:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=load_template("landing.html"),
                        content_type="text/html", charset="utf-8")


async def handle_login_page(request: web.Request) -> web.Response:
    return web.Response(text=load_template("login.html"),
                        content_type="text/html", charset="utf-8")


async def handle_admin_login_page(request: web.Request) -> web.Response:
    return web.Response(text=load_template("admin_login.html"),
                        content_type="text/html", charset="utf-8")


async def handle_admin_page(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or not sess.get("is_admin"):
        return web.HTTPFound("/admin-login")
    return web.Response(text=load_template("admin.html"),
                        content_type="text/html", charset="utf-8")


async def handle_dashboard_page(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or sess.get("is_admin"):
        return web.HTTPFound("/login")

    username = sess["username"]
    if await is_user_expired(username):
        await destroy_session(sid)
        return web.HTTPFound("/login?expired=1")

    return web.Response(text=load_template("dashboard.html"),
                        content_type="text/html", charset="utf-8")


# ==================== AUTH API ====================

async def api_user_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()

        if not username or not password:
            return _json_error("Username and password required")

        result = await validate_user(username, password)
        if "error" in result:
            return _json_error(result["error"], 401)

        sid = await create_session(username, is_admin=False)
        resp = web.json_response({
            "status": "ok",
            "username": username,
            "account_limit": result["account_limit"],
            "expires_at": result["expires_at"]
        })
        resp.set_cookie("sid", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        return resp
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()

        admin_creds = get_admin_credentials()
        if username != admin_creds["username"] or password != admin_creds["password"]:
            return _json_error("Invalid admin credentials", 401)

        sid = await create_session(username, is_admin=True)
        resp = web.json_response({"status": "ok"})
        resp.set_cookie("sid", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        return resp
    except Exception as e:
        return _json_error(str(e), 500)


async def api_logout(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    await destroy_session(sid)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie("sid")
    return resp


# ==================== ADMIN API ====================

async def _require_admin(request: web.Request) -> Optional[web.Response]:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or not sess.get("is_admin"):
        return _json_error("Unauthorized", 401)
    return None


async def api_admin_create_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        result = await create_user(
            username=data.get("username", ""),
            password=data.get("password", ""),
            account_limit=data.get("account_limit", 1),
            duration_value=data.get("duration_value", 1),
            duration_unit=data.get("duration_unit", "hours")
        )
        if "error" in result:
            return _json_error(result["error"])
        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_list_users(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        users = await list_all_users()
        return web.json_response({"status": "ok", "users": users})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_delete_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        result = await delete_user(username)
        if "error" in result:
            return _json_error(result["error"])

        for key, task in list(bot_state.account_workers.items()):
            if key.startswith(f"{username}::"):
                task.cancel()
                del bot_state.account_workers[key]

        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_extend_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        result = await extend_user_time(
            username=data.get("username", ""),
            duration_value=data.get("duration_value", 1),
            duration_unit=data.get("duration_unit", "hours")
        )
        if "error" in result:
            return _json_error(result["error"])
        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== USER DASHBOARD API ====================

async def _require_user(request: web.Request) -> Optional[web.Response]:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or sess.get("is_admin"):
        return _json_error("Unauthorized", 401)
    return None


def _build_user_accounts(username: str, user: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = time.time()
    result_accounts = []

    for acc in user.get("accounts", []):
        user_uid = str(acc.get("uid") or acc.get("token", "")[:20])
        user_token_prefix = str(acc.get("token", ""))[:20] if acc.get("token") else ""
        merged = None

        for bot_uid, bot_acc in bot_state.accounts.items():
            if bot_acc.get("owner") != username:
                continue
            bot_uid_str = str(bot_uid)
            bot_actual = str(bot_acc.get("actual_uid", ""))
            bot_display = str(bot_acc.get("display_uid", ""))

            if (bot_uid_str == user_uid or
                    bot_actual == user_uid or
                    bot_display == user_uid or
                    (user_token_prefix and bot_uid_str == user_token_prefix)):
                merged = dict(bot_acc)
                real_uid = bot_actual or bot_uid_str
                merged["uid"] = real_uid
                merged["display_uid"] = real_uid
                merged["actual_uid"] = real_uid
                merged["user_input_uid"] = user_uid
                merged["added_at"] = acc.get("added_at", bot_acc.get("added_at", now))
                merged["paused"] = bool(acc.get("paused", bot_acc.get("paused", False)))
                if merged["paused"]:
                    merged["status"] = "PAUSED"
                break

        if merged is None:
            merged = {
                "uid": user_uid,
                "display_uid": user_uid,
                "actual_uid": user_uid,
                "user_input_uid": user_uid,
                "nickname": f"Player_{user_uid[:6]}",
                "region": "BD",
                "level": 1,
                "initial_exp": 0,
                "current_exp": 0,
                "gained_exp": 0,
                "likes": 0,
                "status": "CONNECTING",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "added_at": acc.get("added_at", now),
                "owner": username,
                "paused": bool(acc.get("paused", False))
            }

        result_accounts.append(merged)

    return result_accounts


async def api_user_stats(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        user = await get_user(username)
        if not user:
            return _json_error("User not found", 404)

        now = time.time()
        remaining = max(0, user.get("expires_at", 0) - now)

        result_accounts = _build_user_accounts(username, user)

        # Override with freshest bot_state data
        for acc in result_accounts:
            real_uid = str(acc.get("actual_uid") or acc.get("uid") or "")
            user_input_uid = str(acc.get("user_input_uid") or "")

            fresh_bot_data = None
            for check_key in [real_uid, user_input_uid, str(acc.get("uid", ""))]:
                if check_key and check_key in bot_state.accounts:
                    candidate = bot_state.accounts[check_key]
                    if candidate.get("owner") == username:
                        if fresh_bot_data is None:
                            fresh_bot_data = candidate
                        elif candidate.get("current_exp", 0) > fresh_bot_data.get("current_exp", 0):
                            fresh_bot_data = candidate

            if fresh_bot_data:
                acc["current_exp"] = fresh_bot_data.get("current_exp", acc.get("current_exp", 0))
                acc["gained_exp"] = fresh_bot_data.get("gained_exp", acc.get("gained_exp", 0))
                acc["level"] = fresh_bot_data.get("level", acc.get("level", 1))
                acc["nickname"] = fresh_bot_data.get("nickname", acc.get("nickname"))
                acc["region"] = fresh_bot_data.get("region", acc.get("region", "BD"))
                acc["status"] = fresh_bot_data.get("status", acc.get("status", "ONLINE"))
                acc["matches_played"] = fresh_bot_data.get("matches_played", acc.get("matches_played", 0))
                acc["active_matches"] = fresh_bot_data.get("active_matches", acc.get("active_matches", 0))
                acc["last_updated"] = fresh_bot_data.get("last_updated", acc.get("last_updated", "--"))
                acc["likes"] = fresh_bot_data.get("likes", acc.get("likes", 0))

        total_gained = sum(a.get("gained_exp", 0) for a in result_accounts)
        total_matches = sum(a.get("matches_played", 0) for a in result_accounts)

        return web.json_response({
            "status": "ok",
            "username": username,
            "account_limit": user.get("account_limit", 1),
            "accounts_used": len(user.get("accounts", [])),
            "remaining_seconds": int(remaining),
            "expires_at": user.get("expires_at", 0),
            "accounts": result_accounts,
            "total_gained_exp": total_gained,
            "total_matches": total_matches,
            "created_at": user.get("created_at", 0),
            "total_accounts_added": user.get("total_accounts_added", 0)
        })
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_add_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()

        account_payload = {}
        if data.get("token"):
            account_payload["token"] = str(data["token"]).strip()
        elif data.get("uid") and data.get("password"):
            account_payload["uid"] = str(data["uid"]).strip()
            account_payload["password"] = str(data["password"]).strip()
        else:
            return _json_error("Provide UID+Password or Token")

        result = await add_account_to_user(username, account_payload)
        if "error" in result:
            return _json_error(result["error"])

        cb = bot_state.refresh_callbacks.get("on_user_account_added")
        if cb:
            asyncio.create_task(cb(username, account_payload))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_remove_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        user = await get_user(username)
        if not user:
            return _json_error("User not found")

        all_keys = {acc_id}
        for acc in user.get("accounts", []):
            uid_val = str(acc.get("uid") or "")
            token_val = str(acc.get("token") or "")
            all_keys.add(uid_val)
            if token_val:
                all_keys.add(token_val[:20])
                all_keys.add(f"tok_{token_val[:20]}")

        for bot_uid, ba in bot_state.accounts.items():
            if ba.get("owner") != username:
                continue
            for check_id in [str(bot_uid), str(ba.get("actual_uid", "")),
                             str(ba.get("display_uid", "")), str(ba.get("uid", ""))]:
                if check_id and check_id == acc_id:
                    all_keys.add(str(bot_uid))
                    all_keys.add(str(ba.get("actual_uid", "")))
                    all_keys.add(str(ba.get("display_uid", "")))
                    all_keys.add(str(ba.get("uid", "")))

        removed = False
        for k in list(all_keys):
            if not k:
                continue
            result = await remove_account_from_user(username, k)
            if "status" in result:
                removed = True
                break

        if not removed:
            return _json_error("Account not found")

        for k in all_keys:
            if not k:
                continue
            worker_key = f"{username}::{k}"
            if worker_key in bot_state.account_workers:
                try:
                    bot_state.account_workers[worker_key].cancel()
                except Exception:
                    pass
                bot_state.account_workers.pop(worker_key, None)
            bot_state.accounts.pop(k, None)
            bot_state.account_credentials.pop(k, None)
            bot_state.account_credentials.pop(f"tok_{k}", None)

        bot_state.recalc_totals()

        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e), 500)


async def _resolve_user_account(username: str, account_id: str):
    user = await get_user(username)
    if not user:
        return None, None, None
    target = str(account_id).strip()
    for acc in user.get("accounts", []):
        uid_val = str(acc.get("uid") or "")
        token_val = str(acc.get("token") or "")
        candidates = {uid_val, token_val[:20], f"tok_{token_val[:20]}" if token_val else ""}
        if target in candidates:
            return user, acc, uid_val or token_val[:20]
    # Also resolve against live bot-state aliases/actual UID.
    # Token accounts are often represented in bot_state by the resolved UID,
    # while the persisted user record has only the token. Map both directions.
    for key, ba in bot_state.accounts.items():
        if ba.get("owner") != username:
            continue
        live_ids = {str(key), str(ba.get("uid", "")), str(ba.get("actual_uid", "")), str(ba.get("display_uid", ""))}
        actual = str(ba.get("actual_uid") or ba.get("uid") or key)
        credential = (bot_state.account_credentials.get(actual) or
                      bot_state.account_credentials.get(str(key)) or
                      bot_state.account_credentials.get(f"tok_{str(key)[:20]}"))
        auth_token = str((credential or {}).get("auth_token") or (credential or {}).get("token") or "")
        if auth_token:
            live_ids.add(auth_token[:20])
            live_ids.add(f"tok_{auth_token[:20]}")

        if target not in live_ids:
            continue

        for acc in user.get("accounts", []):
            uid_val = str(acc.get("uid") or "")
            token_val = str(acc.get("token") or "")
            token_prefix = token_val[:20] if token_val else ""
            # Guest/UID account.
            if uid_val and actual == uid_val:
                return user, acc, uid_val
            # Token account: return the persisted token prefix so
            # set_account_paused() can update the correct record.
            if token_prefix and (
                target in {token_prefix, f"tok_{token_prefix}"} or
                str(key) in {token_prefix, f"tok_{token_prefix}"} or
                actual == uid_val or
                (auth_token and auth_token[:20] == token_prefix)
            ):
                return user, acc, token_prefix
    return user, None, None


async def api_user_pause_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]
        data = await request.json()
        account_id = str(data.get("account_id", "")).strip()
        if not account_id:
            return _json_error("account_id required")
        _user, acc, resolved = await _resolve_user_account(username, account_id)
        if not acc:
            return _json_error("Account not found", 404)

        # Always persist against the real stored identifier.  For token accounts
        # this is the token prefix; for UID/password accounts it is the UID.
        stored_id = str(acc.get("uid") or "") or str(acc.get("token") or "")[:20] or resolved
        result = await set_account_paused(username, stored_id, True)
        if "error" in result:
            return _json_error(result["error"])

        cb = bot_state.refresh_callbacks.get("on_pause_account")
        if not cb:
            return _json_error("Account process controller is not ready", 503)
        await cb(username, acc)
        return web.json_response({"status": "ok", "paused": True, "account_id": stored_id})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_resume_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]
        data = await request.json()
        account_id = str(data.get("account_id", "")).strip()
        if not account_id:
            return _json_error("account_id required")
        _user, acc, resolved = await _resolve_user_account(username, account_id)
        if not acc:
            return _json_error("Account not found", 404)

        stored_id = str(acc.get("uid") or "") or str(acc.get("token") or "")[:20] or resolved
        result = await set_account_paused(username, stored_id, False)
        if "error" in result:
            return _json_error(result["error"])

        cb = bot_state.refresh_callbacks.get("on_resume_account")
        if not cb:
            return _json_error("Account process controller is not ready", 503)
        await cb(username, acc)
        return web.json_response({"status": "ok", "paused": False, "account_id": stored_id})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_refresh(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        candidates = {acc_id}

        user = await get_user(username)
        if user:
            for acc in user.get("accounts", []):
                uid_val = str(acc.get("uid") or "")
                token_val = str(acc.get("token") or "")
                if uid_val:
                    candidates.add(uid_val)
                if token_val:
                    candidates.add(token_val[:20])
                    candidates.add(f"tok_{token_val[:20]}")

        for bot_uid, ba in bot_state.accounts.items():
            if ba.get("owner") != username:
                continue
            bot_uid_str = str(bot_uid)
            match = (bot_uid_str == acc_id or
                     str(ba.get("actual_uid", "")) == acc_id or
                     str(ba.get("display_uid", "")) == acc_id or
                     str(ba.get("uid", "")) == acc_id)
            if match:
                candidates.add(bot_uid_str)
                if ba.get("actual_uid"):
                    candidates.add(str(ba["actual_uid"]))
                if ba.get("display_uid"):
                    candidates.add(str(ba["display_uid"]))
                if ba.get("uid"):
                    candidates.add(str(ba["uid"]))

        for c in list(candidates):
            if c and not c.startswith("tok_") and len(c) >= 15:
                candidates.add(f"tok_{c}")

        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb:
            for c in candidates:
                if c:
                    try:
                        asyncio.create_task(cb(c))
                    except Exception:
                        pass

        await asyncio.sleep(1.5)

        updated_account = None
        if user:
            all_accounts = _build_user_accounts(username, user)
            for a in all_accounts:
                if (str(a.get("uid", "")) == acc_id or
                        str(a.get("actual_uid", "")) == acc_id or
                        str(a.get("display_uid", "")) == acc_id or
                        str(a.get("user_input_uid", "")) == acc_id):
                    updated_account = a
                    break

        if not updated_account:
            for bot_uid, ba in bot_state.accounts.items():
                if ba.get("owner") != username:
                    continue
                if (str(bot_uid) == acc_id or
                        str(ba.get("actual_uid", "")) == acc_id or
                        str(ba.get("display_uid", "")) == acc_id):
                    updated_account = dict(ba)
                    updated_account["uid"] = str(ba.get("actual_uid", bot_uid))
                    updated_account["display_uid"] = updated_account["uid"]
                    updated_account["user_input_uid"] = acc_id
                    break

        return web.json_response({
            "status": "ok",
            "account": updated_account
        })
    except Exception as e:
        return _json_error(str(e), 500)



# ==================== PACKAGE / PRICING MANAGEMENT ====================
PACKAGES_FILE = "packages.json"
DEFAULT_PACKAGES = [
    {"id":"starting","name":"Starting","price":99,"old_price":199,"duration_value":1,"duration_unit":"days","account_limit":3,"icon":"fa-bolt","color":"blue","popular":False,"enabled":True,"features":["Access To The Panel For 1 Day","Fast Leveling Performance","Run Multiple Accounts At Once","Restart & Manage Accounts Anytime","Runs While You Are Offline","Telegram Payment Support & Quick Help","3 Concurrent Accounts"]},
    {"id":"basic","name":"Basic","price":199,"old_price":399,"duration_value":2,"duration_unit":"days","account_limit":3,"icon":"fa-star","color":"purple","popular":True,"enabled":True,"features":["Access To The Panel For 2 Days","Fast Leveling Performance","Run Multiple Accounts At Once","Restart & Manage Accounts Anytime","Runs While You Are Offline","Telegram Payment Support & Quick Help","3 Concurrent Accounts"]},
    {"id":"premium","name":"Premium","price":249,"old_price":599,"duration_value":3,"duration_unit":"days","account_limit":4,"icon":"fa-crown","color":"orange","popular":False,"enabled":True,"features":["Access To The Panel For 3 Days","Fastest Leveling Speed Available","Run Multiple Accounts At Once","Restart & Manage Accounts Anytime","Runs 24/7 — No Need To Stay Online","Telegram Payment Support & Quick Help","4 Concurrent Accounts"]},
    {"id":"safe","name":"Safe","price":699,"old_price":999,"duration_value":7,"duration_unit":"days","account_limit":5,"icon":"fa-shield-halved","color":"emerald","popular":False,"enabled":True,"features":["Access To The Panel For 7 Days","Fastest Leveling Speed Available","Run Multiple Accounts At Once","Restart & Manage Accounts Anytime","Runs 24/7 — No Need To Stay Online","Telegram Payment Support & Quick Help","5 Concurrent Accounts"]}
]


def _load_packages():
    if not os.path.exists(PACKAGES_FILE):
        _save_packages(DEFAULT_PACKAGES)
        return list(DEFAULT_PACKAGES)
    try:
        with open(PACKAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("invalid package data")
        return data
    except Exception:
        return list(DEFAULT_PACKAGES)


def _save_packages(data):
    tmp = PACKAGES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PACKAGES_FILE)


def _package_id(name):
    import re
    x = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return x or f"plan-{int(time.time())}"


async def api_public_packages(request: web.Request) -> web.Response:
    packages = [p for p in _load_packages() if p.get("enabled", True)]
    return web.json_response({"status":"ok", "currency":"BDT", "symbol":"৳", "packages":packages})


async def api_admin_list_packages(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err: return err
    return web.json_response({"status":"ok", "currency":"BDT", "symbol":"৳", "packages":_load_packages()})


async def api_admin_save_package(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err: return err
    try:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not name: return _json_error("Package name is required")
        try: price = float(data.get("price", 0))
        except Exception: return _json_error("Invalid price")
        try: old_price = float(data.get("old_price", 0))
        except Exception: old_price = 0
        try: duration_value = int(data.get("duration_value", 1))
        except Exception: duration_value = 1
        try: account_limit = int(data.get("account_limit", 1))
        except Exception: account_limit = 1
        if price < 0 or old_price < 0 or duration_value < 1 or account_limit < 1:
            return _json_error("Price, duration and account limit must be valid")
        unit = str(data.get("duration_unit", "days")).lower()
        if unit not in ("minutes", "hours", "days"): return _json_error("Invalid duration unit")
        features = data.get("features", [])
        if isinstance(features, str): features = [x.strip() for x in features.split("\\n") if x.strip()]
        if not isinstance(features, list): features = []
        item = {
            "id": str(data.get("id") or _package_id(name)), "name": name,
            "price": price, "old_price": old_price, "duration_value": duration_value,
            "duration_unit": unit, "account_limit": account_limit,
            "icon": str(data.get("icon") or "fa-box"), "color": str(data.get("color") or "blue"),
            "popular": bool(data.get("popular", False)), "enabled": bool(data.get("enabled", True)),
            "features": [str(x).strip() for x in features if str(x).strip()]
        }
        packages = _load_packages()
        found = False
        for i,p in enumerate(packages):
            if str(p.get("id")) == item["id"]:
                packages[i] = item; found = True; break
        if not found: packages.append(item)
        _save_packages(packages)
        return web.json_response({"status":"ok", "package":item, "packages":packages})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_delete_package(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err: return err
    try:
        data = await request.json()
        pid = str(data.get("id", "")).strip()
        packages = [p for p in _load_packages() if str(p.get("id")) != pid]
        _save_packages(packages)
        return web.json_response({"status":"ok", "packages":packages})
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== TELEGRAM CONTACT SETTINGS ====================
TELEGRAM_SETTINGS_FILE = "telegram_settings.json"
DEFAULT_TELEGRAM_SETTINGS = {"link": "https://t.me/Nxt_mehedi_s", "username": "@Nxt_mehedi_s"}

def _load_telegram_settings():
    if not os.path.exists(TELEGRAM_SETTINGS_FILE):
        _save_telegram_settings(DEFAULT_TELEGRAM_SETTINGS)
        return dict(DEFAULT_TELEGRAM_SETTINGS)
    try:
        with open(TELEGRAM_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("invalid telegram settings")
        return {"link": str(data.get("link") or DEFAULT_TELEGRAM_SETTINGS["link"]),
                "username": str(data.get("username") or DEFAULT_TELEGRAM_SETTINGS["username"])}
    except Exception:
        return dict(DEFAULT_TELEGRAM_SETTINGS)

def _save_telegram_settings(data):
    tmp = TELEGRAM_SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TELEGRAM_SETTINGS_FILE)

async def api_public_telegram(request: web.Request) -> web.Response:
    s = _load_telegram_settings()
    return web.json_response({"status":"ok", "telegram":s})

async def api_admin_get_telegram(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err: return err
    return web.json_response({"status":"ok", "telegram":_load_telegram_settings()})

async def api_admin_save_telegram(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err: return err
    try:
        data = await request.json()
        link = str(data.get("link", "")).strip()
        username = str(data.get("username", "")).strip()
        if not link.startswith(("https://t.me/", "http://t.me/")):
            return _json_error("Telegram link must start with https://t.me/")
        if username and not username.startswith("@"):
            username = "@" + username
        if not username:
            username = link.rstrip("/").split("/")[-1]
            if username and not username.startswith("@"):
                username = "@" + username
        saved = {"link":link, "username":username}
        _save_telegram_settings(saved)
        return web.json_response({"status":"ok", "telegram":saved})
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== SERVER START ====================

async def start_web_dashboard(host: str = "0.0.0.0", port: int = 20335):
    app = web.Application()

    app.router.add_get("/", handle_root)
    app.router.add_get("/logo.png", handle_logo)
    app.router.add_get("/login", handle_login_page)
    app.router.add_get("/admin-login", handle_admin_login_page)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/dashboard", handle_dashboard_page)

    app.router.add_post("/api/login", api_user_login)
    app.router.add_post("/api/admin/login", api_admin_login)
    app.router.add_post("/api/logout", api_logout)

    app.router.add_post("/api/admin/create-user", api_admin_create_user)
    app.router.add_get("/api/admin/users", api_admin_list_users)
    app.router.add_post("/api/admin/delete-user", api_admin_delete_user)
    app.router.add_post("/api/admin/extend-user", api_admin_extend_user)
    app.router.add_get("/api/public/packages", api_public_packages)
    app.router.add_get("/api/admin/packages", api_admin_list_packages)
    app.router.add_post("/api/admin/packages", api_admin_save_package)
    app.router.add_post("/api/admin/packages/delete", api_admin_delete_package)

    app.router.add_get("/api/user/stats", api_user_stats)
    app.router.add_post("/api/user/add-account", api_user_add_account)
    app.router.add_post("/api/user/remove-account", api_user_remove_account)
    app.router.add_post("/api/user/refresh", api_user_refresh)
    app.router.add_post("/api/user/pause", api_user_pause_account)
    app.router.add_post("/api/user/resume", api_user_resume_account)

    # Telegram contact / purchase destination
    app.router.add_get("/api/public/telegram", api_public_telegram)
    app.router.add_get("/api/admin/get-telegram", api_admin_get_telegram)
    app.router.add_post("/api/admin/save-telegram", api_admin_save_telegram)

    # Popup config routes
    app.router.add_get("/api/public/popup", api_public_popup)
    app.router.add_get("/api/admin/get-popup", api_admin_get_popup)
    app.router.add_post("/api/admin/save-popup", api_admin_save_popup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")