"""Fail closed before a retired WhatsApp writer opens or changes storage."""
from __future__ import annotations

import json
import os
from pathlib import Path


class LegacyHistoryWriterDisabled(RuntimeError):  # noqa: N818 - plan contract
    pass


def legacy_history_writers_disabled(disabled: bool = False) -> bool:
    """Resolve once at construction; failed config reads preserve dormant behavior."""
    if disabled is True:
        return True
    # load_config also normalizes/writes the file; use its decoding without side effects.
    from yeoman_shared.config.loader import _migrate_config_with_change, convert_keys
    from yeoman_shared.config.schema import Config

    root = Path(os.environ.get('YEOMAN_HOME', '').strip() or Path.home() / '.yeoman')
    path = root / 'config.json'
    try:
        if not path.exists():
            return False
        raw, _ = _migrate_config_with_change(json.loads(path.read_text()))
        return Config.model_validate(convert_keys(raw)).history.legacy_writers_disabled is True
    except (OSError, ValueError):
        return False


def require_legacy_history_writer(*, disabled: bool, channel: str) -> None:
    if channel == 'whatsapp' and disabled is True:
        raise LegacyHistoryWriterDisabled('legacy_whatsapp_writer_retired')
