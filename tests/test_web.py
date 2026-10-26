import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from websockets.asyncio.server import serve
from http.server import ThreadingHTTPServer

from dome_web import Dashboard, handler_for


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard("TEST_SECRET", folder=Path(self.temp.name), timeout=1)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.dashboard))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    async def asyncTearDown(self):
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        await asyncio.to_thread(self.dashboard.close)
        self.temp.cleanup()

    async def request(self, path, data=None, origin=None):
        def execute():
            headers = {}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = origin
            req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers)
            with build_opener(ProxyHandler({})).open(req, timeout=3) as response:
                return response.read().decode()
        return await asyncio.to_thread(execute)

    async def test_snapshot_and_static_files_do_not_expose_key(self):
        self.dashboard.event("echo TEST_SECRET")
        for path in ("/", "/app.js", "/app.css", "/api/state"):
            body = await self.request(path)
            self.assertNotIn("TEST_SECRET", body)
        state = json.loads(await self.request("/api/state"))
        self.assertTrue(state["key_configured"])
        self.assertIn("[REDACTED]", state["events"][0]["text"])
        with self.assertRaises(HTTPError) as exc:
            await self.request("/.env")
        self.assertEqual(exc.exception.code, 404)

    async def test_cross_origin_connection_is_rejected(self):
        with self.assertRaises(HTTPError) as exc:
            await self.request("/api/connection", {"action":"connect", "url":"ws://127.0.0.1:1"},
                               origin="https://example.com")
        self.assertEqual(exc.exception.code, 403)
        self.assertEqual(json.loads(self.dashboard.snapshot())["status"], "idle")

    async def test_check_auth_ping_catalog_and_telemetry(self):
        requests = []
        async def fake_dome(ws):
            self.assertEqual(ws.request.path, "/gateway")
            auth = json.loads(await ws.recv())
            self.assertEqual(auth, {"type":"auth", "key":"TEST_SECRET"})
            await ws.send(json.dumps({"type":"auth_response", "success":True, "role":"team", "team":1}))
            async for raw in ws:
                req = json.loads(raw)
                requests.append(req["type"])
                await ws.send(json.dumps({"type":"telemetry", "data":{"nodes":{"battery_01":{"online":True,"soc_pct":80}}, "game_time":{"hour":12.5}}}))
                await ws.send(json.dumps({"type":req["type"]+"_response", "request_id":req["request_id"], "success":True, "nodes":[{"node_id":"battery_01","title":"Battery"}]}))

        async with serve(fake_dome,"127.0.0.1",0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            self.dashboard.ws_path = "/gateway"
            await self.request("/api/connection", {"action":"check", "url":url})
            for _ in range(100):
                state = json.loads(self.dashboard.snapshot())
                if state["status"] in {"checked","error"}:
                    break
                await asyncio.sleep(.02)
            self.assertEqual(state["status"], "checked", state["error"])
            self.assertEqual(state["nodes"]["battery_01"]["soc_pct"],80)
            self.assertEqual(state["team"],1)
            self.assertIsNotNone(state["ping_ms"])
            self.assertEqual(state["version"], "2.2")
            self.assertEqual(set(state["diagnostics"]), {"tcp","websocket","auth","api","telemetry"})
            self.assertTrue(all(item["state"] == "ok" for item in state["diagnostics"].values()))
            self.assertEqual(requests,["ping","describe_nodes"])
            await self.request("/api/connection", {"action":"disconnect"})
            self.assertEqual(json.loads(self.dashboard.snapshot())["status"],"idle")

    async def test_invalid_address_is_rejected_without_connecting(self):
        with self.assertRaises(HTTPError) as exc:
            await self.request("/api/connection", {"action":"connect", "url":"ftp://127.0.0.1/"})
        self.assertEqual(exc.exception.code,400)
        self.assertEqual(json.loads(self.dashboard.snapshot())["status"],"idle")


if __name__ == "__main__":
    unittest.main()
