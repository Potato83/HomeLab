import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from .config import load_config, load_env
from .http import FetchError
from .locking import instance_lock
from .models import Vacancy
from .matching import decide
from .runner import Monitor
from .service import scan
from .storage import Store
from .telegram import Telegram, deliver

def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Вакансии и события по ключевым словам')
    parser.add_argument('command', nargs='?', choices=['run', 'once', 'preview', 'status', 'demo'], default='run')
    parser.add_argument('--config')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    load_env()
    config = load_config(args.config or os.environ.get('WORKBOT_CONFIG', 'config.toml'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'demo':
        v = Vacancy('demo', '1', 'Junior SOC', 'Пример', 'https://example.org/job/1')
        print(json.dumps({'vacancy': v.to_dict(), 'decision': decide(v, config['filters']).__dict__}, ensure_ascii=False))
        return 0
    if args.command == 'preview':
        result = scan(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(any(s.get('error') for s in result['sources'].values()))
    path = os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3')
    with instance_lock(path):
        store = Store(path)
        monitor = None
        try:
            if args.command == 'status':
                from .reporting import status_text
                print(status_text(store, config))
                return 0
            telegram = Telegram(os.environ.get('BOT_TOKEN'), os.environ.get('OWNER_ID'))
            monitor = Monitor(config, store, telegram)
            if args.command == 'once':
                result = scan(monitor.config, store)
                deliver(store, telegram, monitor.enabled, config['monitor']['max_messages_per_cycle'], rules=config['filters'])
                return int(any(s.get('error') for s in result['sources'].values()))
            try:
                telegram.set_commands()
            except FetchError as exc:
                logging.warning('%s', exc)
            running = True
            def stop(signum, frame):
                nonlocal running
                running = False
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while running:
                try:
                    monitor.run_background()
                    offset = int(store.get_state('telegram_offset', '0'))
                    for update in telegram.updates(offset):
                        uid = update.get('update_id') if isinstance(update, dict) else None
                        if type(uid) is not int or uid < offset:
                            continue
                        try:
                            monitor.handle(update)
                        except ValueError as exc:
                            telegram.send(str(exc))
                        store.set_state('telegram_offset', uid + 1)
                except (FetchError, RuntimeError) as exc:
                    logging.warning('%s', exc)
                    for _ in range(5):
                        if not running:
                            break
                        time.sleep(1)
            return 0
        finally:
            if monitor:
                monitor.close()
            store.close()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, sqlite3.Error, FetchError, RuntimeError) as exc:
        logging.error('%s', exc)
        sys.exit(1)
