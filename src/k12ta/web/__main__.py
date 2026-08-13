"""Entry point for the capture surface.

python -m k12ta.web --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse

import uvicorn

from k12ta.web.app import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
