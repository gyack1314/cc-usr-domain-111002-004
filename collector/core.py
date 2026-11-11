"""采集核心：计划执行、遇人工确认页暂停、交接单恢复、浏览器附着。

设计约束：
- 自动部分只读取已获授权的页面；遇到人工确认页立即停止导航与读取，
  不点击确认按钮、不补填确认字段、不修改门户状态。
- 恢复必须引用未消费的 handoff_id，并在页面重新核对身份、空间、分类
  与授权完成标志（页面上“已登录”不等于条件一致）。
- 附着工作人员提供的浏览器时，不导出、不复制任何 cookie 或存储状态。
"""
import asyncio
import hashlib
import secrets
import time

from playwright.async_api import async_playwright

from .models import (
    PLAN_ACTIVE,
    PLAN_AWAITING,
    PLAN_CANCELLED,
    PLAN_COMPLETED,
    PLAN_NEEDS_RECONFIRM,
    TICKET_CANCELLED,
    TICKET_CONSUMED,
    TICKET_EXPIRED,
    TICKET_PENDING,
    TICKET_RESUMING,
    HandoffTicket,
    Plan,
)
from .store import Store


class CollectorError(Exception):
    pass


def cancel_plan(store, plan_id):
    """取消计划：计划终止，其未消费的交接单一并失效。"""
    with store.locked():
        plan = store.get_plan(plan_id)
        if plan is None:
            return {"outcome": "error", "error": f"计划 {plan_id} 不存在"}
        plan.status = PLAN_CANCELLED
        store.save_plan(plan)
        count = 0
        for ticket in store.list_tickets():
            if ticket.plan_id == plan_id and ticket.status == TICKET_PENDING:
                ticket.status = TICKET_CANCELLED
                store.save_ticket(ticket)
                count += 1
    store.audit("plan-cancelled", plan_id=plan_id, tickets=count)
    return {"outcome": "cancelled", "plan_id": plan_id, "tickets": count}


def reconfirm_plan(store, plan_id):
    """需重新确认的计划在人工重新确认后重新激活；原有进度与交接单保留。"""
    with store.locked():
        plan = store.get_plan(plan_id)
        if plan is None:
            return {"outcome": "error", "error": f"计划 {plan_id} 不存在"}
        if plan.status != PLAN_NEEDS_RECONFIRM:
            return {"outcome": "error", "error": "仅“需重新确认”状态的计划可重新激活"}
        plan.status = PLAN_ACTIVE
        plan.revision += 1
        store.save_plan(plan)
    store.audit("plan-reconfirmed", plan_id=plan_id, revision=plan.revision)
    return {"outcome": "active", "plan_id": plan_id, "revision": plan.revision}


class Collector:
    """浏览器资料采集器。单进程使用；状态落盘，交接单可跨进程查看。"""

    def __init__(self, base_url, store, handoff_ttl=1800.0):
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.handoff_ttl = float(handoff_ttl)
        self.after_doc_read = None  # 观察钩子：演示用来模拟来源侧状态变化
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._owns_browser = False
        self._resume_events = {}
        self._attached_pending_verify = False  # 附着的新页面：恢复前需先重建导航上下文

    @classmethod
    async def launch(cls, base_url, state_dir, *, headless=True, handoff_ttl=1800.0, browser_args=("--no-sandbox",)):
        self = cls(base_url, Store(state_dir), handoff_ttl)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=headless, args=list(browser_args))
        self._owns_browser = True
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.page.set_default_timeout(10000)
        await self.page.goto(self.base_url)
        self.store.audit("collector-launched", base_url=self.base_url)
        return self

    async def attach(self, cdp_url):
        """附着到工作人员另行提供的浏览器连接。

        只建立 CDP 连接并接管页面引用；不导出 storage_state、不读取或复制
        任何 cookie。附着后的首次恢复仍会在页面重新核对会话约束。
        """
        if self._pw is None:
            self._pw = await async_playwright().start()
        browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = None
        for candidate in context.pages:
            if candidate.url.rstrip("/") == self.base_url:
                page = candidate
                break
        if page is None:
            for candidate in context.pages:
                if candidate.url.startswith(self.base_url):
                    page = candidate
                    break
        if page is None:
            page = await context.new_page()
            await page.goto(self.base_url)
        page.set_default_timeout(10000)
        self.browser = browser
        self.context = context
        self.page = page
        self._owns_browser = False
        self._attached_pending_verify = True
        self.store.audit("browser-attached", cdp=cdp_url, page_url=page.url)

    async def close(self):
        try:
            if self.browser is not None and self._owns_browser:
                await self.browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self.store.close()

    # ---- 计划 ----
    async def create_plan(self, plan_id, workspace, category, user, scope):
        scope = list(scope)
        if not all([plan_id, workspace, category, user]) or not scope:
            return {"outcome": "error", "error": "计划编号、工作空间、分类、用户与资料范围均不能为空"}
        if len(set(scope)) != len(scope):
            return {"outcome": "error", "error": "资料范围存在重复编号"}
        if self.store.get_plan(plan_id):
            return {"outcome": "error", "error": f"计划 {plan_id} 已存在"}
        plan = Plan(plan_id=plan_id, workspace=workspace, category=category, user=user, scope=scope, created_at=time.time())
        self.store.save_plan(plan)
        self.store.audit("plan-created", plan_id=plan_id, workspace=workspace, category=category, user=user, scope=",".join(scope))
        return {"outcome": "created", "plan": plan.to_dict()}

    def get_plan(self, plan_id):
        plan = self.store.get_plan(plan_id)
        return plan.to_dict() if plan else None

    def list_handoffs(self):
        return [ticket.to_dict() for ticket in self.store.list_tickets()]

    def get_handoff(self, handoff_id):
        ticket = self.store.get_ticket(handoff_id)
        return ticket.to_dict() if ticket else None

    async def run_plan(self, plan_id):
        plan = self.store.get_plan(plan_id)
        if plan is None:
            return {"outcome": "error", "error": f"计划 {plan_id} 不存在"}
        if plan.status == PLAN_CANCELLED:
            return {"outcome": "error", "error": "计划已取消"}
        if plan.status == PLAN_NEEDS_RECONFIRM:
            return {"outcome": "error", "error": "计划条件已变更，需重新确认后再运行"}
        if plan.status == PLAN_COMPLETED:
            return {"outcome": "completed", "plan_id": plan_id, "read": sorted(plan.read)}
        page = self._require_page()
        self.store.audit("plan-started", plan_id=plan_id, workspace=plan.workspace, category=plan.category, revision=plan.revision)
        if not page.url.startswith(self.base_url):
            await page.goto(self.base_url)
        await self._apply_context(page, plan)
        self._attached_pending_verify = False
        return await self._drain(page, plan)

    # ---- 恢复 ----
    async def resume(self, handoff_id):
        """引用未消费的交接单恢复。重复或并发调用只返回既有恢复结果。"""
        executor = False
        event = None
        with self.store.locked():
            ticket = self.store.get_ticket(handoff_id)
            if ticket is None:
                return {"outcome": "error", "error": f"交接单 {handoff_id} 不存在"}
            if ticket.status == TICKET_CONSUMED:
                self.store.audit("resume-duplicate-served", handoff_id=handoff_id)
                return ticket.result
            if ticket.status == TICKET_CANCELLED:
                return {"outcome": "error", "error": "交接单已取消，关联计划已终止"}
            if ticket.status == TICKET_EXPIRED:
                return {"outcome": "error", "error": "交接单已过期，需要重新发起交接"}
            if ticket.status == TICKET_RESUMING:
                event = self._resume_events.get(handoff_id)
            else:  # TICKET_PENDING：认领，保证并发恢复不会驱动两个页面
                if time.time() > ticket.expires_at:
                    ticket.status = TICKET_EXPIRED
                    self.store.save_ticket(ticket)
                    self.store.audit("handoff-expired", handoff_id=handoff_id)
                    return {"outcome": "error", "error": "交接单已过期，需要重新发起交接"}
                plan = self.store.get_plan(ticket.plan_id)
                if plan is None or plan.status == PLAN_CANCELLED:
                    ticket.status = TICKET_CANCELLED
                    self.store.save_ticket(ticket)
                    return {"outcome": "error", "error": "计划已取消，交接单失效"}
                ticket.status = TICKET_RESUMING
                self.store.save_ticket(ticket)
                event = asyncio.Event()
                self._resume_events[handoff_id] = event
                executor = True
        if not executor:
            if event is None:
                return {"outcome": "error", "error": "交接单正在其他进程恢复中"}
            await event.wait()
            done = self.store.get_ticket(handoff_id)
            self.store.audit("resume-duplicate-served", handoff_id=handoff_id)
            return done.result
        try:
            result = await self._resume_execute(ticket)
        except Exception as exc:  # 异常也落账，交接单不卡在 resuming
            result = {"outcome": "error", "error": f"恢复执行异常: {exc}"}
        with self.store.locked():
            done = self.store.get_ticket(handoff_id)
            done.status = TICKET_CONSUMED
            done.result = result
            self.store.save_ticket(done)
        self.store.audit("resume-finished", handoff_id=handoff_id, outcome=result["outcome"])
        event.set()
        return result

    async def _resume_execute(self, ticket):
        plan = self.store.get_plan(ticket.plan_id)
        self.store.audit("resume-requested", handoff_id=ticket.handoff_id, plan_id=plan.plan_id)
        page = self._require_page()
        if self._attached_pending_verify:
            # 附着的是工作人员另行提供的页面：先重建计划的导航上下文，
            # 再核对会话约束（身份与授权完成标志不可由采集端伪造）。
            await self._apply_context(page, plan)
            self._attached_pending_verify = False
            self.store.audit("context-reapplied", handoff_id=ticket.handoff_id, plan_id=plan.plan_id)
        checks = await self._verify(page, plan, ticket.handoff_id)
        failed = [check for check in checks if not check["ok"]]
        if failed:
            plan.status = PLAN_NEEDS_RECONFIRM
            self.store.save_plan(plan)
            self.store.audit("plan-needs-reconfirm", plan_id=plan.plan_id, reason=",".join(check["name"] for check in failed))
            return {"outcome": "needs_reconfirm", "plan_id": plan.plan_id, "failed_checks": failed, "checks": checks}
        fresh = self.store.get_plan(plan.plan_id)
        if fresh.status == PLAN_CANCELLED:
            return {"outcome": "error", "error": "恢复期间计划已取消"}
        plan.status = PLAN_ACTIVE
        self.store.save_plan(plan)
        result = await self._drain(page, plan)
        result["checks"] = checks
        return result

    async def _verify(self, page, plan, handoff_id):
        """在页面重新核对身份、空间、分类与授权完成标志，并比对已读资料版本。"""
        checks = []

        def record(name, ok, observed, expected):
            checks.append({"name": name, "ok": bool(ok), "observed": observed, "expected": expected})

        await page.click("#check")  # 页面自带的“核对页面”按钮：刷新授权标志，不修改门户状态
        identity = (await page.text_content("#identity") or "").split("|")
        user = identity[0].strip() if len(identity) > 0 else ""
        space = identity[1].strip() if len(identity) > 1 else ""
        category = identity[2].strip() if len(identity) > 2 else ""
        authorized = await page.get_attribute("body", "data-authorized")
        record("identity", user == plan.user, user, plan.user)
        record("workspace", space == plan.workspace, space, plan.workspace)
        record("category", category == plan.category, category, plan.category)
        record("authorization", authorized == "true", authorized, "true")
        if all(check["ok"] for check in checks) and plan.read:
            await page.click("#open")
            await self._wait_content(page)
            if await self._gate_visible(page):
                record("authorization", False, "确认页", "已授权")
            else:
                for doc_id, version in sorted(plan.read.items()):
                    actual = await page.get_attribute(f'article[data-record="{doc_id}"]', "data-version")
                    record(f"version:{doc_id}", actual == version, actual, version)
        for check in checks:
            self.store.audit("verify", handoff_id=handoff_id, plan_id=plan.plan_id, check=check["name"], ok=check["ok"], observed=str(check["observed"]))
        return checks

    # ---- 取消 / 重新确认 ----
    async def cancel(self, plan_id):
        return cancel_plan(self.store, plan_id)

    async def reconfirm(self, plan_id):
        return reconfirm_plan(self.store, plan_id)

    # ---- 内部 ----
    def _require_page(self):
        if self.page is None:
            raise CollectorError("无可用浏览器页面，请先 launch 或 attach")
        return self.page

    async def _apply_context(self, page, plan):
        await page.select_option("#space", plan.workspace)
        await page.select_option("#category", plan.category)
        await page.wait_for_selector(f'body[data-space="{plan.workspace}"][data-category="{plan.category}"]', state="attached")

    async def _wait_content(self, page):
        await page.wait_for_selector('#content :is(article, [data-handoff="required"], [data-empty="true"])')

    async def _gate_visible(self, page):
        return bool(await page.locator('#content [data-handoff="required"]').count())

    async def _drain(self, page, plan):
        """接续未读资料；遇人工确认页立即暂停并输出交接单。"""
        if not plan.remaining:
            plan.status = PLAN_COMPLETED
            self.store.save_plan(plan)
            return {"outcome": "completed", "plan_id": plan.plan_id, "read": sorted(plan.read)}
        await page.click("#open")
        await self._wait_content(page)
        if await self._gate_visible(page):
            return await self._pause(plan, "打开资料列表时遇到人工确认页")
        for doc_id in plan.remaining:
            button = f'#content button[data-open-doc="{doc_id}"]'
            if not await page.locator(button).count():
                raise CollectorError(f"范围内资料 {doc_id} 不在当前分类列表中")
            await page.click(button)
            await page.wait_for_selector(f'article[data-record="{doc_id}"] .doc-body .body, #content [data-handoff="required"]')
            if await self._gate_visible(page):
                return await self._pause(plan, f"读取 {doc_id} 前遇到人工确认页")
            version = await page.get_attribute(f'article[data-record="{doc_id}"]', "data-version")
            text = await page.text_content(f'article[data-record="{doc_id}"] .body')
            # 只提取正文；[data-private] 合成私密字段不进入采集结果与日志
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            self.store.write_material(plan, doc_id, text)
            self.store.save_result(plan, {
                "doc_id": doc_id,
                "version": version,
                "sha256": digest,
                "chars": len(text),
                "revision": plan.revision,
                "fetched_at": round(time.time(), 3),
            })
            plan.read[doc_id] = version
            self.store.save_plan(plan)
            self.store.audit("doc-read", plan_id=plan.plan_id, doc_id=doc_id, version=version, sha256=digest)
            if self.after_doc_read is not None:
                self.after_doc_read(plan, doc_id)
        plan.status = PLAN_COMPLETED
        self.store.save_plan(plan)
        self.store.audit("plan-completed", plan_id=plan.plan_id, read=",".join(sorted(plan.read)))
        return {"outcome": "completed", "plan_id": plan.plan_id, "read": sorted(plan.read)}

    async def _pause(self, plan, reason):
        """立即停止自动导航与读取，输出一次性交接单。"""
        now = time.time()
        ticket = HandoffTicket(
            handoff_id=secrets.token_hex(16),
            plan_id=plan.plan_id,
            workspace=plan.workspace,
            category=plan.category,
            user=plan.user,
            stopped_url=self.page.url,
            reason=reason,
            completed=sorted(plan.read),
            remaining=plan.remaining,
            created_at=round(now, 3),
            expires_at=round(now + self.handoff_ttl, 3),
        )
        plan.status = PLAN_AWAITING
        self.store.save_plan(plan)
        self.store.save_ticket(ticket)
        self.store.audit(
            "handoff-created",
            handoff_id=ticket.handoff_id,
            plan_id=plan.plan_id,
            stopped_url=ticket.stopped_url,
            completed=",".join(ticket.completed),
            remaining=",".join(ticket.remaining),
        )
        return {"outcome": "paused", "plan_id": plan.plan_id, "handoff": ticket.to_dict()}
