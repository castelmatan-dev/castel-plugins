#!/usr/bin/env python3
"""Household products list: dates, warranty status, duplicates, search.

Standard library only (Python 3.8+). Works on a local copy of the
products list JSON: {"report": ..., "products": [ {...}, ... ]}.

Commands (all output is JSON on stdout; errors go to stderr):
  init     --file F                         create an empty list if F does not exist
  validate --file F                         check structure and field formats
  find     --file F QUERY                   flexible search (name, brand, model, serial, aliases, id)
  status   --file F [--id ID] [--today D]   warranty status and days left (incl. split parts)
  add      --file F --record R [--update]   add (or update) a record; computes warranty dates
  calc     --start D --months N [--today D] warranty end date + status, before any record exists
                                            (use it for the confirmation card, never compute by hand)

Exit codes: 0 ok · 1 error · 2 duplicate (add without --update) · 3 not found
"""

import argparse
import calendar
import datetime as dt
import json
import os
import re
import sys
import unicodedata

EXPIRING_DAYS = 60
REQUIRED = ("id", "name_he")
DATE_FIELDS = ("purchase_date", "delivery_date", "warranty_start", "warranty_until")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEARCH_FIELDS = ("id", "name_he", "brand", "model", "vendor", "serial_no")
WARRANTY_SOURCES = ("document", "official_web", "user")
# Personal identifiers that must never be stored inside a saved link.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PERSONAL_URL_PARAMS = re.compile(r"[?&#](authuser|email|user|login_hint)=", re.I)


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def parse_date(value, field):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        fail("{}: expected YYYY-MM-DD, got {!r}".format(field, value))


def is_date(value):
    try:
        dt.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def is_months(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def add_months(d, months):
    """Calendar month arithmetic; clamps to month end (Jan 31 + 1 -> Feb 28/29)."""
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def load(path):
    if not os.path.exists(path):
        fail("file not found: {} (run init first)".format(path), 3)
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            fail("invalid JSON in {}: {}".format(path, e))
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        fail('expected an object with a "products" list')
    return data


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def url_problems(key, value):
    if not isinstance(value, str):
        return []
    if EMAIL_RE.search(value) or PERSONAL_URL_PARAMS.search(value):
        return ["{}: link contains a personal identifier (email / authuser); "
                "store it without that part".format(key)]
    return []


def problems(rec, strict=True):
    """Field errors. strict=False (validate of a stored list) tolerates legacy free-text
    warranty_source written by older versions; new records (add) are always strict."""
    errs = []
    for k in REQUIRED:
        if not rec.get(k):
            errs.append("missing required field: " + k)
    if rec.get("id") and not SLUG_RE.match(str(rec["id"])):
        errs.append("id must be lowercase-with-hyphens: {!r}".format(rec["id"]))
    for k in DATE_FIELDS:
        if rec.get(k) and not is_date(rec[k]):
            errs.append("{}: expected YYYY-MM-DD, got {!r}".format(k, rec[k]))
    wm = rec.get("warranty_months")
    if wm is not None and not is_months(wm):
        errs.append("warranty_months must be a non-negative integer")
    price = rec.get("price_ils")
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
        errs.append("price_ils must be a non-negative number")
    parts = rec.get("warranty_parts")
    if parts is not None:
        if not isinstance(parts, list):
            errs.append("warranty_parts must be a list")
        else:
            for i, p in enumerate(parts):
                where = "warranty_parts[{}]".format(i)
                if not isinstance(p, dict) or not p.get("part"):
                    errs.append(where + ": needs a 'part' name")
                    continue
                if p.get("months") is None and not p.get("until"):
                    errs.append(where + ": needs 'months' or 'until'")
                if p.get("months") is not None and not is_months(p["months"]):
                    errs.append(where + ": months must be a non-negative integer")
                if p.get("until") and not is_date(p["until"]):
                    errs.append(where + ": until must be YYYY-MM-DD")
    src = rec.get("warranty_source")
    if src is not None and src not in WARRANTY_SOURCES and strict:
        errs.append("warranty_source must be one of: " + ", ".join(WARRANTY_SOURCES))
    if src == "official_web" and not rec.get("warranty_source_url"):
        errs.append("warranty_source official_web needs warranty_source_url")
    for k, v in rec.items():
        if k.endswith("_url"):
            errs += url_problems(k, v)
    return errs


def warranty_start(rec):
    return rec.get("warranty_start") or rec.get("purchase_date")


def with_warranty_until(rec):
    """Fill warranty_until (and each part's until) from the start date + months, unless set explicitly.

    The start date is warranty_start if given (e.g. delivery date), else purchase_date.
    """
    rec = dict(rec)
    start = warranty_start(rec)
    if not rec.get("warranty_until") and start and rec.get("warranty_months") is not None:
        rec["warranty_until"] = add_months(parse_date(start, "warranty_start"), rec["warranty_months"]).isoformat()
    if isinstance(rec.get("warranty_parts"), list):
        parts = []
        for p in rec["warranty_parts"]:
            p = dict(p)
            if not p.get("until") and start and p.get("months") is not None:
                p["until"] = add_months(parse_date(start, "warranty_start"), p["months"]).isoformat()
            parts.append(p)
        rec["warranty_parts"] = parts
    return rec


def status_of(until, today):
    if not until:
        return {"status": "unknown", "days_left": None, "warranty_until": None}
    days = (parse_date(until, "warranty_until") - today).days
    if days < 0:
        st = "expired"
    elif days < EXPIRING_DAYS:
        st = "expiring"
    else:
        st = "valid"
    return {"status": st, "days_left": days, "warranty_until": until}


def warranty_status(rec, today):
    """Overall status from warranty_until; with no main term, the latest-ending part decides."""
    parts = [dict(p, **status_of(p.get("until"), today)) for p in rec.get("warranty_parts") or []]
    main = status_of(rec.get("warranty_until"), today)
    if main["status"] == "unknown" and parts:
        dated = [p for p in parts if p.get("until")]
        if dated:
            main = status_of(max(p["until"] for p in dated), today)
    if parts:
        main["parts"] = [{k: p.get(k) for k in ("part", "months", "until", "status", "days_left", "note") if p.get(k) is not None}
                         for p in parts]
    return main


def norm(s):
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub("[\"'`׳״’]", "", s)  # quotes, geresh, gershayim
    return re.sub(r"[\s\-_/.]+", " ", s).strip()


def score(rec, query):
    q = norm(query)
    if not q:
        return 0
    hay = [norm(rec.get(k, "")) for k in SEARCH_FIELDS]
    hay += [norm(a) for a in rec.get("aliases", []) or []]
    hay = [h for h in hay if h]
    if q in hay:
        return 100
    best = 0
    for h in hay:
        if q in h or h in q:
            best = max(best, 80)
    tokens = q.split()
    joined = " ".join(hay)
    hits = sum(1 for t in tokens if t in joined)
    if tokens and hits:
        best = max(best, int(60 * hits / len(tokens)))
    return best


def is_duplicate(a, b):
    if a.get("id") and a.get("id") == b.get("id"):
        return True
    if a.get("serial_no") and norm(a["serial_no"]) == norm(b.get("serial_no", "")):
        return True
    return bool(
        a.get("vendor") and a.get("invoice_no")
        and norm(a["vendor"]) == norm(b.get("vendor", ""))
        and norm(a["invoice_no"]) == norm(b.get("invoice_no", ""))
    )


def cmd_init(args):
    if os.path.exists(args.file):
        out({"created": False, "file": args.file})
        return
    save(args.file, {"report": None, "products": []})
    out({"created": True, "file": args.file})


def cmd_validate(args):
    data = load(args.file)
    report, warnings, ids = [], [], set()
    for i, rec in enumerate(data["products"]):
        errs = problems(rec, strict=False)
        src = rec.get("warranty_source")
        if src is not None and src not in WARRANTY_SOURCES:
            warnings.append({"index": i, "id": rec.get("id"),
                             "warning": "legacy free-text warranty_source (older version); left as is"})
        if rec.get("id") in ids:
            errs.append("duplicate id")
        ids.add(rec.get("id"))
        if errs:
            report.append({"index": i, "id": rec.get("id"), "errors": errs})
    out({"ok": not report, "count": len(data["products"]), "problems": report, "warnings": warnings})
    if report:
        sys.exit(1)


def cmd_find(args):
    data = load(args.file)
    scored = [(score(r, args.query), r) for r in data["products"]]
    hits = [dict(r, _score=s) for s, r in sorted(scored, key=lambda x: -x[0]) if s >= 40]
    out({"query": args.query, "matches": hits})
    if not hits:
        sys.exit(3)


def cmd_status(args):
    data = load(args.file)
    today = parse_date(args.today, "--today") if args.today else dt.date.today()
    recs = data["products"]
    if args.id:
        recs = [r for r in recs if r.get("id") == args.id]
        if not recs:
            fail("no product with id {!r}".format(args.id), 3)
    rows = []
    for r in recs:
        row = {"id": r.get("id"), "name_he": r.get("name_he"), "brand": r.get("brand"),
               "model": r.get("model"), "price_ils": r.get("price_ils"),
               "purchase_date": r.get("purchase_date"), "warranty_start": warranty_start(r),
               "warranty_source": r.get("warranty_source"),
               "warranty_source_url": r.get("warranty_source_url"),
               "status_item": r.get("status", "active")}
        row.update(warranty_status(with_warranty_until(r), today))
        rows.append(row)
    rows.sort(key=lambda x: (x["days_left"] is None, x["days_left"] if x["days_left"] is not None else 0))
    out({"today": today.isoformat(), "products": rows})


def cmd_add(args):
    data = load(args.file)
    with open(args.record, encoding="utf-8") as f:
        try:
            rec = json.load(f)
        except json.JSONDecodeError as e:
            fail("invalid JSON in record: {}".format(e))
    if not isinstance(rec, dict):
        fail("record must be a JSON object")
    errs = problems(rec)
    if errs:
        fail("record is invalid:\n- " + "\n- ".join(errs))
    rec = with_warranty_until(rec)
    rec.setdefault("status", "active")
    dup_idx = [i for i, r in enumerate(data["products"]) if is_duplicate(rec, r)]
    if dup_idx and not args.update:
        out({"added": False, "duplicate_of": data["products"][dup_idx[0]]})
        sys.exit(2)
    if dup_idx:
        i = dup_idx[0]
        merged = dict(data["products"][i])
        merged.update(rec)
        data["products"][i] = merged
        action, final = "updated", merged
    else:
        data["products"].append(rec)
        action, final = "added", rec
    save(args.file, data)
    out({action: True, "record": final})


def cmd_calc(args):
    start = parse_date(args.start, "--start")
    if args.months < 0:
        fail("--months must be non-negative")
    today = parse_date(args.today, "--today") if args.today else dt.date.today()
    until = add_months(start, args.months).isoformat()
    res = {"start": start.isoformat(), "months": args.months, "today": today.isoformat()}
    res.update(status_of(until, today))
    out(res)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "validate", "find", "status", "add", "calc"):
        sp = sub.add_parser(name)
        if name == "calc":
            sp.add_argument("--start", required=True)
            sp.add_argument("--months", required=True, type=int)
            sp.add_argument("--today")
            continue
        sp.add_argument("--file", required=True)
        if name == "find":
            sp.add_argument("query")
        if name == "status":
            sp.add_argument("--id")
            sp.add_argument("--today")
        if name == "add":
            sp.add_argument("--record", required=True)
            sp.add_argument("--update", action="store_true")
    args = p.parse_args()
    {"init": cmd_init, "validate": cmd_validate, "find": cmd_find,
     "status": cmd_status, "add": cmd_add, "calc": cmd_calc}[args.cmd](args)


if __name__ == "__main__":
    main()
