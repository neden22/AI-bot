import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY, price TEXT NOT NULL,
 source TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, symbol TEXT NOT NULL,
 price TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS samples_symbol ON samples(symbol, id DESC);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, symbol TEXT NOT NULL,
 kind TEXT NOT NULL, status TEXT NOT NULL, budget TEXT NOT NULL,
 limit_price TEXT, qty TEXT, entry TEXT, stop TEXT, target TEXT,
 exit TEXT, pnl TEXT, reason TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
 level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 result TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, bucket TEXT UNIQUE NOT NULL,
 ts REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, report_id INTEGER UNIQUE NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_try REAL NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY, ts REAL NOT NULL, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
 question TEXT NOT NULL, answer TEXT NOT NULL, provider TEXT NOT NULL);
'''


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path):
    con = sqlite3.connect(path, timeout=10, isolation_level=None, factory=Connection)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=10000')
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA synchronous=FULL')
    return con


def initialize(path, feed):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript(SCHEMA)
        defaults = {'cash': '10000', 'auto': False, 'killed': False, 'feed': feed,
                    'heartbeat': 0, 'worker_pid': None, 'daily_day': '',
                    'daily_equity': '10000', 'daily_tripped': False}
        for key, value in defaults.items():
            con.execute('INSERT OR IGNORE INTO meta VALUES(?, ?)', (key, json.dumps(value)))
        stored_feed = get(con, 'feed')
        if stored_feed != feed:
            raise ValueError('Use a separate ORBIT_DB when changing feed; simulation and market data cannot mix.')


def get(con, key):
    row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def put(con, key, value):
    con.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)', (key, json.dumps(value)))


def event(con, message, level='info'):
    con.execute('INSERT INTO events(ts,level,message) VALUES(?,?,?)', (time.time(), level, message))


@contextmanager
def transaction(con):
    con.execute('BEGIN IMMEDIATE')
    try:
        yield
        con.commit()
    except BaseException:
        con.rollback()
        raise


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]
