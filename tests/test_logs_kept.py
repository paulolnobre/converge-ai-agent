"""The bot's and the OCR's logs go to the server's journald, which keeps them
for 7 days across deploys: the container's own log file is deleted every
time a deploy recreates it, and with it what happened before."""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.prod.yml"


def test_bot_and_ocr_log_to_journald_with_a_tag():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for name, tag in (("reception-bot", "converge-agent-bot"), ("ocr", "converge-agent-ocr")):
        logging = services[name]["logging"]
        assert logging["driver"] == "journald"
        assert logging["options"]["tag"] == tag
