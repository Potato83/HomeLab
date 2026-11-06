import logging
import re
from os import environ


class SafeFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        # Hide known environment secrets if a library includes them in its log output
        for name, value in environ.items():
            if value and (name == "PROXY_URL" or any(word in name.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "KEY", "AUTH"))):
                text = text.replace(value, "[REDACTED]")
        # Also hide strings matching the usual Telegram bot token format
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)


def setup_logging():
    # Write to stderr: show our audit events and only warnings/errors from libraries
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("serverbot").setLevel(logging.INFO)
