import logging
from .http import FetchError, HttpClient
from .profile_matching import combined_collection_filters, decide_profiles
from .sources import ParseError, collect
from .reporting import status_text
from .directions import collection_rules, source_settings

log = logging.getLogger(__name__)


def scan(config, store=None, client=None):
    settings = config["monitor"]
    collection_filters = combined_collection_filters(config)
    client = client or HttpClient(settings["request_timeout_seconds"], settings["request_delay_seconds"],
                                  scan_timeout=settings.get('scan_timeout_seconds', 600))
    output = {"sources": {}, "vacancies": []}
    for name, source in config["sources"].items():
        if not source.get("enabled"):
            continue
        if name in ('ctftime', 'ods_events') and not collection_filters.get('events_enabled', False):
            continue
        log.info("%s: начинаю проверку", name)
        try:
            # Curated internship programmes keep their source-specific security
            # semantics; their hard-coded category must not relabel other roles.
            expandable = {'habr', 'yandex_jobs', 'mts', 'sber', 'hh', 'kaspersky', 'bastion'}
            rules = collection_rules(collection_filters) if name in expandable else collection_filters
            local = source_settings(name, source, collection_filters)
            if store and name in ('ctftime', 'ods_events') and (collection_filters.get('event_cancellations', True) or collection_filters.get('event_updates', True)):
                local['_watched_events'] = store.watched_events(name)
            vacancies = collect(name, local, client, rules)
            records = [(v, decide_profiles(v, config, allow_unverified=source.get('notify_unverified', False))) for v in vacancies]
            if store:
                store.save_snapshot(name, records, rules=collection_filters)
            summary = {"found": len(records), "matched": sum(d.matched for _, d in records), "error": None}
            summary['unverified'] = sum(v.active and v.recruitment_status == 'unknown' for v, _ in records)
            if name in ('ctftime', 'ods_events'):
                summary['watch_errors'] = sum(bool(v.event_check_error) for v, _ in records)
            log.info("%s: найдено %s, подходит %s", name, summary["found"], summary["matched"])
            output["vacancies"].extend({"vacancy": v.to_dict(), "decision": {"matched": d.matched, "reason": d.reason, "warnings": d.warnings, "matching_profiles": d.matching_profiles}} for v, d in records)
        except (FetchError, ParseError) as exc:
            summary = {"error": str(exc)}
            if store:
                store.source_error(name, str(exc))
            log.warning("%s: %s", name, exc)
        output["sources"][name] = summary
    return output
