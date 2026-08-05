"""
Minimal .env loader, shared by the CLI and the review app.

Avoids a python-dotenv dependency for a handful of Azure OpenAI variables.
Values already present in the environment win, so a real deployment can set
them the usual way and ignore the file.
"""

import os


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
