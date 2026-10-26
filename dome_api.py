"""Dome protocol, aiohttp transport and shared connection lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import itertools
import json
import math
import os
from pathlib import Path
import random
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp_socks import ProxyConnector

ROOT = Path(__file__).resolve().parent
VERSION = "2.2"


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def local_proxy(value):
    if value is None or not str(value).strip():
        return None
    try:
        url = urlsplit(str(value).strip())
        if (url.scheme not in {"socks5", "socks4", "http"} or
                url.hostname not in {"127.0.0.1", "localhost", "::1"} or
                not url.port or url.username or url.password or url.query or url.fragment or
                url.path not in {"", "/"}):
            raise ValueError
        return urlunsplit((url.scheme, url.netloc, "", "", ""))
    except (ValueError, AttributeError):
        raise ValueError("Для локального туннеля укажите socks5://127.0.0.1:10808 или http://127.0.0.1:10809 без логина/пароля.") from None


class DomeError(ConnectionError):
    def __init__(self, stage, code, detail, *, retryable=True):
        super().__init__(detail)
        self.stage, self.code, self.retryable = stage, code, retryable


class AuthError(DomeError):
    def __init__(self, detail="Сервер отклонил ключ доступа."):
        super().__init__("auth", "auth_rejected", detail, retryable=False)


class RequestTimeout(DomeError, TimeoutError):
    pass


@dataclass(frozen=True)
class Endpoint:
    url: str
    http_url: str
    origin: str | None

    @classmethod
    def parse(cls, raw, ws_path=None):
        try:
            value = urlsplit(str(raw).strip())
            if (value.scheme not in {"http", "https", "ws", "wss"} or not value.hostname or
                    value.port == 0 or value.username or value.password or value.query or value.fragment):
                raise ValueError
            path = value.path or "/"
            if ws_path is not None:
                if not ws_path.startswith("/") or ws_path.startswith("//") or "?" in ws_path or "#" in ws_path:
                    raise ValueError
                path = ws_path
            elif value.scheme in {"http", "https"} and path in {"/index.html", "/static/index.html"}:
                path = "/"
            secure = value.scheme in {"https", "wss"}
            return cls(urlunsplit(("wss" if secure else "ws", value.netloc, path, "", "")),
                       urlunsplit(("https" if secure else "http", value.netloc, value.path or "/", "", "")),
                       urlunsplit(("https" if secure else "http", value.netloc, "", "", ""))
                       if value.scheme in {"http", "https"} else None)
        except (ValueError, AttributeError):
            raise ValueError("Укажите ws://хост:порт, wss://хост/путь или HTTP-адрес купола без ключей в URL.") from None


@dataclass(frozen=True)
class Options:
    connect_timeout: float = 15
    request_timeout: float = 15
    idle_timeout: float = 20
    ping_interval: float = 30
    reconnect_delay: float = 5

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name}: нужно конечное число больше нуля.")

    @classmethod
    def from_env(cls):
        return cls(**{name: float(os.getenv("DOME_" + name.upper(), field.default))
                      for name, field in cls.__dataclass_fields__.items()})


class JsonLog:
    def __init__(self, folder, secret):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.path = folder / f"client_{self.run_id}.jsonl"
        self.file = self.path.open("x", encoding="utf-8", buffering=1)
        self.secret, self.seq = secret, 0

    def redact(self, value):
        return str(value).replace(self.secret, "[REDACTED]") if self.secret else str(value)

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if str(k).lower() in {"key", "dome_key", "api_key", "authorization"}
                    else self.clean(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.clean(v) for v in value]
        return self.redact(value) if isinstance(value, str) else value

    def write(self, kind, **fields):
        self.seq += 1
        self.file.write(json.dumps(self.clean(dict(ts=timestamp(), run_id=self.run_id,
                                                  seq=self.seq, kind=kind, **fields)), ensure_ascii=False) + "\n")

    def close(self):
        self.file.close()


class DomeClient:
    """Single reader, serialized writes, independent API request deadlines."""

    def __init__(self, url, key, log, *, connect_timeout=15, request_timeout=15,
                 on_message=None, on_event=None, proxy=None, allow_control=False):
        self.endpoint = Endpoint.parse(url) if isinstance(url, str) else url
        self.key, self.log = key, log
        self.connect_timeout, self.request_timeout = connect_timeout, request_timeout
        self.on_message, self.on_event = on_message, on_event
        self.proxy = local_proxy(proxy)
        self.allow_control = allow_control
        self.http = self.ws = self.reader = None
        self.role = self.team = self.telemetry = None
        self.pending = {}
        self.ids = itertools.count(1)
        self.telemetry_received = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.last_send = self.last_telemetry = 0.0
        self.phase, self.phase_started = "tcp", 0.0

    def event(self, **data):
        self.log.write("diagnostic", **data)
        if self.on_event:
            try:
                self.on_event(self.log.clean(data))
            except Exception as exc:
                self.log.write("error", where="observer", error=str(exc))

    def stage(self, stage, state, detail="", **fields):
        if state == "running":
            self.phase, self.phase_started = stage, time.monotonic()
        self.event(event="stage", stage=stage, state=state, detail=detail,
                   elapsed_ms=round((time.monotonic() - self.phase_started) * 1000), **fields)

    def notify(self, message):
        if self.on_message:
            try:
                self.on_message(self.log.clean(message))
            except Exception as exc:
                self.log.write("error", where="observer", error=str(exc))

    def decode(self, raw):
        try:
            message = json.loads(raw)
            if isinstance(message, dict):
                return message
        except (ValueError, TypeError, UnicodeError):
            pass
        self.log.write("error", where="protocol", error="Пропущено некорректное JSON-сообщение.")
        return None

    async def open(self):
        if self.http is not None:
            raise RuntimeError("Для нового соединения создайте новый DomeClient.")
        trace = aiohttp.TraceConfig()

        async def connected(*_):
            self.stage("tcp", "ok", "Локальный туннель открыт; ожидается ответ API." if self.proxy
                       else "TCP-канал открыт; связь с API ещё не подтверждена.")
            self.stage("websocket", "running", "Ожидание HTTP 101 Switching Protocols.")

        async def redirect(_, __, params):
            target = urlsplit(urljoin(str(params.url), params.response.headers.get("Location", "")))
            original = urlsplit(self.endpoint.http_url)
            if (target.netloc != original.netloc or
                    (target.scheme in {"https", "wss"}) != (original.scheme == "https")):
                raise DomeError("websocket", "cross_origin_redirect", "Перенаправление на другой адрес. Укажите его явно.", retryable=False)

        trace.on_connection_create_end.append(connected)
        trace.on_request_redirect.append(redirect)
        self.stage("tcp", "running", f"Соединение через {self.proxy}." if self.proxy else "Соединение с сервером.")
        connector = ProxyConnector.from_url(self.proxy) if self.proxy else None
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.connect_timeout),
                                          connector=connector, trace_configs=[trace], trust_env=False,
                                          cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with asyncio.timeout(self.connect_timeout):
                self.ws = await self.http.ws_connect(self.endpoint.url, origin=self.endpoint.origin,
                    timeout=aiohttp.ClientWSTimeout(ws_close=1), max_msg_size=32 * 1024 * 1024, heartbeat=20, compress=0)
            self.stage("websocket", "ok", "WebSocket открыт, HTTP 101.")
            self.stage("auth", "running", "Отправлен ключ команды; ожидается auth_response.")
            buffered = []
            async with asyncio.timeout(self.request_timeout):
                await self.ws.send_json({"type": "auth", "key": self.key})
                while True:
                    frame = await self.ws.receive()
                    if frame.type not in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                        raise self.closed_error("auth")
                    auth = self.decode(frame.data)
                    if auth is None:
                        continue
                    if auth.get("type") == "auth_response":
                        break
                    if auth.get("type") == "session_revoked":
                        raise DomeError("auth", "session_revoked", "Сервер отозвал сессию.")
                    if len(buffered) < 100:
                        buffered.append(auth)
            if auth.get("success") is not True:
                raise AuthError(self.log.redact(auth.get("error") or "Сервер отклонил ключ доступа."))
            if auth.get("role") not in {"team", "guest"}:
                raise DomeError("auth", "invalid_role", "В auth_response нет роли team/guest.", retryable=False)
            self.role, self.team = auth["role"], auth.get("team")
            self.stage("auth", "ok", f"Роль: {self.role}; команда: {self.team or '—'}.")
            self.notify(dict(type="auth_response", success=True, role=self.role, team=self.team))
            self.reader = asyncio.create_task(self._read(), name="dome-reader")
            for message in buffered:
                self.dispatch(message)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            if isinstance(exc, DomeError):
                error = exc
            elif isinstance(exc, aiohttp.WSServerHandshakeError):
                error = DomeError("websocket", "handshake_rejected",
                                  f"Сервер вернул HTTP {exc.status}, нужен WebSocket HTTP 101. Проверьте порт и путь API.",
                                  retryable=exc.status >= 500 or exc.status == 429)
            elif isinstance(exc, TimeoutError):
                detail = {"tcp":"TCP-соединение не установлено", "websocket":"TCP установлен, но сервер не завершил WebSocket-рукопожатие", "auth":"WebSocket открыт, но auth_response не получен"}[self.phase]
                seconds = self.request_timeout if self.phase == "auth" else self.connect_timeout
                error = DomeError(self.phase, self.phase + "_timeout", detail + f" за {seconds:g} с.")
            elif isinstance(exc, (aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError)):
                error = DomeError("tcp", "tls_error", "Ошибка проверки TLS-сертификата.", retryable=False)
            else:
                error = DomeError(self.phase, "connection_error", self.log.redact(str(exc) or type(exc).__name__))
            self.stage(error.stage, "error", str(error), code=error.code)
            await self.close()
            raise error from exc

    def closed_error(self, stage="stream"):
        code = self.ws.close_code if self.ws is not None else None
        if code == 4401:
            return AuthError("Соединение закрыто с кодом 4401: авторизация не принята.")
        return DomeError(stage, "session_revoked" if code == 4403 else "connection_closed",
                         f"Соединение закрыто сервером (код {code or 'не указан'}).")

    def dispatch(self, message):
        kind = message.get("type")
        self.notify(message)
        if kind == "session_revoked":
            raise DomeError("stream", "session_revoked", "Сервер отозвал сессию; требуется переподключение.")
        if kind == "telemetry":
            data = message.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
                self.log.write("error", where="telemetry", error="Нет объекта data.nodes.")
                return
            self.telemetry = data
            self.last_telemetry = time.monotonic()
            self.telemetry_received.set()
        rid = message.get("request_id")
        item = self.pending.get(str(rid)) if isinstance(rid, (str, int)) else None
        if item:
            expected, future = item
            if kind == expected and not future.done():
                future.set_result(message)

    async def _read(self):
        failure = None
        try:
            async for frame in self.ws:
                if frame.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                    message = self.decode(frame.data)
                    if message is not None:
                        self.dispatch(message)
                elif frame.type == aiohttp.WSMsgType.ERROR:
                    raise self.closed_error()
            raise self.closed_error()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc if isinstance(exc, DomeError) else DomeError("stream", "reader_error", str(exc))
            raise failure
        finally:
            for _, future in self.pending.values():
                if not future.done():
                    future.set_exception(failure or DomeError("stream", "disconnected", "Соединение остановлено."))

    async def request(self, kind, **fields):
        if kind not in {"ping", "describe_nodes", "query"} and not (kind == "control" and self.allow_control):
            raise ValueError("Разрешены только ping, describe_nodes и query.")
        rid = str(next(self.ids))
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = (kind + "_response", future)
        request = dict(fields, type=kind, request_id=rid)
        try:
            async with self.send_lock:
                if self.reader is None or self.reader.done():
                    if self.reader is not None:
                        await self.reader
                    raise DomeError("api", "disconnected", "Нет активной сессии.")
                await asyncio.sleep(max(0, self.last_send + .26 - time.monotonic()))
                async with asyncio.timeout(self.request_timeout):
                    await self.ws.send_json(request)
                    self.last_send = time.monotonic()
            response = await asyncio.wait_for(future, self.request_timeout)
            logged_request = request
            if kind=="control" and request.get("action")=="upload_file":
                logged_request = dict(request,value=dict(request["value"],content=f"[{len(request['value']['content'].encode('utf-8'))} bytes]"))
            self.log.write("request", request=logged_request, success=response.get("success"), error=response.get("error"))
            return response
        except TimeoutError as exc:
            raise RequestTimeout("api", "request_timeout", f"Нет ответа на {kind} за {self.request_timeout:g} с.") from exc
        except (aiohttp.ClientError, OSError) as exc:
            if isinstance(exc, DomeError):
                raise
            raise DomeError("api", "request_transport_error", f"Не удалось выполнить {kind}: {self.log.redact(exc)}") from exc
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def wait_telemetry(self, timeout):
        waiter = asyncio.create_task(self.telemetry_received.wait())
        try:
            done, _ = await asyncio.wait({waiter, self.reader}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if self.reader in done:
                await self.reader
            if waiter not in done:
                raise DomeError("telemetry", "telemetry_timeout", f"Авторизация и ping успешны, но телеметрии нет {timeout:g} с.")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()
        if self.http is not None:
            await self.http.close()


def require_success(response):
    if response.get("success") is not True:
        raise DomeError("api", "api_rejected", str(response.get("error") or "API отклонил запрос."), retryable=False)


async def session(client, options, *, once=False):
    await client.open()
    client.stage("api", "running", "Проверка ping и прав доступа.")

    async def ping():
        start = time.monotonic()
        require_success(await client.request("ping"))
        client.event(event="ping", ping_ms=round((time.monotonic() - start) * 1000))

    await ping()
    if client.role == "team":
        catalog = await client.request("describe_nodes")
        require_success(catalog)
        client.log.write("catalog", nodes=catalog.get("nodes", []))
    client.stage("api", "ok", "API отвечает.")
    if client.role == "team":
        client.stage("telemetry", "running", "Ожидание первого снимка.")
        await client.wait_telemetry(options.idle_timeout)
        client.stage("telemetry", "ok", "Телеметрия получена.")
    else:
        client.stage("telemetry", "skipped", "Режим гостя: телеметрия оборудования недоступна.")
    client.event(event="ready", role=client.role, team=client.team)
    if once:
        return
    next_ping = time.monotonic() + options.ping_interval
    while True:
        now = time.monotonic()
        deadline = next_ping if client.role == "guest" else min(next_ping, client.last_telemetry + options.idle_timeout)
        done, _ = await asyncio.wait({client.reader}, timeout=max(0, deadline - now))
        if done:
            await client.reader
        now = time.monotonic()
        if client.role == "team" and now - client.last_telemetry >= options.idle_timeout:
            raise DomeError("telemetry", "stale_telemetry", f"Поток телеметрии не обновлялся {options.idle_timeout:g} с.")
        if now >= next_ping:
            await ping()
            next_ping = time.monotonic() + options.ping_interval


async def observe(url, key, log, *, options=None, once=False, on_message=None, on_event=None, proxy=None,
                  allow_control=False, on_client=None):
    """The same supervisor is used by the CLI and local web panel."""
    options = options or Options()
    proxy = local_proxy(proxy)
    attempt = failures = 0

    def emit(**event):
        log.write("diagnostic", **event)
        if on_event:
            try:
                on_event(log.clean(event))
            except Exception as exc:
                log.write("error", where="observer", error=str(exc))

    while True:
        attempt += 1
        emit(event="attempt", attempts=attempt, url=url.url if isinstance(url, Endpoint) else Endpoint.parse(url).url,
             proxy=proxy)
        client = DomeClient(url, key, log, connect_timeout=options.connect_timeout,
                            request_timeout=options.request_timeout, on_message=on_message, on_event=on_event, proxy=proxy,
                            allow_control=allow_control)
        if on_client:
            on_client(client)
        try:
            await session(client, options, once=once)
            emit(event="checked", role=client.role, team=client.team)
            return 0
        except DomeError as exc:
            emit(event="failure", stage=exc.stage, code=exc.code, detail=str(exc), retryable=exc.retryable)
            if once or not exc.retryable:
                return 2 if isinstance(exc, AuthError) else 1
            failures = 0 if client.last_telemetry else min(failures + 1, 6)
        finally:
            if on_client:
                on_client(None)
            await client.close()
        delay = min(60, options.reconnect_delay * 2 ** max(0, failures - 1))
        delay += random.uniform(0, delay * .1)
        emit(event="retry", delay_s=round(delay, 2))
        await asyncio.sleep(delay)
