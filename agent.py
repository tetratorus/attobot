#!/usr/bin/env python3
"""Minimal agent."""
import base64, collections, fcntl, hashlib, importlib.util, json, mimetypes, os, pathlib, pwd, re, requests, shutil, subprocess, sys, threading, time
sys.modules.setdefault("agent", sys.modules[__name__])

AGENT_DIR = sys.argv[1] if len(sys.argv) > 1 else "agent"
BLOB_DIR = f"{AGENT_DIR}/blobs"

CFG = { # defaults
    "model": "deepseek-v4-pro",
    "api_base": "https://api.deepseek.com/v1",
    "telegram_api_base": "https://api.telegram.org",
    "temperature": 1.0,
    "reasoning_effort": "medium",
    "context_tokens": 1000000,
    "multimodal_support": False,
    "provider": "",
    "opt": [],
    "life_tail": 50,
    "memory_limit": 10000,
    "tool_timeout": 30,
    "trigger_tick": 30,
    "inbox_tick": 2,
    "inbox_preview": 1000,
    "chat_msg_max": 4000,
    "tool_output_limit": 5000,
}

os.makedirs(BLOB_DIR, exist_ok=True)
os.makedirs(f"{AGENT_DIR}/memory", exist_ok=True)

SYS_MSG = re.compile(r"<system-message>.*</system-message>", re.DOTALL)

def _unwrap_sys_msg(content):
    if SYS_MSG.fullmatch(content):
        return content[len("<system-message>"):-len("</system-message>")]
    return content

def life(event, agent_dir=AGENT_DIR):
    if not isinstance(event, str):
        event = repr(event)
    event = event.replace("\n", "\\n")
    if len(event) > 200:
        event = f"{event[:100]}…{event[-100:]}"
    with open(f"{agent_dir}/LIFE.md", "a") as f:
        f.write(f"[{time.strftime('%Y%m%dT%H%M%S')}] {event}\n")

def _msg(line):
    try: m = json.loads(line)
    except Exception: return {}
    return m if isinstance(m, dict) else {}

def load_messages(): # Loads and heals messages.jsonl by dropping malformed lines and invalid tool-call blocks.
    with open(f"{AGENT_DIR}/messages.jsonl", "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        msgs = []
        for line in f.read().splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(m, dict) and "role" in m:
                msgs.append(m)
        out, i = [], 0
        while i < len(msgs):
            m = msgs[i]
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                if m.get("role") != "tool":
                    out.append(m)
                i += 1
                continue
            j = i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                j += 1
            need = [tc.get("id") for tc in m["tool_calls"]]
            got = [t.get("tool_call_id") for t in msgs[i + 1:j]]
            if need and all(cid in got for cid in need):
                out.extend(msgs[i:j])
            i = j
        f.seek(0); f.truncate()
        f.write("".join(json.dumps(m) + "\n" for m in out))
        return out

def _msg_summary(m):
    role = m.get("role", "?")
    if role == "assistant" and m.get("tool_calls"):
        calls = ", ".join(
            f"{tc['function']['name']}({tc['function'].get('arguments', '')})"
            for tc in m["tool_calls"]
        )
        content = m.get("content") or ""
        return f"assistant {calls}" + (f" {content}" if content else "")
    if role == "tool":
        return f"tool {m.get('tool_call_id', '?')}: {m.get('content', '')}"
    return f"{role}: {m.get('content', '')}"

def append_msg(m, agent_dir=AGENT_DIR):
    msgs = m if isinstance(m, list) else [m]
    with open(f"{agent_dir}/messages.jsonl", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")
    for msg in msgs:
        life(_msg_summary(msg), agent_dir)

def _default_chat(messages, tools):
    body = {
        "model": CFG["model"],
        "messages": messages,
        "temperature": CFG["temperature"],
    }
    if CFG["reasoning_effort"]:
        body["reasoning_effort"] = CFG["reasoning_effort"]
    if tools:
        body["tools"] = tools
    r = requests.post(f"{CFG['api_base']}/chat/completions",
        headers={"Authorization": f"Bearer {CFG['api_key']}"},
        json=body, timeout=120)
    data = r.json()
    if "choices" not in data:
        if 400 <= r.status_code < 500 and r.status_code != 429:
            sys.exit(f"fatal llm error {r.status_code}: {data}")
        raise RuntimeError(f"{r.status_code}: {data}")
    return data["choices"][0]["message"]

llm = _default_chat

def llm_w_retry(messages, tools=None):
    delay = 1
    while True:
        try:
            return llm(messages, tools)
        except Exception as e:
            append_msg({"role": "user", "content": f"<system-message>[llm retry in {delay}s] {e}</system-message>"})
            time.sleep(delay)
            delay = min(delay * 2, 900)

_TG_MEDIA = {ext: (f"send{kind}", kind.lower()) for kind, exts in {"Photo": "jpg jpeg png gif webp", "Video": "mp4 mov webm", "Voice": "ogg", "Audio": "mp3 m4a wav"}.items() for ext in exts.split()}

def _tg_send(endpoint, payload, files=None, cfg=None):
    cfg = cfg or CFG
    payload = {"chat_id": cfg["telegram_chat_id"], **payload}
    if cfg.get("telegram_thread_id"):
        payload["message_thread_id"] = cfg["telegram_thread_id"]
    base = cfg.get("telegram_api_base", "https://api.telegram.org")
    r = requests.post(f"{base}/bot{cfg['telegram_token']}/{endpoint}",
                      data=payload, files=files, timeout=60)
    data = r.json()
    if not data.get("ok"):
        life(f"[telegram send error] {data}")
        return f"telegram error: {data.get('description', data)}"
    return None

def send_text(text):
    if not CFG.get("telegram_token"):
        return "no chat configured"
    errors = [err for i in range(0, len(text), CFG["chat_msg_max"])
              if (err := _tg_send("sendMessage", {"text": text[i:i+CFG['chat_msg_max']]}))]
    return errors[0] if errors else "sent"

def send_attachment(args):
    if not CFG.get("telegram_token"):
        return "no chat configured"
    if not (path := args.get("path")):
        return "error: SEND_ATTACHMENT requires path; text-only chat replies are automatic"
    text = args.get("text", "")
    ext = pathlib.Path(path).suffix.lower().lstrip(".")
    endpoint, field = _TG_MEDIA.get(ext, ("sendDocument", "document"))
    with open(path, "rb") as f:
        err = _tg_send(endpoint, {"caption": text[:1024]}, files={field: f})
    return err or f"sent {field} {path}"

def stash(content):
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    open(f"{BLOB_DIR}/{h}", "w").write(content)
    return f"[stash {h}]"

def clip(s):
    if not isinstance(s, str) or len(s) <= CFG["tool_output_limit"]:
        return s
    h = CFG["tool_output_limit"] // 2
    return f"{s[:h]}\n... {len(s) - 2*h} chars truncated, {stash(s)} ...\n{s[-h:]}"

def read_file(args):
    path = args["path"]
    if pathlib.Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"} and CFG["multimodal_support"]:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        return [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
    return "\n".join(f"{i+1}\t{line}" for i, line in enumerate(open(path).read().splitlines()))

def write_file(args):
    path = args["path"]
    open(path, "w").write(args["content"])
    return f"wrote {path} ({len(args['content'])} chars)"

def edit_file(args):
    path, old, new = args["path"], args["old"], args["new"]
    text = open(path).read()
    count = text.count(old)
    if not args.get("replace_all") and count != 1:
        return f"error: OLD must appear exactly once in {path} (found {count}; pass replace_all to replace every occurrence)"
    if count == 0:
        return f"error: OLD not found in {path}"
    open(path, "w").write(text.replace(old, new))
    return f"edited {path}" + (f" ({count} replacements)" if args.get("replace_all") else "")

def bash(args):
    return subprocess.Popen(args["cmd"], shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def search(args):
    from ddgs import DDGS
    n = min(args.get("n", 5), 10)
    try:
        results = list(DDGS().text(args["query"], max_results=n))
        return "\n\n".join(f"{i+1}. {r['title']}\n   {r['href']}\n   {r['body']}" for i, r in enumerate(results)) or "(no results)"
    except Exception as e:
        return f"search error: {e}"

def web_fetch(args):
    from markdownify import markdownify
    url = args["url"]
    if url.startswith("http://"):
        url = "https://" + url[7:]
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        return markdownify(r.text, heading_style="ATX", strip=["script", "style"]).strip()
    except Exception as e:
        return f"fetch error: {e}"

def stash_messages(args):
    path = args.get("path") or f"{AGENT_DIR}/messages.jsonl"

    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        lines = f.read().splitlines()
    n = len(lines)

    if "start" in args and "end" in args:
        s, e = int(args["start"]) - 1, int(args["end"])
        if s < 0 or e > n or s >= e:
            return f"error: invalid range start={args['start']} end={args['end']} (file has {n} lines)"
    else:
        s, e = n // 4, (3 * n) // 4

    while s < n and _msg(lines[s]).get("role") == "tool":
        s += 1
    while e < n and _msg(lines[e]).get("role") == "tool":
        e += 1
    if s >= e:
        return "nothing safe to stash"

    target = "\n".join(lines[s:e])
    try:
        response = llm(
            [{"role": "user", "content": "Summarize this conversation segment in 1 paragraph. Be terse, factual.\n\n" + target}],
            None,
        )
        summary = (response.get("content") or "").strip()
    except Exception as exc:
        summary = f"(summary failed: {exc})"
    marker = stash(target)
    count = e - s
    placeholder = json.dumps({"role": "user", "content": f"<system-message>\n{count} lines stashed to {marker}. \nsummary: {summary}</system-message>"})

    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        content = f.read()
        if target not in content:
            return "stash failed because target range no longer in file (file was modified)"
        f.seek(0); f.truncate()
        f.write(content.replace(target, placeholder, 1))
    return f"{count} lines stashed to {marker}"

TOOLS = [
    ("SEND_ATTACHMENT", send_attachment, "Send a file to your Telegram topic (photo for images, voice for .ogg, video for .mp4, audio for .mp3/.m4a/.wav, document otherwise). `text` is an optional caption (capped at 1024 chars). For a plain text message, do NOT use this — just write a normal assistant reply.",
        {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path"]}),
    ("READ_FILE", read_file, "Read a file.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ("WRITE_FILE", write_file, "Overwrite a file.",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}),
    ("EDIT_FILE", edit_file, "Replace OLD with NEW in a file. Default: OLD must appear exactly once. Pass replace_all=true to replace every occurrence.",
        {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]}),
    ("BASH", bash, "Run a shell command.",
        {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}),
    ("SEARCH", search, "Search the web via DuckDuckGo. Returns title, URL, and snippet for up to 10 results.",
        {"type": "object", "properties": {"query": {"type": "string"}, "n": {"type": "integer"}}, "required": ["query"]}),
    ("WEB_FETCH", web_fetch, "Fetch and extract text from a URL.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
    ("STASH", lambda args: stash(args["content"]), "Save text to a blob, return [stash <hash>].",
        {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}),
    ("STASH_MESSAGES", stash_messages, "Collapse a line range of a messages.jsonl (default: your own, middle half) into one [stash <hash>] line with an LLM summary. 1-indexed, end inclusive, snapped to turn boundaries. Line numbers shift afterwards — re-read, or stash bottom-up.",
        {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}}),
]
TOOL_FNS = {n: f for n, f, _, _ in TOOLS}
TOOL_SCHEMAS = [{"type": "function", "function": {"name": n, "description": d, "parameters": p}} for n, _, d, p in TOOLS]

def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def load_agent_tools():
    for path in sorted(pathlib.Path(f"{AGENT_DIR}/tools").glob("*.py")):
        try:
            mod = _load_module(path.stem, path)
            TOOL_FNS[mod.NAME] = mod.run
            TOOL_SCHEMAS.append({"type": "function", "function": {
                "name": mod.NAME, "description": mod.DESCRIPTION, "parameters": mod.PARAMETERS}})
        except Exception as e:
            life(f"[tool load failed] {path.name}: {e}")

# ---------- background tool wrapper ----------

def bg_run(name, args, tool_call_id, tool_fn):
    bg_id = hashlib.sha256(f"{tool_call_id}{time.time()}".encode()).hexdigest()[:8]
    bg_dir = pathlib.Path(f"{AGENT_DIR}/bg")
    bg_dir.mkdir(parents=True, exist_ok=True)
    holder = {"result": None, "done": False, "pid": None}

    def work():
        try:
            r = tool_fn(args)
            if hasattr(r, "pid") and hasattr(r, "communicate"):
                holder["pid"] = r.pid
                out, _ = r.communicate()
                holder["result"] = clip(out or f"(exit {r.returncode})")
            else:
                holder["result"] = clip(r)
        except Exception as e:
            holder["result"] = f"error: {e}"
        finally:
            holder["done"] = True

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(CFG["tool_timeout"])
    if holder["done"]:
        return holder["result"]

    json_path = bg_dir / f"{bg_id}.json"
    json_path.write_text(json.dumps({
        "name": name, "tool_call_id": tool_call_id,
        "pid": holder.get("pid"), "started": time.time(),
    }))

    def emit():
        t.join()
        append_msg({"role": "user", "content": f"<system-message>[bg {bg_id} done, tc:{tool_call_id}] {holder['result']}</system-message>"})
        try: json_path.unlink()
        except Exception: pass

    threading.Thread(target=emit, daemon=True).start()
    pid_info = f" (pid {holder['pid']})" if holder.get("pid") else ""
    return f"[backgrounded bg/{bg_id}{pid_info} — see {AGENT_DIR}/bg/{bg_id}.json; kill the pid to stop it]"

# ---------- channels ----------

def start_chat():
    token = CFG["telegram_token"]
    base = CFG.get("telegram_api_base", "https://api.telegram.org")
    locked_cid = CFG["telegram_chat_id"]
    locked_tid = CFG.get("telegram_thread_id")
    poll_offset = pathlib.Path(f"{AGENT_DIR}/tg_poll.offset")

    def poll_in():
        try: offset = int(poll_offset.read_text())
        except (FileNotFoundError, ValueError): offset = 0
        while True:
            try:
                r = requests.post(f"{base}/bot{token}/getUpdates",
                                  data={"offset": offset, "timeout": 25}, timeout=45)
                updates = r.json().get("result") or []
                for u in updates:
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    msg_cid = str((msg.get("chat") or {}).get("id") or "")
                    tid_raw = msg.get("message_thread_id")
                    msg_tid = str(tid_raw) if tid_raw is not None else None
                    if msg_cid != locked_cid or msg_tid != locked_tid:
                        continue  # not our chat/topic — drop silently (no bus, no LIFE)
                    if msg.get("photo"):
                        file_id = max(msg["photo"], key=lambda p: p.get("file_size", 0))["file_id"]
                    else:
                        file_id = next((msg[k]["file_id"] for k in ("document", "voice", "video", "audio") if msg.get(k)), None)
                    body = msg.get("text") or ""
                    if file_id:
                        meta = requests.get(f"{base}/bot{token}/getFile",
                                            params={"file_id": file_id}, timeout=30).json()
                        rel = meta["result"]["file_path"]
                        blob = requests.get(f"{base}/file/bot{token}/{rel}", timeout=60).content
                        inbound = pathlib.Path(f"{AGENT_DIR}/inbound")
                        inbound.mkdir(parents=True, exist_ok=True)
                        save = inbound / f"{u['update_id']}_{rel.rsplit('/', 1)[-1]}"
                        save.write_bytes(blob)
                        caption = msg.get("caption") or ""
                        body = f"(file: {save})" + (f" {caption}" if caption else "")
                    if not body:
                        continue
                    append_msg({"role": "user", "content": f"[telegram {u['update_id']}] {body}"})
                    if mid := msg.get("message_id"):
                        try:
                            requests.post(f"{base}/bot{token}/setMessageReaction",
                                json={"chat_id": locked_cid, "message_id": mid,
                                      "reaction": [{"type":"emoji","emoji":"👀"}]}, timeout=10)
                        except Exception: pass
                if updates:
                    poll_offset.write_text(str(offset))
            except Exception as e:
                life(f"[chat error] {e}")  # transient channel hiccup — log only, don't wake
                time.sleep(5)

    threading.Thread(target=poll_in, daemon=True, name="chat-poll").start()

_trigger_queue = collections.deque()
_trigger_queue_lock = threading.Lock()

def _flush_trigger_over_idle():
    path = f"{AGENT_DIR}/messages.jsonl"
    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.read().splitlines()
            last = _msg(lines[-1]) if lines else {}
            if lines and (last.get("role") != "assistant" or last.get("tool_calls")):
                return
            with _trigger_queue_lock:
                if not _trigger_queue:
                    return
                m = _trigger_queue.popleft()
            if len(lines) >= 2 and _unwrap_sys_msg(_msg(lines[-2]).get("content") or "").startswith("[trigger "):
                lines = lines[:-2]
            lines.append(json.dumps(m))
            f.seek(0); f.truncate()
            f.write("\n".join(lines) + "\n")
    except FileNotFoundError:
        return
    life(_msg_summary(m))

def start_triggers():
    triggers_dir = pathlib.Path(f"{AGENT_DIR}/triggers")
    triggers_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = triggers_dir / "heartbeat.json"
    if not heartbeat.exists():
        heartbeat.write_text(json.dumps({"next": time.time() + 225, "repeat_s": 225, "cap": 3600, "message": "Check your latest state to see if there's work, and then pick something worthwhile to do. If there is genuinely nothing to do, reply immediately to this trigger with a simple text message; the harness will interpret this as an idle and backoff the heartbeat interval."}))

    def loop():
        while True:
            try:
                ts = time.time()
                for f in triggers_dir.glob("*.json"):
                    try: job = json.loads(f.read_text())
                    except Exception: continue
                    if job.get("next", 0 if "watch" in job or "cmd" in job else float("inf")) > ts: continue
                    if w := job.get("watch"):  # fire on file change; repeat_s = cooldown
                        try: h = hashlib.sha256(open(w, "rb").read()).hexdigest()
                        except OSError: continue
                        if h == job.get("seen"): continue
                        job["seen"] = h
                    msg = job.get("message", "")
                    if c := job.get("cmd"):  # computed condition: fire with stdout; no output = no fire
                        try:
                            msg = clip(subprocess.run(c, shell=True, capture_output=True, text=True,
                                                      timeout=60).stdout.strip())
                        except Exception as e:
                            msg = f"(cmd error: {e})"
                        if not msg:
                            if job.get("repeat_s"): job["next"] = ts + job["repeat_s"]
                            f.write_text(json.dumps(job))
                            continue
                    one_shot = not (job.get("repeat_s") or job.get("watch"))
                    if not one_shot:
                        if job.get("repeat_s"):
                            job["next"] = ts + job["repeat_s"]
                        f.write_text(json.dumps(job))
                    content = f"[trigger {f.stem}] {msg}"
                    if f.stem.startswith("subconscious-"):  # subconscious has no channel of its own; surface its triggers to Telegram via the primary
                        send_text(content)
                    with _trigger_queue_lock:
                        _trigger_queue.append({"role": "user", "content": f"<system-message>{content}</system-message>"})
                    if one_shot:
                        f.unlink()
            except Exception as e:
                life(f"[trigger error] {e}")  # log only, don't wake
            _flush_trigger_over_idle()
            time.sleep(CFG["trigger_tick"])

    threading.Thread(target=loop, daemon=True, name="triggers").start()

def start_inbox():
    inbox = pathlib.Path(f"{AGENT_DIR}/mail_inbox")
    inbox.mkdir(parents=True, exist_ok=True)

    def deliver(drop):
        sender = pwd.getpwuid(drop.stat().st_uid).pw_name
        text = drop.read_bytes()[:CFG["inbox_preview"] * 4].decode("utf-8", errors="replace")[:CFG["inbox_preview"]]
        append_msg({"role": "user", "content": f"[mail from {sender}] {drop.name}\n{text}"})
        send_text(f"mail from {sender}\n{text}")

    def watch():
        done = inbox / "processed"
        while True:
            try:
                done.mkdir(exist_ok=True)
                for f in sorted(inbox.iterdir()):
                    if not (f.is_file() and not f.name.startswith(".")):
                        continue
                    try:
                        deliver(f)
                        f.rename(done / f.name)  # delivered exactly once; survives restarts
                    except Exception as e:
                        life(f"[inbox deliver error] {f.name}: {e}")
                        try: f.rename(done / f.name)  # move aside so a bad file can't block the rest
                        except Exception: pass
            except Exception as e:
                life(f"[inbox error] {e}")  # log only, don't wake
            time.sleep(CFG["inbox_tick"])

    threading.Thread(target=watch, daemon=True, name="inbox-poll").start()

def _stash_directive_arg(content):
    content = _unwrap_sys_msg(content)
    marker = "STASH_MESSAGE:"
    if marker not in content:
        return None
    head, _, tail = content.partition(marker)
    head = head.strip()
    if head and not (head.startswith("[trigger ") and head.endswith("]")):
        return None
    return tail.strip()

def _exec_stash_directive(arg):
    path = f"{AGENT_DIR}/messages.jsonl"
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        msgs = [json.loads(ln) for ln in f.read().splitlines()]
        kept = [m for m in msgs if not (m.get("role") == "user" and _stash_directive_arg(m.get("content") or "") is not None)]
        f.seek(0); f.truncate()
        f.write("".join(json.dumps(m) + "\n" for m in kept))
        n = len(kept)
    p = arg.split() if arg else []
    if arg == "all" and n > 1:
        stash_messages({"start": 1, "end": n})
    elif len(p) == 2 and p[0].isdigit() and p[1].isdigit():
        stash_messages({"start": int(p[0]), "end": int(p[1])})
    else:
        stash_messages({})
    life(f"[stash directive] {arg or 'default'}")

# ---------- main loop ----------

def build_system():
    soul = open(f"{AGENT_DIR}/SOUL.md").read()
    harness = open(__file__).read()
    memory = open(f"{AGENT_DIR}/MEMORY.md").read()
    if len(memory) > CFG["memory_limit"]:
        h = CFG["memory_limit"] // 2
        memory = f"{memory[:h]}\n…\n{memory[-h:]}\n[WARNING: MEMORY.md is too large and partially omitted. Move detail into agent/memory/<name>.md files and keep one-line pointers here.]"
    sub = ""
    if AGENT_DIR != "subconscious" and os.path.isdir("subconscious"):
        sub = ("<subconscious>\nYou have a subconscious: a sibling agent in subconscious/ that reviews your stream and generates helpful system messages.</subconscious>\n\n")
    return (f"<soul>\n{soul}\n</soul>\n\n"
            f"<harness>\n{harness}\n</harness>\n\n"
            f"{sub}"
            f"<memory>\n{memory}\n</memory>")

def life_block():
    chunk = 65536
    with open(f"{AGENT_DIR}/LIFE.md", "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - chunk))
        lines = f.read().decode("utf-8", errors="replace").splitlines(keepends=True)
    if size > chunk and lines:
        lines = lines[1:]
    tail = "".join(lines[-CFG["life_tail"]:])
    earlier = size - len(tail.encode())
    return (f"<life>\n[harness] the tail of your own LIFE.md, an append-only timestamped log of your history - captures context that may have been lost due to edits to your context window.\n"
            f"[{earlier} bytes earlier]\n{tail}</life>")

def main():
    config_path = pathlib.Path(f"{AGENT_DIR}/config.json")
    if not config_path.exists():
        sys.exit(f"missing {config_path}. Run: python setup.py")
    if not pathlib.Path(f"{AGENT_DIR}/SOUL.md").exists():
        sys.exit(f"missing {AGENT_DIR}/SOUL.md — copy a soul template in")
    CFG.update(json.loads(config_path.read_text()))
    if not CFG.get("api_key"):
        sys.exit(f"{config_path} missing required field: api_key")
    if CFG.get("telegram_token") and not CFG.get("telegram_chat_id"):
        sys.exit(f"{config_path} has telegram_token but no telegram_chat_id")
    if not CFG["multimodal_support"] and "tools/ocr_image" not in CFG["opt"]:
        CFG["opt"].append("tools/ocr_image")
    if CFG["provider"] and f"providers/{CFG['provider']}" not in CFG["opt"]:
        CFG["opt"].append(f"providers/{CFG['provider']}")
    for entry in CFG["opt"]:
        src = pathlib.Path(__file__).resolve().parent / "opt" / f"{entry}.py"
        dst = pathlib.Path(f"{AGENT_DIR}/{entry}.py")
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
    load_agent_tools()
    if CFG["provider"]:
        global llm
        llm = _load_module(f"provider_{CFG['provider']}", f"{AGENT_DIR}/providers/{CFG['provider']}.py").chat
    pathlib.Path(f"{AGENT_DIR}/MEMORY.md").touch(exist_ok=True)
    pathlib.Path(f"{AGENT_DIR}/messages.jsonl").touch(exist_ok=True)
    if CFG.get("telegram_token"):  # no token → no chat channel; agent wakes on triggers/mail only
        start_chat()
    start_triggers()
    start_inbox()
    append_msg({"role": "user", "content": f"<system-message>[start] multimodal_support={CFG['multimodal_support']} provider={CFG['provider'] or 'openai_compat'}</system-message>"})

    def file_hash():
        return hashlib.sha256(open(f"{AGENT_DIR}/messages.jsonl", "rb").read()).hexdigest()

    last_hash = ""  # force a first turn on startup
    owe_turn = False
    while True:
        if not owe_turn and file_hash() == last_hash:
            time.sleep(1)
            continue

        directive = None
        for m in load_messages():
            if m.get("role") != "user":
                continue
            directive = _stash_directive_arg(m.get("content") or "")
            if directive is not None:
                break
        if directive is not None:
            _exec_stash_directive(directive)
            owe_turn = True  # stash collapses partial or all messages; an assistant turn is required to re-orient
            last_hash = file_hash()
            continue

        system, life_tail, messages = build_system(), life_block(), load_messages()
        if len(system) + len(life_tail) + sum(len(json.dumps(m)) for m in messages) > (CFG["context_tokens"] * 4 * 0.8): # ≈4 chars/token, 20% buffer
            append_msg({"role": "user", "content": f"<system-message>[stash_messages] {stash_messages({})}</system-message>"})
            system, life_tail, messages = build_system(), life_block(), load_messages()

        msg = llm_w_retry(
            # Ensure ≥1 user message
            [{"role": "system", "content": system}] + [{"role": "user", "content": "Be Useful"}] + messages + [{"role": "user", "content": f"<system-message>{life_tail}</system-message>"}],
            tools=TOOL_SCHEMAS)
        assistant = {"role": "assistant", "content": msg.get("content") or "",
                     **{k: msg[k] for k in ("reasoning_content", "tool_calls") if msg.get(k)}}
        owe_turn = False

        i, inbound = next(((i, m) for i, m in reversed(list(enumerate(messages))) if m.get("role") == "user"), (None, {}))
        tail = messages[i + 1:] if i is not None else messages
        did_tool_call = bool(assistant.get("tool_calls")) or any(m.get("role") == "tool" or m.get("tool_calls") for m in tail)
        if not assistant.get("tool_calls"):
            append_msg(assistant)
            if not SYS_MSG.fullmatch(inbound.get("content") or "") or did_tool_call:
                if text := (assistant.get("content") or "").strip(): send_text(text)
        else:
            tool_results = []
            for tc in assistant["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    result = bg_run(name, json.loads(tc["function"]["arguments"]), tc["id"], TOOL_FNS[name])
                except Exception as e:
                    result = f"error: {e}"
                tool_results.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            append_msg([assistant] + tool_results)
            owe_turn = True

        # Tool activity resets heartbeat cadence; idle heartbeat turns back off.
        if did_tool_call or _unwrap_sys_msg(inbound.get("content", "")).startswith("[trigger heartbeat]"):
            f = pathlib.Path(f"{AGENT_DIR}/triggers/heartbeat.json")
            try:
                job = json.loads(f.read_text())
                if did_tool_call:
                    job["idles"] = 0
                    job["next"] = time.time() + job["repeat_s"]
                else:
                    job["idles"] = job.get("idles", 0) + 1
                    job["next"] = time.time() + min(job["repeat_s"] * 2 ** job["idles"], job.get("cap", 3600))
                f.write_text(json.dumps(job))
            except Exception:
                pass

        last_hash = file_hash()
        _flush_trigger_over_idle()

if __name__ == "__main__":
    for extra in sys.argv[2:]:  # extra agent dirs each get their own process
        subprocess.Popen([sys.executable, __file__, extra])
    main()
