import concurrent.futures
import fcntl
import functools
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import requests

ROOT = pathlib.Path(__file__).resolve().parent
HEADERS = {"X-Attobot-Lab": "1"}


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for observable state")


class TriggerPersistenceTests(unittest.TestCase):
    def test_interrupted_delivery_recovers_before_read_or_append(self):
        for action in ("agent.load_messages()", "agent.append_msg({'role': 'user', 'content': 'new mail'})"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                stream = root / "messages.jsonl"
                history = [{'role': 'user', 'content': 'remember this task'}, {'role': 'assistant', 'content': 'waiting'}]
                stream.write_text("".join(json.dumps(message) + "\n" for message in history))
                inode = stream.stat().st_ino
                (root / "trigger-queue").mkdir()
                notification = {'role': 'user', 'content': '<system-message>[trigger reminder] saved reminder</system-message>'}
                (root / "trigger-queue/0001.json").write_text(json.dumps(notification))
                crash = '''
import agent, builtins, os, pathlib
original = builtins.open
class InterruptedFile:
    def __init__(self, file): self.file = file
    def __getattr__(self, name): return getattr(self.file, name)
    def __enter__(self): return self
    def __exit__(self, *args): self.file.close()
    def truncate(self, *args):
        self.file.truncate(*args)
        self.file.flush()
        os._exit(91)
def interrupted_open(path, *args, **kwargs):
    file = original(path, *args, **kwargs)
    return InterruptedFile(file) if str(path).endswith('/messages.jsonl') else file
builtins.open = interrupted_open
agent._flush_trigger_over_idle()
'''
                result = subprocess.run([sys.executable, '-c', crash, directory], cwd=ROOT, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 91, result.stderr)
                result = subprocess.run([sys.executable, '-c', 'import agent; ' + action, directory], cwd=ROOT,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                messages = [json.loads(line) for line in stream.read_text().splitlines()]
                self.assertEqual(messages[:3], history + [notification])
                if 'append_msg' in action:
                    self.assertEqual(messages[-1]['content'], 'new mail')
                self.assertEqual(stream.stat().st_ino, inode)

    def test_concurrent_appenders_keep_the_existing_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = pathlib.Path(directory) / 'messages.jsonl'
            stream.touch()
            processes = [subprocess.Popen([sys.executable, '-c',
                "import agent,sys; [agent.append_msg({'role':'user','content':sys.argv[2]+':'+str(i)}) for i in range(20)]", directory, str(worker)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for worker in range(4)]
            try:
                for process in processes:
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
            contents = [json.loads(line)['content'] for line in stream.read_text().splitlines()]
            self.assertEqual(len(contents), 80)
            self.assertEqual(set(contents), {f'{worker}:{i}' for worker in range(4) for i in range(20)})

    def test_queue_does_not_collide_when_clock_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c',
                "import agent; agent.time.time_ns=lambda: 1; agent._queue_trigger({'content':'first'}); agent._queue_trigger({'content':'second'})", directory],
                cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = list((pathlib.Path(directory) / 'trigger-queue').glob('*.json'))
            self.assertEqual(sorted(json.loads(path.read_text())['content'] for path in pending), ['first', 'second'])

    def test_fired_one_shot_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "triggers").mkdir()
            stream = root / "messages.jsonl"
            stream.write_text(json.dumps({"role": "user", "content": "busy task"}) + "\n")
            trigger = root / "triggers/reminder.json"
            trigger.write_text(json.dumps({"next": 0, "message": "keep this reminder"}))
            process = subprocess.Popen([sys.executable, "-c", "import agent,time; agent.CFG['trigger_tick']=.01; agent.start_triggers(); time.sleep(60)", directory],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_for(lambda: not trigger.exists())
            finally:
                process.terminate()
                process.wait(timeout=5)
            self.assertNotIn("keep this reminder", stream.read_text())
            stream.write_text(json.dumps({"role": "assistant", "content": "task finished"}) + "\n")
            result = subprocess.run([sys.executable, "-c", "import agent; agent._flush_trigger_over_idle()", directory],
                                    cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            messages = [json.loads(line) for line in stream.read_text().splitlines()]
            self.assertEqual(messages[-1]["content"], "<system-message>[trigger reminder] keep this reminder</system-message>")
            self.assertEqual(list((root / "trigger-queue").glob("*.json")), [])


class WebFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        (root / "page").mkdir()
        (root / "page/index.html").write_text("<h1>HTTP-only fixture</h1><p>Read from a real server.</p>")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_explicit_http_scheme_is_preserved(self):
        import agent
        self.assertEqual(agent.web_fetch({"url": self.url + "/page/"}), "# HTTP-only fixture\n\nRead from a real server.")

    def test_server_redirects_are_still_followed(self):
        import agent
        self.assertEqual(agent.web_fetch({"url": self.url + "/page"}), "# HTTP-only fixture\n\nRead from a real server.")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = pathlib.Path(self.temp.name).resolve()
        self.process = None
        self.start()
        self.addCleanup(self.stop)

    def start(self, port=0):
        env = {key: value for key, value in os.environ.items() if key != "ATTOBOT_API_KEY"}
        self.process = subprocess.Popen([sys.executable, str(ROOT / "lab.py"), "serve", "--state", str(self.state), "--port", str(port)],
                                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            line = pool.submit(self.process.stdout.readline).result(timeout=10)
        match = re.search(r"port (\d+)", line)
        self.assertIsNotNone(match, line)
        self.port = int(match[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self.token = (self.state / "telegram-token").read_text().strip()
        self.bot = f"{self.url}/bot{self.token}"

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.communicate(timeout=10)
            self.process = None

    def operator(self, route, body=None, **kwargs):
        if body is None and not kwargs:
            response = requests.get(self.url + route, headers=HEADERS, timeout=5)
        else:
            response = requests.post(self.url + route, headers=HEADERS, json=body, timeout=5, **kwargs)
        response.raise_for_status()
        return response.json()

    def test_bot_authentication_and_real_setup_discovery(self):
        self.assertTrue(requests.get(self.bot + "/getMe", timeout=5).json()["result"]["can_read_all_group_messages"])
        self.assertEqual(requests.get(self.url + "/botwrong/getMe", timeout=5).status_code, 401)

    def test_persistent_updates_and_acknowledgements(self):
        self.operator("/api/messages", {"text": "first"})
        first = requests.post(self.bot + "/getUpdates", data={"offset": 0}, timeout=5).json()["result"]
        self.assertEqual(first[0]["message"]["text"], "first")
        repeated = requests.post(self.bot + "/getUpdates", data={"offset": 0}, timeout=5).json()["result"]
        self.assertEqual(first, repeated)
        next_offset = first[0]["update_id"] + 1
        requests.post(self.bot + "/getUpdates", data={"offset": next_offset}, timeout=5).raise_for_status()
        token, port = self.token, self.port
        self.stop()
        self.start(port)
        self.assertEqual(self.token, token)
        self.assertEqual(requests.post(self.bot + "/getUpdates", data={"offset": 0}, timeout=5).json()["result"], [])
        self.assertEqual(self.operator("/api/state")["messages"][0]["text"], "first")
        self.operator("/api/messages", {"text": "second"})
        self.assertEqual(requests.post(self.bot + "/getUpdates", data={"offset": next_offset}, timeout=5).json()["result"][0]["message"]["text"], "second")

    def test_long_poll_and_competing_consumer(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(requests.post, self.bot + "/getUpdates", data={"timeout": 3}, timeout=5)
            wait_for(lambda: self.operator("/api/state")["transport"]["polling"])
            self.assertFalse(pending.done())
            conflict = requests.post(self.bot + "/getUpdates", data={"timeout": 0}, timeout=5)
            self.assertEqual(conflict.status_code, 409)
            self.operator("/api/messages", {"text": "wake up"})
            self.assertEqual(pending.result(timeout=5).json()["result"][0]["message"]["text"], "wake up")

    def test_attachment_round_trip_and_real_download_failure(self):
        content = b"real file bytes\x00\xff"
        message = self.operator("/api/messages", data={"caption": "inspect this"}, files={"file": ("sample.bin", content, "application/octet-stream")})
        file_id = message["document"]["file_id"]
        metadata = requests.get(self.bot + "/getFile", params={"file_id": file_id}, timeout=5).json()["result"]
        endpoint = f"{self.url}/file/bot{self.token}/{metadata['file_path']}"
        self.operator("/api/fault", {"operation": "download"})
        self.assertEqual(requests.get(endpoint, timeout=5).status_code, 503)
        self.assertEqual(requests.get(endpoint, timeout=5).content, content)
        response = requests.post(self.bot + "/sendDocument", data={"chat_id": 1, "message_thread_id": 1, "caption": "returned"},
                                 files={"document": ("returned.bin", content)}, timeout=5)
        response.raise_for_status()
        outgoing = response.json()["result"]
        download = requests.get(f"{self.url}/api/files/{outgoing['document']['file_id']}", headers=HEADERS, timeout=5)
        self.assertEqual(download.content, content)
        self.assertEqual(self.operator("/api/state")["messages"][-1]["direction"], "assistant")

    def test_topic_routing_payloads_and_reactions(self):
        message = self.operator("/api/messages", {"text": "topic two", "message_thread_id": 2})
        update = requests.post(self.bot + "/getUpdates", timeout=5).json()["result"][0]
        self.assertEqual(update["message"]["message_thread_id"], 2)
        response = requests.post(self.bot + "/setMessageReaction", json={"chat_id": 1, "message_id": message["message_id"], "reaction": []}, timeout=5)
        self.assertTrue(response.json()["result"])

    def test_operator_boundary_rejects_cross_origin_and_rebinding(self):
        self.assertEqual(requests.post(self.url + "/api/messages", json={"text": "no header"}, timeout=5).status_code, 403)
        for headers in ({**HEADERS, "Origin": "https://example.com"}, {**HEADERS, "Host": f"example.com:{self.port}"}):
            self.assertEqual(requests.post(self.url + "/api/messages", headers=headers, json={"text": "cross origin"}, timeout=5).status_code, 403)
        self.assertEqual(self.operator("/api/state")["messages"], [])

    def test_fault_is_consumed_and_adapter_does_not_invent_methods(self):
        self.operator("/api/fault", {"operation": "poll"})
        self.assertEqual(requests.post(self.bot + "/getUpdates", timeout=5).status_code, 503)
        self.assertEqual(requests.post(self.bot + "/getUpdates", timeout=5).status_code, 200)
        self.assertEqual(requests.post(self.bot + "/unknownMethod", timeout=5).status_code, 400)

    def test_adapter_does_not_start_a_model_implicitly(self):
        self.assertFalse(self.operator("/api/state")["agent"]["running"])
        response = requests.post(self.url + "/api/start", headers=HEADERS, json={}, timeout=5)
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.state / "agent").exists())

    def test_real_setup_targets_adapter_and_installs_sibling(self):
        primary = self.state / "space 'quote' $budget" / "custom"
        env = {**os.environ, "ATTOBOT_API_KEY": "unused-by-setup"}
        result = subprocess.run([sys.executable, str(ROOT / "setup.py"), str(primary), "--token", self.token,
                                 "--chat", "1", "--thread", "1", "--telegram-api-base", self.url,
                                 "--provider", "openai_responses", "--model", "gpt-6-astra", "--api-base", "https://api.openai.com/v1",
                                 "--subconscious", "--systemd"], cwd=self.state, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        sub = primary.parent / "subconscious"
        config = json.loads((primary / "config.json").read_text())
        subconfig = json.loads((sub / "config.json").read_text())
        self.assertEqual(config["telegram_api_base"], self.url)
        self.assertNotIn("api_key", config)
        self.assertEqual(subconfig["primary_dir"], str(primary))
        self.assertEqual(subconfig["model"], "gpt-6-astra")
        self.assertEqual(subconfig["opt"], ["tools/nudge", "tools/stash_messages"])
        self.assertFalse(any(key.startswith("telegram") for key in subconfig))
        self.assertEqual((sub / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("RestartPreventExitStatus=78", (self.state / "attobot.service").read_text())
        watch = json.loads((sub / "triggers" / "primary.json").read_text())
        stream = primary / "messages.jsonl"
        stream.write_text("initial\n")
        first = subprocess.run(watch["cmd"], shell=True, cwd=self.state, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr)
        stream.write_text("initial\n" + "new line\n" * 12)
        second = subprocess.run(watch["cmd"], shell=True, cwd=self.state, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.count("new line"), 12)

    def test_harness_respects_a_real_process_lock(self):
        primary = self.state / "locked"
        primary.mkdir()
        with (primary / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, str(ROOT / "agent.py"), str(primary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertIn("already running", result.stderr)
        self.assertFalse((primary / "messages.jsonl").exists())

    def test_invalid_harness_config_stops_without_sibling(self):
        primary, sub = self.state / "bad", self.state / "unstarted"
        primary.mkdir()
        (primary / "config.json").write_text("{invalid")
        (primary / "SOUL.md").write_text("test")
        result = subprocess.run([sys.executable, str(ROOT / "agent.py"), str(primary), str(sub)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertFalse(sub.exists())


@unittest.skipUnless(os.environ.get("ATTOBOT_LIVE_URL"), "set ATTOBOT_LIVE_URL to an isolated running lab to spend real model tokens")
class LiveAgentTests(unittest.TestCase):
    def test_real_model_tools_attachments_and_process_isolation(self):
        url = os.environ["ATTOBOT_LIVE_URL"].rstrip("/")
        def api(path, body=None, **kwargs):
            method = requests.get if body is None and not kwargs else requests.post
            response = method(url + path, headers=HEADERS, timeout=30, **({"json": body} if body is not None else {}), **kwargs)
            response.raise_for_status()
            return response.json()
        state = api("/api/state")
        owned = not state["agent"]["running"]
        if owned:
            api("/api/start", {"soak": False})
            self.addCleanup(lambda: api("/api/stop", {}))
        token = uuid.uuid4().hex
        api("/api/messages", data={"caption": f"Read this attachment with READ_FILE. Create proof-{token}.txt containing exactly its contents using WRITE_FILE. Then SEND_ATTACHMENT that file back. This is a local integration test; do not do unrelated work."},
            files={"file": ("challenge.txt", token.encode(), "text/plain")})
        def returned_file():
            state = api("/api/state")
            self.assertTrue(state["agent"]["running"], state["logs"]["runner"])
            return next((message["document"] for message in state["messages"] if message["direction"] == "assistant" and token in message.get("document", {}).get("file_name", "")), None)
        file = wait_for(returned_file, timeout=180)
        response = requests.get(f"{url}/api/files/{file['file_id']}", headers=HEADERS, timeout=10)
        self.assertEqual(response.content.strip(), token.encode())
        logs = api("/api/state")["logs"]
        self.assertIn("READ_FILE(", logs["primary"])
        self.assertIn("WRITE_FILE(", logs["primary"])
        self.assertIn("SEND_ATTACHMENT(", logs["primary"])
        self.assertIn("[start]", logs["subconscious"])
        runtime = shutil.which("container") or "/opt/homebrew/bin/container"
        name = os.environ.get("ATTOBOT_LIVE_CONTAINER", "attobot-lab")
        result = subprocess.run([runtime, "exec", name, "python", "-c", "import os; assert os.geteuid() != 0; print('non-root')"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
