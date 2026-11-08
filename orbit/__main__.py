import argparse
import multiprocessing
import os
import signal
import threading
from . import db
from .config import Config, load_env
from .runtime import InstanceLock, Supervisor
from .server import create_server


def main():
    parser = argparse.ArgumentParser(description='ORBIT local paper-trading mission control')
    parser.add_argument('--feed', choices=['coinbase', 'simulation'])
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    load_env()
    config = Config.from_env(args.feed, args.port)
    if config.feed not in ('coinbase', 'simulation') or not 1024 <= config.port <= 65535:
        parser.error('Invalid feed or port (1024–65535)')
    # On POSIX, new DB and lock files are owner-only; use a dedicated OS account in deployment.
    if os.name != 'nt':
        os.umask(0o077)
    with InstanceLock(config.db + '.lock'):
        db.initialize(config.db, config.feed)
        supervisor = Supervisor(config)
        server = create_server(config, supervisor.wake)
        stopping = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        thread = None
        try:
            supervisor.start()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f'ORBIT • PAPER • {config.feed}\nhttp://127.0.0.1:{config.port}', flush=True)
            while not stopping.wait(1):
                supervisor.check()
        finally:
            supervisor.close()
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
