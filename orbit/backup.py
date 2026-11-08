"""Consistent hot backup including committed WAL contents."""
import argparse
import os
import sqlite3
from pathlib import Path
from .config import load_env


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Consistent SQLite backup (no overwrite)')
    parser.add_argument('--db', default=os.getenv('ORBIT_DB', 'data/orbit.sqlite3'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source, target = Path(args.db).resolve(), Path(args.output).resolve()
    if not source.is_file():
        parser.error('Source database does not exist')
    if source == target or target.exists():
        parser.error('Output must be a new file')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            result = dst.execute('PRAGMA integrity_check').fetchone()[0]
    if result != 'ok':
        raise RuntimeError('Backup integrity check failed')
    print(f'Backup verified: {target}')


if __name__ == '__main__':
    main()
