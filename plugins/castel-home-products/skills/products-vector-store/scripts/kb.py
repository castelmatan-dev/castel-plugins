#!/usr/bin/env python3
"""Thin client for the products vector store workflows in n8n.

Standard library only (Python 3.8+). Reads its two settings from a secrets
file the user fills in by hand, and never prints the key:
  $KB_ENV_FILE, else ~/.config/castel-kb/.env.local
    KB_N8N_URL=https://<instance>/webhook
    KB_N8N_KEY=<shared header key>

Commands (JSON on stdout; errors on stderr):
  check
  list   [--product-id ID]
  store  --product-id ID --doc-kind KIND --drive-file-id FID --title T --source-url U [--lang L]
  status --upload-id UID [--wait SECONDS]
  search --query Q [--product-id ID] [--doc-kind KIND] [--k N]

Exit codes: 0 ok · 1 error · 2 not configured / auth · 3 not found · 4 already running (409)
The only network destination is KB_N8N_URL. Nothing is sent anywhere else.
"""

import argparse
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DOC_KINDS = ("manual", "warranty", "safety", "usage_summary")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FINAL = ("active", "failed")
TIMEOUT = 30
_SECRET = {"key": None}


def env_path():
    return os.environ.get("KB_ENV_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "castel-kb", ".env.local")


def redact(text):
    key = _SECRET["key"]
    text = str(text)
    return text.replace(key, "***") if key else text


def out(obj):
    print(redact(json.dumps(obj, ensure_ascii=False, indent=2)))


def fail(msg, code=1):
    print(redact(msg), file=sys.stderr)
    sys.exit(code)


def read_values(path):
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m:
                v = m.group(2)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                values[m.group(1)] = v
    return values


def sync_example_names(path, names):
    """Lesson-13 pattern: keep .env.example next to the secrets file listing the NAMES only."""
    example = os.path.join(os.path.dirname(path), ".env.example")
    header = []
    if os.path.isfile(example):
        with open(example, encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    break
                if line.startswith("#"):
                    header.append(line.rstrip("\n"))
    new = "\n".join(header + ["{}=".format(n) for n in names]) + "\n"
    old = open(example, encoding="utf-8").read() if os.path.isfile(example) else ""
    if new != old:
        with open(example, "w", encoding="utf-8") as f:
            f.write(new)
        return True
    return False


def describe(values):
    """Say whether each setting is filled and well-formed, without revealing any value."""
    url, key = values.get("KB_N8N_URL", ""), values.get("KB_N8N_KEY", "")
    return {
        "KB_N8N_URL": "missing" if not url else ("ok" if url.startswith("https://") else "set, but must start with https://"),
        "KB_N8N_KEY": "missing" if not key else (
            "ok (64 hex chars)" if re.fullmatch(r"[0-9a-f]{64}", key)
            else "set, {} chars, not 64 hex: check for spaces or a partial paste".format(len(key))),
    }


def load_settings():
    """Read KB_N8N_URL and KB_N8N_KEY. Values never leave this process except in the request itself."""
    path = env_path()
    if not os.path.isfile(path):
        fail("not configured: secrets file missing ({}). See references/workflows.md, 'הגדרה'.".format(path), 2)
    values = read_values(path)
    missing = [k for k in ("KB_N8N_URL", "KB_N8N_KEY") if not values.get(k)]
    if missing:
        fail("not configured: {} empty or missing in {}".format(", ".join(missing), path), 2)
    url = values["KB_N8N_URL"].rstrip("/")
    if not url.startswith("https://"):
        fail("KB_N8N_URL must start with https://", 2)
    _SECRET["key"] = values["KB_N8N_KEY"]
    mode = os.stat(path).st_mode
    warn = bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
    return url, values["KB_N8N_KEY"], warn


def call(method, path, payload=None, query=None):
    base, key, _ = load_settings()
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-KB-Key": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body}
        return e.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        fail("vector store unavailable: {}".format(getattr(e, "reason", e)), 1)
    except ValueError:
        fail("n8n returned a non-JSON response for {}".format(path), 1)


def check_status(code, body):
    if code in (401, 403):
        fail("auth failed ({}): the key in the secrets file does not match n8n".format(code), 2)
    if code == 404:
        fail("not found: {}".format(body.get("message", "")), 3)
    if code == 409:
        fail("already running: {}".format(body.get("message", "an upload for this source is in progress")), 4)
    if code == 413:
        fail("too large: {}".format(body.get("message", "document exceeds the page limit")), 1)
    if code >= 400:
        fail("n8n error {}: {}".format(code, body.get("message", body)), 1)
    return body


def slug(value, field):
    if not SLUG_RE.match(value or ""):
        fail("{} must be lowercase-with-hyphens, got {!r}".format(field, value))
    return value


def cmd_check(_a):
    path = env_path()
    if not os.path.isfile(path):
        fail("not configured: secrets file missing ({}). See references/workflows.md, 'הגדרה'.".format(path), 2)
    values = read_values(path)
    status = describe(values)
    synced = sync_example_names(path, list(values.keys()))
    if any(v != "ok" and not v.startswith("ok ") for v in status.values()):
        out({"configured": False, "secrets_file": path, "settings": status, "example_names_synced": synced})
        sys.exit(2)
    base, _key, warn = load_settings()
    res = {"configured": True, "secrets_file": path, "settings": status, "example_names_synced": synced,
           "url_host": urllib.parse.urlparse(base).netloc, "file_permissions_too_open": warn}
    code, body = call("GET", "/kb-products/list", query={"product_id": "__healthcheck__"})
    res["n8n_reachable"] = code < 500 and code not in (401, 403)
    res["auth_ok"] = code not in (401, 403)
    out(res)


def cmd_list(a):
    query = {"product_id": slug(a.product_id, "--product-id") if a.product_id else None}
    out(check_status(*call("GET", "/kb-products/list", query=query)))


def cmd_store(a):
    if a.doc_kind not in DOC_KINDS:
        fail("--doc-kind must be one of: " + ", ".join(DOC_KINDS))
    if not a.source_url.startswith("https://drive.google.com/") and not a.source_url.startswith("https://docs.google.com/"):
        fail("--source-url must be a Google Drive link (products folders only)")
    payload = {"product_id": slug(a.product_id, "--product-id"), "doc_kind": a.doc_kind,
               "drive_file_id": a.drive_file_id, "source_title": a.title, "source_url": a.source_url}
    if a.lang:
        payload["lang"] = a.lang
    out(check_status(*call("POST", "/kb-products/store", payload=payload)))


def cmd_status(a):
    deadline = time.time() + max(0, a.wait)
    while True:
        body = check_status(*call("GET", "/kb-products/status", query={"upload_id": a.upload_id}))
        if body.get("status") in FINAL or time.time() >= deadline:
            body["final"] = body.get("status") in FINAL
            out(body)
            return
        time.sleep(5)


def cmd_search(a):
    payload = {"query": a.query, "k": max(1, min(a.k, 20))}
    if a.product_id:
        payload["product_id"] = slug(a.product_id, "--product-id")
    if a.doc_kind:
        if a.doc_kind not in DOC_KINDS:
            fail("--doc-kind must be one of: " + ", ".join(DOC_KINDS))
        payload["doc_kind"] = a.doc_kind
    body = check_status(*call("POST", "/kb-products/search", payload=payload))
    if not body.get("chunks"):
        out({"chunks": [], "found": False})
        sys.exit(3)
    out(dict(body, found=True))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sp = sub.add_parser("list")
    sp.add_argument("--product-id")
    sp = sub.add_parser("store")
    for name in ("--product-id", "--doc-kind", "--drive-file-id", "--title", "--source-url"):
        sp.add_argument(name, required=True)
    sp.add_argument("--lang")
    sp = sub.add_parser("status")
    sp.add_argument("--upload-id", required=True)
    sp.add_argument("--wait", type=int, default=0)
    sp = sub.add_parser("search")
    sp.add_argument("--query", required=True)
    sp.add_argument("--product-id")
    sp.add_argument("--doc-kind")
    sp.add_argument("--k", type=int, default=6)
    a = p.parse_args()
    {"check": cmd_check, "list": cmd_list, "store": cmd_store,
     "status": cmd_status, "search": cmd_search}[a.cmd](a)


if __name__ == "__main__":
    main()
