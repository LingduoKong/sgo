import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from web.auth import AuthStore, AuthError


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100000.0
        self.sent = []
        self.store = AuthStore(Path(self.tmp.name) / 'auth.db', 's' * 48,
                               lambda email, code: self.sent.append((email, code)),
                               clock=lambda: self.now)
        self.store.allow('Owner@Example.com', admin=True)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        self.store.request_code('owner@example.com', '127.0.0.1')
        return self.store.verify('owner@example.com', self.sent[-1][1], '127.0.0.1')

    def test_only_exact_allowlist_receives_codes(self):
        self.store.request_code('stranger@example.com', 'a')
        self.store.request_code('owner+other@example.com', 'a')
        self.assertEqual(self.sent, [])
        self.store.request_code(' OWNER@example.com ', 'a')
        self.assertEqual(self.sent[0][0], 'owner@example.com')

    def test_absolute_24_hour_expiry_survives_reopen(self):
        token = self.login()
        self.now += 86399
        self.assertEqual(self.store.user(token)['email'], 'owner@example.com')
        reopened = AuthStore(self.store.path, 's' * 48, lambda *a: None, clock=lambda: self.now)
        self.assertIsNotNone(reopened.user(token))
        self.now += 1
        self.assertIsNone(self.store.user(token))

    def test_code_expiry_and_replay(self):
        token = self.login()
        with self.assertRaises(AuthError):
            self.store.verify('owner@example.com', self.sent[-1][1], 'a')
        self.now += 61
        self.store.request_code('owner@example.com', 'a')
        self.now += 600
        with self.assertRaises(AuthError):
            self.store.verify('owner@example.com', self.sent[-1][1], 'a')
        self.assertIsNotNone(self.store.user(token))

    def test_five_guesses_invalidate_code(self):
        self.store.request_code('owner@example.com', 'a')
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.store.verify('owner@example.com', 'wrong', 'a')
        with self.assertRaises(AuthError):
            self.store.verify('owner@example.com', self.sent[-1][1], 'a')

    def test_revocation_and_logout(self):
        token = self.login()
        self.store.revoke('owner@example.com')
        self.assertIsNone(self.store.user(token))
        self.store.allow('owner@example.com')
        self.now += 61
        token = self.login()
        self.store.logout(token)
        self.assertIsNone(self.store.user(token))

    def test_resend_throttled_and_new_code_replaces_old(self):
        self.store.request_code('owner@example.com', 'a')
        old = self.sent[-1][1]
        with self.assertRaises(AuthError):
            self.store.request_code('owner@example.com', 'a')
        self.now += 61
        self.store.request_code('owner@example.com', 'a')
        with self.assertRaises(AuthError):
            self.store.verify('owner@example.com', old, 'a')
        self.assertTrue(self.store.verify('owner@example.com', self.sent[-1][1], 'a'))

    def test_atomic_code_consumption(self):
        self.store.request_code('owner@example.com', 'a')
        code = self.sent[-1][1]
        def attempt(_):
            try:
                return self.store.verify('owner@example.com', code, 'a')
            except AuthError:
                return None
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(bool(x) for x in results), 1)

    def test_quota_persisted(self):
        for _ in range(6):
            self.store.charge('owner@example.com', heavy=True)
        with self.assertRaises(AuthError):
            self.store.charge('owner@example.com', heavy=True)
        reopened = AuthStore(self.store.path, 's' * 48, lambda *a: None, clock=lambda: self.now)
        with self.assertRaises(AuthError):
            reopened.charge('owner@example.com', heavy=True)

    def test_secrets_are_not_stored_in_plaintext(self):
        token = self.login()
        raw = self.store.path.read_bytes()
        self.assertNotIn(token.encode(), raw)
        self.assertNotIn(self.sent[-1][1].encode(), raw)

    def test_failed_delivery_invalidates_code(self):
        def fail(*args):
            raise RuntimeError('private provider error')
        self.store.sender = fail
        with self.assertRaises(AuthError) as error:
            self.store.request_code('owner@example.com', 'a')
        self.assertNotIn('private', str(error.exception))

    def test_actual_model_call_budget(self):
        for _ in range(100):
            self.store.model_call('owner@example.com')
        with self.assertRaises(AuthError):
            self.store.model_call('owner@example.com')

    def test_failed_mail_does_not_spend_delivery_allowance(self):
        def fail(*args):
            raise RuntimeError('provider failure')
        self.store.sender = fail
        for _ in range(5):
            with self.assertRaises(AuthError) as error:
                self.store.request_code('owner@example.com', 'a')
            self.assertEqual(error.exception.status, 503)
            self.now += 61
        self.store.sender = lambda email, code: self.sent.append((email, code))
        self.store.request_code('owner@example.com', 'a')
        self.assertEqual(len(self.sent), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count FROM limits WHERE key='mail:owner@example.com'").fetchone()['count'], 1)
            self.assertEqual(db.execute("SELECT count FROM limits WHERE key='mail-global'").fetchone()['count'], 1)

    def test_failed_delivery_still_has_resend_cooldown(self):
        self.store.sender = lambda *args: (_ for _ in ()).throw(RuntimeError('provider failure'))
        with self.assertRaises(AuthError):
            self.store.request_code('owner@example.com', 'a')
        with self.assertRaises(AuthError) as error:
            self.store.request_code('owner@example.com', 'a')
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.retry_after, 60)
