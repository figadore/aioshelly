"""WsRpc for Shelly."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from asyncio import tasks
from dataclasses import dataclass
from typing import Any, Callable, cast

import aiohttp
import async_timeout
from aiohttp import ClientWebSocketResponse, WSMsgType, client_exceptions

from .common import ConnectionOptions
from .const import NOTIFY_WS_CLOSED, WS_HEARTBEAT
from .exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    InvalidMessage,
    JSONRPCError,
    RPCError,
    RPCTimeout,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RouteData:
    """RouteData (src/dst) class."""

    src: str | None
    dst: str | None


class RPCCall:
    """RPCCall class."""

    def __init__(
        self,
        call_id: int,
        method: str,
        params: dict[str, Any] | None,
        route: RouteData,
    ):
        """Initialize RPC class."""
        self.call_id = call_id
        self.method = method
        self.params = params
        self.src = route.src
        self.dst = route.dst
        self.resolve: asyncio.Future = asyncio.Future()
        self.auth = {}
        if params and "auth" in params:
            self.auth = params["auth"]
            del params["auth"]

    @property
    def request_frame(self) -> dict[str, Any]:
        """Request frame."""
        msg = {
            "id": self.call_id,
            "method": self.method,
            "src": self.src,
        }
        if self.auth:
            msg["auth"] = self.auth
        for obj in ("params", "dst"):
            if getattr(self, obj) is not None:
                msg[obj] = getattr(self, obj)
        return msg


def hex_hash(message: str) -> str:
    """
    Get hex representation of sha256 hash of string
    """
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class WsRPC:
    """WsRPC class."""

    def __init__(self, ip_address: str, on_notification: Callable) -> None:
        """Initialize WsRPC class."""
        self._ip_address = ip_address
        self._auth: dict[str, Any] = {}
        self._on_notification = on_notification
        self._rx_task: tasks.Task[None] | None = None
        self._client: ClientWebSocketResponse | None = None
        self._calls: dict[int, RPCCall] = {}
        self._call_id = 0
        self._route = RouteData(f"aios-{id(self)}", None)

    @property
    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    async def connect(
        self,
        aiohttp_session: aiohttp.ClientSession,
        options: ConnectionOptions | None = None,
    ) -> None:
        """Connect to device."""
        if self.connected:
            raise RuntimeError("Already connected")

        _LOGGER.debug("Trying to connect to device at %s", self._ip_address)
        try:
            self._client = await aiohttp_session.ws_connect(
                f"http://{self._ip_address}/rpc", heartbeat=WS_HEARTBEAT
            )
        except (
            client_exceptions.WSServerHandshakeError,
            client_exceptions.ClientError,
        ) as err:
            raise CannotConnect(f"Error connecting to {self._ip_address}") from err

        self._rx_task = asyncio.create_task(self._rx_msgs())

        _LOGGER.info("Connected to %s", self._ip_address)

        # https://shelly-api-docs.shelly.cloud/gen2/Overview/CommonDeviceTraits/#authentication-over-websocket
        if options and options.password:
            try:
                # First API call should fail with 401 if auth enabled
                await self.call("Sys.GetStatus", {})
            except JSONRPCError as err:
                if err.code != 401:
                    raise err
                response_message = json.loads(err.message)

                username = "admin"  # always
                ha1 = hex_hash(
                    f"{username}:{response_message['realm']}:{options.password}"
                )
                # Static noise
                ha2 = hex_hash("dummy_method:dummy_uri")
                cnonce = secrets.randbelow(10**8)
                if "nc" not in response_message:
                    response_message["nc"] = 1
                hashed = hex_hash(
                    f"{ha1}:{response_message['nonce']}:{response_message['nc']}:{cnonce}:auth:{ha2}"
                )
                auth = {
                    "realm": response_message["realm"],
                    "username": "admin",
                    "nonce": response_message["nonce"],
                    "cnonce": cnonce,
                    "response": hashed,
                    "algorithm": "SHA-256",
                }
                self.set_auth(auth)
                # Verify auth
                try:
                    await self.call("Sys.GetStatus")
                except JSONRPCError as err:
                    _LOGGER.info("Auth failed, password may be incorrect")
                    raise err

    async def disconnect(self) -> None:
        """Disconnect all sessions."""
        self._rx_task = None
        if self._client is None:
            return

        await self._client.close()

    async def _handle_call(self, frame_id: str) -> None:
        assert self._client

        await self._send_json(
            {
                "id": frame_id,
                "src": self._route.src,
                "error": {"code": 500, "message": "Not Implemented"},
            }
        )

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        if peer_src := frame.get("src"):
            if self._route.dst is not None and peer_src != self._route.dst:
                _LOGGER.warning(
                    "Remote src changed: %s -> %s", self._route.dst, peer_src
                )
            self._route.dst = peer_src

        frame_id = frame.get("id")

        if method := frame.get("method"):
            # peer is invoking a method
            params = frame.get("params")
            if frame_id:
                # and expects a response
                _LOGGER.debug("handle call for frame_id: %s", frame_id)
                asyncio.create_task(self._handle_call(frame_id))
            else:
                # this is a notification
                _LOGGER.debug("Notification: %s %s", method, params)
                self._on_notification(method, params)

        elif frame_id:
            # looks like a response
            if frame_id not in self._calls:
                _LOGGER.warning("Response for an unknown request id: %s", frame_id)
                return

            call = self._calls.pop(frame_id)
            if not call.resolve.cancelled():
                call.resolve.set_result(frame)

        else:
            _LOGGER.warning("Invalid frame: %s", frame)

    async def _rx_msgs(self) -> None:
        assert self._client

        while not self._client.closed:
            try:
                frame = await self._receive_json_or_raise()
            except ConnectionClosed:
                break

            if not self._client.closed:
                self._handle_frame(frame)

        _LOGGER.debug("Websocket connection closed")

        for call_item in self._calls.values():
            call_item.resolve.cancel()
        self._calls.clear()

        if not self._client.closed:
            await self._client.close()

        self._client = None
        self._on_notification(NOTIFY_WS_CLOSED)

    async def _receive_json_or_raise(self) -> dict[str, Any]:
        """Receive json or raise."""
        assert self._client
        msg = await self._client.receive()

        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise ConnectionClosed("Connection was closed.")

        if msg.type == WSMsgType.ERROR:
            raise ConnectionFailed()

        if msg.type != WSMsgType.TEXT:
            raise InvalidMessage(f"Received non-Text message: {msg.type}")

        _LOGGER.debug("recv(%s): %s", self._ip_address, msg.data)
        try:
            data: dict[str, Any] = msg.json()
        except ValueError as err:
            raise InvalidMessage("Received invalid JSON.") from err

        return data

    @property
    def connected(self) -> bool:
        """Return if we're currently connected."""
        return self._client is not None and not self._client.closed

    def set_auth(self, auth: dict[str, Any]) -> None:
        """
        Sets digest auth params
        """
        self._auth = auth

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: int = 10
    ) -> dict[str, Any]:
        """Websocket RPC call."""
        if self._client is None:
            raise RuntimeError("Not connected")

        if self._auth:
            if not params:
                params = {}
            if "auth" not in params:
                params["auth"] = self._auth
        call = RPCCall(self._next_id, method, params, self._route)
        self._calls[call.call_id] = call
        await self._send_json(call.request_frame)

        try:
            async with async_timeout.timeout(timeout):
                resp = await call.resolve
        except asyncio.TimeoutError as exc:
            _LOGGER.warning("%s timed out: %s", call, exc)
            raise RPCTimeout(call) from exc
        except Exception as exc:
            _LOGGER.error("%s ???: %s", call, exc)
            raise RPCError(call, exc) from exc

        if "result" in resp:
            _LOGGER.debug("%s(%s) -> %s", call.method, call.params, resp["result"])
            return cast(dict, resp["result"])

        try:
            code, msg = resp["error"]["code"], resp["error"]["message"]
            raise JSONRPCError(code, msg)
        except KeyError as err:
            raise RPCError(f"bad response: {resp}") from err

    async def _send_json(self, data: dict[str, Any]) -> None:
        """Send json frame to device."""
        _LOGGER.debug("send(%s): %s", self._ip_address, data)
        assert self._client
        await self._client.send_json(data)
