"""X-Plane's local Web API v2, without a simulator plug-in.

Names belong in configuration; the numeric IDs below belong only to one
connection.  Command acknowledgements never stand in for aircraft feedback.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Callable

import aiohttp

from .models import TelemetryValue


class XPlaneError(RuntimeError):
    """An X-Plane transport or API operation failed."""


class XPlaneAPIError(XPlaneError):
    """The server explicitly rejected an operation."""

    def __init__(self, code: str, message: str = "", status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(f"{code}: {message}" if message else code)


class XPlaneConnectionError(XPlaneError):
    """The connection is unavailable; actions must not be replayed."""


class XPlaneTimeoutError(XPlaneConnectionError):
    """An acknowledgement or transport response did not arrive in time."""


class XPlaneClient:
    """An explicitly connected, generation-aware asynchronous API client.

    ``read_event`` yields dictionaries with ``kind``, ``generation`` and
    ``keys``. A ``lost`` event also includes ``reason``. ``data`` dataref
    values remain base64 strings; callers decide whether they represent text.
    The caller owns heartbeat, resync and reconnect scheduling.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8086", timeout: float = 2.0):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.generation = 0
        self.healthy = False
        self.values: dict[str, TelemetryValue] = {}
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.capabilities: dict[str, Any] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_id = 0
        self._ids: dict[str, int] = {}
        self._types: dict[str, str] = {}
        self._id_keys: dict[int, set[str]] = {}
        self._commands: dict[str, int] = {}
        self._subscribed: set[str] = set()
        self._changed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._closing = False

    @property
    def dataref_ids(self) -> dict[str, int]:
        """Resolved logical keys for this connection, as a detached copy."""
        return dict(self._ids)

    @property
    def dataref_types(self) -> dict[str, str]:
        """Server-advertised types for resolved logical keys."""
        return dict(self._types)

    @property
    def command_ids(self) -> dict[str, int]:
        """Resolved command paths for this connection, as a detached copy."""
        return dict(self._commands)

    async def connect(self) -> dict[str, Any]:
        """Open a fresh API v2 connection and discard all prior IDs."""
        await self.close()
        self._closing = False
        self.generation += 1
        generation = self.generation
        self._ids.clear()
        self._types.clear()
        self._id_keys.clear()
        self._commands.clear()
        self._subscribed.clear()
        self.capabilities = {}
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"Accept": "application/json"},
        )
        try:
            capabilities = await self._http_get("/api/capabilities")
            if not isinstance(capabilities, dict):
                raise XPlaneAPIError("invalid_response", "Malformed capabilities response")
            versions = capabilities.get("api", {}).get("versions", [])
            if "v2" not in versions:
                raise XPlaneAPIError("unsupported_api", "X-Plane Web API v2 is required")
            self._ws = await self._session.ws_connect(self.base_url + "/api/v2", autoping=True)
            self.capabilities = capabilities
            self.healthy = True
            self._reader = asyncio.create_task(
                self._receive(self._ws, generation), name="xplane-websocket"
            )
            return capabilities
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise

    async def resolve_datarefs(self, specs: dict[str, str]) -> dict[str, int]:
        """Register logical keys by exact name; return only names that exist."""
        self._require_connected()
        generation = self.generation
        found = await self._lookup("datarefs", list(dict.fromkeys(specs.values())))
        self._require_generation(generation)
        result = {}
        for key, path in specs.items():
            metadata = found.get(path)
            previous = self._ids.get(key)
            if metadata is None:
                self._ids.pop(key, None)
                self._types.pop(key, None)
                self._subscribed.discard(key)
                if key in self.values:
                    self._invalidate([key])
                continue
            identifier = metadata["id"]
            self._ids[key] = identifier
            self._types[key] = metadata.get("value_type", "")
            result[key] = identifier
            if previous != identifier:
                self._invalidate([key])
        self._id_keys = {}
        for key, identifier in self._ids.items():
            self._id_keys.setdefault(identifier, set()).add(key)
        return result

    async def resolve_commands(self, paths: list[str]) -> dict[str, int]:
        """Resolve exact command paths, without caching across connections."""
        self._require_connected()
        generation = self.generation
        found = await self._lookup("commands", list(dict.fromkeys(paths)))
        self._require_generation(generation)
        result = {}
        for path in paths:
            if path in found:
                result[path] = found[path]["id"]
                self._commands[path] = result[path]
            else:
                self._commands.pop(path, None)
        return result

    async def subscribe(self, keys: list[str]) -> None:
        """Replace subscriptions, acknowledge them, and acquire a snapshot.

        Subscribe to full arrays: indexed subscriptions have compacted array
        positions in this API. REST reads cover static/idempotent subscriptions
        and cannot overwrite a newer WebSocket revision.
        """
        async with self._subscription_lock:
            self._require_connected()
            desired = set(keys)
            missing = desired - self._ids.keys()
            if missing:
                raise KeyError(f"Unresolved datarefs: {', '.join(sorted(missing))}")
            generation = self.generation
            if self._subscribed and desired != self._subscribed:
                await self._request_ws("dataref_unsubscribe_values", {"datarefs": "all"})
                self._require_generation(generation)
                self._invalidate(self._subscribed - desired)
            self._subscribed = desired
            if desired:
                identifiers = sorted({self._ids[key] for key in desired})
                await self._request_ws(
                    "dataref_subscribe_values",
                    {"datarefs": [{"id": identifier} for identifier in identifiers]},
                )
                self._require_generation(generation)
                await self.resync()

    async def command(self, path: str) -> None:
        """Issue a one-shot command once, with no automatic retry."""
        self._require_connected()
        if path not in self._commands:
            raise KeyError(f"Unresolved command: {path}")
        await self._request_ws(
            "command_set_is_active",
            {"commands": [{"id": self._commands[path], "is_active": True, "duration": 0}]},
        )

    async def write(self, key: str, value: Any) -> None:
        """Write once and await acceptance, without optimistic cache updates."""
        self._require_connected()
        if key not in self._ids:
            raise KeyError(f"Unresolved dataref: {key}")
        if not self._valid_value(value):
            raise ValueError("Dataref writes must contain finite JSON values")
        await self._request_ws(
            "dataref_set_values", {"datarefs": [{"id": self._ids[key], "value": value}]}
        )

    def get(self, key: str, default: Any = None) -> Any:
        value = self.values.get(key)
        if value is None or not value.valid or value.generation != self.generation:
            return default
        return value.value

    async def wait_value(
        self,
        key: str,
        predicate: Callable[[Any], bool],
        timeout: float = 2.0,
        min_revision: int | None = None,
    ) -> Any:
        """Wait for simulator feedback; a command ACK is insufficient."""
        generation = self.generation
        deadline = time.monotonic() + timeout
        while True:
            self._require_generation(generation)
            changed = self._changed
            value = self.values.get(key)
            if (
                value is not None
                and value.valid
                and value.generation == generation
                and (min_revision is None or value.revision >= min_revision)
                and predicate(value.value)
            ):
                return value.value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XPlaneTimeoutError(f"No confirming feedback for {key}")
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except asyncio.TimeoutError as error:
                raise XPlaneTimeoutError(f"No confirming feedback for {key}") from error

    async def heartbeat(self) -> None:
        """Verify the active WebSocket can acknowledge a harmless operation."""
        self._require_connected()
        # This built-in dataref exists even when the simulation is paused.
        if "__heartbeat" not in self._ids:
            resolved = await self.resolve_datarefs({"__heartbeat": "sim/time/paused"})
            if "__heartbeat" not in resolved:
                raise XPlaneAPIError("missing_heartbeat", "sim/time/paused is unavailable")
        await self._request_ws(
            "dataref_subscribe_values", {"datarefs": [{"id": self._ids["__heartbeat"]}]}
        )

    async def expire_health(
        self, reason: str = "Simulator health lease expired", *, generation: int | None = None
    ) -> None:
        """Invalidate this generation immediately, then close its socket.

        A caller's independent lease deadline need not wait for the longer
        timeout of an outstanding ACK. An old lease cannot fault a new session.
        """
        expected = self.generation if generation is None else generation
        if expected != self.generation or not self.healthy:
            return
        await self._fault(reason, expected)

    async def resync(self) -> None:
        """Acquire REST snapshots without overwriting newer streamed data."""
        self._require_connected()
        generation = self.generation
        snapshots = [
            (key, self._ids[key], self.values.get(key).revision if key in self.values else 0)
            for key in sorted(self._subscribed)
        ]

        async def read_one(key: str, identifier: int, revision: int) -> None:
            reply = await self._http_get(f"/api/v2/datarefs/{identifier}/value")
            if not isinstance(reply, dict) or "data" not in reply:
                await self._fault("Malformed dataref value response", generation)
                raise XPlaneConnectionError("Malformed dataref value response")
            if generation != self.generation or not self.healthy:
                return
            current_revision = self.values.get(key).revision if key in self.values else 0
            if (
                current_revision == revision
                and self._ids.get(key) == identifier
                and key in self._subscribed
            ):
                self._put_values({key: reply["data"]})

        await asyncio.gather(*(read_one(*item) for item in snapshots))
        self._require_generation(generation)

    async def read_event(self) -> dict[str, Any]:
        return await self.updates.get()

    async def close(self) -> None:
        """Close resources, invalidate feedback, and cancel pending actions."""
        self._closing = True
        self.healthy = False
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        self._fail_pending(XPlaneConnectionError("X-Plane client closed"))
        self._invalidate(list(self.values))
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if ws is not None:
            await ws.close()
        if session is not None:
            await session.close()

    def _require_connected(self) -> None:
        if not self.healthy or self._ws is None or self._ws.closed:
            raise XPlaneConnectionError("X-Plane is not connected")

    def _require_generation(self, generation: int) -> None:
        self._require_connected()
        if generation != self.generation:
            raise XPlaneConnectionError("X-Plane connection generation changed")

    async def _lookup(self, resource: str, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        found = {}
        # Keep exact-name URLs modest; a missing name may fail the entire batch.
        for start in range(0, len(paths), 16):
            batch = paths[start : start + 16]
            try:
                payload = await self._http_get(
                    f"/api/v2/{resource}", [("filter[name]", path) for path in batch]
                )
            except XPlaneAPIError as error:
                if error.status != 404:
                    raise
                if len(batch) == 1:
                    continue
                found.update(await self._lookup(resource, batch[: len(batch) // 2]))
                found.update(await self._lookup(resource, batch[len(batch) // 2 :]))
                continue
            records = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise XPlaneAPIError("invalid_response", f"Malformed {resource} list")
            for record in records:
                if (
                    isinstance(record, dict)
                    and record.get("name") in batch
                    and type(record.get("id")) is int
                ):
                    found[record["name"]] = record
        return found

    async def _http_get(self, path: str, params: Any = None) -> Any:
        session = self._session
        generation = self.generation
        if session is None or session.closed:
            raise XPlaneConnectionError("X-Plane HTTP session is closed")
        try:
            async with session.get(self.base_url + path, params=params) as response:
                try:
                    body = await response.json()
                except (ValueError, aiohttp.ContentTypeError) as error:
                    raise XPlaneAPIError("invalid_response", "Expected a JSON response") from error
                if response.status != 200:
                    raise XPlaneAPIError(
                        body.get("error_code", "http_error")
                        if isinstance(body, dict)
                        else "http_error",
                        body.get("error_message", "") if isinstance(body, dict) else str(body),
                        response.status,
                    )
                return body
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as error:
            await self._fault(f"HTTP transport failed: {error}", generation)
            if isinstance(error, asyncio.TimeoutError):
                raise XPlaneTimeoutError("X-Plane HTTP response timed out") from error
            raise XPlaneConnectionError("X-Plane HTTP transport failed") from error

    async def _request_ws(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_connected()
        generation = self.generation
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(self.timeout):
                async with self._send_lock:
                    self._require_generation(generation)
                    assert self._ws is not None
                    await self._ws.send_json({"req_id": request_id, "type": kind, "params": params})
                result = await future
            self._require_generation(generation)
            if result.get("success") is not True:
                raise XPlaneAPIError(
                    result.get("error_code", "operation_rejected"), result.get("error_message", "")
                )
            return result
        except asyncio.TimeoutError as error:
            await self._fault(f"{kind} acknowledgement timed out", generation)
            raise XPlaneTimeoutError(
                f"{kind} acknowledgement timed out; action was not retried"
            ) from error
        except (aiohttp.ClientError, OSError) as error:
            await self._fault(f"WebSocket send failed: {error}", generation)
            raise XPlaneConnectionError("X-Plane WebSocket send failed") from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A failed send can fault this pending future before it is
                # awaited. Retrieve its exception as well as the send error.
                future.exception()

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse, generation: int) -> None:
        try:
            async for frame in ws:
                if generation != self.generation:
                    return
                if frame.type == aiohttp.WSMsgType.TEXT:
                    try:
                        message = json.loads(frame.data)
                    except (TypeError, ValueError) as error:
                        raise XPlaneConnectionError("Invalid WebSocket JSON") from error
                    if not isinstance(message, dict):
                        raise XPlaneConnectionError("Invalid WebSocket message")
                    if message.get("type") == "result":
                        future = self._pending.get(message.get("req_id"))
                        if future is not None and not future.done():
                            future.set_result(message)
                    elif message.get("type") == "dataref_update_values":
                        data = message.get("data")
                        if not isinstance(data, dict):
                            raise XPlaneConnectionError("Invalid dataref update")
                        logical = {}
                        for raw_identifier, value in data.items():
                            try:
                                identifier = int(raw_identifier)
                            except (TypeError, ValueError) as error:
                                raise XPlaneConnectionError(
                                    "Invalid dataref ID in update"
                                ) from error
                            for key in self._id_keys.get(identifier, ()):
                                if key in self._subscribed or key == "__heartbeat":
                                    logical[key] = value
                        self._put_values(logical)
                elif frame.type == aiohttp.WSMsgType.ERROR:
                    raise XPlaneConnectionError(f"WebSocket error: {ws.exception()}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fault(str(error), generation)
        finally:
            if generation == self.generation and self.healthy:
                await self._fault("X-Plane WebSocket closed", generation)

    def _put_values(self, raw_values: dict[str, Any]) -> None:
        if not raw_values or not self.healthy:
            return
        now = time.monotonic()
        for key, raw in raw_values.items():
            previous = self.values.get(key)
            self.values[key] = TelemetryValue(
                value=raw,
                valid=self._valid_value(raw),
                revision=previous.revision + 1 if previous is not None else 1,
                generation=self.generation,
                updated_at=now,
            )
        self.updates.put_nowait(
            {"kind": "update", "generation": self.generation, "keys": sorted(raw_values)}
        )
        self._wake_waiters()

    def _invalidate(self, keys: Any) -> None:
        for key in keys:
            previous = self.values.get(key)
            self.values[key] = TelemetryValue(
                value=previous.value if previous is not None else None,
                valid=False,
                revision=previous.revision + 1 if previous is not None else 0,
                generation=self.generation,
                updated_at=previous.updated_at if previous is not None else 0.0,
            )
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        previous, self._changed = self._changed, asyncio.Event()
        previous.set()

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _fault(self, reason: str, generation: int) -> None:
        if generation != self.generation or self._closing:
            return
        self.healthy = False
        self.generation += 1
        self._fail_pending(XPlaneConnectionError(reason))
        self._invalidate(list(self.values))
        self.updates.put_nowait(
            {"kind": "lost", "generation": self.generation, "keys": [], "reason": reason}
        )
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    @staticmethod
    def _valid_value(value: Any) -> bool:
        if isinstance(value, list):
            return all(XPlaneClient._valid_value(item) for item in value)
        if isinstance(value, float):
            return math.isfinite(value)
        return isinstance(value, (int, bool, str))
