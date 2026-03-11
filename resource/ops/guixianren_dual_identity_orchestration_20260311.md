# 龟仙人双 Identity 编排 Runbook（2026-03-11）

Owner: `feiqiao-guard-delivery-lead`  
Scope: 线上钉钉中转 + 本机 tmux/codex 执行 + 双 identity 协同

## 1) 目标架构

唯一主链路：

`用户(龟仙人) -> DingTalk Stream Bridge -> 本机 API(/v1/chat/*) -> leader + collab -> 过程回传 -> 最终回传`

固定角色：

- `feiqiao-guard-delivery-lead`: 主调度、收口、审计
- `feiqiao-guard-collab-executor`: 协作执行与验证

## 2) 路由与绑定

当前双实例路由文件：

- `/Users/yangxi/claude/codex_project/fqsh/.runtime/identity_routes.local.dual.json`

运行时关键环境：

- `FQG_IDENTITY_ROUTES_PATH=/Users/yangxi/claude/codex_project/fqsh/.runtime/identity_routes.local.dual.json`
- `FQG_CHAT_COLLAB_IDENTITY_ID=feiqiao-guard-collab-executor`

## 3) 门禁策略

1. 非共享会话门禁
- `allow_shared_session=false`，leader/collab 必须独立 `session_id/codex_home`。

2. 路由门禁
- `/v1/chat/routes` 中两实例都必须 `route_status=ok`。

3. 进程门禁
- `/v1/chat/leader/snapshot` 中两实例必须 `process_probe_ok=true`。

4. 回传门禁
- 终态必须携带本轮任务标识（`QUESTION_TAG + TASK_GUARD_TOKEN`），禁止跨任务串线。

## 4) 标准操作

1. 启停

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/local_dingtalk_tmux_stack.sh restart
```

2. 双实例路由检查

```bash
curl -sS http://127.0.0.1:3001/v1/chat/routes
curl -sS http://127.0.0.1:3001/v1/chat/leader/snapshot
```

3. 通知即审计（非侵入）

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/run_guixianren_audit_on_demand.sh
```

输出：

- `resource/reports/guixianren_audit_<timestamp>/audit_summary.md`
- `resource/reports/guixianren_audit_<timestamp>/audit_report.json`

审计窗口默认值：

- `FQG_AUDIT_HEARTBEAT_MAX_AGE_SECONDS=180`
- `FQG_AUDIT_ACTIVITY_MAX_AGE_SECONDS=7200`

说明：

- 优先使用 DingTalk callback/inbound/reply 新鲜度；
- 若 callback 暂时静默，但最近存在本地双实例 `chat_inbound_received` 证据，也可判活动有效。

4. 双实例深度回归（30用例）

```bash
cd /Users/yangxi/claude/codex_project/fqsh
python3 scripts/run_dual_identity_deep_suite.py
```

输出：

- `artifacts/ops/<日期>/dual_identity_deep_suite_<timestamp>/summary.tsv`
- `artifacts/ops/<日期>/dual_identity_deep_suite_<timestamp>/report.json`

## 5) 故障收口

1. 如果 collab 显示错误 identity
- 向 `feiqiao-guard-collab-executor` 下发一次 S0 身份自证指令并校验 snapshot。

2. 如果路由缺失/冲突
- 回退到双实例路由文件并重启本地栈，禁止继续压测。

3. 如果桥接看似在线但无回传
- 优先看 `bridge_heartbeat.json` 的 `callback/inbound/reply age`，超过阈值即判 FAIL。

## 6) Git 维护要求

1. 本类变更必须入仓
- 路由编排文件
- 门禁规则
- 审计脚本与 runbook

2. 证据必须可复放
- 每次改动后至少保留一次 `audit_summary.md + audit_report.json` 证据路径。
