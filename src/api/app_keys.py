"""Application keys shared between web_api and the extracted route modules.

A key belongs here once more than one module needs it. Keys read by a single
domain stay in that domain's module — ``APP_SHUTDOWN_EVENT`` in
:mod:`src.api.runtime_routes` is the example to follow.

This module deliberately imports nothing from ``src`` so any route module can
depend on it without forming a cycle back through ``web_api``.
"""

from __future__ import annotations

from aiohttp import ClientSession, web

# Owned by web_api's cleanup context, which opens and closes the session around
# the application lifetime. Route modules only ever read it.
APP_HTTP_SESSION: web.AppKey[ClientSession] = web.AppKey("http_session", ClientSession)
