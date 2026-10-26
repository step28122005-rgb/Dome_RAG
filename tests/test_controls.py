import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from websockets.asyncio.server import serve

from dome_web import Dashboard,handler_for
from dome_controls import schema,validate_value,preconditions,catalog_specs


class ControlParameterTests(unittest.TestCase):
    def test_parameter_types_ranges_and_optional_fields(self):
        speed=schema("printer_3d_01","set_speed")
        self.assertEqual(validate_value(speed,100),100)
        for value in (True,49,201,float("nan"),"100"):
            with self.assertRaises(ValueError): validate_value(speed,value)
        self.assertEqual(validate_value(schema("smart_panel_01","line_on"),2.0),2)
        self.assertEqual(validate_value(schema("thermal_insulation_01","set_lock"),{"lock":0,"locked":False}),{"lock":0,"locked":False})
        with self.assertRaises(ValueError): validate_value(schema("thermal_insulation_01","set_lock"),{"lock":0,"locked":0})
        with self.assertRaises(ValueError): validate_value(schema("cnc_01","move"),{"mode":"relative"})
        with self.assertRaises(ValueError): validate_value(schema("cnc_01","move"),{"mode":"relative","x":1,"gcode":"M3"})

    def test_machine_preconditions(self):
        nodes={"printer_3d_01":{"online":True,"state":"IDLE","bed_clear":False,"files":["part.gcode"]}}
        with self.assertRaises(ValueError): preconditions("printer_3d_01","start_job","part.gcode",nodes)
        nodes["printer_3d_01"]["bed_clear"]=True
        preconditions("printer_3d_01","start_job","part.gcode",nodes)
        with self.assertRaises(ValueError): preconditions("printer_3d_01","start_job","missing.gcode",nodes)
        nodes["printer_3d_01"].update(hold_reason="IL_CO2",state="PAUSED")
        nodes["dome_automation_01"]={"interlocks":{"IL_CO2":{"state":"TRIPPED"}}}
        with self.assertRaises(ValueError): preconditions("printer_3d_01","resume",None,nodes)

    def test_operator_actions_are_not_exposed_as_remote_controls(self):
        specs=catalog_specs([{"node_id":"network_link_01","controls":[],"service_actions":["reconnect"],"operator_controls":["replace_patchcord"]}])
        self.assertEqual([s["action"] for s in specs["network_link_01"]],["reconnect"])


class ControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.dashboard=Dashboard("FAKE_CONTROL_KEY",folder=Path(self.temp.name),timeout=1)
        self.http=ThreadingHTTPServer(("127.0.0.1",0),handler_for(self.dashboard))
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.base=f"http://127.0.0.1:{self.http.server_address[1]}"
        self.dome=None
        self.received=[]
        self.accept=True
        self.apply=True

    async def asyncTearDown(self):
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        await asyncio.to_thread(self.dashboard.close)
        if self.dome:
            self.dome.close()
            await self.dome.wait_closed()
        text="\n".join(p.read_text(encoding="utf-8") for p in Path(self.temp.name).glob("*.jsonl"))
        self.assertNotIn("FAKE_CONTROL_KEY",text)
        self.temp.cleanup()

    async def request(self,path,data=None,origin=None):
        def run():
            headers={"Content-Type":"application/json"}
            if origin: headers["Origin"]=origin
            req=Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
            with build_opener(ProxyHandler({})).open(req,timeout=3) as response:
                return json.load(response)
        return await asyncio.to_thread(run)

    async def ready(self):
        catalog=[{"node_id":"supply_ventilation_01","title":"Вентиляция","controls":["set_speed","turn_on"],"queries":["list_files"],"operator_controls":["clean_filter"]}]
        async def dome(ws):
            await ws.recv()
            await ws.send(json.dumps({"type":"auth_response","success":True,"role":"team","team":1}))
            speed=30
            async def telemetry():
                await ws.send(json.dumps({"type":"telemetry","data":{"nodes":{"supply_ventilation_01":{"online":True,"status":"OK","speed_pct":speed}}}}))
            await telemetry()
            async for raw in ws:
                req=json.loads(raw)
                if req["type"]=="describe_nodes":
                    await ws.send(json.dumps({"type":"describe_nodes_response","success":True,"request_id":req["request_id"],"nodes":catalog}))
                elif req["type"]=="control":
                    self.received.append(req)
                    await ws.send(json.dumps({"type":"control_response","success":self.accept,"error":None if self.accept else "Rejected by fake dome","request_id":req["request_id"]}))
                    if self.accept and self.apply and req["action"]=="set_speed": speed=req["value"]
                    await asyncio.sleep(.05)
                    await telemetry()
                elif req["type"]=="query":
                    self.received.append(req)
                    await ws.send(json.dumps({"type":"query_response","success":True,"request_id":req["request_id"],"result":[{"name":"part.gcode"}]}))
                else:
                    await ws.send(json.dumps({"type":"ping_response","success":True,"request_id":req["request_id"]}))
        self.dome=await serve(dome,"127.0.0.1",0)
        port=self.dome.sockets[0].getsockname()[1]
        await self.request("/api/connection",{"action":"connect","url":f"ws://127.0.0.1:{port}"})
        for _ in range(100):
            state=json.loads(self.dashboard.snapshot())
            if state["status"]=="connected": return
            await asyncio.sleep(.02)
        self.fail("Fake dome did not connect")

    def action(self,token="test-one",action="set_speed",value=60,kind="control"):
        return dict(token=token,node_id="supply_ventilation_01",action=action,value=value,type=kind)

    async def result(self,command_id):
        for _ in range(150):
            state=json.loads(self.dashboard.snapshot())
            record=next((r for r in state["commands"] if r["id"]==command_id),None)
            if record and record["status"] not in {"sending","verifying"}: return record
            await asyncio.sleep(.02)
        self.fail("Command did not complete")

    async def test_control_reaches_dome_once_and_is_verified(self):
        await self.ready()
        request=self.action()
        first=await self.request("/api/action",request)
        duplicate=await self.request("/api/action",request)
        self.assertEqual(first["command_id"],duplicate["command_id"])
        record=await self.result(first["command_id"])
        self.assertEqual(record["status"],"verified")
        self.assertEqual(record["observed"],60)
        self.assertEqual(len(self.received),1)
        with self.assertRaises(HTTPError): await self.request("/api/action",self.action(value=80))

    async def test_out_of_range_and_operator_actions_never_reach_dome(self):
        await self.ready()
        for request in [self.action(value=101),self.action(action="clean_filter",value=None),self.action(action="send_gcode",value="M3")]:
            with self.assertRaises(HTTPError): await self.request("/api/action",request)
        self.assertEqual(self.received,[])

    async def test_disconnected_guest_and_stale_data_block_controls(self):
        with self.assertRaises(HTTPError): await self.request("/api/action",self.action())
        await self.ready()
        self.dashboard.update(role="guest")
        with self.assertRaises(HTTPError): await self.request("/api/action",self.action())
        self.dashboard.update(role="team")
        self.dashboard.last_telemetry=time.monotonic()-11
        with self.assertRaises(HTTPError): await self.request("/api/action",self.action())
        self.assertEqual(self.received,[])

    async def test_server_rejection_is_shown_without_retry(self):
        await self.ready()
        self.accept=False
        result=await self.request("/api/action",self.action())
        record=await self.result(result["command_id"])
        self.assertEqual(record["status"],"error")
        self.assertEqual(len(self.received),1)

    async def test_read_only_query_uses_same_connection(self):
        await self.ready()
        result=await self.request("/api/action",self.action(action="list_files",value={},kind="query"))
        record=await self.result(result["command_id"])
        self.assertEqual(record["status"],"done")
        self.assertEqual(record["result"],[{"name":"part.gcode"}])
        self.assertEqual(self.received[0]["type"],"query")

    async def test_accepted_command_is_not_reported_done_without_verification(self):
        await self.ready()
        self.apply=False
        result=await self.request("/api/action",self.action())
        await asyncio.sleep(.5)
        record=json.loads(self.dashboard.snapshot())["commands"][-1]
        self.assertEqual(record["status"],"verifying")
        await self.request("/api/connection",{"action":"disconnect"})
        record=await self.result(result["command_id"])
        self.assertEqual(record["status"],"unknown")
        self.assertEqual(len(self.received),1)

    async def test_cross_origin_action_is_rejected(self):
        await self.ready()
        with self.assertRaises(HTTPError) as exc:
            await self.request("/api/action",self.action(),origin="https://example.com")
        self.assertEqual(exc.exception.code,403)
        self.assertEqual(self.received,[])


if __name__=="__main__": unittest.main()
