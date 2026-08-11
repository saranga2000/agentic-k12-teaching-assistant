"""Entry point for the labelling tool.

    python -m k12ta.label
"""

from __future__ import annotations

import uvicorn

from k12ta.label.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
