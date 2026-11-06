"""Compact Telegram status: narrow lines, explicit failures, no monospace tables."""
import os
import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from serverbot.collectors.battery import get_battery_reading
from serverbot.collectors.disk import GetDiskUsage
from serverbot.collectors.host import read_host_snapshot
from serverbot.collectors.system import get_system_reading
from serverbot.collectors.temperature import get_temperature
from serverbot.state import threshold_status, low_threshold_status

ICONS = {'ok':'🟢','warning':'🟡','critical':'🔴','error':'🔴'}
RANK = {'ok':0,'warning':1,'critical':2,'error':3}
LABELS = {'system:updates':'Обновления','bot:workbot-db':'Источники WorkBot',
          'bot:chatbot-db':'База ChatBot','host:collector':'Сбор показателей',
          'gateway:health':'OmniRoute API','network:dns':'DNS','network:https':'Интернет','time:ntp':'Время','disk:smart':'SSD SMART'}


def short_detail(key, detail):
    if key == 'system:updates':
        match = re.search(r'(\d+) package updates pending', detail)
        if match:
            return f'Доступно пакетов: {match[1]}'
    if key == 'bot:workbot-db':
        match = re.search(r'Source errors: (\d+)', detail)
        if match:
            return f'Ошибок источников: {match[1]}'
    if key == 'host:collector':
        return 'Показатели отсутствуют или устарели'
    return detail[:120]


async def build_mobile_status(config, http_session):
    problems, metrics = [], []
    worst = 'ok'
    def issue(name,status,detail):
        nonlocal worst
        if RANK[status] > RANK[worst]:
            worst = status
        if status != 'ok':
            problems.append(f'{ICONS[status]} {escape(name[:60])}: {escape(detail[:120])}')
    try:
        system = await get_system_reading(1)
        cpu = threshold_status(system.cpu_percent,config.system.cpu_warning_percent if config.system else 80,
                               config.system.cpu_critical_percent if config.system else 95)
        ram = 100*system.memory_used_bytes/system.memory_total_bytes if system.memory_total_bytes else 0
        issue('CPU',cpu,f'{system.cpu_percent:.0f}%')
        issue('RAM',threshold_status(ram,config.system.memory_warning_percent if config.system else 80,
                                    config.system.memory_critical_percent if config.system else 95),f'{ram:.0f}%')
        metrics += [f'CPU · {system.cpu_percent:.0f}%',
                    f'RAM · {ram:.0f}% · свободно {system.memory_available_bytes/1024**3:.1f} ГиБ']
        minutes = int(system.uptime_seconds//60)
        days,hours = divmod(minutes//60,24)
        uptime = f'{days} д {hours} ч' if days else f'{hours} ч {minutes%60} мин'
        metrics.append('В работе · '+uptime)
    except (OSError,ValueError,IndexError):
        issue('CPU / RAM','error','Не удалось получить показатели')
    seen = set()
    for disk in config.disks:
        try:
            device = os.stat(disk.path).st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = GetDiskUsage(disk.path)
            issue('Диск '+disk.path,threshold_status(usage.percent,disk.warning,disk.critical),f'Занято {usage.percent:.0f}%')
            if len(seen)<=3:
                metrics.append(f'Диск {escape(disk.path[:40])} · {usage.percent:.0f}% · свободно {usage.free/1024**3:.0f} ГиБ')
        except OSError:
            issue('Диск '+disk.path,'error','Нет данных')
    temperatures = []
    for check in config.temperatures:
        reading = get_temperature(check)
        if reading.error is not None or reading.value is None:
            issue(check.name,'error','Температура недоступна')
        else:
            issue(check.name,threshold_status(reading.value,check.warning,check.critical),f'{reading.value:.0f} °C')
            if len(temperatures)<3:
                temperatures.append(f'{escape(check.name[:30])} {reading.value:.0f}°')
    if temperatures:
        metrics.append('Температура · '+' / '.join(temperatures))
    for check in config.batteries[:2]:
        battery = get_battery_reading(check)
        if battery.battery_error is not None or battery.percentage is None:
            issue('Батарея','error','Нет данных')
        else:
            issue('Батарея',low_threshold_status(battery.percentage,check.warning_percent,check.critical_percent),f'{battery.percentage}%')
            power = '🔌 От сети' if battery.charger_online else '🔋 От батареи'
            if battery.charger_online is None:
                power = 'Питание неизвестно'
                issue('Питание','error','Нет данных адаптера')
            elif not battery.charger_online:
                issue('Питание','warning','Адаптер отключён')
            metrics.append(f'{power} · заряд {battery.percentage}%')
    host = read_host_snapshot(config.host_snapshot) if config.host_snapshot is not None else []
    for reading in host:
        issue(LABELS.get(reading.key,reading.name),reading.status,short_detail(reading.key,reading.detail))
    def state(keys):
        rows=[r for r in host if r.key in keys]
        return ICONS[max((r.status for r in rows),key=RANK.get)] if len(rows)==len(keys) else '⚪'
    blocks=[]
    for user,title in (('serverbot','ServerBot'),('chatbot','ChatBot'),('workbot','WorkBot')):
        keys=['service:'+user,'bot:'+user]
        if user=='chatbot': keys.append('bot:chatbot-db')
        if user=='workbot': keys.append('bot:workbot-db')
        blocks.append(state(keys)+' '+title)
    now=datetime.now(ZoneInfo('Europe/Moscow')).strftime('%H:%M МСК')
    heading = 'Всё в порядке' if not problems else f'Требует внимания: {len(problems)}'
    result=[f'{ICONS[worst]} <b>HomeLab</b> · {now}',escape(heading)]
    if problems:
        result += ['\n<b>Что проверить</b>',*problems[:6]]
        if len(problems)>6:
            result.append(f'Ещё {len(problems)-6} — в подробностях')
    result += ['\n<b>Ресурсы</b>',*metrics,'\n<b>Боты</b>',*blocks]
    result += ['\n<b>Система</b>',state(['vm:kali','vm:ssh'])+' Kali · VM и SSH',
               state(['network:dns','network:https'])+' Интернет · DNS и HTTPS',
               state(['disk:smart'])+' SSD · SMART',state(['time:ntp'])+' Время · синхронизация',
               state(['service:omniroute','service:nginx','gateway:health'])+' OmniRoute · API']
    if any(r.key=='host:collector' for r in host):
        result.append('⚪ Состояние служб сейчас неизвестно')
    return '\n'.join(result)

DETAIL_PAGES = {'resources':'Ресурсы', 'bots':'Боты и службы',
                'network':'Сеть и Kali', 'storage':'Диски и обновления'}


def percent_bar(value):
    filled = round(max(0,min(100,value))/10)
    return '▰'*filled+'▱'*(10-filled)


def host_description(row):
    if row is None:
        return 'Нет свежих данных'
    detail=row.detail
    if detail=='Health query failed': return 'Не удалось выполнить проверку'
    if detail=='Waiting for confirmation of first failed sample': return 'Первый сбой · ожидаем повторную проверку'
    if row.key in {'memory:swap','disk:inodes'}:
        match=re.search(r'([0-9.]+)% used',detail)
        if match: return f'Занято {match[1]}%'
    if row.key.startswith('service:'):
        return 'Служба запущена' if row.status=='ok' else 'Служба остановлена'
    if row.key in {'bot:serverbot','bot:chatbot','bot:workbot'}:
        match=re.search(r'found (\d+)',detail)
        return f'Процессов: {match[1]}' if match else detail[:160]
    translations={'Database schema ready':'Схема базы корректна',
                  'Database healthcheck failed':'Ошибка проверки базы',
                  'Database query failed':'База не отвечает на запрос',
                  'running':'Виртуальная машина работает',
                  'VM is not running':'Виртуальная машина остановлена',
                  'SSH responds':'SSH отвечает', 'Unexpected SSH banner':'Неожиданный ответ SSH',
                  'Resolver responds':'Имена разрешаются','DNS lookup failed':'Не удалось разрешить имя',
                  'Alpine HTTPS endpoint responds':'Alpine CDN доступен по HTTPS',
                  'Unexpected HTTP status':'Неожиданный HTTP-статус',
                  'OmniRoute health endpoint responds':'Gateway отвечает',
                  'chrony synchronized':'Часы синхронизированы',
                  'chrony not synchronized':'Часы не синхронизированы'}
    if detail in translations: return translations[detail]
    return short_detail(row.key,detail)


async def build_detail_page(config, http_session, page):
    if page not in DETAIL_PAGES:
        raise ValueError('Unknown status page')
    now=datetime.now(ZoneInfo('Europe/Moscow')).strftime('%H:%M МСК')
    lines=[f'<b>{DETAIL_PAGES[page]}</b> · {now}']
    host=read_host_snapshot(config.host_snapshot) if config.host_snapshot is not None else []
    indexed={r.key:r for r in host}
    def block(key,label):
        row=indexed.get(key)
        icon=ICONS[row.status] if row else '⚪'
        return f'\n{icon} <b>{escape(label)}</b>\n{escape(host_description(row))}'
    if page=='resources':
        try:
            s=await get_system_reading(3)
            ram=100*s.memory_used_bytes/s.memory_total_bytes if s.memory_total_bytes else 0
            lines += [f'\n<b>Процессор</b> · {s.cpu_percent:.0f}%',percent_bar(s.cpu_percent),
                      'Нагрузка 1 / 5 / 15 мин\n'+' / '.join(f'{v:.2f}' for v in s.load_average),
                      f'\n<b>Оперативная память</b> · {ram:.0f}%',percent_bar(ram),
                      f'Занято {s.memory_used_bytes/1024**3:.1f} из {s.memory_total_bytes/1024**3:.1f} ГиБ',
                      f'Доступно {s.memory_available_bytes/1024**3:.1f} ГиБ',
                      f'Без перезагрузки · {int(s.uptime_seconds//86400)} д {int(s.uptime_seconds//3600)%24} ч {int(s.uptime_seconds//60)%60} мин']
            for title,processes,kind in [('Больше CPU',s.top_cpu,'cpu'),('Больше RAM',s.top_memory,'ram')]:
                if processes:
                    lines.append('\n<b>'+title+'</b>')
                    for process in processes[:3]:
                        metric=f'{process.cpu_percent:.1f}%' if kind=='cpu' else f'{process.memory_bytes/1024**2:.0f} МиБ'
                        lines.append(f'• {escape(process.name[:40])} · {metric}')
        except (OSError,ValueError,IndexError):
            lines.append('\n🔴 Не удалось прочитать CPU и RAM')
        if config.temperatures:
            lines.append('\n<b>Температуры</b>')
            for check in config.temperatures[:8]:
                reading=get_temperature(check)
                if reading.error is not None or reading.value is None:
                    lines.append('🔴 '+escape(check.name[:40])+' · нет данных')
                else:
                    status=threshold_status(reading.value,check.warning,check.critical)
                    lines.append(f'{ICONS[status]} {escape(check.name[:40])} · {reading.value:.0f} °C')
        for check in config.batteries[:2]:
            battery=get_battery_reading(check)
            lines.append('\n<b>Питание</b>')
            lines.append('🔌 От сети' if battery.charger_online else '🔋 От батареи' if battery.charger_online is False else '⚪ Адаптер · нет данных')
            lines.append(f'Заряд · {battery.percentage}%' if battery.percentage is not None else '⚪ Заряд · нет данных')
        lines.append(block('memory:swap','Swap'))
    elif page=='bots':
        for name,title in [('serverbot','ServerBot'),('chatbot','ChatBot'),('workbot','WorkBot')]:
            lines.append('\n<b>'+title+'</b>')
            for key,label in [('service:'+name,'Служба'),('bot:'+name,'Приложение')]:
                row=indexed.get(key)
                lines.append(f'{ICONS[row.status] if row else "⚪"} {escape(host_description(row))}')
            extra='bot:chatbot-db' if name=='chatbot' else 'bot:workbot-db' if name=='workbot' else None
            if extra:
                row=indexed.get(extra)
                lines.append(f'{ICONS[row.status] if row else "⚪"} {escape(host_description(row))}')
        lines.append('\n<b>Системные службы</b>')
        for name,label in [('sshd','SSH'),('nftables','Firewall'),('libvirtd','Libvirt'),('sing-box','Прокси'),('chronyd','Время'),('crond','Cron'),('omniroute','OmniRoute'),('nginx','Gateway proxy')]:
            row=indexed.get('service:'+name)
            lines.append(f'{ICONS[row.status] if row else "⚪"} {label} · {escape(host_description(row))}')
        lines.append('\n<i>Процесс и база не гарантируют ответ каждого обработчика.</i>')
    elif page=='network':
        for key,label in [('vm:kali','Kali · виртуальная машина'),('vm:ssh','Kali · SSH'),
                          ('gateway:health','OmniRoute · API'),('network:dns','DNS хоста'),('network:https','Интернет хоста'),('time:ntp','Синхронизация времени')]:
            lines.append(block(key,label))
    else:
        seen=set()
        for disk in config.disks[:16]:
            try:
                device=os.stat(disk.path).st_dev
                if device in seen: continue
                seen.add(device)
                if len(seen)>4: continue
                usage=GetDiskUsage(disk.path)
                status=threshold_status(usage.percent,disk.warning,disk.critical)
                lines += [f'\n{ICONS[status]} <b>Диск {escape(disk.path[:40])}</b> · {usage.percent:.0f}%',
                          percent_bar(usage.percent),f'Занято {usage.used/1024**3:.1f} из {usage.total/1024**3:.1f} ГиБ',
                          f'Свободно {usage.free/1024**3:.1f} ГиБ']
            except OSError:
                lines.append('🔴 Диск '+escape(disk.path[:40])+' · нет данных')
        smart=indexed.get('disk:smart')
        lines.append(f'\n{ICONS[smart.status] if smart else "⚪"} <b>NVMe · SMART</b>')
        if not smart: lines.append('Нет свежих данных')
        elif smart.status=='error': lines.append(escape(host_description(smart)))
        if smart:
            # Display the fixed metric fields as short localized rows.
            fields=[('Warning','Критическое предупреждение',''),('wear','Износ','%'),('media errors','Ошибки данных',''),
                    ('unsafe shutdowns','Аварийные отключения за всё время','')]
            for field,label,suffix in fields:
                match=re.search(re.escape(field)+r'=(\d+)',smart.detail)
                if match: lines.append(f'{label} · {match[1]}{suffix}')
        lines.append(block('disk:inodes','Inode'))
        lines.append(block('system:updates','Обновления Alpine'))
        updates=indexed.get('system:updates')
        if updates and 'deferred' in updates.detail:
            lines.append('Автоустановка отложена: проверь журнал обслуживания.')
        lines.append('\n<i>Обновления ядра и SSH обслуживаются отдельно. Автоперезагрузка выключена.</i>')
    if any(r.key=='host:collector' for r in host):
        lines.append('\n🔴 Снимок мониторинга отсутствует или устарел')
    lines.append('\n<i>Службы и SMART · минутный снимок</i>')
    return '\n'.join(lines)
