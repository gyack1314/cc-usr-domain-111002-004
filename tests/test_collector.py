"""采集端核心流程检查：暂停、恢复、条件核对、一次性交接单与脱敏。"""
import asyncio
import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from collector.core import Collector
from portal.__main__ import make_server


def control(base, payload):
    request = urllib.request.Request(base + "/__control", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def actions(base):
    with urllib.request.urlopen(base + "/__state") as response:
        return json.load(response)["actions"]


class CollectorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tmp = tempfile.mkdtemp()
        self.collector = await Collector.launch(self.base, self.tmp, headless=True)
        self.operator = await self.collector.context.new_page()
        await self.operator.goto(self.base + "/?operator=1")

    async def asyncTearDown(self):
        await self.collector.close()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def confirm(self):
        await self.operator.click("#confirm")
        await self.operator.wait_for_selector('body[data-authorized="true"]')

    async def test_pause_then_resume_completes(self):
        await self.collector.create_plan("T1", "A", "sensitive", "demo-user", ["DOC-17", "DOC-18"])
        result = await self.collector.run_plan("T1")
        self.assertEqual("paused", result["outcome"])
        self.assertFalse(any(action["name"] == "open-restricted" for action in actions(self.base)))
        ticket = result["handoff"]["handoff_id"]
        await self.confirm()
        result = await self.collector.resume(ticket)
        self.assertEqual("completed", result["outcome"])
        self.assertEqual(2, sum(1 for action in actions(self.base) if action.get("record")))
        again = await self.collector.resume(ticket)
        self.assertEqual(result, again)

    async def test_workspace_mismatch_needs_reconfirm(self):
        await self.collector.create_plan("T2", "A", "sensitive", "demo-user", ["DOC-17"])
        result = await self.collector.run_plan("T2")
        ticket = result["handoff"]["handoff_id"]
        await self.collector.page.select_option("#space", "B")
        await self.confirm()
        result = await self.collector.resume(ticket)
        self.assertEqual("needs_reconfirm", result["outcome"])
        self.assertEqual("needs_reconfirm", self.collector.get_plan("T2")["status"])
        self.assertFalse(any(action.get("record") for action in actions(self.base)))

    async def test_expired_ticket_rejected(self):
        self.collector.handoff_ttl = 0.5
        await self.collector.create_plan("T3", "A", "sensitive", "demo-user", ["DOC-17"])
        result = await self.collector.run_plan("T3")
        ticket = result["handoff"]["handoff_id"]
        await asyncio.sleep(0.8)
        result = await self.collector.resume(ticket)
        self.assertEqual("error", result["outcome"])
        self.assertIn("重新发起交接", result["error"])

    async def test_cancel_invalidates_ticket(self):
        await self.collector.create_plan("T4", "A", "sensitive", "demo-user", ["DOC-17"])
        result = await self.collector.run_plan("T4")
        ticket = result["handoff"]["handoff_id"]
        await self.collector.cancel("T4")
        result = await self.collector.resume(ticket)
        self.assertEqual("error", result["outcome"])
        self.assertEqual("cancelled", self.collector.get_handoff(ticket)["status"])

    async def test_state_files_redacted(self):
        await self.collector.create_plan("T5", "A", "sensitive", "demo-user", ["DOC-17"])
        result = await self.collector.run_plan("T5")
        await self.confirm()
        await self.collector.resume(result["handoff"]["handoff_id"])
        for path in Path(self.tmp).rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for word in ("合成私密", "private_note", "cookie", "password"):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
