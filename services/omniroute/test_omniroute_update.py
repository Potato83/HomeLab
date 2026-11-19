import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/bots/serverbot/deploy/host'))
import omniroute_update as updater


OLD_IMAGE = 'a' * 64
NEW_IMAGE = 'b' * 64


def image(identifier):
    digest = 'sha256:' + identifier
    return {'Id': identifier, 'Digest': digest, 'RepoDigests': [updater.REPO + '@' + digest]}


class UpdateTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(
            updater, STATE=root, DATA=root / 'data', PENDING=root / 'pending.json',
            MAINTENANCE=root / 'maintenance', SNAPSHOT=root / 'snapshot',
            COPYING=root / 'copying', FAILED_DATA=root / 'failed-data',
            PULLED=root / 'last-pulled', PULL_STARTED=root / 'pull-started',
            REJECTED=root / 'rejected',
        ))

    def test_first_pull_does_not_prune_the_operators_image(self):
        with (patch.object(updater, 'image', side_effect=[image(OLD_IMAGE), image(NEW_IMAGE)]),
              patch.object(updater, 'podman'), patch.object(updater, 'prune_image') as prune):
            self.assertEqual(updater.pull()[0], NEW_IMAGE)
        self.assertNotIn(OLD_IMAGE, [call.args[0] for call in prune.call_args_list])
        self.assertEqual(updater.saved_image(updater.PULLED), NEW_IMAGE)

    def test_recovery_retries_start_before_reopening_nginx(self):
        updater.MAINTENANCE.touch()
        updater.save({'active_container': 'old-container'}, 'finished')
        with (patch.object(updater, 'container', return_value=('old-container', OLD_IMAGE, 'false')),
              patch.object(updater, 'start', side_effect=[updater.UpdateError('start failed'), None]) as start,
              patch.object(updater, 'wait_ready') as ready):
            with self.assertRaises(updater.UpdateError):
                updater.recover()
            self.assertTrue(updater.MAINTENANCE.exists())
            self.assertTrue(updater.PENDING.exists())
            updater.recover()
        self.assertEqual(start.call_count, 2)
        ready.assert_called_once()
        self.assertFalse(updater.MAINTENANCE.exists())
        self.assertFalse(updater.PENDING.exists())

    def test_interrupted_commit_can_still_roll_back_before_rotating(self):
        updater.MAINTENANCE.touch()
        updater.SNAPSHOT.mkdir()
        record = {'candidate': NEW_IMAGE, 'old_image': OLD_IMAGE, 'old_previous': None}
        updater.save(record, 'committing')
        with (patch.object(updater, 'container', return_value=('new-container', NEW_IMAGE, 'true')),
              patch.object(updater, 'image', return_value=None), patch.object(updater, 'podman') as podman,
              patch.object(updater, 'wait_ready', side_effect=updater.UpdateError('readiness failed')),
              patch.object(updater, 'rollback') as rollback):
            with self.assertRaises(updater.UpdateError):
                updater.recover()
        rollback.assert_called_once()
        podman.assert_not_called()
        self.assertEqual(json.loads(updater.PENDING.read_text())['phase'], 'trying')
        self.assertEqual(updater.saved_image(updater.REJECTED), NEW_IMAGE)

    def test_restore_completes_if_interrupted_between_directory_renames(self):
        updater.FAILED_DATA.mkdir()
        updater.SNAPSHOT.mkdir()
        (updater.SNAPSHOT / 'storage.sqlite').write_text('before migration')
        updater.restore_snapshot()
        updater.restore_snapshot()
        self.assertEqual((updater.DATA / 'storage.sqlite').read_text(), 'before migration')


if __name__ == '__main__':
    unittest.main()
