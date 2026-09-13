"""ASGI boundary: authentication, ownership, CSRF, bounds and persistent quotas."""
import base64
import hashlib
import html
from pathlib import Path
import json
import os
import re
from urllib.parse import urlsplit, parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from .auth import AuthError, store_from_env

PAID = {'/api/infer-spec', '/api/suggest-changes', '/api/suggest-segments', '/api/cohort/generate'}
MAX_BODY = 65536
COOKIE = 'sgo_session'


def check_payload(value, depth=0):
    if depth > 12:
        raise AuthError('Request nesting too deep', 422)
    if isinstance(value, str) and len(value) > 6000:
        raise AuthError('Text is limited to 6,000 characters', 422)
    if isinstance(value, list):
        if len(value) > 50:
            raise AuthError('Too many items', 422)
        for item in value:
            check_payload(item, depth+1)
    if isinstance(value, dict):
        if len(value) > 40:
            raise AuthError('Too many fields', 422)
        for key, item in value.items():
            check_payload(key, depth+1)
            check_payload(item, depth+1)


def bounded_int(value, maximum, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise AuthError(f'{name} must be between 1 and {maximum}', 422)


def check_workload(path, body, query):
    check_payload(body)
    if isinstance(body, dict):
        if 'parallel' in body:
            bounded_int(body['parallel'], 2, 'Parallel calls')
        if 'segments' in body:
            segments = body['segments']
            if not isinstance(segments, list) or not 1 <= len(segments) <= 10:
                raise AuthError('Use 1–10 audience segments', 422)
            total = 0
            for segment in segments:
                if not isinstance(segment, dict):
                    raise AuthError('Invalid segment', 422)
                count = segment.get('count', 8)
                bounded_int(count, 50, 'Panel size')
                total += count
            bounded_int(total, 50, 'Panel size')
        if 'changes' in body:
            if not isinstance(body['changes'], list) or not 1 <= len(body['changes']) <= 3:
                raise AuthError('Use 1–3 changes', 422)
            for change in body['changes']:
                if not isinstance(change, dict) or not all(isinstance(change.get(k), str) for k in ('id', 'label', 'description')):
                    raise AuthError('Each change needs an id, label and description', 422)
    if path.startswith('/api/cohort/upload/'):
        if not isinstance(body, list) or not 1 <= len(body) <= 50 or any(not isinstance(p, dict) for p in body):
            raise AuthError('Upload 1–50 personas', 422)
    for key, maximum in [('parallel', 2), ('sample', 5)]:
        if key in query:
            try:
                if len(query[key]) != 1:
                    raise ValueError()
                number = int(query[key][0])
            except ValueError:
                raise AuthError(f'Invalid {key}', 422) from None
            bounded_int(number, maximum, key)
    if 'probes' in query:
        if len(query['probes']) != 1:
            raise AuthError('Duplicate probes parameter', 422)
        probes = query['probes'][0].split(',')
        if len(probes) > 3 or len(set(probes)) != len(probes) or any(p not in {'framing', 'authority', 'order'} for p in probes):
            raise AuthError('Invalid probes', 422)


class SecurityMiddleware:
    def __init__(self, app, sessions, model_context=None):
        self.app = app
        self.model_context = model_context
        self.sessions = sessions
        self.auth = None
        self.busy = False
        self.origin = os.getenv('APP_ORIGIN', 'http://127.0.0.1:8000').rstrip('/')
        url = urlsplit(self.origin)
        self.local = url.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (url.scheme != 'https' and not (self.local and url.scheme == 'http')) or not url.netloc or url.path or url.query or url.fragment or url.username or url.password:
            raise ValueError('APP_ORIGIN must be an HTTPS origin (HTTP is permitted only for localhost)')
        self.cookie = COOKIE if self.local else '__Host-sgo_session'
        self.host = url.netloc.lower()
        # Allow only the checked-in inline script/handlers, never arbitrary model HTML.
        source = (Path(__file__).parent / 'static' / 'index.html').read_text()
        scripts = re.findall(r'<script>([\s\S]*?)</script>', source)
        handlers = [html.unescape(h) for h in re.findall(r'\bon[a-z]+="([^"]*)"', source) if '${' not in h]
        hashes = sorted({"'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'" for text in scripts + handlers})
        self.script_policy = "script-src 'self' 'unsafe-hashes' " + ' '.join(hashes)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = scope['path']
        method = scope['method']
        headers = request.headers
        async def safe_send(message):
            if message['type'] == 'http.response.start':
                extra = {
                    'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
                    'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer',
                    'permissions-policy': 'camera=(), microphone=(), geolocation=()',
                    'content-security-policy': "default-src 'self'; " + self.script_policy + "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                }
                if not self.local:
                    extra['strict-transport-security'] = 'max-age=31536000'
                message['headers'] = [(k,v) for k,v in message.get('headers', []) if k.decode().lower() not in extra]
                message['headers'] += [(k.encode(),v.encode()) for k,v in extra.items()]
            await send(message)
        acquired = False
        context_token = None
        try:
            if headers.get('host', '').lower() != self.host:
                raise AuthError('Invalid host', 400)
            if method in {'POST', 'PATCH', 'PUT', 'DELETE'} or '/stream/' in path:
                if headers.get('x-sgo-request') != '1' or headers.get('origin', self.origin) != self.origin or headers.get('sec-fetch-site', 'same-origin') not in {'same-origin', 'none'}:
                    raise AuthError('Same-origin request required', 403)
            public = path in {'/login', '/healthz', '/static/login.js', '/static/login.css'}
            if not public:
                if self.auth is None:
                    try:
                        self.auth = store_from_env()
                    except (ValueError, OSError):
                        raise AuthError('Access is not configured. Contact the administrator.', 503) from None
                scope.setdefault('state', {})['auth'] = self.auth
                scope['state']['secure_cookie'] = not self.local
                scope['state']['cookie_name'] = self.cookie
                user = self.auth.user(request.cookies.get(self.cookie))
                scope['state']['user'] = user
                scope['state'].update(api_key='', base_url='', model='')
                if self.model_context is not None and user:
                    context_token = self.model_context.set((self.auth, request.cookies.get(self.cookie)))
                if not path.startswith('/auth/'):
                    if not user:
                        if path == '/':
                            return await RedirectResponse('/login', status_code=303)(scope, receive, safe_send)
                        raise AuthError('Login required', 401)
                    if path.startswith('/api/'):
                        # Match all current resource paths before FastAPI dispatch.
                        for prefix in ('/api/session/', '/api/calibrate/', '/api/cohort/upload/', '/api/evaluate/stream/', '/api/counterfactual/prepare/', '/api/counterfactual/stream/', '/api/bias-audit/stream/', '/api/results/', '/api/report/'):
                            if path.startswith(prefix):
                                sid = path[len(prefix):].split('/')[0]
                                resource = self.sessions.get(sid)
                                if not resource or resource.get('owner') != user['email']:
                                    raise AuthError('Session not found', 404)
                        if any(headers.get(k) for k in ('x-llm-key', 'x-llm-base', 'x-llm-model')):
                            raise AuthError('Model configuration is managed by the administrator', 400)
                        if path == '/api/nemotron/setup' and not user['admin']:
                            raise AuthError('Administrator access required', 403)
            # Buffer a bounded body before handlers or JSON parsing. Check actual chunks too.
            if int(headers.get('content-length', '0')) > MAX_BODY:
                raise AuthError('Request too large (64 KiB maximum)', 413)
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > MAX_BODY:
                    raise AuthError('Request too large (64 KiB maximum)', 413)
                if not message.get('more_body'):
                    break
            data = None
            if body:
                try:
                    data = json.loads(body)
                except (ValueError, RecursionError):
                    raise AuthError('Invalid JSON', 400) from None
                check_payload(data)
            if path.startswith('/api/'):
                check_workload(path, data, parse_qs(scope['query_string'].decode(), keep_blank_values=True))
                if path == '/api/nemotron/setup' and isinstance(data, dict) and data.get('path'):
                    raise AuthError('Dataset paths are managed by the server', 403)
                paid = path in PAID or '/stream/' in path
                limited = paid or path == '/api/nemotron/setup'
                if limited:
                    if self.busy:
                        raise AuthError('Another task is running. Please wait.', 429)
                    if paid:
                        self.auth.charge(user['email'], heavy='/stream/' in path)
                    self.busy = True
                    acquired = True
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            await self.app(scope, replay, safe_send)
        except AuthError as error:
            await JSONResponse({'detail': str(error)}, status_code=error.status,
                               headers={'Retry-After': str(error.retry_after)} if error.retry_after else None)(scope, receive, safe_send)
        except (ValueError, UnicodeError):
            await JSONResponse({'detail': 'Invalid request'}, status_code=400)(scope, receive, safe_send)
        finally:
            if acquired:
                self.busy = False
            if context_token is not None:
                self.model_context.reset(context_token)
