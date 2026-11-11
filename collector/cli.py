"""采集端状态查看与计划控制命令行。

查看交接单/计划/日志、取消计划、重新激活计划均可跨进程执行（直接作用于
状态目录）；恢复恢复需要持有浏览器页面，须在采集进程内调用
``await collector.resume(handoff_id)``。

用法：
    python -m collector.cli --state state/demo plans
    python -m collector.cli --state state/demo handoffs
    python -m collector.cli --state state/demo handoff <handoff_id>
    python -m collector.cli --state state/demo log [--tail 20]
    python -m collector.cli --state state/demo cancel <plan_id>
    python -m collector.cli --state state/demo reconfirm <plan_id>
"""
import argparse
import json
import time

from .core import cancel_plan, reconfirm_plan
from .store import Store


def _fmt_ts(value):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="collector-cli", description="采集端状态查看与计划控制")
    parser.add_argument("--state", default="state", help="状态目录（默认 state）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plans", help="列出采集计划")
    sub.add_parser("handoffs", help="列出交接单")
    handoff = sub.add_parser("handoff", help="查看单张交接单")
    handoff.add_argument("id")
    log = sub.add_parser("log", help="查看审计日志")
    log.add_argument("--tail", type=int, default=20)
    cancel = sub.add_parser("cancel", help="取消计划（其未消费交接单一并失效）")
    cancel.add_argument("plan_id")
    reconfirm = sub.add_parser("reconfirm", help="人工重新确认后重新激活计划")
    reconfirm.add_argument("plan_id")
    args = parser.parse_args()

    store = Store(args.state)
    if args.cmd == "plans":
        for plan in store.list_plans():
            print(f"{plan.plan_id}  空间={plan.workspace} 分类={plan.category} 状态={plan.status} "
              f"已读={len(plan.read)}/{len(plan.scope)} 修订={plan.revision}")
    elif args.cmd == "handoffs":
        for ticket in store.list_tickets():
            print(f"{ticket.handoff_id}  计划={ticket.plan_id} 状态={ticket.status} "
              f"已完成={len(ticket.completed)} 待读={len(ticket.remaining)} 过期={_fmt_ts(ticket.expires_at)}")
    elif args.cmd == "handoff":
        ticket = store.get_ticket(args.id)
        if ticket is None:
            print(f"交接单 {args.id} 不存在")
            raise SystemExit(1)
        data = ticket.to_dict()
        data["created_at"] = _fmt_ts(ticket.created_at)
        data["expires_at"] = _fmt_ts(ticket.expires_at)
        _show(data)
    elif args.cmd == "log":
        for event in store.read_audit()[-args.tail:]:
            print(json.dumps(event, ensure_ascii=False))
    elif args.cmd == "cancel":
        _show(cancel_plan(store, args.plan_id))
    elif args.cmd == "reconfirm":
        _show(reconfirm_plan(store, args.plan_id))


if __name__ == "__main__":
    main()
