"""Personal WHOOP connector for Claude.

A tiny MCP server (streamable HTTP, JSON responses) that reads your WHOOP
cycles and workouts. It serves one person: you sign in to WHOOP once at
/login, and the rotating refresh token is kept in Redis (or memory).

Environment:
  WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET  from developer.whoop.com
  PUBLIC_URL      e.g. https://whoop-connector.onrender.com (no trailing slash)
  CONNECTOR_KEY   long random string; the MCP URL is PUBLIC_URL/mcp/<key>
  REDIS_URL       optional; without it tokens live in memory only
  PORT            set by the host
"""

import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
CONNECTOR_KEY = os.environ.get("CONNECTOR_KEY", "")
REDIS_URL = os.environ.get("REDIS_URL", "")

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API = "https://api.prod.whoop.com/developer"
SCOPES = "offline read:cycles read:workout"
REDIRECT_URI = PUBLIC_URL + "/callback"
MCP_PATH = "/mcp/" + CONNECTOR_KEY
KJ_PER_KCAL = 4.184
# A WHOOP cycle starts when you fall asleep. Shifting the start by six hours
# labels a cycle that began at 11:30pm with the day you actually lived.
DAY_SHIFT = timedelta(hours=6)


# ---------- token storage ----------

class Store:
    def __init__(self):
        self.mem = {}
        self.redis = None
        if REDIS_URL:
            import redis  # only needed when REDIS_URL is set
            self.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)

    def get(self, key):
        if self.redis:
            return self.redis.get(key)
        return self.mem.get(key)

    def set(self, key, value, ttl=None):
        if self.redis:
            self.redis.set(key, value, ex=ttl)
        else:
            self.mem[key] = value

    def delete(self, key):
        if self.redis:
            self.redis.delete(key)
        else:
            self.mem.pop(key, None)


store = Store()
token_lock = threading.Lock()


class NotConnected(Exception):
    pass


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def save_tokens(tok):
    store.set("whoop:tokens", json.dumps({
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 120,
    }))


def access_token(force_refresh=False):
    # WHOOP rotates refresh tokens and rejects concurrent refreshes, so refresh under a lock.
    with token_lock:
        raw = store.get("whoop:tokens")
        if not raw:
            raise NotConnected()
        tok = json.loads(raw)
        if not force_refresh and time.time() < tok["expires_at"]:
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise NotConnected()
        try:
            new = post_form(TOKEN_URL, {
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "offline",
            })
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                store.delete("whoop:tokens")
                raise NotConnected()
            raise
        save_tokens(new)
        return new["access_token"]


def whoop_get(path, params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    for attempt in (0, 1):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token(force_refresh=attempt == 1)})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raise


def whoop_collection(path, start, end, max_pages=20):
    records, token = [], None
    for _ in range(max_pages):
        params = {"limit": 25, "start": start, "end": end}
        if token:
            params["nextToken"] = token
        page = whoop_get(path, params)
        records.extend(page.get("records", []))
        token = page.get("next_token")
        if not token:
            break
    return records


# ---------- date helpers ----------

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def offset(s):
    # timezone_offset looks like "-05:00"
    if not s:
        return timedelta(0)
    sign = -1 if s.startswith("-") else 1
    h, m = s.lstrip("+-").split(":")
    return sign * timedelta(hours=int(h), minutes=int(m))


def local_day(rec, shift=timedelta(0)):
    return (parse_ts(rec["start"]) + offset(rec.get("timezone_offset")) + shift).date().isoformat()


def window(args):
    end_d = date.fromisoformat(args.get("end_date") or date.today().isoformat())
    start_d = date.fromisoformat(args.get("start_date") or (end_d - timedelta(days=13)).isoformat())
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    if (end_d - start_d).days > 92:
        start_d = end_d - timedelta(days=92)
    # Pad by a day on each side; records are filtered by their local day afterwards.
    q_start = datetime.combine(start_d - timedelta(days=1), datetime.min.time(), timezone.utc)
    q_end = datetime.combine(end_d + timedelta(days=2), datetime.min.time(), timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return start_d.isoformat(), end_d.isoformat(), iso(q_start), iso(q_end)


# ---------- tools ----------

def tool_daily_energy(args):
    s, e, qs, qe = window(args)
    days = {}
    for c in whoop_collection("/v2/cycle", qs, qe):
        d = local_day(c, DAY_SHIFT)
        if not (s <= d <= e):
            continue
        score = c.get("score") or {}
        kj = score.get("kilojoule")
        days[d] = {
            "date": d,
            "kcal_burned": round(kj / KJ_PER_KCAL) if kj is not None else None,
            "strain": round(score["strain"], 1) if score.get("strain") is not None else None,
            "in_progress": c.get("end") is None,
            "scored": c.get("score_state") == "SCORED",
        }
    return {"start_date": s, "end_date": e, "days": [days[k] for k in sorted(days)]}


def tool_workouts(args):
    s, e, qs, qe = window(args)
    out = []
    for w in whoop_collection("/v2/activity/workout", qs, qe):
        d = local_day(w)
        if not (s <= d <= e):
            continue
        score = w.get("score") or {}
        kj = score.get("kilojoule")
        mins = (parse_ts(w["end"]) - parse_ts(w["start"])).total_seconds() / 60 if w.get("end") else None
        out.append({
            "date": d,
            "sport": w.get("sport_name"),
            "minutes": round(mins) if mins is not None else None,
            "kcal": round(kj / KJ_PER_KCAL) if kj is not None else None,
            "strain": round(score["strain"], 1) if score.get("strain") is not None else None,
        })
    out.sort(key=lambda x: x["date"])
    return {"start_date": s, "end_date": e, "workouts": out}


DATE_ARGS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "First day, YYYY-MM-DD. Defaults to 13 days before end_date."},
        "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD. Defaults to today."},
    },
    "additionalProperties": False,
}

TOOLS = {
    "get_daily_energy": {
        "fn": tool_daily_energy,
        "description": "Total calories burned (kcal, including resting) and day strain per day from WHOOP, labelled by the local day the WHOOP cycle covers. Up to 93 days.",
    },
    "get_workouts": {
        "fn": tool_workouts,
        "description": "WHOOP workouts per day: sport, minutes, kcal and strain. Up to 93 days.",
    },
}


# ---------- MCP (JSON-RPC over streamable HTTP, JSON responses) ----------

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def rpc(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return ok({
            "protocolVersion": want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "whoop-personal", "version": "1.0.0"},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [
            {"name": n, "description": t["description"], "inputSchema": DATE_ARGS,
             "annotations": {"readOnlyHint": True, "destructiveHint": False}}
            for n, t in TOOLS.items()
        ]})
    if method == "tools/call":
        p = msg.get("params") or {}
        tool = TOOLS.get(p.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool"}}
        try:
            data = tool["fn"](p.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data})
        except NotConnected:
            text = "WHOOP is not connected. Open " + PUBLIC_URL + "/ and sign in to WHOOP again."
            return ok({"content": [{"type": "text", "text": text}], "isError": True})
        except ValueError as e:
            return ok({"content": [{"type": "text", "text": "Bad date: " + str(e)}], "isError": True})
        except urllib.error.HTTPError as e:
            return ok({"content": [{"type": "text", "text": "WHOOP API error " + str(e.code)}], "isError": True})
        except Exception as e:  # network trouble and the like
            return ok({"content": [{"type": "text", "text": "WHOOP request failed: " + type(e).__name__}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}


# ---------- HTTP ----------

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>WHOOP connector</title><body style="font:16px/1.5 system-ui;max-width:34rem;margin:3rem auto;padding:0 16px">
<h1 style="font-size:1.3rem">WHOOP connector</h1>{body}</body>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "whoop-connector"

    def log_message(self, fmt, *args):
        # Never log query strings: they carry the OAuth code and the connector key.
        print("%s %s" % (self.command, self.path.split("?")[0].replace(CONNECTOR_KEY, "***") if CONNECTOR_KEY else self.path.split("?")[0]), flush=True)

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def page(self, code, body):
        self.send(code, PAGE.format(body=body))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if u.path == "/":
            connected = bool(store.get("whoop:tokens"))
            status = "<p><b>Connected to WHOOP.</b></p>" if connected else "<p>Not connected to WHOOP yet.</p>"
            return self.page(200, status + "<p>To sign in, open <code>/login?key=&lt;your connector key&gt;</code>.</p>")
        if u.path == "/login":
            if not CONNECTOR_KEY or not secrets.compare_digest((q.get("key") or [""])[0], CONNECTOR_KEY):
                return self.page(403, "<p>Wrong or missing key.</p>")
            if not CLIENT_ID or not CLIENT_SECRET:
                return self.page(500, "<p>Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET on the server first.</p>")
            state = secrets.token_hex(4)  # WHOOP wants exactly 8 characters
            store.set("whoop:state:" + state, "1", ttl=600)
            url = AUTH_URL + "?" + urllib.parse.urlencode({
                "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                "scope": SCOPES, "state": state,
            })
            return self.send(302, "", headers={"Location": url})
        if u.path == "/callback":
            state = (q.get("state") or [""])[0]
            if not state or not store.get("whoop:state:" + state):
                return self.page(400, "<p>This sign-in link expired. Start again from /login.</p>")
            store.delete("whoop:state:" + state)
            if "error" in q:
                return self.page(400, "<p>WHOOP said: " + html.escape(q["error"][0]) + "</p>")
            try:
                tok = post_form(TOKEN_URL, {
                    "grant_type": "authorization_code", "code": (q.get("code") or [""])[0],
                    "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                })
            except urllib.error.HTTPError as e:
                return self.page(502, "<p>WHOOP refused the sign-in (HTTP %d). Check the client ID, secret and redirect URL.</p>" % e.code)
            save_tokens(tok)
            return self.page(200, "<p><b>Connected to WHOOP.</b> You can close this tab.</p>")
        if CONNECTOR_KEY and u.path == MCP_PATH:
            return self.send(405, "", headers={"Allow": "POST"})
        return self.send(404, "Not found", "text/plain")

    def do_POST(self):
        if not CONNECTOR_KEY or urllib.parse.urlparse(self.path).path != MCP_PATH:
            return self.send(404, "Not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n) or b"null")
        except (ValueError, json.JSONDecodeError):
            return self.send(400, json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), "application/json")
        if isinstance(msg, list):
            out = [r for r in (rpc(m) for m in msg) if r is not None]
        else:
            out = rpc(msg) if isinstance(msg, dict) else None
        if not out:
            return self.send(202)
        return self.send(200, json.dumps(out), "application/json")

    def do_DELETE(self):
        self.send(405, "", headers={"Allow": "POST"})


if __name__ == "__main__":
    if not CONNECTOR_KEY:
        print("CONNECTOR_KEY is not set; the MCP endpoint is disabled.", flush=True)
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
