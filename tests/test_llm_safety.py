import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from web.llm_safety import LimitedClient

class ModelSafetyTests(unittest.TestCase):
    def test_caps_output_and_counts_every_call(self):
        create = Mock(return_value='result')
        underlying = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        charge = Mock()
        client = LimitedClient(underlying, charge)
        self.assertEqual(client.chat.completions.create(model='test', messages=[], max_tokens=16384), 'result')
        self.assertEqual(create.call_args.kwargs['max_tokens'], 2048)
        charge.assert_called_once()

    def test_upstream_errors_do_not_expose_secret_response(self):
        underlying = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=RuntimeError('secret-key-in-upstream-response')))))
        with self.assertRaisesRegex(RuntimeError, '^Model request failed'):
            LimitedClient(underlying, lambda: None).chat.completions.create(model='test', messages=[])
