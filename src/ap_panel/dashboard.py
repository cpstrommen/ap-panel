"""Loopback-only diagnostic dashboard; flight controls stay on the physical panel."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

LOG = logging.getLogger(__name__)


def _authority(value: str, port: int) -> tuple[str, int] | None:
    """Accept an explicit loopback host and the configured dashboard port."""
    try:
        parsed = urlsplit("//" + value)
        if (
            parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port != port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return parsed.hostname, parsed.port
    except ValueError:
        return None


def create_app(
    runtime: Any, assets_path: str | Path | None = None, port: int = 8787
) -> web.Application:
    """Create an aiohttp application for the runtime's diagnostic interface.

    ``status``, ``profile_summaries``, ``subscribe`` and ``unsubscribe`` are
    synchronous; action methods are asynchronous. This function does not start
    the runtime or open hardware connections.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("Dashboard port must be an integer from 1 to 65535")
    if assets_path is None:
        packaged = Path(__file__).parent / "web"
        assets = packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "web"
    else:
        assets = Path(assets_path)
    sockets: set[web.WebSocketResponse] = set()

    @web.middleware
    async def boundary(request: web.Request, handler: Any) -> web.StreamResponse:
        authority = _authority(request.headers.get("Host", ""), port)
        if authority is None:
            return web.json_response(
                {"error": "Dashboard requires its loopback host and port"}, status=403
            )
        if request.method == "POST" or request.path == "/api/events":
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    valid = (
                        parsed.scheme == "http"
                        and not parsed.path
                        and not parsed.query
                        and not parsed.fragment
                        and _authority(parsed.netloc, port) == authority
                    )
                except ValueError:
                    valid = False
                if not valid:
                    return web.json_response(
                        {"error": "Cross-origin requests are not allowed"}, status=403
                    )
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
        except (ValueError, RuntimeError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except KeyError:
            response = web.json_response({"error": "Unknown profile or control"}, status=404)
        except Exception:
            LOG.exception("Dashboard request failed: %s", request.path)
            response = web.json_response(
                {"error": "The dashboard request failed; see the application log"}, status=500
            )
        if not response.prepared:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
            response.headers["Cache-Control"] = (
                "no-store" if request.path.startswith("/api/") else "no-cache"
            )
        return response

    app = web.Application(middlewares=[boundary], client_max_size=4096)

    async def body(request: web.Request, allowed: set[str]) -> dict[str, Any]:
        if not request.can_read_body:
            result: Any = {}
        else:
            if request.content_type != "application/json":
                raise ValueError("Request body must be application/json")
            try:
                result = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("Request body must contain valid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("Request body must be a JSON object")
        unknown = set(result) - allowed
        if unknown:
            raise ValueError("Unexpected request fields: " + ", ".join(sorted(unknown)))
        return result

    def success() -> web.Response:
        return web.json_response({"ok": True, "status": runtime.status()})

    async def status(request: web.Request) -> web.Response:
        return web.json_response(runtime.status())

    async def profiles(request: web.Request) -> web.Response:
        return web.json_response({"profiles": runtime.profile_summaries()})

    async def start(request: web.Request) -> web.Response:
        await body(request, set())
        result = await runtime.start()
        if result is False:
            state = runtime.status()
            raise ValueError(state.get("error") or "The panel is not ready to start")
        return success()

    async def stop(request: web.Request) -> web.Response:
        await body(request, set())
        await runtime.stop()
        return success()

    async def select_profile(request: web.Request) -> web.Response:
        data = await body(request, {"profile_id"})
        profile_id = data.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 80:
            raise ValueError("profile_id must be a nonempty string of at most 80 characters")
        await runtime.select_profile(profile_id)
        return success()

    def require_bench() -> None:
        if runtime.status().get("mode") != "bench":
            raise ValueError("Output tests are available only in bench mode")

    async def lamp(request: web.Request) -> web.Response:
        require_bench()
        data = await body(request, {"index", "duration"})
        index = data.get("index")
        if index is not None and (type(index) is not int or not 0 <= index < 24):
            raise ValueError("index must be null or an integer from 0 to 23")
        duration = data.get("duration", 0.5)
        if (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 0.1 <= duration <= 5
        ):
            raise ValueError("duration must be between 0.1 and 5 seconds")
        await runtime.bench_lamp(index, float(duration))
        return success()

    async def walk(request: web.Request) -> web.Response:
        require_bench()
        await body(request, set())
        await runtime.bench_walk()
        return success()

    async def lcd(request: web.Request) -> web.Response:
        require_bench()
        data = await body(request, {"text"})
        text = data.get("text")
        if (
            not isinstance(text, str)
            or len(text) > 80
            or any(not 32 <= ord(char) <= 126 for char in text)
        ):
            raise ValueError("text must contain at most 80 printable ASCII characters")
        await runtime.bench_lcd(text)
        return success()

    async def events(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
        await ws.prepare(request)
        sockets.add(ws)
        queue = runtime.subscribe()

        async def send_updates() -> None:
            await ws.send_json({"type": "status", "data": runtime.status()})
            while True:
                message = await queue.get()
                if isinstance(message, dict) and "type" in message:
                    await ws.send_json(message)
                else:
                    await ws.send_json({"type": "event", "data": message})

        sender = asyncio.create_task(send_updates())
        try:
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
                # This socket publishes diagnostics only; actions use validated POST routes.
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                await sender
            result = runtime.unsubscribe(queue)
            if inspect.isawaitable(result):
                await result
            sockets.discard(ws)
        return ws

    async def asset(request: web.Request) -> web.FileResponse:
        filename = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}[request.path]
        path = assets / filename
        if not path.is_file():
            raise web.HTTPNotFound(reason="Dashboard asset is unavailable")
        return web.FileResponse(path)

    async def close_sockets(application: web.Application) -> None:
        await asyncio.gather(
            *(ws.close(code=1001, message=b"Application stopped") for ws in list(sockets))
        )

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/profiles", profiles)
    app.router.add_post("/api/run", start)
    app.router.add_post("/api/stop", stop)
    app.router.add_post("/api/profile", select_profile)
    app.router.add_post("/api/bench/lamp", lamp)
    app.router.add_post("/api/bench/walk", walk)
    app.router.add_post("/api/bench/lcd", lcd)
    app.router.add_get("/api/events", events)
    for path in ("/", "/app.css", "/app.js"):
        app.router.add_get(path, asset)
    app.on_shutdown.append(close_sockets)
    return app
