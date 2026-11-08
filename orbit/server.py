import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from . import assistant, db
from .engine import Engine

STATIC = Path(__file__).parent / 'static'


def snapshot(config):
    with db.connect(config.db) as con:
        con.execute('BEGIN')
        engine = Engine(con, config)
        data = {'portfolio': engine.portfolio(), 'orders': db.rows(con, 'SELECT * FROM orders ORDER BY id DESC LIMIT 100'),
                'quotes': db.rows(con, 'SELECT * FROM quotes ORDER BY symbol'),
                'events': db.rows(con, 'SELECT * FROM events ORDER BY id DESC LIMIT 30'),
                'reports': db.rows(con, '''SELECT r.*,o.status AS delivery,o.attempts FROM reports r
                    LEFT JOIN outbox o ON o.report_id=r.id ORDER BY r.id DESC LIMIT 20'''),
                'equity': db.rows(con, 'SELECT ts,value FROM (SELECT * FROM equity ORDER BY id DESC LIMIT 200) ORDER BY id'),
                'signals': {s: engine.signal(s) for s in config.symbols},
                'meta': {r['key']: json.loads(r['value']) for r in con.execute('SELECT * FROM meta')},
                'messages': db.rows(con, 'SELECT * FROM messages ORDER BY id DESC LIMIT 10'),
                'now': time.time(), 'quote_max_age': config.max_quote_age,
                'telegram_configured': bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')),
                'llm_configured': bool(os.getenv('OLLAMA_MODEL')), 'mode': 'PAPER'}
        con.commit()
        return data


def create_server(config, wake):
    token = secrets.token_urlsafe(32)
    chat_lock = threading.Lock()
    allowed_hosts = {f'127.0.0.1:{config.port}', f'localhost:{config.port}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(35)

        def reply(self, value, status=200, content_type='application/json; charset=utf-8', filename=None):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            if self.headers.get('Host') not in allowed_hosts:
                self.reply({'error': 'Invalid Host'}, 403)
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            path = urlparse(self.path).path
            if path == '/api/state':
                state = snapshot(config)
                state['csrf'] = token
                self.reply(state)
            elif path.startswith('/api/commands/'):
                command_id = path.rsplit('/', 1)[-1]
                with db.connect(config.db) as con:
                    row = con.execute('SELECT id,status,result FROM commands WHERE id=?', (command_id,)).fetchone()
                self.reply(dict(row) if row else {'error': 'Command not found'}, 200 if row else 404)
            elif path == '/api/export.csv':
                with db.connect(config.db) as con:
                    data = db.rows(con, 'SELECT * FROM orders ORDER BY id')
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=['id','symbol','kind','status','budget','limit_price',
                    'qty','entry','stop','target','exit','pnl','reason','created','updated'])
                writer.writeheader()
                writer.writerows(data)
                self.reply(output.getvalue().encode('utf-8-sig'), content_type='text/csv; charset=utf-8', filename='orbit-orders.csv')
            elif path in ('/', '/app.js', '/style.css'):
                filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                self.reply((STATIC / filename).read_bytes(), content_type=mime + '; charset=utf-8')
            else:
                self.reply({'error': 'Not found'}, 404)

        def do_POST(self):
            if not self.trusted():
                return
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{h}' for h in allowed_hosts}:
                self.reply({'error': 'Invalid Origin'}, 403)
                return
            if not hmac.compare_digest(self.headers.get('X-Orbit-Token', ''), token):
                self.reply({'error': 'Invalid CSRF token'}, 403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Body size limit: 16 KiB')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('JSON object required')
                if self.path == '/api/commands':
                    kind = payload.get('kind')
                    if kind not in ('buy','close','cancel','auto','kill','arm','report'):
                        raise ValueError('Unknown command')
                    request_id = payload.get('request_id')
                    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
                        raise ValueError('request_id required (8–100 characters)')
                    body = payload.get('payload', {})
                    if not isinstance(body, dict):
                        raise ValueError('payload must be an object')
                    with db.connect(config.db) as con, db.transaction(con):
                        queued = con.execute("SELECT COUNT(*) FROM commands WHERE status='queued'").fetchone()[0]
                        if queued > 100:
                            raise ValueError('Command queue is full')
                        existing = con.execute('SELECT * FROM commands WHERE request_id=?', (request_id,)).fetchone()
                        serialized = json.dumps(body, sort_keys=True)
                        if existing and (existing['kind'] != kind or existing['payload'] != serialized):
                            raise ValueError('request_id already used for different command')
                        con.execute('INSERT OR IGNORE INTO commands(request_id,kind,payload,created) VALUES(?,?,?,?)',
                                    (request_id, kind, serialized, time.time()))
                        row = con.execute('SELECT id,status,result FROM commands WHERE request_id=?', (request_id,)).fetchone()
                    wake.set()
                    self.reply(dict(row), 202)
                elif self.path == '/api/chat':
                    question = payload.get('question', '')
                    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
                        raise ValueError('Question length: 1–2000 characters')
                    if not chat_lock.acquire(blocking=False):
                        self.reply({'error': 'Analyst is busy'}, 429)
                        return
                    try:
                        self.reply(assistant.answer(config, question.strip()))
                    finally:
                        chat_lock.release()
                else:
                    self.reply({'error': 'Not found'}, 404)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.reply({'error': str(exc)}, 400)
            except sqlite3.Error:
                self.reply({'error': 'Database unavailable; inspect service logs'}, 503)

    server = ThreadingHTTPServer(('127.0.0.1', config.port), Handler)
    server.daemon_threads = True
    return server
