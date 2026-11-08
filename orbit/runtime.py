import json
import multiprocessing as mp
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from . import db, market
from .engine import Engine


class InstanceLock:
    """OS advisory lock; automatically released on process death."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('ORBIT is already running for this database') from None
        return self

    def __exit__(self, *_):
        self.file.close()


def drain_control(control, timeout=0):
    """A killed receiver cannot poison a shared multiprocessing condition lock."""
    try:
        if not control.poll(timeout):
            return False
        while True:
            if control.recv_bytes() == b'stop':
                return True
            if not control.poll():
                return False
    except EOFError:
        return True


def worker(config, control):
    if hasattr(signal, 'SIGINT'):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    con = db.connect(config.db)
    engine = Engine(con, config)
    with db.transaction(con):
        db.put(con, 'worker_pid', os.getpid())
        db.event(con, f'Торговий процес PID {os.getpid()} запущений')
    next_tick = 0
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        while not drain_control(control):
            # Durable commands and their effects commit atomically. A crash rolls both back.
            for command in db.rows(con, "SELECT * FROM commands WHERE status='queued' ORDER BY id LIMIT 20"):
                with db.transaction(con):
                    con.execute('SAVEPOINT command_effect')
                    try:
                        result = engine.command(command['kind'], json.loads(command['payload']))
                        status = 'done'
                    except (ValueError, KeyError, TypeError) as exc:
                        con.execute('ROLLBACK TO command_effect')
                        result, status = str(exc), 'failed'
                    con.execute('RELEASE command_effect')
                    con.execute('UPDATE commands SET status=?,result=? WHERE id=?', (status, result, command['id']))
            if time.monotonic() >= next_tick:
                futures = {symbol: pool.submit(market.quote, symbol, config.feed) for symbol in config.symbols}
                for symbol, future in futures.items():
                    try:
                        price, timestamp, source = future.result()
                        with db.transaction(con):
                            con.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?)', (symbol, price, source, timestamp))
                            con.execute('INSERT INTO samples(symbol,price,ts) VALUES(?,?,?)', (symbol, price, timestamp))
                    except Exception:
                        # Never persist request exception text: URLs may contain credentials in other adapters.
                        with db.transaction(con):
                            db.event(con, f'{symbol}: джерело котирувань недоступне; перевірте мережу.', 'warning')
                with db.transaction(con):
                    engine.on_quotes()
                    con.execute('INSERT INTO equity(ts,value) VALUES(?,?)', (time.time(), engine.portfolio()['equity']))
                    # Bound high-frequency samples; the order/audit/report history is retained.
                    con.execute('DELETE FROM samples WHERE ts<?', (time.time() - 7 * 86400,))
                    con.execute('DELETE FROM equity WHERE ts<?', (time.time() - 30 * 86400,))
                next_tick = time.monotonic() + config.interval
            with db.transaction(con):
                engine.report()  # UNIQUE UTC hour: restart-safe local report generation.
                db.put(con, 'heartbeat', time.time())
            if drain_control(control, 1):
                break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        with db.transaction(con):
            db.event(con, 'Торговий процес зупинено коректно')
        con.close()
        control.close()


def notifier(config, control):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    con = db.connect(config.db)
    try:
        while not drain_control(control, 2):
            if not token or not chat:
                continue
            row = con.execute("SELECT * FROM outbox WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 1",
                              (time.time(),)).fetchone()
            if not row:
                continue
            try:
                data = urlencode({'chat_id': chat, 'text': row['body']}).encode()
                request = Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data)
                with urlopen(request, timeout=8) as response:
                    result = json.loads(response.read(65536))
                if not result.get('ok'):
                    raise ValueError('Telegram did not acknowledge delivery')
                with db.transaction(con):
                    con.execute("UPDATE outbox SET status='sent',last_error=NULL WHERE id=?", (row['id'],))
            except Exception:
                with db.transaction(con):
                    attempts = row['attempts'] + 1
                    con.execute('UPDATE outbox SET attempts=?,next_try=?,last_error=? WHERE id=?',
                                (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)),
                                 'Delivery failed; check network, token and chat ID', row['id']))
    finally:
        con.close()
        control.close()


class WakeSignal:
    """The thread lock lives only in the parent; children communicate via OS pipes."""
    def __init__(self):
        self.sender = None
        self.lock = threading.Lock()

    def set(self):
        with self.lock:
            if self.sender is not None:
                try:
                    self.sender.send_bytes(b'wake')
                except (OSError, BrokenPipeError):
                    pass  # Commands are durable; the replacement worker will read them.


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.context = mp.get_context('spawn')
        self.stopping = False
        self.wake = WakeSignal()
        self.processes = {}
        self.restarts = []

    def start(self):
        self.spawn('engine', worker)
        self.spawn('notifier', notifier)

    def spawn(self, name, target):
        receiver, sender = self.context.Pipe(duplex=False)
        process = self.context.Process(name=f'orbit-{name}', target=target, args=(self.config, receiver))
        process.start()
        receiver.close()
        self.processes[name] = (process, target, sender)
        if name == 'engine':
            with self.wake.lock:
                self.wake.sender = sender

    def check(self):
        for name, (process, target, sender) in list(self.processes.items()):
            if not process.is_alive() and not self.stopping:
                process.join()
                self.restarts = [stamp for stamp in self.restarts if time.monotonic() - stamp < 60]
                if len(self.restarts) >= 3:
                    raise RuntimeError('Worker restart budget exhausted; inspect data and logs')
                self.restarts.append(time.monotonic())
                with db.connect(self.config.db) as con, db.transaction(con):
                    db.event(con, f'{name}: завершився з кодом {process.exitcode}; перезапуск', 'error')
                with self.wake.lock:
                    sender.close()
                    if name == 'engine':
                        self.wake.sender = None
                self.spawn(name, target)

    def close(self):
        self.stopping = True
        with self.wake.lock:
            self.wake.sender = None
            for _, _, sender in self.processes.values():
                try:
                    sender.send_bytes(b'stop')
                except (OSError, BrokenPipeError):
                    pass
        for process, _, sender in self.processes.values():
            process.join(timeout=12)
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join()
            sender.close()
