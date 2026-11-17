"""Local preview server for zcode-hub frontend — serves static + mocks admin API."""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).parent / "frontend" / "admin"

ACCOUNTS = [
    {"id": 1, "name": "aria.finch28", "token_masked": "sk-a1…9f2c", "status": "active", "enabled": True, "mode": "jwt",
     "provider": "z.ai", "plan": {"show_name": "GLM Coding Plan Max"},
     "quota": {"GLM-5.3-Flash": {"remaining": 842000000, "total": 1200000000, "expires_at": 1764547200},
               "GLM-5.3": {"remaining": 318000000, "total": 900000000, "expires_at": 1764547200}},
     "recent_results": ["200", "200", "200", "200", "200"]},
    {"id": 2, "name": "sable.ashby60", "token_masked": "sk-b7…31de", "status": "active", "enabled": True, "mode": "jwt",
     "provider": "z.ai", "plan": {"show_name": "GLM Coding Plan Pro"},
     "quota": {"GLM-5.3-Flash": {"remaining": 96000000, "total": 600000000, "expires_at": 1764460800}},
     "recent_results": ["200", "429", "200", "200"]},
    {"id": 3, "name": "iris.finch82", "token_masked": "sk-c4…88ab", "status": "exhausted", "enabled": False, "mode": "api-key",
     "provider": "z.ai", "plan": {"show_name": "Trial"},
     "quota": {"GLM-5.3-Flash": {"remaining": 0, "total": 50000000, "expires_at": 1764201600}},
     "recent_results": ["3007", "3007"], "last_error": "quota exhausted (billing empty)"},
    {"id": 4, "name": "garrow.sorrel60", "token_masked": "sk-d9…02fe", "status": "cooling", "enabled": True, "mode": "jwt",
     "provider": "z.ai", "plan": {"show_name": "GLM Coding Plan Max"},
     "quota": {"GLM-5.3-Flash": {"remaining": 770000000, "total": 1200000000, "expires_at": 1764547200}},
     "recent_results": ["200", "200"], "cooling_until": 1764000000 + 3720}]


REQUESTS = [
    {"id": i, "time": f"2026-11-18 1{i}:2{i%9}:11", "model": m, "status": s, "tokens": t, "latency_ms": l, "key_hint": "gw-…7f2", "endpoint": e}
    for i, (m, s, t, l, e) in enumerate([
        ("claude-sonnet-4-5", 200, 4821, 934, "/v1/messages"),
        ("gpt-5.2", 200, 12045, 1822, "/v1/chat/completions"),
        ("deepseek-v4", 429, 512, 96, "/v1/messages"),
        ("claude-sonnet-4-5", 200, 2233, 611, "/v1/messages"),
        ("gemini-3-pro", 500, 88, 240, "/v1/responses"),
        ("claude-sonnet-4-5", 200, 9877, 1401, "/v1/messages"),
    ] * 5)
]


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/verify", "/meta"):
            return self._send(json.dumps({"ok": True, "version": "2.7.49", "gateway_key": True}))
        if path.startswith("/admin/api/"):
            name = path[len("/admin/api/"):]
            if name == "accounts":
                stats = {"total": len(ACCOUNTS), "active": sum(1 for a in ACCOUNTS if a["status"] == "active"),
                         "exhausted": sum(1 for a in ACCOUNTS if a["status"] == "exhausted"),
                         "cooling": sum(1 for a in ACCOUNTS if a["status"] == "cooling"), "disabled": 0,
                         "calls": 3421, "fail": 47}
                for a in ACCOUNTS:
                    a["use_count"] = 842; a["fail_count"] = 6
                return self._send(json.dumps({"accounts": ACCOUNTS, "stats": stats}))
            if name == "monitoring":
                now_ms = 1764000000000
                entries = []
                models = ["claude-sonnet-4-5", "gpt-5.2", "glm-5.3-flash"]
                for i in range(40):
                    ok = i % 7 != 0
                    entries.append({
                        "ts": now_ms - i * 61000,
                        "account": ["briarlow17", "trial-mvxmwyyg", "sable.ashby60"][i % 3],
                        "model": models[i % 3],
                        "ok": True if ok else False,
                        "status": 200 if ok else (429 if i % 5 == 0 else 503),
                        "input_tokens": 1200 + i * 37, "output_tokens": 3400 + i * 91,
                        "ms_ttft": 420 + i * 13, "ms_total": 900 + i * 51,
                        "stream": i % 3 == 0,
                        "endpoint": "/v1/messages" if i % 2 == 0 else "/v1/chat/completions",
                        "preview": "Sample completion text for preview…",
                        "error": None if ok else "upstream 429 rate limited",
                    })
                return self._send(json.dumps({"stats": {"persist": False, "keep": 500}, "entries": entries, "inflight": 2}))
            if name == "settings":
                return self._send(json.dumps({"gateway_key_masked": "gw-…7f2", "admin_password_set": True, "refresh_interval": 300}))
            return self._send(json.dumps({}))
        # static
        rel = path.lstrip("/")
        if rel.startswith("admin/"):
            rel = rel[len("admin/"):]
        if not rel or rel in ("admin", ""):
            rel = "login.html"
        f = ROOT / rel
        if not f.exists() and not f.suffix:
            f = ROOT / (rel + ".html")
        if not f.exists():
            return self._send("not found", 404, "text/plain")
        ctype = "text/html" if f.suffix == ".html" else "text/css" if f.suffix == ".css" else "application/javascript" if f.suffix == ".js" else "text/plain"
        return self._send(f.read_bytes(), 200, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    do_POST = do_GET

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8932), Handler).serve_forever()
