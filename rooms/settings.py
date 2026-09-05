"""Shared local paths and loopback endpoint; no credentials are stored here."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('A2A_PORT', '41241'))
if not 1024 <= PORT <= 65535:
    raise ValueError('A2A_PORT must be between 1024 and 65535')
BASE_URL = f'http://127.0.0.1:{PORT}'
DATA = Path(os.environ.get('A2A_ROOM_DATA', str(ROOT / 'data/roundtable'))).expanduser().resolve()
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip() if (ROOT / 'VERSION').exists() else 'dev'
