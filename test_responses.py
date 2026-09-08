import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import agent

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('responses_under_test', ROOT / 'opt/providers/openai_responses.py')
responses = importlib.util.module_from_spec(spec)
spec.loader.exec_module(responses)


class ResponsesRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = mock.patch.dict(agent.CFG, {'api_key': 'offline-fixture', 'api_base': 'http://unused.invalid',
                                                'model': 'fixture', 'max_tokens': 4, 'max_tokens_limit': 16})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.incomplete = {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                           'output': [{'type': 'function_call', 'call_id': 'partial', 'name': 'BASH', 'arguments': '{'}]}

    def response(self, payload, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        return response

    def test_output_limit_retries_with_larger_budget_and_no_partial_tools(self):
        complete = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'text': 'finished'}]}]}
        with mock.patch.object(responses.requests, 'post', side_effect=[self.response(self.incomplete), self.response(complete)]) as post:
            result = responses.chat([{'role': 'user', 'content': 'work'}], None)
        self.assertEqual(result['content'], 'finished')
        self.assertNotIn('tool_calls', result)
        self.assertEqual([call.kwargs['json']['max_output_tokens'] for call in post.call_args_list], [4, 8])
        self.assertEqual(post.call_args_list[0].kwargs['json']['input'], post.call_args_list[1].kwargs['json']['input'])
        self.assertEqual(agent.CFG['max_tokens'], 4)

    def test_recovery_is_bounded_and_not_fatal_or_infinitely_retried(self):
        with mock.patch.object(responses.requests, 'post', return_value=self.response(self.incomplete)) as post:
            with mock.patch.object(agent, 'llm', responses.chat), mock.patch.object(agent, 'life'):
                with self.assertRaises(agent.GenerationLimitError):
                    agent.llm_w_retry([{'role': 'user', 'content': 'work'}])
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.kwargs['json']['max_output_tokens'] for call in post.call_args_list], [4, 8, 16])

    def test_authentication_error_remains_fatal(self):
        with mock.patch.object(responses.requests, 'post', return_value=self.response({'error': 'bad key'}, 401)) as post:
            with self.assertRaises(agent.FatalLLMError):
                responses.chat([{'role': 'user', 'content': 'work'}], None)
        self.assertEqual(post.call_count, 1)

    def test_exhausted_employee_stays_alive_without_spending_in_a_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.joinpath('config.json').write_text(json.dumps({'api_key': 'offline-fixture'}))
            root.joinpath('SOUL.md').write_text('Offline test employee.')
            code = '''
import agent, pathlib
counter = pathlib.Path(agent.AGENT_DIR) / 'calls'
def incomplete(*args, **kwargs):
    counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
    raise agent.GenerationLimitError('output limit exhausted')
agent.llm_w_retry = incomplete
agent.run()
'''
            process = subprocess.Popen([sys.executable, '-c', code, directory], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not root.joinpath('calls').exists() and process.poll() is None:
                    time.sleep(.02)
                time.sleep(1.2)
                self.assertIsNone(process.poll())
                self.assertEqual(root.joinpath('calls').read_text(), '1')
                self.assertIn('waiting for new input', root.joinpath('LIFE.md').read_text())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
