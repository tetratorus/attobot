import fcntl
import functools
import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


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


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = pathlib.Path(self.temp.name).resolve()

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

    def test_missing_config_explains_required_state(self):
        primary, sub = self.state / "missing", self.state / "unstarted"
        result = subprocess.run([sys.executable, str(ROOT / "agent.py"), str(primary), str(sub)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertIn("provide config.json and SOUL.md before starting", result.stderr)
        self.assertFalse(sub.exists())


if __name__ == "__main__":
    unittest.main()
