import unittest
from workbot.telegram import Telegram


class TelegramAccessTests(unittest.TestCase):
    def test_private_owner_requires_matching_sender(self):
        telegram = Telegram("dummy", "123")
        for sender in (None, {}, {"id": 999}):
            with self.subTest(sender=sender):
                message = {"chat": {"id": 123, "type": "private"}, "text": "/status"}
                if sender is not None:
                    message["from"] = sender
                self.assertIsNone(telegram.command({"message": message}))
        self.assertEqual(telegram.command({"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/status"}}), "/status")

    def test_owner_sender_cannot_authorize_group(self):
        telegram = Telegram("dummy", "123")
        self.assertIsNone(telegram.command({"message": {"chat": {"id": 123, "type": "group"}, "from": {"id": 123}, "text": "/status"}}))
