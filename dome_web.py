"""Local web panel using the same transport and dependencies as the CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import hashlib
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlsplit

from dotenv import load_dotenv

from dome_api import Endpoint, JsonLog, Options, ROOT, VERSION, local_proxy, observe
from dome_controls import catalog_specs, expectation, preconditions, read_path, validate_value


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def websocket_url(value):
    return Endpoint.parse(value).url


class Dashboard:
    def __init__(self, key, url="", *, folder=None, timeout=10, retry_delay=10, options=None, ws_path=None, proxy=None):
        self.key, self.timeout, self.retry_delay = key, timeout, retry_delay
        self.ws_path = ws_path
        self.proxy = local_proxy(proxy)
        self.options = options or Options(connect_timeout=timeout, request_timeout=timeout,
                                          idle_timeout=max(timeout, 10), reconnect_delay=retry_delay)
        self.folder = Path(folder) if folder else ROOT / "logs"
        self.lock = threading.RLock()
        self.data = dict(status="idle", url=url, key_configured=bool(key), role=None,
                         team=None, error=None, ping_ms=None, telemetry_count=0,
                         last_telemetry_at=None, nodes={}, catalog=[], game_time=None,
                         attempts=0, log_file=None, version=VERSION, transport="aiohttp",
                         stage=None, error_code=None, diagnostics={}, proxy=self.proxy, control_specs={}, commands=[])
        self.events = deque(maxlen=100)
        self.messages = deque(maxlen=50)
        self.loop = asyncio.new_event_loop()
        self.job = None
        self.client = None
        self.active_log = None
        self.action_task = None
        self.last_telemetry = 0
        self.action_records = {}
        self.action_fingerprints = {}
        self.line_switches = {}
        self.command_lock = asyncio.Lock()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def event(self, text, level="info"):
        with self.lock:
            self.events.append(dict(time=timestamp(), text=str(text), level=level))

    def snapshot(self):
        with self.lock:
            raw = json.dumps(dict(self.data, events=list(self.events), messages=list(self.messages)),
                             ensure_ascii=False)
        # Secrets never cross the local HTTP boundary, including echoed server messages.
        return raw.replace(self.key, "[REDACTED]") if self.key else raw

    def receive(self, message):
        kind = message.get("type")
        if kind == "auth_response":
            self.update(role=message.get("role"), team=message.get("team"))
            self.event(f"Авторизация: {message.get('role')}; команда {message.get('team') or '—'}.")
        elif kind == "telemetry":
            data = message.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
                return
            nodes = data.get("nodes")
            with self.lock:
                self.data.update(nodes=nodes if isinstance(nodes, dict) else {},
                                 game_time=data.get("game_time"), last_telemetry_at=timestamp(),
                                 telemetry_count=self.data["telemetry_count"] + 1)
                self.last_telemetry = time.monotonic()
        elif kind == "describe_nodes_response" and message.get("success"):
            nodes = message.get("nodes")
            catalog = nodes if isinstance(nodes, list) else []
            self.update(catalog=catalog, control_specs=catalog_specs(catalog))
        elif kind == "meeting_message":
            msg = message.get("message")
            if isinstance(msg, dict):
                with self.lock:
                    self.messages.append(dict(msg, received_at=timestamp()))
        elif kind == "session_revoked":
            self.event("Сервер отозвал сессию. Требуется переподключение.", "warning")

    def protocol_event(self, event):
        kind = event["event"]
        if kind == "attempt":
            self.update(status="connecting", attempts=event["attempts"], diagnostics={},
                        error=None, error_code=None, role=None, team=None, ping_ms=None)
            self.event(f"Попытка {event['attempts']}: {event['url']}" +
                       (f" через {event['proxy']}" if event.get("proxy") else ""))
        elif kind == "stage":
            with self.lock:
                self.data["stage"] = event["stage"]
                self.data["diagnostics"][event["stage"]] = event
            if event["state"] != "running":
                self.event(f"{event['stage'].upper()}: {event['detail']}",
                           "error" if event["state"] == "error" else "info")
        elif kind == "ping":
            self.update(ping_ms=event["ping_ms"])
        elif kind == "ready":
            self.update(status="connected", error=None, error_code=None)
            self.event("Подключение готово: " + ("гостевой чат." if event["role"] == "guest" else "телеметрия поступает."), "success")
        elif kind == "checked":
            self.update(status="checked")
            self.event("Разовая проверка пройдена.", "success")
        elif kind == "failure":
            self.update(status="error", stage=event["stage"], error=event["detail"], error_code=event["code"])
            with self.lock:
                self.data["diagnostics"][event["stage"]] = dict(event, state="error")
            self.event(f"{event['stage'].upper()} [{event['code']}]: {event['detail']}", "error")
        elif kind == "retry":
            self.update(status="reconnecting")
            self.event(f"Повтор через {event['delay_s']:g} с.")

    async def _stop(self):
        await self._cancel_action()
        if self.job is not None:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
            self.job = None
        self.update(status="idle", error=None)

    async def _cancel_action(self):
        if self.action_task is not None and not self.action_task.done():
            self.action_task.cancel()
            await asyncio.gather(self.action_task, return_exceptions=True)

    def set_client(self, client):
        self.client = client

    def action_update(self, record, **values):
        with self.lock:
            record.update(values)
            self.data["commands"] = list(self.action_records.values())[-30:]

    async def _queue_action(self, request):
        token = request.get("token")
        if not isinstance(token,str) or not 1 <= len(token) <= 80 or not all(c.isalnum() or c in "-_" for c in token):
            raise ValueError("Нужен уникальный идентификатор команды.")
        fingerprint = hashlib.sha256(json.dumps({k:request.get(k) for k in ("node_id","action","type","value")},
                                                sort_keys=True,allow_nan=False).encode()).hexdigest()
        if token in self.action_records:
            if self.action_fingerprints.get(token)!=fingerprint:
                raise ValueError("Этот идентификатор уже относится к другой команде.")
            return self.action_records[token]["id"]
        if self.action_task is not None and not self.action_task.done():
            raise ValueError("Дождитесь результата предыдущей команды.")
        client = self.client
        if self.data["status"] != "connected" or self.data["role"] != "team" or client is None:
            raise ValueError("Управление доступно только активной команде при установленном соединении.")
        if time.monotonic()-self.last_telemetry > 10:
            raise ValueError("Телеметрия устарела. Управление временно недоступно.")
        node_id, action, kind = request.get("node_id"), request.get("action"), request.get("type","control")
        if not isinstance(node_id,str) or not isinstance(action,str) or kind not in {"query","control"}:
            raise ValueError("Неверный формат команды.")
        specs = self.data["control_specs"].get(node_id,[])
        spec = next((s for s in specs if s["action"]==action and s["type"]==kind),None)
        if spec is None:
            raise ValueError("Команда не разрешена текущим каталогом describe_nodes.")
        value = validate_value(spec, request.get("value"))
        json.dumps(value,allow_nan=False)
        nodes = self.data["nodes"]
        if kind == "control":
            preconditions(node_id,action,value,nodes)
            if node_id=="smart_panel_01" and action in {"line_on","line_off","reset_protection"}:
                switches = [t for t in self.line_switches.get(value,[]) if time.monotonic()-t < 120]
                if len(switches)>=3:
                    raise ValueError("Для этой линии уже было 3 переключения за 2 минуты. Подождите.")
                self.line_switches[value] = switches+[time.monotonic()]
        public_value = dict(value,content=f"[{len(value['content'].encode('utf-8'))} bytes]") if action=="upload_file" else value
        record = dict(id=uuid.uuid4().hex, token=token, node_id=node_id, action=action, type=kind,
                      label=spec["label"], value=public_value, status="sending", time=timestamp(), detail="Отправка команды.")
        with self.lock:
            self.action_records[token] = record
            self.action_fingerprints[token] = fingerprint
            if len(self.action_records)>100:
                oldest=next(iter(self.action_records))
                self.action_records.pop(oldest)
                self.action_fingerprints.pop(oldest,None)
        self.action_update(record)
        self.action_task = asyncio.create_task(self._execute_action(client,record,value))
        return record["id"]

    def action(self, request):
        future = asyncio.run_coroutine_threadsafe(self._queue_action(request),self.loop)
        try:
            return future.result(timeout=5)
        except TimeoutError:
            future.cancel()
            raise

    async def _execute_action(self, client, record, value):
        log = self.active_log
        seq = None
        try:
            node_id, action, kind = (record[k] for k in ("node_id","action","type"))
            payload = dict(node_id=node_id)
            if kind == "query":
                payload.update(query=action,params=value or {})
            else:
                payload["action"] = action
                if value is not None:
                    payload["value"] = value
            log_payload = dict(payload)
            if action=="upload_file":
                log_payload["value"]=record["value"]
            if log:
                log.write("decision", source="web-user", situation="Команда из веб-панели", plan=log_payload)
                seq=log.seq
            expect = expectation(node_id,action,value,self.data["nodes"]) if kind=="control" else None
            if log:
                log.write("action",request=dict(type=kind,**log_payload),response={"success":None,"pending":True},ref=seq)
                seq=log.seq
            response = await client.request(kind,**payload)
            if log:
                log.write("action_response",response=response,ref=seq)
            if response.get("success") is not True:
                self.action_update(record,status="error",detail=str(response.get("error") or "Сервер отклонил команду."))
                self.event(f"{node_id} / {action}: {record['detail']}","error")
                return
            if kind == "query":
                self.action_update(record,status="done",detail="Ответ получен.",result=response.get("result"))
                if action=="list_files" and isinstance(response.get("result"),list):
                    with self.lock:
                        target=self.data["nodes"].get(node_id)
                        if isinstance(target,dict):
                            target["files"]=response["result"]
                self.event(f"{node_id} / {action}: данные получены.","success")
                return
            self.action_update(record,status="verifying",detail="API принял команду. Проверяется телеметрия.",expected=expect)
            sent_after = self.data["telemetry_count"]
            deadline=time.monotonic()+12
            observed=None
            while time.monotonic()<deadline:
                if client is not self.client or self.data["status"]!="connected":
                    break
                if self.data["telemetry_count"]>sent_after:
                    node=self.data["nodes"].get(node_id) or {}
                    if expect:
                        observed=read_path(node,expect["path"])
                        if type(observed) in (int,float) and type(expect["target"]) in (int,float):
                            ok=math.isclose(observed,expect["target"],rel_tol=1e-5,abs_tol=.01)
                        else:
                            ok=type(observed) is type(expect["target"]) and observed==expect["target"]
                        if ok:
                            self.action_update(record,status="verified",detail="Результат подтверждён телеметрией.",observed=observed)
                            if log: log.write("verification",ref=seq,expected=expect,observed=observed,ok=True)
                            self.event(f"{node_id} / {action}: подтверждено.","success")
                            return
                    else:
                        self.action_update(record,status="accepted",detail="Команда принята; новый снимок получен. Автоматическая проверка этого действия не определена.")
                        if log: log.write("verification",ref=seq,expected=None,observed="fresh telemetry",ok=None)
                        return
                await asyncio.sleep(.2)
            self.action_update(record,status="unknown",detail="Команда принята, но результат не подтверждён. Автоматического повтора не будет.",observed=observed)
            if log: log.write("verification",ref=seq,expected=expect,observed=observed,ok=False)
        except asyncio.CancelledError:
            self.action_update(record,status="unknown",detail="Соединение остановлено; результат команды не подтверждён.")
            if log and seq:
                log.write("verification",ref=seq,ok=None,error="connection stopped")
            raise
        except Exception as exc:
            self.action_update(record,status="unknown",detail=f"{exc}. Не повторяется автоматически.")
            if log and seq:
                log.write("verification",ref=seq,ok=None,error=str(exc))
            self.event(f"Ошибка команды: {exc}","error")

    async def _command(self, action, url):
        async with self.command_lock:
            await self._stop()
            if action == "disconnect":
                self.event("Подключение остановлено пользователем.")
                return
            self.update(status="connecting", url=url.url, error=None, role=None, team=None,
                        ping_ms=None, nodes={}, catalog=[], last_telemetry_at=None,
                        telemetry_count=0, attempts=0, game_time=None, diagnostics={}, stage=None,control_specs={})
            with self.lock:
                self.messages.clear()
            self.job = asyncio.create_task(self._monitor(url, once=action == "check"))

    def command(self, action, url=None):
        if action not in {"connect", "check", "disconnect"}:
            raise ValueError("Неизвестное действие.")
        if action != "disconnect":
            if not self.key:
                raise ValueError("Добавьте DOME_KEY в локальный файл .env и перезапустите панель.")
            url = Endpoint.parse(url, self.ws_path)
        future = asyncio.run_coroutine_threadsafe(self._command(action, url), self.loop)
        try:
            return future.result(timeout=6)
        except TimeoutError:
            future.cancel()
            raise

    async def _monitor(self, url, once):
        log = None
        try:
            log = JsonLog(self.folder, self.key)
            self.active_log = log
            self.update(log_file=log.path.name)
            log.write("start", agent="dome-web-panel", version=VERSION,
                      mode="check" if once else "observe", url=url.url)
            await observe(url, self.key, log, options=self.options, once=once,
                          on_message=self.receive, on_event=self.protocol_event, proxy=self.proxy,
                          allow_control=True,on_client=self.set_client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.update(status="error", error=str(exc))
            self.event(str(exc), "error")
        finally:
            await self._cancel_action()
            self.active_log=None
            if log is not None:
                log.write("stop", agent="dome-web-panel")
                log.close()

    def close(self):
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(timeout=6)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def handler_for(dashboard):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def allowed(self):
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in hosts and
                    (origin is None or origin in {f"http://{host}" for host in hosts}))

        def send(self, body, content_type="application/json; charset=utf-8", status=200):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send('{"error":"Forbidden"}', status=403)
            path = urlsplit(self.path).path
            if path == "/api/state":
                return self.send(dashboard.snapshot())
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.css": ("app.css", "text/css; charset=utf-8"),
                     "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                     "/controls.js": ("controls.js", "application/javascript; charset=utf-8")}
            if path in files:
                name, mime = files[path]
                return self.send((ROOT / "web" / name).read_bytes(), mime)
            return self.send('{"error":"Not found"}', status=404)

        def do_POST(self):
            if not self.allowed():
                # Drain small bodies so Windows can deliver the 403 before closing.
                with suppress(ValueError, TimeoutError, OSError):
                    size = int(self.headers.get("Content-Length", 0))
                    if 0 < size <= 4096:
                        self.rfile.read(size)
                return self.send('{"error":"Forbidden"}', status=403)
            if self.path not in {"/api/connection","/api/action"}:
                return self.send('{"error":"Not found"}', status=404)
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Ожидается JSON.")
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= (20*1024*1024 if self.path=="/api/action" else 4096):
                    raise ValueError("Недопустимый размер запроса.")
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise ValueError("Ожидается объект JSON.")
                if self.path=="/api/action":
                    command_id=dashboard.action(request)
                    self.send(json.dumps(dict(ok=True,command_id=command_id)))
                else:
                    dashboard.command(request.get("action"), request.get("url"))
                    self.send('{"ok":true}')
            except (ValueError, TimeoutError) as exc:
                text = str(exc)
                if dashboard.key:
                    text = text.replace(dashboard.key, "[REDACTED]")
                self.send(json.dumps({"error": text}, ensure_ascii=False), status=400)

    return Handler


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Локальная веб-панель купола.")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--url", default=os.getenv("DOME_URL", ""), help="Начальный адрес; соединение запускается кнопкой.")
    parser.add_argument("--proxy", default=os.getenv("DOME_PROXY", ""))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535.")
    folder = Path(os.getenv("AGENT_LOG_DIR", "logs"))
    if not folder.is_absolute():
        folder = ROOT / folder
    try:
        options = Options.from_env()
        proxy = local_proxy(args.proxy)
    except ValueError as exc:
        parser.error(str(exc))
    dashboard = Dashboard(os.getenv("DOME_KEY", "").strip(), args.url, folder=folder, options=options,
                          ws_path=os.getenv("DOME_WS_PATH") or None, proxy=proxy)
    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(dashboard))
        print(f"Панель купола: http://127.0.0.1:{args.port}/", flush=True)
        print("Ctrl+C — остановить. Подключение запускается из панели.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        dashboard.close()


if __name__ == "__main__":
    main()
