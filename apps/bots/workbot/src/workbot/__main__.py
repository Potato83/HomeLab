import argparse
import json
import logging
import os
import signal
import sys
import time
import sqlite3
from contextlib import nullcontext

from .config import load_config, load_env
from .http import FetchError, telegram_call
from .matching import decide
from .models import Vacancy
from .service import scan
from .reporting import source_views, sources_text, status_text
from .runner import Monitor
from .storage import Store
from .telegram import Telegram, format_message
from .diagnostics import diagnose, format_report, send_test_message
from .locking import instance_lock
from .maintenance import backup_database
from .sites import load_sites, sites_messages, filter_sites, handle_sites
from .settings import handle_settings, saved_config, settings_text
from .pipeline import handle_feedback, handle_track
from .insights import handle_insights
from .interface import HOME_KEYBOARD, handle_controls, home_text
from .career import deliver_reminders, handle_career
from .daily import handle_daily
from .jobs import handle_jobs
from .workflows import handle_application_buttons, handle_input, handle_profile_buttons
from .profiles import handle_profile

HELP = """<b>WorkBot · команды</b>
/menu — главное меню
/check — проверить сейчас
/status — очередь и ошибки
/today — план поиска на сегодня

<b>Поиск и отклики</b>
/jobs [слова] [source=mts] [direction=security] [level=intern] [format=remote] [profile="Имя"] [page=2] — поиск по названию, компании, категории и описанию
/job ID [page=2] — полная карточка и описание
/track [ignored] [page=2] — мои вакансии / скрытые
/track ID saved|applied|interview|offer|rejected|closed|ignored|remove — статус
/application ID — карточка отклика с заметками и кнопками
/note ID текст — заметка (- очистить)
/contact ID текст — контакт (- очистить)
/next ID текст — следующий шаг (- очистить)
/remind ID ГГГГ-ММ-ДД ЧЧ:ММ текст — напомнить по Москве
/remind ID off|deadline — отменить / напомнить за день до дедлайна источника
/reminders — список напоминаний
/track_export — все отклики в CSV
/insights — сводка по отмеченным вакансиям

<b>Настройки</b>
/settings — панель с кнопками
/settings all — все фильтры и команды
/profiles — управление профилями кнопками
/profile list|save NAME|use NAME|delete NAME — профили командой
/set параметр значение — изменить поиск
/source NAME on|off — источник
/sources — подключённые источники
/settings_reset — вернуть настройки TOML
/cancel — отменить ожидаемый ввод

<b>Каталог ссылок</b>
/sites [слова] — каталог и поиск
/site_add URL название | примечание — добавить ссылку
/site_del ID — скрыть
/site_hidden [слова] — скрытые ссылки
/site_restore ID — восстановить
/site_rename ID название — переименовать
/site_note ID текст — примечание (- очистить)
/site_export — JSON-файл каталога
/site_import — импорт с предпросмотром
/site_import_confirm TOKEN — применить импорт
/help — эта справка"""


def main():
    parser = argparse.ArgumentParser(description="Монитор стажировок, junior-вакансий и мероприятий")
    parser.add_argument("command", nargs="?", default="run", choices=["demo", "preview", "once", "run", "status", "sources", "settings", "sites", "chat-id", "doctor", "test-telegram", "backup"])
    parser.add_argument("--json", action="store_true", help="Вывод результата в JSON")
    parser.add_argument("--config", help="Путь к TOML-конфигурации")
    parser.add_argument("--online", action="store_true", help="doctor: проверить Telegram и источники по сети")
    parser.add_argument("--skip-sources", action="store_true", help="doctor --online: пропустить опрос сайтов")
    parser.add_argument("--output", help="backup: новый файл резервной копии SQLite")
    parser.add_argument('--query', default='', help='sites: поиск по каталогу')
    parser.add_argument('--saved', action='store_true', help='sites: учитывать изменения каталога из базы')
    args = parser.parse_args()
    if (args.online or args.skip_sources) and args.command != 'doctor':
        parser.error('--online и --skip-sources доступны только для doctor')
    if args.skip_sources and not args.online:
        parser.error('--skip-sources используется вместе с --online')
    if args.command == 'backup' and not args.output:
        parser.error('backup требует --output с новым именем файла')
    if args.output and args.command != 'backup':
        parser.error('--output доступен только для backup')
    if args.query and args.command != 'sites':
        parser.error('--query доступен только для sites')
    if args.saved and args.command not in ('sites', 'preview', 'settings'):
        parser.error('--saved доступен только для sites, preview и settings')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env_error = None
    try:
        load_env()
    except (ValueError, OSError) as exc:
        if args.command != 'doctor':
            raise
        env_error = str(exc)
    if args.command == 'sites':
        from .bookmarks import BookmarkCatalog, catalog_view
        if args.saved:
            store = Store(os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3'))
            try:
                catalog = BookmarkCatalog(store).view()
            finally:
                store.close()
        else:
            catalog = catalog_view(load_sites())
        print(json.dumps(filter_sites(catalog, args.query), ensure_ascii=False, indent=2) if args.json else '\n\n'.join(sites_messages(catalog, args.query)))
        return 0
    if args.command == 'doctor':
        report = diagnose(args.config, online=args.online, skip_sources=args.skip_sources, env_error=env_error)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
        return 0 if report['ok'] else 1
    if args.command == 'test-telegram':
        result = send_test_message(os.environ.get('BOT_TOKEN'), os.environ.get('OWNER_ID'))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result['message'])
        return 0
    if args.command == 'backup':
        result = backup_database(os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3'), args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
              f'Резервная копия: {result["path"]}\nРазмер: {result["bytes"]} байт\nSHA-256: {result["sha256"]}')
        return 0
    if args.command == "chat-id":
        token = os.environ.get("BOT_TOKEN")
        if not token:
            raise ValueError("Сначала заполните BOT_TOKEN в .env")
        with instance_lock(os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3')):
            updates = telegram_call(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        chats = {str(m["chat"]["id"]) for u in updates if (m := u.get("message")) and m["chat"]["type"] == "private"}
        print("\n".join(sorted(chats)) or "Нет сообщений. Напишите вашему боту /start и повторите команду.")
        return 0
    config = load_config(args.config or os.environ.get("WORKBOT_CONFIG", "config.toml"))
    if args.command == 'settings':
        config = saved_config(config, os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3'))
        print(json.dumps(config, ensure_ascii=False, indent=2) if args.json else settings_text(config))
        return 0
    if args.command == 'preview' and args.saved:
        config = saved_config(config, os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3'))
    if args.command == "demo":
        demo = [
            Vacancy("demo", "1", "Стажёр SOC", "Демонстрационная компания", "https://example.org/vacancy/1", cities=["Санкт-Петербург"], work_format="hybrid"),
            Vacancy("demo", "2", "Junior AppSec Engineer", "Демонстрационная компания", "https://example.org/vacancy/2", cities=["Москва"], work_format="remote"),
            Vacancy("demo", "3", "Стажёр по информационной безопасности", "Демонстрационная компания", "https://example.org/vacancy/3"),
            Vacancy("demo", "4", "Senior SOC Analyst", "Демонстрационная компания", "https://example.org/vacancy/4", work_format="remote"),
            Vacancy("demo", "5", "Junior DevOps", "Демонстрационная компания", "https://example.org/vacancy/5", work_format="remote"),
        ]
        from dataclasses import asdict
        result = [{"vacancy": asdict(v), "decision": asdict(decide(v, config["filters"]))} for v in demo]
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("ДЕМОНСТРАЦИЯ: все объявления вымышленные; отправки и записи в базу нет.\n")
            for item in result:
                print(format_message(item) if item["decision"]["matched"] else f'Пропущено: {item["vacancy"]["title"]}: {item["decision"]["reason"]}')
                print()
        return 0
    if args.command == "preview":
        result = scan(config)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for name, state in result["sources"].items():
                print(f"{name}: {state}")
            for item in result["vacancies"]:
                if item["decision"]["matched"]:
                    print("\n" + format_message(item))
        return int(any(s.get("error") for s in result["sources"].values()))
    # Validate Telegram configuration before changing persistent state.
    telegram = None if args.command in ("status", "sources") else Telegram(os.environ.get("BOT_TOKEN"), os.environ.get("OWNER_ID"))
    db_path = os.environ.get('WORKBOT_DB', 'data/workbot.sqlite3')
    guard = instance_lock(db_path) if args.command in ('run', 'once') else nullcontext()
    with guard:
        return persistent_command(args, config, telegram, db_path)


def persistent_command(args, config, telegram, db_path):
    store = Store(db_path)
    try:
        if args.command == "run":
            try:
                telegram.set_commands()
            except FetchError as exc:
                logging.warning("Не удалось обновить меню команд Telegram: %s", exc)
        monitor = Monitor(config, store, telegram)
        if args.command in ("status", "sources"):
            report = status_text if args.command == "status" else sources_text
            print(json.dumps(source_views(store, monitor.config), ensure_ascii=False, indent=2) if args.json else report(store, monitor.config))
            return 0
        if args.command == "once":
            return monitor.check_and_dispatch()
        running = True
        def stop(signum, frame):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        reminder_due = 0.0
        while running:
            try:
                if time.monotonic() >= reminder_due:
                    reminder_due = time.monotonic() + 60
                    try:
                        deliver_reminders(store, telegram)
                        store.set_state('last_reminder_error', '')
                    except FetchError as exc:
                        store.set_state('last_reminder_error', str(exc))
                        logging.warning('Напоминания: %s', exc)
                monitor.run_background()
                offset = int(store.get_state("telegram_offset", "0"))
                for update in telegram.updates(offset):
                    if (handle_jobs(telegram, monitor, update) or handle_profile_buttons(telegram, monitor, update)
                            or handle_application_buttons(telegram, monitor, update)
                            or (update.get('callback_query') and (handle_track(telegram, store, update) or handle_career(telegram, store, update)))):
                        store.set_state('telegram_offset', update['update_id'] + 1)
                        continue
                    if handle_controls(telegram, monitor, update):
                        store.set_state('telegram_offset', update['update_id'] + 1)
                        continue
                    if handle_feedback(telegram, store, update):
                        store.set_state("telegram_offset", update['update_id'] + 1)
                        continue
                    command = telegram.command(update)
                    if handle_input(telegram, monitor, update):
                        pass
                    elif command in ('/start', '/menu'):
                        telegram.send(home_text(monitor.config), reply_markup=HOME_KEYBOARD)
                    elif command == '/help':
                        telegram.send(HELP)
                    elif command == "/status":
                        telegram.send(('🔎 Выполняется фоновая проверка.\n' if monitor.background else '') + status_text(store, monitor.config))
                    elif command == "/sources":
                        telegram.send(sources_text(store, monitor.config))
                    elif handle_settings(telegram, monitor, update):
                        pass
                    elif handle_profile(telegram, monitor, update):
                        pass
                    elif handle_track(telegram, store, update):
                        pass
                    elif handle_career(telegram, store, update):
                        pass
                    elif handle_daily(telegram, store, update):
                        pass
                    elif handle_insights(telegram, store, update):
                        pass
                    elif handle_sites(telegram, store, update):
                        pass
                    elif command == "/check":
                        if monitor.config['monitor'].get('paused'):
                            telegram.send('Мониторинг на паузе. Возобновить: /set pause off')
                            store.set_state('telegram_offset', update['update_id'] + 1)
                            continue
                        last = float(store.get_state("manual_check_at", "0"))
                        if time.time() - last < 60:
                            telegram.send("Повторная проверка будет доступна через минуту.")
                        else:
                            store.set_state("manual_check_at", time.time())
                            started = monitor.request_check()
                            telegram.send('Проверка запущена в фоне. Найденное придёт автоматически; состояние — /status.' if started else 'Проверка уже выполняется либо нет включённых источников. Состояние — /status.')
                    elif command:
                        telegram.send('Команда не найдена. Главное меню: /menu · Справка: /help')
                    store.set_state("telegram_offset", update["update_id"] + 1)
            except (FetchError, RuntimeError) as exc:
                logging.warning("%s", exc)
                # Telegram polling errors do not shift source scan deadlines.
                for _ in range(5):
                    if not running:
                        break
                    time.sleep(1)
        return 0
    finally:
        if 'monitor' in locals():
            monitor.close()
        store.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, sqlite3.Error, FetchError, RuntimeError) as exc:
        logging.error("%s", exc)
        sys.exit(1)
