"""Tiny stdlib .env loader for the helper scripts (no python-dotenv dependency).

The bridge itself gets its env from docker-compose's env_file, so this is only
for running list_dialpad_targets.py / setup_dialpad.py by hand. Real shell env
vars take precedence over .env (same default as python-dotenv).
"""

import os


def load(path: str = None):
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding
    anything already set). Defaults to a .env next to this file. No-op if absent."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if (val[:1], val[-1:]) in (('"', '"'), ("'", "'")):
                val = val[1:-1]                      # quoted: keep as-is
            else:
                hash_at = val.find(" #")             # unquoted: drop inline comment
                if hash_at != -1:
                    val = val[:hash_at].rstrip()
            os.environ.setdefault(key, val)
