#!/usr/bin/env python3
"""Create the agent folder: config.json + a copy of the SOUL.md template.

CLI-first: pass --token / --chat / --thread for non-interactive use (e.g.
from another agent's BASH call). When running interactively without --chat,
discovers chat_id (and thread_id) by polling Telegram and asking the user
to send a message from the chat they want the bot to serve.

Fails if config.json already exists — delete it to reconfigure.

Usage:
  python setup.py --token 123:abc --chat -1001234567 --thread 42
  python setup.py --token 123:abc   # interactive chat-id discovery
  python setup.py                   # fully interactive
"""
import argparse
import json
import os
import pathlib
import shlex
import shutil
import sys

import requests


def assert_bot_settings(token, base="https://api.telegram.org"):
    r = requests.get(f"{base}/bot{token}/getMe", timeout=10)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"getMe failed: {data}")
    bot = data["result"]
    issues = []
    if not bot.get("can_join_groups"):
        issues.append("In @BotFather: /setjoingroups → Enable.")
    if not bot.get("can_read_all_group_messages"):
        issues.append("In @BotFather: /setprivacy → Disable (privacy mode must be OFF).")
    if issues:
        for line in issues:
            print(f"FIX: {line}")
        sys.exit("bot settings need adjustment; rerun after fixing")
    print(f"bot @{bot.get('username')} settings ok")


def discover_chat(token, base="https://api.telegram.org"):
    print("Send any message to your bot from the chat/topic you want it to serve (Ctrl-C to cancel)...")
    offset = 0
    while True:
        try:
            r = requests.post(f"{base}/bot{token}/getUpdates",
                              data={"offset": offset, "timeout": 25}, timeout=45)
        except KeyboardInterrupt:
            sys.exit("aborted")
        for u in r.json().get("result") or []:
            msg = u.get("message") or {}
            cid = (msg.get("chat") or {}).get("id")
            if cid:
                tid = msg.get("message_thread_id")
                print(f"detected chat_id={cid}" + (f" thread_id={tid}" if tid is not None else ""))
                return str(cid), str(tid) if tid is not None else None
            offset = u["update_id"] + 1


p = argparse.ArgumentParser()
p.add_argument("dir", nargs="?", default="agent", help="agent state folder (default: agent)")
p.add_argument("--token")
p.add_argument("--chat")
p.add_argument("--thread", help='Telegram thread_id (forum topic).')
p.add_argument("--api-key", dest="api_key", help='LLM provider API key (Moonshot, OpenAI, etc.)')
p.add_argument("--telegram-api-base")
p.add_argument("--api-base")
p.add_argument("--model")
p.add_argument("--provider")
p.add_argument("--reasoning-effort")
p.add_argument("--subconscious", action="store_true", help="also install opt/subconscious as a sibling agent dir")
p.add_argument("--systemd", action="store_true", help="also emit attobot.service + install instructions")
args = p.parse_args()

self_dir = pathlib.Path(args.dir).resolve()
config_path = self_dir / "config.json"
sub = self_dir.parent / "subconscious"
if config_path.exists():
    sys.exit(f"{config_path} already exists. Delete it to reconfigure.")
if args.subconscious and (sub == self_dir or sub.exists()):
    sys.exit(f"{sub} already exists or is the primary directory. Choose a separate primary and unused sibling.")
if args.systemd and pathlib.Path("attobot.service").exists():
    sys.exit("attobot.service already exists. Move it aside to regenerate.")

cfg = {key: getattr(args, key) for key in ("telegram_api_base", "api_base", "model", "provider", "reasoning_effort")
       if getattr(args, key) is not None}
if args.token is not None:
    cfg["telegram_token"] = args.token
if args.chat is not None:
    cfg["telegram_chat_id"] = args.chat
if args.thread:
    cfg["telegram_thread_id"] = args.thread
if args.api_key is not None:
    cfg["api_key"] = args.api_key

interactive = sys.stdin.isatty()

if interactive and not cfg.get("telegram_token"):
    cfg["telegram_token"] = input("Telegram bot token (get it from @BotFather): ").strip()
if cfg.get("telegram_token"):
    assert_bot_settings(cfg["telegram_token"], cfg.get("telegram_api_base", "https://api.telegram.org"))
if interactive and not cfg.get("telegram_chat_id") and cfg.get("telegram_token"):
    cid, tid = discover_chat(cfg["telegram_token"], cfg.get("telegram_api_base", "https://api.telegram.org"))
    cfg["telegram_chat_id"] = cid
    if tid is not None and "telegram_thread_id" not in cfg:
        cfg["telegram_thread_id"] = tid
if interactive and not cfg.get("api_key") and not os.environ.get("ATTOBOT_API_KEY"):
    cfg["api_key"] = input("LLM provider API key (Moonshot by default): ").strip()

for key in ("telegram_token", "telegram_chat_id", "api_key"):
    if not cfg.get(key) and not (key == "api_key" and os.environ.get("ATTOBOT_API_KEY")):
        sys.exit(f"missing required field: {key}")

def write_config(path, config):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(config, f, indent=2)


self_dir.mkdir(parents=True, exist_ok=True)
write_config(config_path, cfg)
print(f"wrote {config_path}")

soul = self_dir / "SOUL.md"
if not soul.exists():
    shutil.copy(pathlib.Path(__file__).parent / "SOUL.md", soul)
    print(f"wrote {soul}")

if args.subconscious:
    shutil.copytree(pathlib.Path(__file__).parent / "opt" / "subconscious", sub)
    write_config(sub / "config.json", {**{key: value for key, value in cfg.items() if not key.startswith("telegram")},
                                      "primary_dir": str(self_dir), "opt": ["tools/nudge", "tools/stash_messages"]})
    for path in (sub / "triggers").glob("*.json"):
        job = json.loads(path.read_text())
        if "watch" in job:
            job["watch"] = str(self_dir / "messages.jsonl")
        for old, new in (("agent/messages.jsonl", self_dir / "messages.jsonl"),
                         ("subconscious/.primary_snap", sub / ".primary_snap"),
                         ("subconscious/messages.jsonl", sub / "messages.jsonl")):
            job["cmd"] = job["cmd"].replace(old, shlex.quote(str(new)))
        path.write_text(json.dumps(job))
    sub_soul = sub / "SOUL.md"
    sub_soul.write_text(sub_soul.read_text().replace("`agent/`", f"`{self_dir}/`").replace("`subconscious/", f"`{sub}/"))
    print(f"wrote {sub}/ (run both: python agent.py {shlex.quote(str(self_dir))} {shlex.quote(str(sub))})")

if args.systemd:
    def unit_quote(value):
        return json.dumps(str(value).replace("%", "%%"), ensure_ascii=False)

    workdir = str(pathlib.Path.cwd().resolve()).replace("%", "%%")
    command = " ".join(unit_quote(value).replace("$", "$$") for value in [sys.executable, pathlib.Path(__file__).resolve().with_name("agent.py"), self_dir] + ([sub] if args.subconscious else []))
    writable = " ".join(unit_quote(path) for path in dict.fromkeys([pathlib.Path.cwd().resolve(), self_dir] + ([sub] if args.subconscious else [])))
    unit = pathlib.Path("attobot.service")
    unit.write_text(f"""[Unit]
Description=attobot
After=network.target

[Service]
Type=simple
WorkingDirectory={workdir}
ExecStart={command}
Restart=always
RestartPreventExitStatus=78
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={writable}

[Install]
WantedBy=default.target
""")
    print(f"wrote {unit}")
    print(f"\nInstall (user service, no sudo):")
    print(f"  mkdir -p ~/.config/systemd/user")
    print(f"  cp {unit} ~/.config/systemd/user/")
    print(f"  systemctl --user daemon-reload")
    print(f"  loginctl enable-linger $USER          # so it survives logout")
    print(f"  systemctl --user enable --now attobot")
    print(f"  journalctl --user -u attobot -f")
