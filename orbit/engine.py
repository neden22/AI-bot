"""Single-writer paper execution engine. All methods run in a DB transaction."""
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from . import db

D = Decimal
FEE = D('.001')
SLIPPAGE = D('.0005')


def amount(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Некоректне число') from None
    if not result.is_finite() or result <= 0 or result > D('1000000000'):
        raise ValueError('Потрібне скінченне додатне число')
    return result


def order_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError('Некоректний ID ордера')
    return value


class Engine:
    def __init__(self, con, config):
        self.con, self.config = con, config

    def active(self):
        return db.rows(self.con, "SELECT * FROM orders WHERE status IN ('pending','open')")

    def price(self, symbol, fresh=True):
        row = self.con.execute('SELECT * FROM quotes WHERE symbol=?', (symbol,)).fetchone()
        if not row or (fresh and not 0 <= time.time() - row['ts'] <= self.config.max_quote_age):
            raise ValueError('Немає свіжої котировки. Торгівлю заблоковано.')
        return D(row['price'])

    def portfolio(self):
        cash = D(db.get(self.con, 'cash'))
        total, reserved, unrealized = cash, D(0), D(0)
        for row in self.active():
            budget = D(row['budget'])
            if row['status'] == 'pending':
                reserved += budget
            else:
                value = D(row['qty']) * self.price(row['symbol'], fresh=False) * (1 - SLIPPAGE) * (1 - FEE)
                total += value
                unrealized += value - budget
        closed = db.rows(self.con, "SELECT pnl FROM orders WHERE status='closed'")
        realized = sum((D(r['pnl']) for r in closed), D(0))
        return {'cash': str(cash), 'reserved': str(reserved), 'available': str(cash - reserved),
                'equity': str(total), 'realized': str(realized), 'unrealized': str(unrealized),
                'closed': len(closed), 'wins': sum(D(r['pnl']) > 0 for r in closed)}

    def daily_guard(self):
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        equity = D(self.portfolio()['equity'])
        if db.get(self.con, 'daily_day') != day:
            db.put(self.con, 'daily_day', day)
            db.put(self.con, 'daily_equity', str(equity))
            db.put(self.con, 'daily_tripped', False)
        baseline = D(db.get(self.con, 'daily_equity'))
        if equity <= baseline * D('.97') and not db.get(self.con, 'daily_tripped'):
            db.put(self.con, 'daily_tripped', True)
            db.put(self.con, 'auto', False)
            self.cancel_pending('daily loss limit')
            db.event(self.con, 'Ліміт втрати 3% за UTC-добу: нові входи заблоковані.', 'error')

    def entry_guard(self, symbol, budget):
        self.daily_guard()
        if db.get(self.con, 'killed') or db.get(self.con, 'daily_tripped'):
            raise ValueError('Захист активний: нові входи заблоковані')
        active = self.active()
        if len(active) >= 3 or any(r['symbol'] == symbol for r in active):
            raise ValueError('Ліміт: 3 позиції та 1 активний ордер на актив')
        portfolio = self.portfolio()
        if budget < D(10) or budget > D(portfolio['equity']) * D('.10'):
            raise ValueError('Розмір ордера: від $10 до 10% капіталу')
        if budget > D(portfolio['available']):
            raise ValueError('Недостатньо вільних паперових коштів')
        # Fixed 2% stop plus friction: planned risk remains under 1% equity.
        if budget * D('.023') > D(portfolio['equity']) * D('.01'):
            raise ValueError('Ризик перевищує 1% капіталу')
        self.price(symbol)

    def buy(self, payload, reason='manual'):
        symbol = payload.get('symbol')
        if symbol not in self.config.symbols:
            raise ValueError('Актив не підтримується')
        budget = amount(payload.get('budget', '250'))
        self.entry_guard(symbol, budget)
        kind = payload.get('kind', 'market')
        if kind not in ('market', 'limit'):
            raise ValueError('Невідомий тип ордера')
        limit = str(amount(payload.get('limit_price'))) if kind == 'limit' else None
        now = time.time()
        cursor = self.con.execute('''INSERT INTO orders(symbol,kind,status,budget,limit_price,reason,created,updated)
            VALUES(?,?,'pending',?,?,?,?,?)''', (symbol, kind, str(budget), limit, reason, now, now))
        order_id = cursor.lastrowid
        if kind == 'market':
            self.fill(order_id, self.price(symbol))
        db.event(self.con, f'PAPER #{order_id}: {symbol}, {kind}, ${budget}')
        return f'Паперовий ордер #{order_id} прийнято'

    def fill(self, order_id, price):
        row = dict(self.con.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
        if row['status'] != 'pending':
            return
        entry = price * (1 + SLIPPAGE)
        if row['kind'] == 'limit':
            entry = min(entry, D(row['limit_price']))
        budget = D(row['budget'])
        qty = budget / (entry * (1 + FEE))
        self.con.execute("UPDATE orders SET status='open',qty=?,entry=?,stop=?,target=?,updated=? WHERE id=?",
                         (str(qty), str(entry), str(entry * D('.98')), str(entry * D('1.04')), time.time(), order_id))
        db.put(self.con, 'cash', str(D(db.get(self.con, 'cash')) - budget))
        db.event(self.con, f'PAPER #{order_id}: виконано; SL −2%, TP +4%')

    def close(self, order_id, reason='manual'):
        row = self.con.execute("SELECT * FROM orders WHERE id=? AND status='open'", (order_id,)).fetchone()
        if not row:
            raise ValueError('Відкрита позиція не знайдена')
        exit_price = self.price(row['symbol']) * (1 - SLIPPAGE)
        proceeds = D(row['qty']) * exit_price * (1 - FEE)
        pnl = proceeds - D(row['budget'])
        self.con.execute("UPDATE orders SET status='closed',exit=?,pnl=?,reason=?,updated=? WHERE id=?",
                         (str(exit_price), str(pnl), reason, time.time(), order_id))
        db.put(self.con, 'cash', str(D(db.get(self.con, 'cash')) + proceeds))
        db.event(self.con, f'PAPER #{order_id}: {reason}, P&L ${pnl:.2f}')
        return f'Позицію #{order_id} закрито: ${pnl:.2f}'

    def cancel_pending(self, reason):
        self.con.execute("UPDATE orders SET status='cancelled',reason=?,updated=? WHERE status='pending'",
                         (reason, time.time()))

    def on_quotes(self):
        self.daily_guard()
        for row in self.active():
            try:
                price = self.price(row['symbol'])
            except ValueError:
                continue
            if row['status'] == 'open':
                if price <= D(row['stop']):
                    self.close(row['id'], 'stop-loss')
                elif price >= D(row['target']):
                    self.close(row['id'], 'take-profit')
        self.daily_guard()
        if not db.get(self.con, 'killed') and not db.get(self.con, 'daily_tripped'):
            for row in self.active():
                if row['status'] != 'pending':
                    continue
                try:
                    price = self.price(row['symbol'])
                except ValueError:
                    continue
                # Revalidate caps at fill time, excluding this reservation.
                portfolio = self.portfolio()
                if D(row['budget']) > D(portfolio['equity']) * D('.10'):
                    self.con.execute("UPDATE orders SET status='cancelled',reason='risk at fill',updated=? WHERE id=?",
                                     (time.time(), row['id']))
                elif price <= D(row['limit_price']):
                    self.fill(row['id'], price)
        if db.get(self.con, 'auto') and not db.get(self.con, 'killed'):
            self.strategy()

    def signal(self, symbol):
        data = db.rows(self.con, 'SELECT price,ts FROM samples WHERE symbol=? ORDER BY id DESC LIMIT 21', (symbol,))[::-1]
        if len(data) < 21:
            return 'warmup', f'{len(data)}/21 зразків для SMA 5/20'
        if time.time() - data[-1]['ts'] > self.config.max_quote_age or any(
                b['ts'] - a['ts'] > self.config.interval * 3 for a, b in zip(data, data[1:])):
            return 'stale', 'Розрив або застарілі дані; очікування безперервної серії'
        prices = [D(r['price']) for r in data]
        fast, slow = sum(prices[-5:]) / 5, sum(prices[-20:]) / 20
        previous_fast, previous_slow = sum(prices[-6:-1]) / 5, sum(prices[-21:-1]) / 20
        signal = 'buy' if previous_fast <= previous_slow and fast > slow else (
            'exit' if previous_fast >= previous_slow and fast < slow else 'hold')
        return signal, f'SMA5 {fast:.2f} / SMA20 {slow:.2f}; {signal}'

    def strategy(self):
        for symbol in self.config.symbols:
            signal, _ = self.signal(symbol)
            try:
                if signal == 'buy' and not any(r['symbol'] == symbol for r in self.active()):
                    self.buy({'symbol': symbol, 'budget': '250'}, 'SMA 5/20 crossover')
                elif signal == 'exit':
                    for row in self.active():
                        if row['symbol'] == symbol and row['status'] == 'open':
                            self.close(row['id'], 'SMA exit')
            except ValueError as exc:
                db.event(self.con, f'{symbol}: {exc}', 'warning')

    def report(self, bucket=None):
        now = time.time()
        bucket = bucket or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')
        existing = self.con.execute('SELECT id FROM reports WHERE bucket=?', (bucket,)).fetchone()
        if existing:
            return f'Звіт #{existing[0]} вже існує'
        p = self.portfolio()
        lines = [f'ORBIT • PAPER • {db.get(self.con, "feed")}',
                 datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                 f'Капітал ${D(p["equity"]):.2f} | Вільно ${D(p["available"]):.2f}',
                 f'Закритий P&L ${D(p["realized"]):.2f} | Плаваючий ${D(p["unrealized"]):.2f}',
                 f'Автопілот: {db.get(self.con, "auto")} | Захист: {db.get(self.con, "killed")}',
                 'Котировки:']
        for symbol in self.config.symbols:
            row = self.con.execute('SELECT * FROM quotes WHERE symbol=?', (symbol,)).fetchone()
            age = max(0, int(now - row['ts'])) if row else None
            lines.append(f'{symbol}: ${D(row["price"]):.2f} ({age}s)' if row else f'{symbol}: даних немає')
            if row and age > self.config.max_quote_age:
                lines.append('⚠ Застарілі дані: виконання заблоковано')
        lines.append('Активні ордери:')
        for row in self.active():
            if row['status'] == 'pending':
                detail = f'limit ${row["limit_price"]}; бюджет зарезервовано'
            else:
                estimate = D(row['qty']) * self.price(row['symbol'], fresh=False) * (1-SLIPPAGE) * (1-FEE)
                detail = (f'entry ${D(row["entry"]):.2f}; SL ${D(row["stop"]):.2f}; '
                          f'TP ${D(row["target"]):.2f}; оцінка P&L ${estimate-D(row["budget"]):.2f}')
            lines.append(f'#{row["id"]} {row["symbol"]}: {row["status"]}, ${row["budget"]}; {detail}')
        if not self.active():
            lines.append('Немає')
        body = '\n'.join(lines)
        cursor = self.con.execute('INSERT INTO reports(bucket,ts,body) VALUES(?,?,?)', (bucket, now, body))
        self.con.execute('INSERT INTO outbox(report_id,body) VALUES(?,?)', (cursor.lastrowid, body))
        db.event(self.con, 'Звіт збережено у SQLite')
        return f'Звіт #{cursor.lastrowid} збережено'

    def command(self, kind, payload):
        if kind == 'buy':
            return self.buy(payload)
        if kind == 'close':
            return self.close(order_id(payload['id']))
        if kind == 'cancel':
            count = self.con.execute("UPDATE orders SET status='cancelled',updated=? WHERE id=? AND status='pending'",
                                     (time.time(), order_id(payload['id']))).rowcount
            if not count:
                raise ValueError('Очікуваний ордер не знайдено')
            return 'Ордер скасовано'
        if kind == 'auto':
            enabled = payload.get('enabled')
            if not isinstance(enabled, bool):
                raise ValueError('enabled має бути boolean')
            self.daily_guard()
            if enabled and (db.get(self.con, 'killed') or db.get(self.con, 'daily_tripped')):
                raise ValueError('Спочатку усунь блокування ризику')
            db.put(self.con, 'auto', enabled)
            return 'Автопілот увімкнено' if enabled else 'Автопілот на паузі; SL/TP працюють'
        if kind == 'kill':
            db.put(self.con, 'killed', True)
            db.put(self.con, 'auto', False)
            self.cancel_pending('kill switch')
            db.event(self.con, 'KILL: входи заблоковано, очікувані ордери скасовано. SL/TP активні.', 'error')
            return 'KILL активний. Відкриті позиції залишено під SL/TP.'
        if kind == 'arm':
            db.put(self.con, 'killed', False)
            db.event(self.con, 'Ручне зняття KILL; автопілот залишається на паузі')
            return 'KILL знято. Добовий ліміт ризику зберігається.'
        if kind == 'report':
            return self.report(f'manual-{time.time_ns()}')
        raise ValueError('Невідома команда')
