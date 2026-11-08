"""Walt OAuth/PKCE and read-only MCP discovery. No tools/call or trading access."""
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ISSUER = 'https://walletbot.me/bot-relay/public-api'
RESOURCE = ISSUER + '/mcp'
RESOURCE_METADATA = ISSUER + '/oauth/v1/protected-resource'
SERVER_METADATA = 'https://walletbot.me/.well-known/oauth-authorization-server/bot-relay/public-api'
SCOPE = 'perps.readonly'
VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
MAX_BODY = 262144


class WaltError(Exception):
    """Only safe, fixed messages may be exposed to the browser."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # Never forward tokens, codes or a PKCE verifier to a redirected endpoint.
        return None


def request(url, payload=None, bearer=None, headers=None, rpc_id=None, notification=False, form=False):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'walletbot.me' or parts.username or parts.password:
        raise WaltError('Недозволена адреса сервера Walt')
    hdr = {'Accept': 'application/json, text/event-stream', 'User-Agent': 'ORBIT/0.2'}
    hdr.update(headers or {})
    if bearer:
        hdr['Authorization'] = 'Bearer ' + bearer
    data = None
    if payload is not None:
        hdr['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = urlencode(payload).encode() if form else json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=hdr), timeout=12) as response:
            session = response.headers.get('Mcp-Session-Id')
            if notification and response.status in (200, 202, 204):
                return {}, session
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                fields, count, deadline = [], 0, time.monotonic()+25
                while count < MAX_BODY and time.monotonic() < deadline:
                    line = response.readline(65536)
                    if not line:
                        break
                    count += len(line)
                    line = line.decode('utf-8').rstrip('\r\n')
                    if line.startswith('data:'):
                        fields.append(line[5:].lstrip(' '))
                    elif not line and fields:
                        result = json.loads('\n'.join(fields))
                        fields = []
                        if isinstance(result, dict) and result.get('id') == rpc_id:
                            return result, session
                raise WaltError('MCP не повернув відповідь у межах ліміту')
            body = response.read(MAX_BODY+1)
            if len(body) > MAX_BODY:
                raise WaltError('Відповідь Walt перевищує ліміт розміру')
            result = json.loads(body)
            if not isinstance(result, dict):
                raise WaltError('Некоректна відповідь Walt')
            return result, session
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 401:
            raise WaltError('Walt відхилив авторизацію. Підключи акаунт повторно.') from None
        raise WaltError(f'Walt повернув HTTP {status}; спробуй пізніше.') from None
    except (URLError, OSError, ValueError):
        # Network exceptions can contain URLs with credentials; do not log or expose them.
        raise WaltError('Не вдалося отримати коректну відповідь Walt. Перевір мережу.') from None


class Connector:
    def __init__(self, config):
        self.redirect_uri = f'http://127.0.0.1:{config.port}/oauth/walt/callback'
        # Separate from SQLite: neither exports nor LLM context include credentials.
        self.path = Path(config.db).resolve().parent / f'walt-oauth-{config.port}.json'
        self.lock = threading.RLock()
        self.pending = None
        self.tools = []
        self.last_check = None
        self.last_error = None
        self.session = None
        self.protocol = VERSIONS[0]
        self.saved = self.load()

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(value, dict) and value.get('redirect_uri') == self.redirect_uri:
                return value
        except (OSError, ValueError):
            pass
        return {'redirect_uri': self.redirect_uri}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(self.saved, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def status(self):
        # Polling state does not make network calls or wait for an OAuth request lock.
        return {'authorized': bool(self.saved.get('access_token')), 'scope': SCOPE,
                'verified': self.last_check is not None and self.last_error is None,
                'last_check': self.last_check, 'error': self.last_error,
                'tools': list(self.tools), 'pending': self.pending is not None}

    def discover(self):
        resource, _ = request(RESOURCE_METADATA)
        metadata, _ = request(SERVER_METADATA)
        if (resource.get('resource') != RESOURCE or ISSUER not in resource.get('authorization_servers', [])
                or SCOPE not in resource.get('scopes_supported', []) or metadata.get('issuer') != ISSUER
                or 'S256' not in metadata.get('code_challenge_methods_supported', [])
                or 'none' not in metadata.get('token_endpoint_auth_methods_supported', [])):
            raise WaltError('Метадані Walt не відповідають очікуваному OAuth/PKCE протоколу')
        for key, suffix in [('authorization_endpoint', 'authorize'), ('token_endpoint', 'token'),
                            ('registration_endpoint', 'register')]:
            if metadata.get(key) != ISSUER + '/oauth/v1/' + suffix:
                raise WaltError('Адреси OAuth Walt змінилися; потрібна перевірка інтеграції')
        return metadata

    def begin(self):
        with self.lock:
            metadata = self.discover()
            if not self.saved.get('client_id'):
                registration, _ = request(metadata['registration_endpoint'], {
                    'client_name': 'ORBIT Read-only', 'redirect_uris': [self.redirect_uri],
                    'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                    'token_endpoint_auth_method': 'none', 'scope': SCOPE})
                client_id = registration.get('client_id')
                if not isinstance(client_id, str) or not client_id:
                    raise WaltError('Walt не видав client_id для ORBIT')
                if registration.get('token_endpoint_auth_method', 'none') != 'none':
                    raise WaltError('Walt запитує непідтримуваний спосіб авторизації клієнта')
                self.saved['client_id'] = client_id
                self.save()
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            self.pending = {'state': state, 'verifier': verifier, 'created': time.monotonic()}
            url = metadata['authorization_endpoint'] + '?' + urlencode({
                'client_id': self.saved['client_id'], 'redirect_uri': self.redirect_uri,
                'response_type': 'code', 'scope': SCOPE, 'resource': RESOURCE,
                'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
            return {'authorization_url': url, 'scope': SCOPE}

    def accept_token(self, payload, previous_refresh=None):
        access = payload.get('access_token')
        if not isinstance(access, str) or not access or len(access) > 65536:
            raise WaltError('Walt не повернув коректний access token')
        if not isinstance(payload.get('token_type'), str) or payload['token_type'].lower() != 'bearer':
            raise WaltError('Непідтримуваний тип токена Walt')
        # Never accept a silently widened permission set.
        scope = payload.get('scope', SCOPE)
        if not isinstance(scope, str) or set(scope.split()) != {SCOPE}:
            raise WaltError('Walt видав інші права. ORBIT дозволяє лише perps.readonly.')
        refresh = payload.get('refresh_token', previous_refresh)
        if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 65536):
            raise WaltError('Walt повернув некоректний refresh token')
        try:
            lifetime = float(payload.get('expires_in', 3600))
        except (TypeError, ValueError):
            raise WaltError('Некоректний час дії токена Walt') from None
        if not math.isfinite(lifetime) or not 0 < lifetime <= 365*86400:
            raise WaltError('Некоректний час дії токена Walt')
        self.saved.update(access_token=access, refresh_token=refresh,
                          expires_at=time.time()+lifetime, scope=SCOPE)
        self.save()
        self.session = None
        self.last_check = None
        self.last_error = None
        self.tools = []

    def callback(self, query):
        with self.lock:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=20)
            state = params.get('state', [])
            pending = self.pending
            if (not pending or len(state) != 1 or not hmac.compare_digest(state[0], pending['state'])
                    or time.monotonic()-pending['created'] > 600):
                raise WaltError('Підключення прострочене або state не збігається. Почни підключення знову.')
            self.pending = None  # One-use, including denial or failed exchange.
            if 'error' in params:
                raise WaltError('Підключення не підтверджено у Walt')
            code = params.get('code', [])
            if len(code) != 1 or not code[0] or len(code[0]) > 8192:
                raise WaltError('Walt не передав коректний код авторизації')
            payload, _ = request(ISSUER+'/oauth/v1/token', {
                'grant_type': 'authorization_code', 'code': code[0], 'client_id': self.saved['client_id'],
                'redirect_uri': self.redirect_uri, 'code_verifier': pending['verifier'], 'resource': RESOURCE}, form=True)
            self.accept_token(payload)

    def access(self):
        if not self.saved.get('access_token'):
            raise WaltError('Спочатку підключи Walt')
        if time.time()+60 < self.saved.get('expires_at', 0):
            return self.saved['access_token']
        refresh = self.saved.get('refresh_token')
        if not refresh:
            raise WaltError('Сесія Walt завершилася. Підключи акаунт повторно.')
        payload, _ = request(ISSUER+'/oauth/v1/token', {
            'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': self.saved['client_id'],
            'resource': RESOURCE, 'scope': SCOPE}, form=True)
        self.accept_token(payload, refresh)
        return self.saved['access_token']

    def rpc(self, method, params, rpc_id):
        # Defense in depth: this connector cannot invoke even a read-labelled server tool.
        if method not in ('initialize', 'notifications/initialized', 'tools/list'):
            raise WaltError('Виклик MCP-інструментів у цій версії вимкнено')
        headers = {'MCP-Protocol-Version': self.protocol}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if rpc_id is not None:
            body['id'] = rpc_id
        result, session = request(RESOURCE, body, bearer=self.access(), headers=headers,
                                  rpc_id=rpc_id, notification=rpc_id is None)
        if session:
            if len(session) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                raise WaltError('Некоректний MCP session ID')
            self.session = session
        if rpc_id is None:
            return {}
        if result.get('id') != rpc_id or result.get('error') is not None or not isinstance(result.get('result'), dict):
            raise WaltError('MCP Walt повернув помилку протоколу')
        return result['result']

    def check(self):
        with self.lock:
            try:
                self.access()
                self.session = None
                self.protocol = VERSIONS[0]
                result = self.rpc('initialize', {'protocolVersion': self.protocol, 'capabilities': {},
                    'clientInfo': {'name': 'ORBIT Read-only', 'version': '0.2.0'}}, 1)
                protocol = result.get('protocolVersion')
                if protocol not in VERSIONS:
                    raise WaltError('Walt обрав непідтримувану версію MCP')
                self.protocol = protocol
                self.rpc('notifications/initialized', {}, None)
                tools, cursor, seen = [], None, set()
                for page in range(10):
                    result = self.rpc('tools/list', {'cursor':cursor} if cursor else {}, page+2)
                    items = result.get('tools', [])
                    if not isinstance(items, list):
                        raise WaltError('Некоректний список MCP-інструментів')
                    for item in items:
                        if isinstance(item, dict) and isinstance(item.get('name'), str):
                            tools.append({'name':item['name'][:200], 'description':str(item.get('description',''))[:500]})
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise WaltError('Некоректна пагінація MCP')
                    seen.add(cursor)
                else:
                    raise WaltError('Забагато сторінок MCP-інструментів')
                self.tools, self.last_check, self.last_error = tools, time.time(), None
                return self.status()
            except WaltError as exc:
                self.last_error = str(exc)
                raise

    def disconnect(self):
        with self.lock:
            self.pending = None
            self.saved = {'redirect_uri': self.redirect_uri}
            self.tools, self.last_check, self.last_error, self.session = [], None, None, None
            if self.path.exists():
                self.path.unlink()
            return {'disconnected': True, 'note': 'Локальні токени видалено. Дозвіл у Walt скасовується окремо.'}
