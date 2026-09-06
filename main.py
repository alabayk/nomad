"""Wasmer-compatible entry point for the Nomad FastAPI application."""

from __future__ import annotations

import os

from app.main import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
