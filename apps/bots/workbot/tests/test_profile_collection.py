from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbot.config import load_config
from workbot.profile_matching import combined_collection_filters
from workbot.reporting import source_views
from workbot.runner import Monitor
from workbot.storage import Store
from workbot.telegram import _group_payload


class ProfileCollectionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(Path(__file__).resolve().parents[1] / 'config.toml'))
        for name, source in self.config['sources'].items():
            source['enabled'] = name == 'ctftime'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'db'))
        self.addCleanup(self.store.close)
        base = deepcopy(self.config['filters'])
        base['events_enabled'] = False
        events = base | {'events_enabled': True, 'event_days': 180, 'event_cancellations': True}
        self.config['filters'] = base
        self.store.set_state('search_profiles', json.dumps({'Работа': {'filters': base}, 'События': {'filters': events}}))
        self.monitor = Monitor(self.config, self.store, Mock())
        self.addCleanup(self.monitor.close)

    def test_other_profile_enables_calendar_and_longer_horizon(self):
        filters = combined_collection_filters(self.monitor.config)
        self.assertTrue(filters['events_enabled'])
        self.assertEqual(filters['event_days'], 180)
        self.assertTrue(next(row for row in source_views(self.store, self.monitor.config) if row['source'] == 'ctftime')['enabled'])
        with patch('workbot.background.BackgroundScan') as factory:
            self.assertTrue(self.monitor.request_check())
            self.assertEqual(factory.call_args.args[2], ['ctftime'])

    def test_group_combines_profile_names_and_warnings(self):
        def row(name, warning):
            return {'payload': json.dumps({'vacancy': {}, 'decision': {'matching_profiles': [name], 'warnings': [warning]}})}
        result = _group_payload([row('ИБ', 'Город неизвестен'), row('DevOps', 'Уточнить формат')])
        self.assertEqual(result['decision']['matching_profiles'], ['ИБ', 'DevOps'])
        self.assertEqual(result['decision']['warnings'], ['Город неизвестен', 'Уточнить формат'])
