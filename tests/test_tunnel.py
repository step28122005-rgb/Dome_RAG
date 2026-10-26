import asyncio
from contextlib import suppress
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from websockets.asyncio.server import serve

from dome_api import DomeClient, DomeError, JsonLog, Options, local_proxy, observe


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = JsonLog(Path(self.temp.name), "TEST_TUNNEL_KEY")
        self.destinations = []
        self.proxy_tasks = set()
        self.expected_target = None
        self.requests = []

    async def asyncTearDown(self):
        tasks = list(self.proxy_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.log.close()
        self.assertNotIn("TEST_TUNNEL_KEY", self.log.path.read_text(encoding="utf-8"))
        self.temp.cleanup()

    async def fake_dome(self, ws):
        self.assertEqual(ws.request.path, "/ws")
        auth = json.loads(await ws.recv())
        self.assertEqual(auth, {"type":"auth", "key":"TEST_TUNNEL_KEY"})
        await ws.send(json.dumps({"type":"auth_response", "success":True, "role":"team", "team":1}))
        await ws.send(json.dumps({"type":"telemetry", "data":{"nodes":{"battery_01":{"soc_pct":64}}}}))
        async for raw in ws:
            request = json.loads(raw)
            self.requests.append(request["type"])
            await ws.send(json.dumps({"type":request["type"] + "_response", "success":True,
                                      "request_id":request["request_id"], "nodes":[]}))

    async def relay(self, reader, writer, host, port):
        self.destinations.append((host, port))
        if (host, port) != self.expected_target:
            raise ValueError("Unexpected destination in test proxy")
        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)

        async def pipe(source, sink):
            while chunk := await source.read(65536):
                sink.write(chunk)
                await sink.drain()

        tasks = [asyncio.create_task(pipe(reader, upstream_writer)),
                 asyncio.create_task(pipe(upstream_reader, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()
            with suppress(Exception):
                await upstream_writer.wait_closed()

    async def socks(self, reader, writer):
        self.proxy_tasks.add(asyncio.current_task())
        try:
            version, count = await reader.readexactly(2)
            if version != 5:
                return
            await reader.readexactly(count)
            writer.write(b"\x05\x00")
            await writer.drain()
            version, command, _, kind = await reader.readexactly(4)
            if kind == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif kind == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
            else:
                return
            port = struct.unpack("!H", await reader.readexactly(2))[0]
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            await self.relay(reader, writer, host, port)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self.proxy_tasks.discard(asyncio.current_task())

    async def http_connect(self, reader, writer):
        self.proxy_tasks.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            method, target, _ = headers.split(b"\r\n",1)[0].decode().split(" ")
            self.assertEqual(method, "CONNECT")
            host, port = target.rsplit(":",1)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await self.relay(reader, writer, host, int(port))
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self.proxy_tasks.discard(asyncio.current_task())

    async def end_to_end(self, scheme, handler):
        async with serve(self.fake_dome, "127.0.0.1", 0) as dome:
            target_port = dome.sockets[0].getsockname()[1]
            self.expected_target = ("127.0.0.1", target_port)
            async with await asyncio.start_server(handler, "127.0.0.1", 0) as proxy:
                proxy_port = proxy.sockets[0].getsockname()[1]
                result = await asyncio.wait_for(observe(f"ws://127.0.0.1:{target_port}/ws", "TEST_TUNNEL_KEY",
                    self.log, options=Options(connect_timeout=2, request_timeout=2, idle_timeout=2),
                    once=True, proxy=f"{scheme}://127.0.0.1:{proxy_port}"),5)
            self.assertEqual(result,0)
            self.assertEqual(self.destinations,[self.expected_target])
            self.assertEqual(self.requests,["ping","describe_nodes"])

    async def test_socks5_tunnel_auth_api_and_telemetry(self):
        await self.end_to_end("socks5", self.socks)

    async def test_http_connect_tunnel_auth_api_and_telemetry(self):
        await self.end_to_end("http", self.http_connect)

    async def test_rejected_proxy_does_not_fall_back_to_direct(self):
        received = []
        async def reject(reader, writer):
            received.append(await reader.read(8192))
            writer.write(b"\x05\xff")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async with await asyncio.start_server(reject,"127.0.0.1",0) as proxy:
            port=proxy.sockets[0].getsockname()[1]
            client=DomeClient("ws://127.0.0.1:1/ws", "TEST_TUNNEL_KEY", self.log,
                              proxy=f"socks5://127.0.0.1:{port}",connect_timeout=1)
            with self.assertRaises(DomeError):
                await client.open()
            await client.close()
        self.assertTrue(received)
        self.assertTrue(received[0].startswith(b"\x05"))
        self.assertNotIn(b"TEST_TUNNEL_KEY",b"".join(received))


class ProxyConfigurationTests(unittest.TestCase):
    def test_only_explicit_local_proxy_routes_are_accepted(self):
        self.assertIsNone(local_proxy(""))
        self.assertEqual(local_proxy("socks5://127.0.0.1:10808/"),"socks5://127.0.0.1:10808")
        self.assertEqual(local_proxy("http://localhost:10809"),"http://localhost:10809")
        for url in ("socks5://example.com:1080","socks5://user:password@127.0.0.1:1080","http://127.0.0.1:0","http://127.0.0.1:1080/?key=secret"):
            with self.assertRaises(ValueError):
                local_proxy(url)


if __name__=="__main__":
    unittest.main()
