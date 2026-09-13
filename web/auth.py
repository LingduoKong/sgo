"""Private email OTP authentication. No credentials or codes are logged."""
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


log = logging.getLogger('sgo.auth')


class AuthError(Exception):
    def __init__(self, message='Invalid or expired code', status=400, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def normalize_email(email):
    email = str(email).strip().lower()
    if len(email) > 254 or not re.fullmatch(r'[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', email):
        raise AuthError('Enter a valid email address')
    return email


class AuthStore:
    def __init__(self, path, secret, sender, clock=time.time):
        if len(secret) < 32 or secret.startswith('replace-with-'):
            raise ValueError('AUTH_SECRET must contain at least 32 random characters')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secret = secret.encode()
        self.sender = sender
        self.clock = clock
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, admin INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS codes(email TEXT PRIMARY KEY, digest TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS logins(digest TEXT PRIMARY KEY, email TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def digest(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def allow(self, email, admin=False):
        with self.db() as db:
            db.execute('INSERT INTO users VALUES (?,?) ON CONFLICT(email) DO UPDATE SET admin=excluded.admin',
                       (normalize_email(email), int(admin)))
        log.info("allowlist_updated subject=%s admin=%s", self.digest(normalize_email(email))[:12], bool(admin))

    def revoke(self, email):
        email = normalize_email(email)
        with self.db() as db:
            for table in ('users', 'codes', 'logins'):
                db.execute(f'DELETE FROM {table} WHERE email=?', (email,))
        log.info('access_revoked subject=%s', self.digest(email)[:12])

    def list_users(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT email,admin FROM users ORDER BY email')]

    def _limits(self, db, buckets):
        now = self.clock()
        db.execute('DELETE FROM limits WHERE expires<=?', (now,))
        for key, maximum, window in buckets:
            row = db.execute('SELECT count,expires FROM limits WHERE key=?', (key,)).fetchone()
            if row and row['count'] >= maximum:
                raise AuthError('Request limit reached. Please try again later.', 429, max(1, math.ceil(row['expires']-now)))
        for key, maximum, window in buckets:
            db.execute('INSERT INTO limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now + window))

    def request_code(self, email, ip):
        email = normalize_email(email)
        now = self.clock()
        digest = None
        delivery_reservations = []
        with self.db() as db:
            db.execute('DELETE FROM codes WHERE expires<=?', (now,))
            db.execute('DELETE FROM logins WHERE expires<=?', (now,))
            # Anonymous attempts are globally bounded; unknown addresses consume no email buckets.
            self._limits(db, [(f'request-ip:{ip}', 20, 3600), ('request-global', 200, 3600)])
            known = db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone()
            if known:
                self._limits(db, [(f'resend:{email}', 1, 60), (f'mail:{email}', 5, 3600), ('mail-global', 50, 86400)])
                for bucket in (f'mail:{email}', 'mail-global'):
                    expiry = db.execute('SELECT expires FROM limits WHERE key=?', (bucket,)).fetchone()['expires']
                    delivery_reservations.append((bucket, expiry))
                code = f'{secrets.randbelow(100000000):08d}'
                digest = self.digest(f'{email}:{code}')
                db.execute('INSERT OR REPLACE INTO codes VALUES (?,?,?,0)', (email, digest, now + 600))
        if digest:
            try:
                self.sender(email, code)
            except Exception:
                with self.db() as db:
                    db.execute('DELETE FROM codes WHERE email=? AND digest=?', (email, digest))
                    # Refund only this failed delivery's reservations in the same window.
                    # Keep request/IP limits and the 60-second cooldown to bound retries.
                    for bucket, expiry in delivery_reservations:
                        db.execute('UPDATE limits SET count=MAX(0,count-1) WHERE key=? AND expires=?', (bucket, expiry))
                raise AuthError('Email delivery unavailable. Please try later.', 503) from None

    def verify(self, email, code, ip):
        email = normalize_email(email)
        token = None
        with self.db() as db:
            self._limits(db, [(f'verify-ip:{ip}', 20, 600), ('verify-global', 200, 600)])
            row = db.execute('SELECT c.* FROM codes c JOIN users u USING(email) WHERE email=?', (email,)).fetchone()
            if row and row['expires'] > self.clock() and row['attempts'] < 5:
                if hmac.compare_digest(row['digest'], self.digest(f'{email}:{code}')):
                    db.execute('DELETE FROM codes WHERE email=?', (email,))
                    token = secrets.token_urlsafe(32)
                    # Bound active devices; new login replaces oldest devices beyond five.
                    db.execute('DELETE FROM logins WHERE expires<=?', (self.clock(),))
                    db.execute('INSERT INTO logins VALUES (?,?,?)', (self.digest(token), email, self.clock() + 86400))
                    db.execute('DELETE FROM logins WHERE email=? AND digest NOT IN (SELECT digest FROM logins WHERE email=? ORDER BY expires DESC LIMIT 5)', (email, email))
                else:
                    db.execute('UPDATE codes SET attempts=attempts+1 WHERE email=?', (email,))
        if not token:
            log.info("login_rejected subject=%s", self.digest(email)[:12])
            raise AuthError()
        log.info("login_accepted subject=%s", self.digest(email)[:12])
        return token

    def user(self, token):
        if not token or len(token) > 128:
            return None
        with self.db() as db:
            row = db.execute('SELECT u.email,u.admin,l.expires FROM logins l JOIN users u USING(email) WHERE l.digest=? AND l.expires>?',
                             (self.digest(token), self.clock())).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM logins WHERE digest=?', (self.digest(token or ''),))

    def model_call(self, email):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                raise AuthError('Access revoked', 401)
            self._limits(db, [(f'model:{email}', 100, 86400), ('model-global', 300, 86400)])

    def charge(self, email, heavy=False):
        with self.db() as db:
            buckets = [(f'paid:{email}', 20, 86400), ('paid-global', 60, 86400)]
            if heavy:
                buckets.append((f'heavy:{email}', 6, 86400))
            self._limits(db, buckets)


def ses_sender(email, code):
    import boto3
    from botocore.config import Config
    region = os.environ['AWS_REGION']
    sender = os.environ['SES_FROM_EMAIL']
    reply_to = os.getenv('SES_REPLY_TO_EMAIL', '').strip()
    boto3.client('sesv2', region_name=region, config=Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 0})).send_email(
        FromEmailAddress=sender, Destination={'ToAddresses': [email]},
        **({'ReplyToAddresses': [normalize_email(reply_to)]} if reply_to else {}),
        Content={'Simple': {
            'Subject': {'Data': 'SGO 登录验证码', 'Charset': 'UTF-8'},
            'Body': {'Text': {'Data': f'你的 SGO 登录验证码是：{code}\n\n10 分钟内有效，只能使用一次。请勿向他人提供。\n如果不是你发起的登录，请忽略此邮件。', 'Charset': 'UTF-8'}}}})


def store_from_env():
    return AuthStore(os.getenv('AUTH_DB_PATH', 'data/private/auth.sqlite3'), os.getenv('AUTH_SECRET', ''), ses_sender)


if __name__ == '__main__':
    import argparse
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    parser = argparse.ArgumentParser(description='Manage the private SGO allowlist (server access required)')
    parser.add_argument('action', choices=['allow', 'revoke', 'list'])
    parser.add_argument('email', nargs='?')
    parser.add_argument('--admin', action='store_true')
    args = parser.parse_args()
    store = store_from_env()
    if args.action == 'list':
        for user in store.list_users():
            print(user['email'], 'admin' if user['admin'] else 'member')
    elif not args.email:
        parser.error('email is required')
    elif args.action == 'allow':
        store.allow(args.email, args.admin)
        print('Allowlist updated.')
    else:
        store.revoke(args.email)
        print('Access, pending codes and logins revoked.')
