import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from aiohttp import web
from websockets.asyncio.server import serve

from dome_api import DomeClient, DomeError, Endpoint, JsonLog, Options, observe, session, ROOT


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = JsonLog(Path(self.temp.name), "FAKE_KEY")
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.log.close()
        self.assertNotIn("FAKE_KEY", self.log.path.read_text(encoding="utf-8"))
        self.temp.cleanup()

    def client(self, server, **kwargs):
        port = server.sockets[0].getsockname()[1]
        client = DomeClient(f"ws://127.0.0.1:{port}", "FAKE_KEY", self.log,
                            connect_timeout=.3, request_timeout=.2, **kwargs)
        self.clients.append(client)
        return client

    async def test_silent_tcp_server_is_reported_as_websocket_timeout(self):
        headers = []
        done = asyncio.Event()
        async def silent(reader, writer):
            try:
                headers.append(await reader.read(8192))
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                done.set()
        events = []
        async with await asyncio.start_server(silent, "127.0.0.1", 0) as server:
            client = self.client(server, on_event=events.append)
            with self.assertRaises(DomeError) as ctx:
                await client.open()
            self.assertEqual(ctx.exception.stage, "websocket")
            self.assertEqual(ctx.exception.code, "websocket_timeout")
            self.assertTrue(any(e["stage"] == "tcp" and e["state"] == "ok" for e in events))
            self.assertNotIn(b"FAKE_KEY", headers[0])
            await asyncio.wait_for(done.wait(), 2)

    async def test_http_200_is_not_a_websocket_and_does_not_receive_key(self):
        seen = []
        async def page(request):
            seen.append(dict(request.headers))
            return web.Response(text="This is a web page")
        app = web.Application()
        app.router.add_get("/", page)
        runner = web.AppRunner(app)
        await runner.setup()
        server = await asyncio.get_running_loop().create_server(runner.server,"127.0.0.1",0)
        try:
            with self.assertRaises(DomeError) as ctx:
                await self.client(server).open()
            self.assertEqual(ctx.exception.code,"handshake_rejected")
            self.assertIn("HTTP 200",str(ctx.exception))
            self.assertFalse(ctx.exception.retryable)
            self.assertNotIn("FAKE_KEY",json.dumps(seen))
        finally:
            server.close()
            await server.wait_closed()
            await runner.cleanup()

    async def test_auth_timeout_is_distinct_from_handshake_timeout(self):
        async def fake(ws):
            await ws.recv()
            await ws.wait_closed()
        async with serve(fake,"127.0.0.1",0) as server:
            with self.assertRaises(DomeError) as ctx:
                await self.client(server).open()
            self.assertEqual(ctx.exception.stage,"auth")
            self.assertEqual(ctx.exception.code,"auth_timeout")

    async def test_telemetry_arriving_before_auth_is_preserved(self):
        async def fake(ws):
            await ws.recv()
            await ws.send(json.dumps({"type":"telemetry","data":{"nodes":{"battery_01":None}}}))
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"team"}))
            await ws.wait_closed()
        async with serve(fake,"127.0.0.1",0) as server:
            client = self.client(server)
            await client.open()
            self.assertTrue(client.telemetry_received.is_set())
            self.assertEqual(client.telemetry,{"nodes":{"battery_01":None}})

    async def test_bad_observer_does_not_break_protocol(self):
        async def fake(ws):
            await ws.recv()
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"guest"}))
            async for raw in ws:
                req=json.loads(raw)
                await ws.send(json.dumps({"type":"ping_response","success":True,"request_id":req["request_id"]}))
        def broken(_):
            raise ValueError("observer failed")
        async with serve(fake,"127.0.0.1",0) as server:
            client=self.client(server,on_message=broken,on_event=broken)
            await session(client,Options(),once=True)
            self.assertEqual(client.role,"guest")

    async def test_stalled_telemetry_reconnects_even_if_websocket_is_open(self):
        async def fake(ws):
            await ws.recv()
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"team"}))
            await ws.send(json.dumps({"type":"telemetry","data":{"nodes":{}}}))
            async for raw in ws:
                req=json.loads(raw)
                await ws.send(json.dumps({"type":req["type"]+"_response","success":True,"request_id":req["request_id"],"nodes":[]}))
        async with serve(fake,"127.0.0.1",0) as server:
            client=self.client(server)
            with self.assertRaises(DomeError) as ctx:
                await session(client,Options(idle_timeout=.2,ping_interval=.05))
            self.assertEqual(ctx.exception.code,"stale_telemetry")

    async def test_cancel_during_auth_releases_connection(self):
        received=asyncio.Event()
        closed=asyncio.Event()
        async def fake(ws):
            await ws.recv()
            received.set()
            await ws.wait_closed()
            closed.set()
        async with serve(fake,"127.0.0.1",0) as server:
            client=self.client(server)
            task=asyncio.create_task(client.open())
            await received.wait()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await asyncio.wait_for(closed.wait(),2)
            self.assertTrue(client.http.closed)

    async def test_requests_are_matched_by_id_and_type_and_rate_limited(self):
        arrivals=[]
        async def fake(ws):
            await ws.recv()
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"team"}))
            requests=[]
            async for raw in ws:
                req=json.loads(raw); requests.append(req); arrivals.append(time.monotonic())
                await ws.send(json.dumps({"type":"wrong_response","success":False,"request_id":req["request_id"]}))
                if len(requests)==3:
                    for saved in reversed(requests):
                        await ws.send(json.dumps({"type":"query_response","success":True,"request_id":saved["request_id"],"result":saved["query"]}))
        async with serve(fake,"127.0.0.1",0) as server:
            client=self.client(server)
            client.request_timeout=2
            await client.open()
            results=await asyncio.gather(*(client.request("query",query=str(i)) for i in range(3)))
            self.assertEqual([r["result"] for r in results],["0","1","2"])
            self.assertTrue(all(b-a>=.24 for a,b in zip(arrivals,arrivals[1:])))
            self.assertFalse(client.pending)

    async def test_http_url_non_root_ws_path_and_real_cli_process(self):
        async def fake(ws):
            self.assertEqual(ws.request.path,"/dome/ws")
            self.assertEqual(json.loads(await ws.recv()),{"type":"auth","key":"FAKE_KEY"})
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"guest"}))
            async for raw in ws:
                req=json.loads(raw)
                await ws.send(json.dumps({"type":"ping_response","success":True,"request_id":req["request_id"]}))
        async with serve(fake,"127.0.0.1",0) as server:
            port=server.sockets[0].getsockname()[1]
            env=dict(os.environ,DOME_KEY="FAKE_KEY",DOME_PROXY="",AGENT_LOG_DIR=self.temp.name,PYTHONIOENCODING="utf-8")
            process=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/"dome_client.py"),
                "--url",f"http://127.0.0.1:{port}/static/index.html","--ws-path","/dome/ws","--once",
                env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(process.communicate(),5)
            self.assertEqual(process.returncode,0,stderr.decode())
            self.assertNotIn(b"FAKE_KEY",stdout+stderr)
            self.assertIn("Проверка пройдена",stderr.decode())


class ConfigurationTests(unittest.TestCase):
    def test_address_normalization_preserves_secure_scheme_and_explicit_path(self):
        self.assertEqual(Endpoint.parse("https://example.com/static/index.html").url,"wss://example.com/")
        self.assertEqual(Endpoint.parse("http://example.com:48765","/ws").url,"ws://example.com:48765/ws")
        self.assertEqual(Endpoint.parse("ws://example.com/dome/ws").url,"ws://example.com/dome/ws")
        for raw in ("ws://user:password@example.com/","ws://example.com/?key=secret","ftp://example.com/","ws://example.com:0/"):
            with self.assertRaises(ValueError):
                Endpoint.parse(raw)

    def test_non_positive_or_infinite_timeouts_are_rejected(self):
        for value in (0,-1,float("inf"),float("nan")):
            with self.assertRaises(ValueError):
                Options(idle_timeout=value)


if __name__=="__main__":
    unittest.main()
