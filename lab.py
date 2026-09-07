#!/usr/bin/env python3
import argparse
import contextlib
import email.parser
import email.policy
import getpass
import hmac
import http.server
import json
import os
import pathlib
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
MAX_BODY = 16 * 1024 * 1024
MEDIA = {f"send{kind.title()}": kind for kind in ("photo", "document", "voice", "video", "audio")}


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Store:
    def __init__(self, path):
        self.path = path
        self.condition = threading.Condition()
        self.polling = False
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('offset', 0);
            """)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def file(self, file_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise APIError(404, "file not found")
        return dict(row)

    def add_message(self, direction, fields, upload=None, kind="document"):
        chat_id = int(fields.get("chat_id", 1))
        payload = {"date": int(time.time()), "chat": {"id": chat_id, "type": "supergroup", "title": "Attobot lab"},
                   "from": {"id": 42 if direction == "assistant" else 1, "is_bot": direction == "assistant",
                            "first_name": "Attobot" if direction == "assistant" else "Operator"}}
        if fields.get("message_thread_id") is not None:
            payload["message_thread_id"] = int(fields["message_thread_id"])
        with self.db() as db:
            if upload:
                name, mime, content = upload
                file_id = uuid.uuid4().hex
                db.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (file_id, pathlib.Path(name).name, mime, content))
                media = {"file_id": file_id, "file_unique_id": file_id, "file_name": pathlib.Path(name).name,
                         "mime_type": mime, "file_size": len(content)}
                payload[kind] = [media] if kind == "photo" else media
                payload["caption"] = str(fields.get("caption", fields.get("text", "")))
            else:
                payload["text"] = str(fields.get("text", ""))
                if not payload["text"]:
                    raise APIError(400, "message text is empty")
            cursor = db.execute("INSERT INTO messages(direction,payload) VALUES (?, '{}')", (direction,))
            payload["message_id"] = cursor.lastrowid
            db.execute("UPDATE messages SET payload=? WHERE id=?", (json.dumps(payload), cursor.lastrowid))
            if direction == "user":
                db.execute("INSERT INTO updates(message_id) VALUES (?)", (cursor.lastrowid,))
        with self.condition:
            self.condition.notify_all()
        return payload

    def updates(self, offset, timeout, limit):
        if offset < 0 or not 1 <= limit <= 100:
            raise APIError(400, "offset must be nonnegative and limit must be 1..100")
        with self.condition:
            if self.polling:
                raise APIError(409, "Conflict: another getUpdates request is active")
            self.polling = True
            try:
                deadline = time.monotonic() + min(max(timeout, 0), 50)
                with self.db() as db:
                    db.execute("UPDATE settings SET value=MAX(value, ?) WHERE name='offset'", (offset,))
                while True:
                    with self.db() as db:
                        rows = db.execute("""SELECT updates.id, messages.payload FROM updates JOIN messages ON messages.id=updates.message_id
                            WHERE updates.id >= (SELECT value FROM settings WHERE name='offset') ORDER BY updates.id LIMIT ?""", (limit,)).fetchall()
                    if rows or time.monotonic() >= deadline:
                        return [{"update_id": row["id"], "message": json.loads(row["payload"])} for row in rows]
                    self.condition.wait(deadline - time.monotonic())
            finally:
                self.polling = False

    def react(self, message_id, reaction):
        with self.db() as db:
            row = db.execute("SELECT payload FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise APIError(400, "message not found")
            payload = json.loads(row["payload"])
            payload["lab_reaction"] = reaction
            db.execute("UPDATE messages SET payload=? WHERE id=?", (json.dumps(payload), message_id))
        return True

    def messages(self):
        with self.db() as db:
            rows = db.execute("SELECT direction,payload FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 200) ORDER BY id").fetchall()
        return [{"direction": row["direction"], **json.loads(row["payload"])} for row in rows]

    def status(self):
        with self.db() as db:
            offset = db.execute("SELECT value FROM settings WHERE name='offset'").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM updates WHERE id>=?", (offset,)).fetchone()[0]
        return {"offset": offset, "pending": pending, "polling": self.polling}

    def fault(self, operation, count=None):
        if operation not in ("download", "send", "poll"):
            raise APIError(400, "unknown fault operation")
        name = f"fault_{operation}"
        with self.db() as db:
            if count is not None:
                if not 0 <= count <= 10:
                    raise APIError(400, "fault count must be 0..10")
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, count))
                return False
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
            if row and row["value"]:
                db.execute("UPDATE settings SET value=value-1 WHERE name=?", (name,))
                return True
        return False


class Lab:
    def __init__(self, state, port, duration):
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state / "chat.sqlite3")
        token_path = self.state / "telegram-token"
        if not token_path.exists():
            with open(os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(secrets.token_hex(24))
        self.token = token_path.read_text().strip()
        self.port, self.duration = port, duration
        self.process = None
        self.deadline = None
        self.lock = threading.RLock()
        self.primary, self.sub = self.state / "agent", self.state / "subconscious"

    def configure(self):
        if (self.primary / "config.json").exists():
            return
        command = [sys.executable, str(ROOT / "setup.py"), str(self.primary), "--token", self.token,
                   "--chat", "1", "--thread", "1", "--subconscious", "--telegram-api-base", f"http://127.0.0.1:{self.port}",
                   "--provider", "openai_responses", "--api-base", os.environ.get("ATTOBOT_API_BASE", "https://api.openai.com/v1"),
                   "--model", os.environ.get("ATTOBOT_MODEL", "gpt-6-astra"), "--reasoning-effort", "medium"]
        result = subprocess.run(command, cwd=self.state, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise APIError(500, "setup failed; check the lab's provider environment and state directory")

    def start(self, soak=False):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise APIError(409, "agent is already running")
            if not os.environ.get("ATTOBOT_API_KEY"):
                raise APIError(400, "start the lab with ATTOBOT_API_KEY to run the real agent")
            self.configure()
            heartbeat = self.primary / "triggers" / "heartbeat.json"
            heartbeat.parent.mkdir(exist_ok=True)
            job = json.loads(heartbeat.read_text()) if heartbeat.exists() else {
                "repeat_s": 225, "cap": 3600, "message": "Check your latest state for useful work. If idle, reply immediately."}
            job["next"] = time.time() + (225 if soak else self.duration + 60)
            heartbeat.write_text(json.dumps(job))
            self.deadline = time.time() + self.duration
            with open(self.state / "runner.log", "a") as log:
                self.process = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), str(self.primary), str(self.sub)],
                                                cwd=self.state, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            return self.status()

    def stop(self):
        with self.lock:
            if self.process:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
            self.deadline = None
            return self.status()

    def status(self):
        with self.lock:
            code = self.process.poll() if self.process else None
            return {"running": self.process is not None and code is None, "exit_code": code,
                    "pid": self.process.pid if self.process else None, "deadline": self.deadline,
                    "configured": bool(os.environ.get("ATTOBOT_API_KEY")),
                    "model": os.environ.get("ATTOBOT_MODEL", "gpt-6-astra")}

    def logs(self):
        result = {}
        for name, path in (("primary", self.primary / "LIFE.md"), ("subconscious", self.sub / "LIFE.md"), ("runner", self.state / "runner.log")):
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 12000))
                    text = stream.read().decode("utf-8", errors="replace")
            except FileNotFoundError:
                text = ""
            for secret in (self.token, os.environ.get("ATTOBOT_API_KEY")):
                if secret:
                    text = text.replace(secret, "[redacted]")
            result[name] = text
        return result


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def lab(self):
        return self.server.lab

    def reply(self, status, body, content_type="application/json", extra=None):
        content = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(content)), "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
                           **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= MAX_BODY:
            raise APIError(413, "request too large")
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        files = {}
        if content_type.startswith("application/json"):
            fields = json.loads(raw or b"{}")
            if not isinstance(fields, dict):
                raise APIError(400, "expected a JSON object")
        elif content_type.startswith("multipart/form-data"):
            message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                value = part.get_payload(decode=True) or b""
                if part.get_filename() is not None:
                    files[name] = (part.get_filename(), part.get_content_type(), value)
                else:
                    fields[name] = value.decode("utf-8")
        else:
            fields = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
        return fields, files

    def operator(self):
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.lab.port}", f"localhost:{self.lab.port}"):
            raise APIError(403, "lab operator API is loopback-only")
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise APIError(403, "cross-origin requests are not allowed")
        if self.headers.get("X-Attobot-Lab") != "1":
            raise APIError(403, "operator requests require X-Attobot-Lab: 1")

    def download(self, file_id):
        file = self.lab.store.file(file_id)
        self.reply(200, file["content"], file["mime"], {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(file['name'], safe='')}"})

    def dispatch(self):
        path = urllib.parse.urlsplit(self.path).path
        fields, files = self.body() if self.command == "POST" else (dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query)), {})
        if path == "/" and self.command == "GET":
            self.reply(200, (ROOT / "lab.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            self.operator()
            if self.command == "GET" and path == "/api/state":
                self.reply(200, {"messages": self.lab.store.messages(), "agent": self.lab.status(), "logs": self.lab.logs(), "transport": self.lab.store.status()})
            elif self.command == "GET" and path.startswith("/api/files/"):
                self.download(path.removeprefix("/api/files/"))
            elif self.command == "POST" and path == "/api/messages":
                fields.setdefault("message_thread_id", 1)
                self.reply(200, self.lab.store.add_message("user", fields, files.get("file")))
            elif self.command == "POST" and path == "/api/start":
                self.reply(200, self.lab.start(bool(fields.get("soak"))))
            elif self.command == "POST" and path == "/api/stop":
                self.reply(200, self.lab.stop())
            elif self.command == "POST" and path == "/api/fault":
                self.lab.store.fault(fields.get("operation"), int(fields.get("count", 1)))
                self.reply(200, {"ok": True})
            else:
                raise APIError(404, "operator route not found")
            return
        file_prefix = f"/file/bot{self.lab.token}/"
        if path.startswith(file_prefix):
            if self.lab.store.fault("download"):
                raise APIError(503, "injected download failure")
            self.download(path.removeprefix(file_prefix).split("/", 1)[0])
            return
        parts = path.strip("/").split("/")
        if len(parts) != 2 or not parts[0].startswith("bot") or not hmac.compare_digest(parts[0][3:], self.lab.token):
            raise APIError(401, "Unauthorized")
        method = parts[1]
        if method == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Attobot lab", "username": "attobot_lab",
                      "can_join_groups": True, "can_read_all_group_messages": True}
        elif method == "getUpdates":
            if self.lab.store.fault("poll"):
                raise APIError(503, "injected polling failure")
            result = self.lab.store.updates(int(fields.get("offset", 0)), int(fields.get("timeout", 0)), int(fields.get("limit", 100)))
        elif method == "getFile":
            file = self.lab.store.file(fields.get("file_id"))
            result = {"file_id": file["id"], "file_unique_id": file["id"], "file_size": len(file["content"]), "file_path": file["id"]}
        elif method == "setMessageReaction":
            reaction = fields.get("reaction", [])
            result = self.lab.store.react(int(fields["message_id"]), json.loads(reaction) if isinstance(reaction, str) else reaction)
        elif method == "sendMessage" or method in MEDIA:
            if self.lab.store.fault("send"):
                raise APIError(503, "injected send failure")
            kind = MEDIA.get(method, "document")
            if method in MEDIA and kind not in files:
                raise APIError(400, "media requests require a multipart file")
            result = self.lab.store.add_message("assistant", fields, files.get(kind), kind)
        else:
            raise APIError(400, f"unsupported Bot API method: {method}")
        self.reply(200, {"ok": True, "result": result})

    def handle_request(self):
        try:
            self.dispatch()
        except APIError as error:
            self.reply(error.status, {"ok": False, "error_code": error.status, "description": error.message})
        except (ValueError, KeyError, TypeError, UnicodeError):
            self.reply(400, {"ok": False, "error_code": 400, "description": "invalid request"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = handle_request
    do_POST = handle_request


def serve(args):
    os.umask(0o077)
    lab = Lab(args.state, args.port, args.duration)
    server = http.server.ThreadingHTTPServer((args.listen, args.port), Handler)
    lab.port = server.server_port
    server.lab = lab
    stopping = threading.Event()

    def watchdog():
        while not stopping.wait(1):
            if lab.deadline and time.time() >= lab.deadline:
                lab.stop()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"Attobot lab listening on port {lab.port}; agents start only when requested", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        lab.stop()
        server.server_close()


def up(args):
    runtime = shutil.which("container") or ("/opt/homebrew/bin/container" if pathlib.Path("/opt/homebrew/bin/container").exists() else None)
    if not runtime:
        sys.exit("Apple container is required. Install it, then run container system start.")
    environment = dict(os.environ)
    if not environment.get("ATTOBOT_API_KEY"):
        if not sys.stdin.isatty():
            sys.exit("Set ATTOBOT_API_KEY or run interactively for a hidden prompt.")
        environment["ATTOBOT_API_KEY"] = getpass.getpass("OpenAI API key (not saved): ").strip()
    if not environment["ATTOBOT_API_KEY"]:
        sys.exit("An API key is required; use 'serve' for adapter-only development.")
    environment["ATTOBOT_MODEL"] = args.model
    environment["ATTOBOT_API_BASE"] = args.api_base
    dns = ["--dns", args.dns] if args.dns else []
    if not args.no_build:
        subprocess.run([runtime, "build", *dns, "--file", str(ROOT / "Containerfile"), "--tag", "attobot-lab", str(ROOT)], check=True)
    volumes = json.loads(subprocess.check_output([runtime, "volume", "list", "--format", "json"], text=True))
    if not any(volume.get("name") == args.name for volume in volumes):
        subprocess.run([runtime, "volume", "create", args.name], check=True)
        subprocess.run([runtime, "run", "--rm", "--user", "0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                        "--volume", f"{args.name}:/work", "--entrypoint", "chown", "attobot-lab", "1000:1000", "/work"], check=True)
    command = [runtime, "run", *dns, "--rm", "--name", args.name, "--cpus", "2", "--memory", "1G", "--read-only", "--cap-drop", "ALL",
               "--publish", f"127.0.0.1:{args.port}:{args.port}", "--volume", f"{args.name}:/work", "--tmpfs", "/tmp",
               "--env", "ATTOBOT_API_KEY", "--env", "ATTOBOT_MODEL", "--env", "ATTOBOT_API_BASE",
               "attobot-lab", "serve", "--listen", "0.0.0.0", "--state", "/work", "--port", str(args.port), "--duration", str(args.duration)]
    print(f"Open http://127.0.0.1:{args.port} and press Start. State stays in volume {args.name}.", flush=True)
    try:
        return subprocess.call(command, env=environment)
    except KeyboardInterrupt:
        subprocess.run([runtime, "stop", args.name], check=False)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Run attobot against a local, persistent Telegram-compatible chat service.")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("serve", help="run the adapter; use up for isolated agent execution")
    local.add_argument("--listen", default="127.0.0.1")
    local.add_argument("--state", type=pathlib.Path, default=pathlib.Path(".lab-state"))
    local.add_argument("--port", type=int, default=8080)
    local.add_argument("--duration", type=int, default=600)
    local.set_defaults(run=serve)
    isolated = commands.add_parser("up", help="build and run the complete lab in Apple container")
    isolated.add_argument("--name", default="attobot-lab")
    isolated.add_argument("--port", type=int, default=8080)
    isolated.add_argument("--duration", type=int, default=600)
    isolated.add_argument("--model", default="gpt-6-astra")
    isolated.add_argument("--api-base", default="https://api.openai.com/v1")
    isolated.add_argument("--dns", help="DNS server for this lab's build and runtime; useful when host VPN DNS is loopback-only")
    isolated.add_argument("--no-build", action="store_true")
    isolated.set_defaults(run=up)
    args = parser.parse_args()
    if args.duration < 1:
        parser.error("duration must be positive")
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
