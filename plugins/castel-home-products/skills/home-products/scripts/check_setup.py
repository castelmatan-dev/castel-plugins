#!/usr/bin/env python3
"""First-run setup check for the home-products skill.

Standard library only (Python 3.8+). Checks what can be checked locally and
records the user's choices. Connectors (Google Drive, Gmail, web search) are
not visible to a script: the agent checks them from its own tool list.

Commands (JSON on stdout):
  check                         is setup done? python version, config path, saved choices
  save --root NAME [--gmail yes|no] [--lang he|en] [--vector on|off]
                                record the choices after the user approved them
                                (--vector: extended mode with the products vector store; off by default)

The config lives outside the plugin folder, so plugin updates never erase it:
  $HOME_PRODUCTS_CONFIG, else ~/.config/home-products/setup.json
No keys, tokens or personal data are stored here: only the Drive folder name and options.
"""

import argparse
import datetime as dt
import json
import os
import sys

MIN_PY = (3, 8)


def config_path():
    return os.environ.get("HOME_PRODUCTS_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".config", "home-products", "setup.json")


def read_config(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def cmd_check(_args):
    path = config_path()
    cfg = read_config(path)
    py_ok = sys.version_info[:2] >= MIN_PY
    print(json.dumps({
        "setup_done": bool(cfg and cfg.get("root")),
        "config_path": path,
        "config": cfg,
        "python": {"ok": py_ok, "version": "{}.{}.{}".format(*sys.version_info[:3]),
                   "required": "{}.{}+".format(*MIN_PY)},
        "keys_needed": [],
        "check_yourself": ["google_drive_connector", "gmail_connector (optional)", "web_search"],
    }, ensure_ascii=False, indent=2))


def safe_target(path):
    """Write only to a .json file that is new or is already this skill's own config.

    The path can come from an environment variable, so never overwrite an unrelated file.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not path.endswith(".json") or os.path.islink(path):
        sys.exit("refusing to write config: {} (must be a regular .json file)".format(path))
    if os.path.exists(path):
        cfg = read_config(path)
        if not (isinstance(cfg, dict) and cfg.get("schema") == 1 and "root" in cfg):
            sys.exit("refusing to overwrite {}: it is not a home-products config".format(path))
    return path


def cmd_save(args):
    path = safe_target(config_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = read_config(path) or {}
    cfg.update({"root": args.root, "gmail": args.gmail, "lang": args.lang,
                "setup_date": dt.date.today().isoformat(), "schema": 1})
    if args.vector is not None:
        cfg["vector"] = args.vector
    cfg.setdefault("vector", "off")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(json.dumps({"saved": True, "config_path": path, "config": cfg}, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sp = sub.add_parser("save")
    sp.add_argument("--root", required=True)
    sp.add_argument("--gmail", choices=("yes", "no"), default="yes")
    sp.add_argument("--lang", choices=("he", "en"), default="he")
    sp.add_argument("--vector", choices=("on", "off"))
    args = p.parse_args()
    {"check": cmd_check, "save": cmd_save}[args.cmd](args)


if __name__ == "__main__":
    main()
