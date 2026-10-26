"""CLI for the Dome API client v2. Python 3.11+."""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from dotenv import load_dotenv

from dome_api import Endpoint, JsonLog, Options, ROOT, VERSION, local_proxy, observe
from aiohttp_socks import ProxyConnector

LOGGER = logging.getLogger("dome")


async def diagnose(endpoint, timeout, log, proxy=None):
    """Independent TCP/HTTP check; sends no authentication key."""
    parsed = urlsplit(endpoint.url)
    start = time.monotonic()
    if proxy:
        LOGGER.info("Локальный туннель: %s", proxy)
    else:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(parsed.hostname,
                                               parsed.port or (443 if parsed.scheme == "wss" else 80)), timeout)
            writer.close()
            await writer.wait_closed()
            LOGGER.info("TCP-канал: открыт, %d мс; ответ API ещё не получен", round((time.monotonic() - start) * 1000))
        except (OSError, TimeoutError) as exc:
            LOGGER.info("TCP: %s", log.redact(str(exc) or type(exc).__name__))
    try:
        connector = ProxyConnector.from_url(proxy) if proxy else None
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as http:
            async with http.get(endpoint.http_url, allow_redirects=False) as response:
                LOGGER.info("HTTP: статус %d", response.status)
    except (aiohttp.ClientError, TimeoutError) as exc:
        LOGGER.info("HTTP: %s", log.redact(str(exc) or "сервер не прислал HTTP-ответ"))


async def run(args, endpoint, key, log):
    options = Options(connect_timeout=args.connect_timeout, request_timeout=args.request_timeout,
                      idle_timeout=args.idle_timeout, ping_interval=args.ping_interval,
                      reconnect_delay=args.reconnect_delay)

    def event(data):
        kind = data["event"]
        if kind == "stage":
            LOGGER.info("%s / %s: %s", data["stage"].upper(), data["state"], data["detail"])
        elif kind == "failure":
            LOGGER.error("[%s:%s] %s", data["stage"], data["code"], data["detail"])
        elif kind == "retry":
            LOGGER.info("Повтор через %.1f с", data["delay_s"])
        elif kind == "checked":
            LOGGER.info("Проверка пройдена. Роль: %s", data["role"])

    if args.diagnose:
        await diagnose(endpoint, min(args.connect_timeout, 5), log, args.proxy)
    task = asyncio.create_task(observe(endpoint, key, log, options=options,
                                      once=args.once or args.diagnose, on_event=event, proxy=args.proxy))
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: task.cancel())
    try:
        return await task
    except asyncio.CancelledError:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


def positive_float(text):
    value = float(text)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("Нужно конечное число больше нуля.")
    return value


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Клиент купола v2: aiohttp, диагностика, телеметрия.")
    parser.add_argument("--url", default=os.getenv("DOME_URL", ""))
    parser.add_argument("--proxy", default=os.getenv("DOME_PROXY", ""), help="Локальный SOCKS5 или HTTP CONNECT туннель.")
    parser.add_argument("--ws-path", default=os.getenv("DOME_WS_PATH") or None,
                        help="Путь WebSocket API, например /ws; уточняется у организаторов.")
    parser.add_argument("--once", action="store_true", help="Разовая проверка без повторов.")
    parser.add_argument("--diagnose", action="store_true", help="TCP + HTTP + полная разовая проверка API.")
    for name, default in (("connect-timeout",15), ("request-timeout",15), ("idle-timeout",20),
                          ("reconnect-delay",5), ("ping-interval",30)):
        parser.add_argument("--" + name, type=positive_float,
                            default=os.getenv("DOME_" + name.upper().replace("-", "_"), str(default)))
    args = parser.parse_args()
    try:
        endpoint = Endpoint.parse(args.url, args.ws_path)
        args.proxy = local_proxy(args.proxy)
    except ValueError as exc:
        parser.error(str(exc))
    key = os.getenv("DOME_KEY", "").strip()
    if not key:
        parser.error("Задайте DOME_KEY в .env или окружении.")
    folder = ROOT / os.getenv("AGENT_LOG_DIR", "logs")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = JsonLog(folder, key)
    log.write("start", agent="dome-api-client", version=VERSION, url=endpoint.url, proxy=args.proxy)
    LOGGER.info("Клиент v%s; API %s; журнал %s", VERSION, log.redact(endpoint.url), log.path)
    code = 1
    try:
        code = asyncio.run(run(args, endpoint, key, log))
    except KeyboardInterrupt:
        code = 0
    finally:
        log.write("stop", exit_code=code)
        log.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
