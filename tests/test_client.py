import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from websockets.asyncio.server import serve

from dome_api import AuthError, DomeClient, JsonLog, Options, observe, session


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = JsonLog(Path(self.tmp.name), "FAKE_SECRET")
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.log.close()
        # Covers accidental secret logging even when received in chat or errors.
        text = self.log.path.read_text(encoding="utf-8")
        self.assertNotIn("FAKE_SECRET", text)
        self.tmp.cleanup()

    def client(self, server):
        port = server.sockets[0].getsockname()[1]
        client = DomeClient(f"ws://127.0.0.1:{port}", "FAKE_SECRET", self.log,
                            connect_timeout=2, request_timeout=0.2)
        self.clients.append(client)
        return client

    async def test_interleaved_telemetry_reply_and_chat(self):
        received = []

        async def handler(ws):
            self.assertEqual(json.loads(await ws.recv()),
                             {"type": "auth", "key": "FAKE_SECRET"})
            await ws.send(json.dumps(dict(type="auth_response", success=True, role="team", team=1)))
            async for raw in ws:
                req = json.loads(raw)
                received.append(req["type"])
                await ws.send("null")  # Invalid shape must not kill the reader.
                await ws.send(json.dumps(dict(type="telemetry", data={"nodes": {"printer_3d_01": None}})))
                await ws.send(json.dumps(dict(type="meeting_message", message={"text": "FAKE_SECRET"})))
                await ws.send(json.dumps(dict(type=req["type"] + "_response", success=True,
                                               request_id=req["request_id"], nodes=[])))

        async with serve(handler, "127.0.0.1", 0) as server:
            client = self.client(server)
            await session(client, Options(idle_timeout=1), once=True)
            self.assertEqual(received, ["ping", "describe_nodes"])
            self.assertEqual(client.telemetry["nodes"]["printer_3d_01"], None)
            self.assertFalse(client.pending)
            with self.assertRaises(ValueError):
                await client.request("control", action="start_job")

    async def test_guest_does_not_request_equipment_catalog(self):
        received = []

        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps(dict(type="auth_response", success=True, role="guest")))
            async for raw in ws:
                req = json.loads(raw)
                received.append(req["type"])
                await ws.send(json.dumps(dict(type="ping_response", request_id=req["request_id"], success=True)))

        async with serve(handler, "127.0.0.1", 0) as server:
            await session(self.client(server), Options(idle_timeout=1), once=True)
            self.assertEqual(received, ["ping"])

    async def test_rejected_auth(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps(dict(type="auth_response", success=False, error="FAKE_SECRET")))

        async with serve(handler, "127.0.0.1", 0) as server:
            with self.assertRaises(AuthError):
                await self.client(server).open()

    async def test_pending_request_fails_on_disconnect(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps(dict(type="auth_response", success=True, role="team")))
            await ws.recv()
            await ws.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            client = self.client(server)
            await client.open()
            with self.assertRaises(ConnectionError):
                await client.request("ping")
            self.assertFalse(client.pending)

    async def test_timeout_cleans_pending(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps(dict(type="auth_response", success=True, role="team")))
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            client = self.client(server)
            await client.open()
            with self.assertRaises(TimeoutError):
                await client.request("ping")
            self.assertFalse(client.pending)

    async def test_revoked_session_reconnects(self):
        connections = 0

        async def handler(ws):
            nonlocal connections
            connections += 1
            await ws.recv()
            if connections == 1:
                await ws.send(json.dumps(dict(type="auth_response", success=True, role="team")))
                await ws.recv()
                await ws.send(json.dumps(dict(type="session_revoked")))
                return
            # Second connection proves reconnect; reject to finish the run cleanly.
            await ws.send(json.dumps(dict(type="auth_response", success=False)))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            options = Options(connect_timeout=2, request_timeout=1, ping_interval=.1, reconnect_delay=.01)
            result = await asyncio.wait_for(observe(f"ws://127.0.0.1:{port}",
                                                   "FAKE_SECRET", self.log, options=options), 3)
            self.assertEqual(result, 2)
            self.assertEqual(connections, 2)


if __name__ == "__main__":
    unittest.main()
