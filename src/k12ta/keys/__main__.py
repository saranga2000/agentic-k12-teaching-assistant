"""Entry point for the parent-only answer-key ingestion app.

python -m k12ta.keys --host 127.0.0.1 --port 8082
"""

from __future__ import annotations

import argparse

import uvicorn

from k12ta.keys.app import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
