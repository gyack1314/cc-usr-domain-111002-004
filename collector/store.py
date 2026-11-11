"""采集端状态存取：计划、交接单、结果与审计日志。

只保存脱敏元数据。审计日志在写入前检查字段名，命中 cookie、口令、
私密等标记直接拒绝，从机制上防止敏感内容进入交接单与日志。
"""
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from .models import HandoffTicket, Plan

REDACT_MARKERS = ("cookie", "password", "passwd", "secret", "token", "private")


def _check_fields(fields):
    for key in fields:
        if any(marker in key.lower() for marker in REDACT_MARKERS):
            raise ValueError(f"审计字段命中脱敏规则: {key}")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "materials").mkdir(exist_ok=True)
        self._mem = threading.RLock()
        self._depth = 0  # 配合 RLock 的同线程重入计数，避免嵌套时提前放锁
        self._lockfile = open(self.root / "lock", "a+")

    def close(self):
        with self._mem:
            if not self._lockfile.closed:
                self._lockfile.close()

    def locked(self):
        """进程内锁 + 文件锁，供多步状态迁移（如交接单认领）原子执行。"""
        store = self

        class _Guard:
            def __enter__(self):
                store._mem.acquire()
                store._depth += 1
                if store._depth == 1:
                    fcntl.flock(store._lockfile, fcntl.LOCK_EX)
                return store

            def __exit__(self, *exc):
                store._depth -= 1
                if store._depth == 0:
                    fcntl.flock(store._lockfile, fcntl.LOCK_UN)
                store._mem.release()

        return _Guard()

    def _read(self, name, default):
        path = self.root / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, name, obj):
        path = self.root / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # 计划
    def get_plan(self, plan_id):
        data = self._read("plans.json", {}).get(plan_id)
        return Plan.from_dict(data) if data else None

    def save_plan(self, plan):
        with self.locked():
            plans = self._read("plans.json", {})
            plans[plan.plan_id] = plan.to_dict()
            self._write("plans.json", plans)

    def list_plans(self):
        return [Plan.from_dict(data) for data in self._read("plans.json", {}).values()]

    # 交接单
    def get_ticket(self, handoff_id):
        data = self._read("handoffs.json", {}).get(handoff_id)
        return HandoffTicket.from_dict(data) if data else None

    def save_ticket(self, ticket):
        with self.locked():
            tickets = self._read("handoffs.json", {})
            tickets[ticket.handoff_id] = ticket.to_dict()
            self._write("handoffs.json", tickets)

    def list_tickets(self):
        return [HandoffTicket.from_dict(data) for data in self._read("handoffs.json", {}).values()]

    # 采集结果：键为 计划@工作空间，不同空间的结果绝不拼接
    def results_key(self, plan):
        return f"{plan.plan_id}@{plan.workspace}"

    def save_result(self, plan, record):
        with self.locked():
            results = self._read("results.json", {})
            results.setdefault(self.results_key(plan), []).append(record)
            self._write("results.json", results)

    def all_results(self):
        return self._read("results.json", {})

    def write_material(self, plan, doc_id, text):
        folder = self.root / "materials" / self.results_key(plan)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{doc_id}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    # 审计日志（只进元数据）
    def audit(self, kind, **fields):
        _check_fields(fields)
        line = {"ts": round(time.time(), 3), "kind": kind, **fields}
        with self.locked():
            with open(self.root / "audit.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def read_audit(self):
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
