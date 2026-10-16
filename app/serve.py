"""Entry point used by the container: uvicorn with the same JSON log format.

    python -m app.serve            # the service, port from PORT (default 8080)
    python -m app.serve mocks      # the mock platforms, port from PORT (default 9000)
"""
from __future__ import annotations

import os
import sys

import uvicorn

from app.config import get_settings
from app.logs import setup_logging


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "api"
    setup_logging(get_settings().log_level)
    if target == "mocks":
        uvicorn.run("app.mocks.server:app", host="0.0.0.0", port=int(os.getenv("PORT", "9000")), log_config=None,
                    access_log=False)
    else:
        uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_config=None,
                    access_log=False, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
