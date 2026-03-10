# 龟仙人长连接稳定模式 Runbook（Skill 固化版）

Updated: 2026-03-10  
Owner: `feiqiao-guard-delivery-lead`

## 1. 固定目标

唯一目标是把以下链路稳定运行并可复放：

`用户(钉钉) -> 龟仙人1 -> DingTalk Stream Bridge -> 本机 tmux/codex identity -> 过程回传 -> 最终回传`

说明：

- 线上仅承担钉钉消息通道与中转能力；
- 执行面固定在本机 `tmux/codex`；
- 不允许把本地模拟结果当成外部真实验收结论。

## 2. 对应 Skill

主 skill（稳定长连接）：

- `skills/identity-longlink-stability-orchestration/SKILL.md`

补充 skill（turn settled 追收 final）：

- `skills/identity-dispatch-final-answer/SKILL.md`

## 3. 强制门禁

1. Mission gate
- 必须先加载 `resource/ops/mission_lock_guixianren_e2e.md`。

2. Channel gate
- `龟仙人1 = ACTIVE`
- `n8n自动化通知机器人 = PAUSED`

3. Trigger gate
- 验收必须包含真实 DingTalk 入站消息。

4. Evidence gate
- 每轮必须有：`run_id`、`summary.tsv`、`report.json`、`msg_id + tag` 序列证据。

5. No-hardcode gate
- 不允许按用户文本关键词硬编码逻辑分支。
- 文本/图片/附件统一走桥接透传。

## 4. 稳定执行顺序

1. 重启本机长连接运行面

```bash
./scripts/local_dingtalk_tmux_stack.sh restart
```

2. 三平面健康检查

```bash
curl -fsS http://127.0.0.1:3001/healthz
curl -sS http://127.0.0.1:3001/v1/chat/routes
curl -sS http://127.0.0.1:3001/v1/chat/leader/snapshot
```

3. 确认 stream 真正连通（不是假启动）

```bash
rg -n "bridge_started|endpoint is \\{" .runtime/local_bridge/bridge.log | tail -n 20
cat .runtime/local_bridge/bridge_heartbeat.json
```

4. 执行验收套件

```bash
python3 scripts/run_guixianren_final_acceptance_suite.py
```

单命令标准巡检（推荐）：

```bash
bash skills/identity-longlink-stability-orchestration/scripts/run_longlink_stability_cycle.sh
```

5. 读取消息链路证据

```bash
tail -n 200 .runtime/local_bridge/bridge.log
tail -n 120 .runtime/local_bridge/api.log
```

6. 出现 `dispatch_turn_settled` 且业务要求“最终答案”时
- 立刻走 `identity-dispatch-final-answer` 做单题收口。
- 桥接默认会先触发一次 `dispatch_force_final_retry`（通用收敛重试），仍未收口再人工续跑。

## 5. 标准事件序列

推荐序列：

- `inbound_received`
- `dispatch_accepted`
- `dispatch_summary`
- `dispatch_progress`（可重复）
- `dispatch_final_result`

可接受交互序列（需要下一轮继续）：

- 终态为 `dispatch_turn_settled`

## 6. 典型故障与处理

1. 看起来在线但无入站
- 查 `bridge_heartbeat.json` 是否增长 `callback_count`。
- 查 `approval_audit.jsonl` 是否出现 `chat_inbound_received`。

2. accepted 后长期无 final
- 先确认是否连续有 `dispatch_progress`。
- 若停在 `turn_settled`，切换到单题收口 skill。

3. route 漂移
- `/v1/chat/routes` 中目标 identity 的 `route_status` 必须是 `ok`。
- 有 `route_error` 直接 fail-close，不继续压测。

## 7. 加固计划（持续补强）

1. 固化 30s 轮询参数的变更审计（防止回退）。
2. 把失败 case 自动归类为：
- channel
- routing
- runtime
- answer-convergence
3. 对关键回归项保留近 60 条滚动事件摘要（20 最新 + 20 稳定 + 20 留底）。
