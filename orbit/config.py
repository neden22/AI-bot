import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path=Path('.env')):
    """Small literal KEY=VALUE loader; never executes shell expressions."""
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


@dataclass(frozen=True)
class Config:
    db: str = 'data/orbit.sqlite3'
    feed: str = 'coinbase'
    interval: float = 30
    port: int = 8765
    symbols: tuple = ('BTC-USD', 'ETH-USD', 'SOL-USD')
    max_quote_age: int = 90

    @classmethod
    def from_env(cls, feed=None, port=None):
        return cls(db=os.getenv('ORBIT_DB', 'data/orbit.sqlite3'),
                   feed=feed or os.getenv('ORBIT_FEED', 'coinbase'),
                   port=port or int(os.getenv('ORBIT_PORT', '8765')))
