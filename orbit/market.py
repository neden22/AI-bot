import json
import math
import time
from datetime import datetime
from decimal import Decimal
from urllib.request import Request, urlopen


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0 or number > Decimal('1000000000'):
        raise ValueError('Price must be a finite positive number')
    return number


def quote(symbol, feed):
    if feed == 'simulation':
        base = {'BTC-USD': 64000, 'ETH-USD': 2800, 'SOL-USD': 140}[symbol]
        phase = time.time() / 120 + list(('BTC-USD', 'ETH-USD', 'SOL-USD')).index(symbol)
        return str(round(base * (1 + .018 * math.sin(phase)), 4)), time.time(), 'SIMULATION'
    request = Request(f'https://api.exchange.coinbase.com/products/{symbol}/ticker',
                      headers={'User-Agent': 'ORBIT/0.1', 'Accept': 'application/json'})
    with urlopen(request, timeout=5) as response:
        payload = json.loads(response.read(65536))
    price = positive(payload['price'])
    timestamp = datetime.fromisoformat(payload['time'].replace('Z', '+00:00')).timestamp()
    if not math.isfinite(timestamp) or not 0 <= time.time() - timestamp <= 90:
        raise ValueError('Stale or future exchange timestamp')
    return str(price), timestamp, 'Coinbase Exchange'
