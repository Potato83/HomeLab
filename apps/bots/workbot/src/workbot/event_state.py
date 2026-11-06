"""Updates to events the owner has already seen, independent of fresh search filters."""
from datetime import timedelta
from .models import Decision, Vacancy

DETAIL_FIELDS = ('start_at', 'end_at', 'registration_deadline', 'cities', 'work_format',
                 'language', 'language_codes', 'price', 'price_kind')
DATE_FIELDS = {'start_at', 'end_at', 'registration_deadline'}


def change_decision(v, payload, rules):
    from .events import instant, event_now
    if not rules.get('events_enabled', False) or not rules.get('event_cancellations', True):
        return Decision(False, 'Уведомления об изменении статуса мероприятий отключены')
    try:
        if instant(payload.get('_event_watch_until', '')) < event_now() - timedelta(days=1):
            return Decision(False, 'Срок наблюдения за мероприятием истёк')
    except ValueError:
        return Decision(False, 'Нет подтверждённой прежней даты мероприятия')
    kind = payload.get('_notification_kind')
    if kind == 'event_cancelled' and v.event_status == 'cancelled' and not payload.get('_event_cancel_notified', False):
        return Decision(True, 'Отмена ранее присланного мероприятия')
    if kind == 'event_restored' and v.source == 'ods_events' and v.active and v.event_status == 'scheduled' and not v.event_check_error:
        return Decision(True, 'Календарь снова объявил ранее отменённое мероприятие активным')
    return Decision(False, 'Нет подтверждённого изменения статуса')


def update_decision(v, payload, rules):
    from .events import instant, event_now
    if not rules.get('events_enabled', False) or not rules.get('event_updates', True):
        return Decision(False, 'Уведомления об изменении условий мероприятий отключены')
    if v.event_check_error or not v.active or v.event_status == 'cancelled':
        return Decision(False, 'Карточка мероприятия недоступна или неактивна')
    try:
        if instant(payload.get('_event_watch_until', '')) < event_now() - timedelta(days=1):
            return Decision(False, 'Срок наблюдения за мероприятием истёк')
    except ValueError:
        return Decision(False, 'Нет подтверждённой прежней даты мероприятия')
    return Decision(True, 'Обновились условия ранее присланного мероприятия')


def transition(v, previous, old, decision, rules):
    generation = old['generation'] if old else 1
    sent = old['sent_generation'] if old else 0
    old_v = Vacancy(**previous['vacancy']) if old else None
    seen = bool(sent and old_v and old_v.kind == 'event')
    notified = previous.get('_notified_status', old_v.recruitment_status if seen else '')
    kind = previous.get('_notification_kind', '')
    meta = {'_event_watch_until': previous.get('_event_watch_until') or (old_v.end_at if seen else ''),
            '_event_cancel_notified': previous.get('_event_cancel_notified', False)}
    pending_fields = list(previous.get('_event_changed_fields', [])) if old and generation > sent else []
    if v.event_status == 'cancelled':
        if seen and not meta['_event_cancel_notified']:
            generation, kind = sent + 1, 'event_cancelled'
            decision = change_decision(v, meta | {'_notification_kind': kind}, rules)
        return generation, notified, kind, meta, decision
    if kind == 'event_cancelled' and generation > sent:
        # A cancelled notice that was never delivered is withdrawn; it must
        # not become a duplicate normal invitation when the status changes.
        generation, kind = sent, ''
        pending_fields = []
    if meta['_event_cancel_notified']:
        if v.source == 'ods_events' and v.active and not v.event_check_error:
            generation, kind = sent + 1, 'event_restored'
            decision = change_decision(v, meta | {'_notification_kind': kind}, rules)
        else:
            decision = Decision(False, 'Возобновление ранее отменённого события не подтверждено')
    elif old and not v.event_check_error:
        changed_fields = [field for field in DETAIL_FIELDS
                          if getattr(old_v, field) != getattr(v, field)]
        reportable_changes = changed_fields if seen else [field for field in changed_fields if field in DATE_FIELDS]
        if v.active and not old['active']:
            generation, notified, kind = generation + 1, '', ''
        elif v.active and v.recruitment_status == 'open' and notified == 'unknown' and generation == sent:
            generation, kind = generation + 1, 'confirmation'
        elif v.active and generation == sent and reportable_changes:
            generation, kind = generation + 1, 'event_update'
        if generation > sent:
            pending_fields = list(dict.fromkeys([*pending_fields, *reportable_changes]))
    if pending_fields:
        meta['_event_changed_fields'] = pending_fields
    if seen and kind == 'event_update' and generation > sent:
        decision = update_decision(v, meta | {'_notification_kind': kind}, rules)
    return generation, notified, kind, meta, decision
