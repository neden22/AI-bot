import json
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from orbit import db
from orbit.config import Config
from orbit.engine import Engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(Config(), db=str(Path(self.temp.name) / 'test.db'), feed='simulation')
        db.initialize(self.config.db, self.config.feed)
        self.con = db.connect(self.config.db)
        self.engine = Engine(self.con, self.config)
        self.set_price('100')

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def set_price(self, value, age=0, symbol='BTC-USD'):
        self.con.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?)',
                         (symbol, value, 'TEST', time.time()-age))

    def buy(self, **kwargs):
        with db.transaction(self.con):
            self.engine.buy({'symbol':'BTC-USD','budget':'250',**kwargs})

    def test_round_trip_accounts_for_both_fees_and_slippage(self):
        self.buy()
        row = self.engine.active()[0]
        expected_qty = D(250) / (D(100) * D('1.0005') * D('1.001'))
        self.assertEqual(D(row['qty']), expected_qty)
        with db.transaction(self.con):
            self.engine.close(row['id'])
        cash = D(db.get(self.con, 'cash'))
        expected_pnl = expected_qty * D(100) * D('.9995') * D('.999') - D(250)
        self.assertEqual(cash, D(10000)+expected_pnl)
        self.assertLess(cash, D(10000))

    def test_pending_reserves_without_spending_then_fill_never_exceeds_limit(self):
        self.buy(kind='limit', limit_price='90')
        p = self.engine.portfolio()
        self.assertEqual(D(p['cash']), D(10000))
        self.assertEqual(D(p['available']), D(9750))
        with db.transaction(self.con):
            self.engine.on_quotes()
        self.assertEqual(self.engine.active()[0]['status'], 'pending')
        self.set_price('90')
        with db.transaction(self.con):
            self.engine.on_quotes()
        self.assertEqual(D(self.engine.active()[0]['entry']), D(90))
        self.assertEqual(D(db.get(self.con, 'cash')), D(9750))

    def test_cancel_releases_reservation(self):
        self.buy(kind='limit', limit_price='90')
        with db.transaction(self.con):
            self.engine.command('cancel', {'id':1})
        self.assertEqual(D(self.engine.portfolio()['available']), D(10000))

    def test_stale_quotes_block_entry_and_exit(self):
        self.buy()
        self.set_price('90', age=91)
        with self.assertRaises(ValueError):
            self.engine.close(1)
        self.assertEqual(self.engine.active()[0]['status'], 'open')
        self.set_price('100', symbol='ETH-USD', age=91)
        with self.assertRaises(ValueError):
            self.buy(symbol='ETH-USD')

    def test_stop_loss_fills_at_gap_price_not_fictional_stop(self):
        self.buy()
        self.set_price('80')
        with db.transaction(self.con):
            self.engine.on_quotes()
        row = self.con.execute('SELECT * FROM orders').fetchone()
        self.assertEqual(row['reason'], 'stop-loss')
        self.assertEqual(D(row['exit']), D(80)*D('.9995'))
        self.assertLess(D(row['pnl']), D('-49'))

    def test_take_profit(self):
        self.buy()
        self.set_price('105')
        with db.transaction(self.con):
            self.engine.on_quotes()
        row = self.con.execute('SELECT * FROM orders').fetchone()
        self.assertEqual(row['reason'], 'take-profit')
        self.assertGreater(D(row['pnl']), 0)

    def test_kill_survives_restart_cancels_pending_but_exits_continue(self):
        self.buy()
        self.set_price('100', symbol='ETH-USD')
        self.buy(symbol='ETH-USD', kind='limit', limit_price='90')
        with db.transaction(self.con):
            self.engine.command('kill', {})
        with db.connect(self.config.db) as other:
            self.assertTrue(db.get(other, 'killed'))
            self.assertEqual(other.execute('SELECT status FROM orders WHERE id=2').fetchone()[0], 'cancelled')
        with self.assertRaises(ValueError):
            self.buy(symbol='SOL-USD')
        self.set_price('105')
        with db.transaction(self.con):
            self.engine.on_quotes()
        self.assertFalse(self.engine.active())

    def test_daily_loss_guard_latches_even_after_recovery(self):
        self.buy(budget='1000')
        self.set_price('60')
        with db.transaction(self.con):
            self.engine.daily_guard()
        self.assertTrue(db.get(self.con, 'daily_tripped'))
        self.set_price('100')
        self.set_price('100', symbol='ETH-USD')
        with self.assertRaises(ValueError):
            self.buy(symbol='ETH-USD')

    def test_risk_caps_and_non_finite_inputs(self):
        for value in ['1001','NaN','Infinity','-1','0','9']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.buy(budget=value)
        self.buy()
        with self.assertRaises(ValueError):
            self.buy()

    def test_report_unique_per_hour_across_new_engine(self):
        with db.transaction(self.con):
            self.engine.report('2030-01-01T01')
            Engine(self.con, self.config).report('2030-01-01T01')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM reports').fetchone()[0], 1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_failed_transaction_rolls_back_execution_and_command_status(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.con):
                self.engine.buy({'symbol':'BTC-USD','budget':'250'})
                raise RuntimeError('simulated crash before commit')
        self.assertEqual(self.engine.active(), [])
        self.assertEqual(D(db.get(self.con, 'cash')), D(10000))

    def test_crossovers_require_contiguous_samples(self):
        values = ['100'] * 20 + ['102']
        for i, price in enumerate(values):
            self.con.execute('INSERT INTO samples(symbol,price,ts) VALUES(?,?,?)',
                             ('BTC-USD', price, time.time()-(20-i)*30))
        self.assertEqual(self.engine.signal('BTC-USD')[0], 'buy')
        self.con.execute('UPDATE samples SET ts=ts-1000 WHERE id=1')
        self.assertEqual(self.engine.signal('BTC-USD')[0], 'stale')

    def test_feed_cannot_change_in_existing_database(self):
        with self.assertRaises(ValueError):
            db.initialize(self.config.db, 'coinbase')

    def test_bad_order_id_is_rejected_without_crashing_process(self):
        for value in [10**100, True, '1', -1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.command('close', {'id':value})


if __name__ == '__main__':
    unittest.main()
