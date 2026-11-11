"""人工接管与条件恢复的端到端演示。

合成门户 + 合成账号 demo-user，不录制任何真实账号操作。
采集端只驱动页面公开控件（进入资料、核对页面、逐篇读取），从不点击
人工确认按钮，也从不调用运维入口 /__control；演示调度（模拟来源侧
授权失效、版本变更）与结果核对才使用运维入口。

运行：python -m collector.demo
"""
import asyncio
import json
import shutil
import socket
import threading
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from collector.core import Collector
from portal.__main__ import make_server

STATE_DIR = Path("state/demo")
PASSED = []


def ok(condition, label):
    if not condition:
        raise AssertionError(f"验证失败: {label}")
    PASSED.append(label)
    print(f"  ✓ {label}")


def control(base, payload):
    """演示调度专用：模拟来源侧授权失效与版本变更。采集端从不调用运维入口。"""
    request = urllib.request.Request(base + "/__control", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def actions(base):
    """只读核对来源动作记录（open-restricted 可证明暂停窗口是否读取资料）。"""
    with urllib.request.urlopen(base + "/__state") as response:
        return json.load(response)["actions"]


def record_reads(base, doc_id):
    return sum(1 for action in actions(base) if action["name"] == "open-restricted" and action.get("record") == doc_id)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def main():
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    server = make_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    print(f"门户: {base}   状态目录: {STATE_DIR}")

    collector = await Collector.launch(base, STATE_DIR, headless=True)
    page = collector.page
    operator = await collector.context.new_page()  # 工作人员页：同 origin 新页 + ?operator=1
    await operator.goto(base + "/?operator=1")

    async def human_confirm():
        """工作人员在 operator 页手动确认（人工动作，采集端不参与）。"""
        await operator.click("#confirm")
        await operator.wait_for_selector('body[data-authorized="true"]')

    demo_pw = await async_playwright().start()
    worker_browser = None
    try:
        print("\n【场景1】正常交接：遇确认页暂停 → 人工确认 → 恢复接续 → 授权中途失效再交接")
        await collector.create_plan("P1", "A", "sensitive", "demo-user", [f"DOC-{n}" for n in range(17, 22)])
        result = await collector.run_plan("P1")
        ok(result["outcome"] == "paused", "未授权时自动停在人工确认页，输出交接单")
        ticket1 = result["handoff"]["handoff_id"]
        print("  交接单 T1:", json.dumps(result["handoff"], ensure_ascii=False))
        snapshot = actions(base)
        await asyncio.sleep(0.3)
        ok(actions(base) == snapshot, "暂停窗口内没有任何来源动作（未读取受限资料）")
        await human_confirm()

        def revoke_after_19(_plan, doc_id):
            if doc_id == "DOC-19":
                control(base, {"confirmed": False})  # 来源侧撤销授权

        collector.after_doc_read = revoke_after_19
        result = await collector.resume(ticket1)
        collector.after_doc_read = None
        ok(result["outcome"] == "paused" and result["handoff"]["completed"] == ["DOC-17", "DOC-18", "DOC-19"],
           "授权中途失效后再次暂停，交接单含已完成范围")
        ticket2 = result["handoff"]["handoff_id"]
        ok(all(record_reads(base, f"DOC-{n}") == 1 for n in (17, 18, 19)), "DOC-17~19 各读取一次")
        snapshot = actions(base)
        await asyncio.sleep(0.3)
        ok(actions(base) == snapshot, "第二次暂停窗口内无来源动作")
        await human_confirm()
        result = await collector.resume(ticket2)
        ok(result["outcome"] == "completed", "第二次恢复后计划完成")
        ok(all(record_reads(base, f"DOC-{n}") == 1 for n in range(17, 22)), "DOC-17~21 全程各读取一次，未从第一页重来")
        kinds = [event["kind"] for event in collector.store.read_audit()]
        ok(kinds.index("resume-requested") < kinds.index("verify") < kinds.index("doc-read"), "恢复前先在页面核对，再读取资料")

        print("\n【场景2】交接期间工作人员切错工作空间 → 恢复核对失败 → 需重新确认")
        control(base, {"confirmed": False})
        await collector.create_plan("P2", "A", "sensitive", "demo-user", ["DOC-22", "DOC-23"])
        result = await collector.run_plan("P2")
        ticket3 = result["handoff"]["handoff_id"]
        await page.select_option("#space", "B")  # 人工在原页切到别的工作空间
        await human_confirm()  # 工作人员完成了确认，但页面空间已不一致
        result = await collector.resume(ticket3)
        ok(result["outcome"] == "needs_reconfirm", "条件变更被拒绝恢复")
        ok("workspace" in {check["name"] for check in result["failed_checks"]}, "核对发现工作空间不一致（已登录且已确认≠条件一致）")
        plan = collector.get_plan("P2")
        ok(plan["status"] == "needs_reconfirm" and plan["read"] == {}, "原计划转为需重新确认，进度与交接单保留")
        ok(not any(key.startswith("P2@") for key in collector.store.all_results()), "错误空间未产生任何采集结果")
        await page.select_option("#space", "A")
        control(base, {"confirmed": False})
        await human_confirm()
        await collector.reconfirm("P2")
        result = await collector.run_plan("P2")
        ok(result["outcome"] == "completed", "重新确认后计划完成")
        ok(set(collector.store.all_results()) == {"P1@A", "P2@A"}, "结果按计划@空间归档，不跨空间拼接")

        print("\n【场景3】原浏览器断开 → 附着工作人员提供的浏览器 → 重新核对后恢复")
        control(base, {"confirmed": False})
        await collector.create_plan("P3", "A", "sensitive", "demo-user", ["DOC-24", "DOC-25"])
        result = await collector.run_plan("P3")
        ticket4 = result["handoff"]["handoff_id"]
        await collector.browser.close()  # 原浏览器断开
        port = free_port()
        worker_browser = await demo_pw.chromium.launch(headless=True, args=["--no-sandbox", f"--remote-debugging-port={port}"])
        worker_context = await worker_browser.new_context()
        await (await worker_context.new_page()).goto(base)  # 工作人员的门户页
        operator = await worker_context.new_page()
        await operator.goto(base + "/?operator=1")
        await human_confirm()
        await collector.attach(f"http://127.0.0.1:{port}")
        result = await collector.resume(ticket4)
        ok(result["outcome"] == "completed", "附着工作人员浏览器后恢复完成")
        audit = collector.store.read_audit()
        attach_at = next(i for i, event in enumerate(audit) if event["kind"] == "browser-attached")
        verify_at = next(i for i, event in enumerate(audit) if event["kind"] == "verify" and i > attach_at)
        read_at = next(i for i, event in enumerate(audit) if event["kind"] == "doc-read" and event.get("doc_id") == "DOC-24")
        ok(attach_at < verify_at < read_at, "附着后先重新核对会话约束再读取")
        source = (Path(__file__).resolve().parent / "core.py").read_text(encoding="utf-8")
        ok("storage_state(" not in source and ".cookies(" not in source, "附着不导出、不复制本机 cookie 或存储状态")

        print("\n【场景4】重复恢复与并发恢复")
        control(base, {"confirmed": False})
        await collector.create_plan("P4", "A", "sensitive", "demo-user", ["DOC-26", "DOC-27"])
        result = await collector.run_plan("P4")
        ticket5 = result["handoff"]["handoff_id"]
        await human_confirm()
        first, second, third = await asyncio.gather(collector.resume(ticket5), collector.resume(ticket5), collector.resume(ticket5))
        ok(first == second == third and first["outcome"] == "completed", "并发恢复只执行一次，调用方拿到同一结果")
        again = await collector.resume(ticket5)
        ok(again == first, "重复恢复返回既有恢复结果")
        verify_count = sum(1 for event in collector.store.read_audit() if event["kind"] == "verify" and event.get("handoff_id") == ticket5)
        ok(verify_count == 4, "恢复核对只执行一批次（身份/空间/分类/授权）")
        ok(record_reads(base, "DOC-26") == 1 and record_reads(base, "DOC-27") == 1, "并发恢复未驱动两个页面重复读取")

        print("\n【场景5】资料版本变更 → 恢复核对失败")
        control(base, {"confirmed": True})
        await collector.create_plan("P5", "A", "sensitive", "demo-user", ["DOC-28", "DOC-29"])

        def revoke_and_bump(_plan, _doc_id):
            control(base, {"confirmed": False, "version": 2})  # 授权失效且资料版本升级

        collector.after_doc_read = revoke_and_bump
        result = await collector.run_plan("P5")
        collector.after_doc_read = None
        ok(result["outcome"] == "paused" and result["handoff"]["completed"] == ["DOC-28"], "读取中途授权失效，再次暂停")
        ticket6 = result["handoff"]["handoff_id"]
        await human_confirm()
        result = await collector.resume(ticket6)
        ok(result["outcome"] == "needs_reconfirm", "版本变更被拒绝恢复")
        ok(any(check["name"] == "version:DOC-28" for check in result["failed_checks"]), "核对发现 DOC-28 版本不一致")
        ok(collector.get_plan("P5")["read"] == {"DOC-28": "1"}, "原进度保留")
        await collector.reconfirm("P5")
        result = await collector.run_plan("P5")
        ok(result["outcome"] == "completed", "重新确认后完成剩余资料")

        print("\n【场景6】交接单过期")
        control(base, {"confirmed": False})
        collector.handoff_ttl = 1
        await collector.create_plan("P6", "A", "sensitive", "demo-user", ["DOC-30"])
        result = await collector.run_plan("P6")
        ticket7 = result["handoff"]["handoff_id"]
        await asyncio.sleep(1.2)
        result = await collector.resume(ticket7)
        collector.handoff_ttl = 1800
        ok(result["outcome"] == "error" and "重新发起交接" in result["error"], "过期交接单被拒绝并提示重新发起交接")
        ok(collector.get_handoff(ticket7)["status"] == "expired", "交接单状态为已过期")
        result = await collector.run_plan("P6")  # 重新发起交接
        ticket7b = result["handoff"]["handoff_id"]
        await human_confirm()
        result = await collector.resume(ticket7b)
        ok(result["outcome"] == "completed", "重新发起交接后恢复完成")

        print("\n【场景7】用户取消")
        control(base, {"confirmed": False})
        await collector.create_plan("P7", "A", "sensitive", "demo-user", ["DOC-31"])
        result = await collector.run_plan("P7")
        ticket8 = result["handoff"]["handoff_id"]
        await collector.cancel("P7")
        result = await collector.resume(ticket8)
        ok(result["outcome"] == "error" and "取消" in result["error"], "取消后旧恢复请求失效")
        ok(collector.get_handoff(ticket8)["status"] == "cancelled", "交接单状态为已取消")
        result = await collector.run_plan("P7")
        ok(result["outcome"] == "error", "已取消计划不能再运行")

        print("\n【全局核查】状态记录脱敏与读取总量")
        forbidden = ("合成私密", "private_note", "cookie", "password", "secret")
        for path in STATE_DIR.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for word in forbidden:
                    assert word not in text, f"{path} 含禁用内容 {word}"
        ok(True, "交接单、日志与结果不含 cookie、口令或合成私密字段")
        total = sum(1 for action in actions(base) if action["name"] == "open-restricted" and action.get("record"))
        ok(total == 14, "受限资料读取总量与计划一致（14 篇，各一次）")
        ok(record_reads(base, "DOC-31") == 0, "已取消计划从未读取资料")

        print(f"\n全部 {len(PASSED)} 项验证通过。")
        print(f"查看交接单: python -m collector.cli --state {STATE_DIR} handoffs")
        print(f"查看日志:   python -m collector.cli --state {STATE_DIR} log")
    finally:
        await collector.close()
        if worker_browser is not None:
            await worker_browser.close()
        await demo_pw.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
