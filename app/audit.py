"""Audit log: a pure ASGI middleware that records every successful change (POST/PUT/PATCH/DELETE under /api).

It reads the caller that `current_user` stored in scope state, so it never re-checks tokens. Bodies are kept with
secrets removed. Writing is best effort: an audit failure never fails the request."""
import json
import logging
import re

import anyio

log = logging.getLogger("spa.audit")
SECRET = re.compile(r"pass|token|secret", re.I)
SKIP = {"/api/auth/logout"}

ENTITY = {
    "appointments": "appointment", "customers": "customer", "therapists": "therapist", "services": "service",
    "users": "user", "spas": "spa", "payments": "payment", "refunds": "refund", "expenses": "expense",
    "recurring": "regular expense", "bonuses": "extra incentive", "plans": "plan", "plan-packages": "plan package",
    "plan-topups": "top-up", "topup": "top-up", "shifts": "shift", "shift-assignments": "shift assignment",
    "onboarding": "customer (onboarding)", "status": "status", "paid": "payment status", "assign": "plan",
    "logo": "logo", "settings": "settings", "password": "password", "auth": "login",
}


def action_for(method: str, path: str) -> str:
    parts = [p for p in path.split("/")[2:] if p and not p.isdigit()]
    if path.startswith("/api/auth/login"):
        return "Sign in"
    noun = ENTITY.get(parts[-1], parts[-1].replace("-", " ")) if parts else "record"
    verb = {"POST": "Add", "PUT": "Edit", "PATCH": "Change", "DELETE": "Delete"}.get(method, method)
    if parts and parts[-1] in ("status", "paid", "password"):
        verb = "Change"
    if parts and parts[-1] == "refunds":
        verb = "Refund"
        noun = "payment"
    if parts and parts[-1] in ("assign", "topup"):
        verb = "Assign" if parts[-1] == "assign" else "Top up"
        noun = "plan"
    return f"{verb} {noun}"


def clean(v):
    if isinstance(v, dict):
        return {k: ("***" if SECRET.search(k) else clean(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v[:50]]
    return v


class AuditMiddleware:
    def __init__(self, app, db):
        self.app, self.db = app, db

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE") \
                or not scope["path"].startswith("/api/") or scope["path"] in SKIP:
            return await self.app(scope, receive, send)
        chunks, status = [], {"code": 0}

        async def rec():
            msg = await receive()
            if msg["type"] == "http.request" and sum(map(len, chunks)) < 20000:
                chunks.append(msg.get("body", b""))
            return msg

        async def snd(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        await self.app(scope, rec, snd)
        if not (200 <= status["code"] < 300):
            return
        user = (scope.get("state") or {}).get("audit_user")
        body = b"".join(chunks)
        details = None
        ctype = dict(scope.get("headers") or []).get(b"content-type", b"")
        if body and b"json" in ctype:
            try:
                details = clean(json.loads(body))
            except ValueError:
                details = None
        elif body:
            details = {"upload": f"{len(body)} bytes"}
        if scope["path"].startswith("/api/auth/login") and isinstance(details, dict):
            user = user or {"username": details.get("username")}
        client = scope.get("client") or ("", 0)
        row = (user.get("spa") if user else None, user.get("id") if user else None, user.get("username") if user else None,
               user.get("role") if user else None, action_for(scope["method"], scope["path"]), scope["method"],
               scope["path"], status["code"], json.dumps(details, default=str) if details is not None else None,
               _ip(scope, client))
        try:
            await anyio.to_thread.run_sync(lambda: self.db.run(
                """INSERT INTO audit_log(spa_id,user_id,username,role,action,method,path,status,details,ip)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", row))
        except Exception:  # noqa: BLE001 - auditing must never break a request
            log.exception("audit write failed")


def _ip(scope, client):
    for k, v in scope.get("headers") or []:
        if k == b"x-forwarded-for":
            return v.decode(errors="ignore").split(",")[0].strip()
    return client[0]
