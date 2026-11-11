# 资料采集会话的人工接管与条件恢复本地门户

本项目是可在本地运行的合成业务门户，提供真实浏览器页面、交互状态及样本资料；`collector/` 是配套的资料采集端（可暂停、可移交、可验证恢复）。资料、用户与工作空间都是虚构内容。

## 环境

Python 3.12、Playwright 1.56.0 与 Chromium。`portal/` 是标准库 HTTP 服务，`web/` 是浏览器页面及 JavaScript 交互，`collector/` 是 Playwright 采集端，`tests/` 检查门户与采集端行为。Python 用于服务与浏览器控制，JavaScript 只在浏览器内运行，不需要 Node.js 服务。

安装依赖及浏览器：

```sh
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m portal --port 8090
```

在浏览器打开本机8090端口即可操作。门户的正常交互在页面完成，`web/data.json` 保存合成来源材料。状态随页面或浏览器上下文生命周期保存；需要持久状态的功能使用当前 origin 的 localStorage。重新开始可创建新的浏览器上下文。

运维入口 GET `/__state` 查看来源动作记录，POST `/__control` 用 JSON 设置 phase、version、confirmed。它们只用于本地来源调度，接入程序的正常操作不能借用运维入口取得业务数据或完成授权。GET `/api/runtime` 和 POST `/api/action` 是门户页面内部协议。

## 验证与容器

```sh
python -m unittest discover -s tests -v
python -m compileall -q portal collector tests
docker build -t human-handoff-portal .
docker run --rm --network none --shm-size=1g human-handoff-portal
```

Dockerfile 固定浏览器基础镜像的完整摘要，依赖版本在 requirements.txt 固定。首次构建需要下载依赖；镜像完成后，上述检查禁用外网，浏览器与门户在同一容器内通信。默认运行来源检查，不运行资料采集。

如需在宿主浏览器操作容器内门户：

```sh
docker run --rm -p 127.0.0.1:8090:8090 human-handoff-portal python -m portal --host 0.0.0.0 --port 8090
```

页面的 DOM 状态、公开身份属性与中文加载提示可用于等待完成，避免使用固定秒数等待。来源动作记录可核对页面操作；不要在日志记录会话秘密或不需要的私密字段。

来源场景、属性及控制方式详见 `source-contract.json`。

## 采集端（collector/）

采集端用 Playwright 驱动浏览器读取已获授权的页面，遇到人工确认页立即停止自动导航与资料读取，输出一次性交接单；不点击确认按钮、不补填确认字段、不修改门户状态，也不借用运维入口取得业务数据或完成授权。

### 采集计划

每个计划绑定计划编号、工作空间、分类、用户与资料范围（可见资料编号），并记录已读取的资料编号与版本。状态保存在状态目录（默认 `state/`）：`plans.json` 计划、`handoffs.json` 交接单、`results.json` 结果（按 `计划@工作空间` 归档，不跨空间拼接）、`audit.jsonl` 审计日志、`materials/` 采集正文。交接单与日志只含元数据，不含 cookie、口令或页面内的合成私密字段。

### 操作方法

- 运行计划（自动部分）：在采集进程内 `await collector.run_plan(plan_id)`；遇人工确认页自动暂停并输出交接单（含停下的页面地址、当前工作空间、计划摘要、已完成范围与一次性 handoff_id）。
- 查看交接单：`python -m collector.cli --state state/demo handoffs` 列出全部，`python -m collector.cli --state state/demo handoff <handoff_id>` 查看单张（含恢复核对记录）。
- 恢复：在持有浏览器页面的采集进程内 `await collector.resume(handoff_id)`。恢复前在页面重新核对身份、空间、分类与授权完成标志（页面上“已登录”不等于条件一致），并比对已读资料版本；条件相同且版本未变时接续未读资料，条件变更时计划转为需重新确认，原有进度与交接单保留。重复或并发恢复同一交接单只返回既有恢复结果；过期交接单被拒绝，需重新发起交接（重新运行计划产生新交接单）。
- 取消：`python -m collector.cli --state state/demo cancel <plan_id>`；取消后旧恢复请求失效。人工重新确认后可用 `python -m collector.cli --state state/demo reconfirm <plan_id>` 重新激活需重新确认的计划。
- 原浏览器断开：`await collector.attach(cdp_url)` 附着到工作人员另行提供的浏览器连接，随后恢复会先重建计划导航上下文、再重新核对会话约束；采集端不导出、不复制任何 cookie 或存储状态。

### 演示

`python -m collector.demo` 用两个页面（采集页 + 工作人员确认页）完成一次正常交接，并依次演示：人工切错工作空间、原浏览器断开后附着恢复、重复与并发恢复、资料版本变更、交接单过期、用户取消。演示以来源动作记录断言暂停窗口内没有读取受限资料、恢复前确实发生页面核对，并扫描状态目录确认无 cookie、口令或合成私密字段。演示使用合成账号 demo-user，不录制任何真实账号操作。
