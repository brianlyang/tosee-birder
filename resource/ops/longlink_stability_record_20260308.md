# 长连接稳定性归档（龟仙人链路）

时间：2026-03-08  
Identity：`feiqiao-guard-delivery-lead`  
范围：`用户 -> 龟仙人 -> DingTalk Stream -> FQG Bridge -> 本地 tmux/Codex`

## 结论

- 运行面常驻问题已由 `launchd + supervisor` 基本解决（不再受 sandbox 命令生命周期误杀）。
- 但链路仍存在“假健康/抖动”风险：需要 heartbeat + 连续失败阈值 + 严格启动门禁。
- 本次修订后，以 `resource/ops/dingtalk_stream_stepwise_hardening_20260308.md` 作为当前权威执行基线。

## 稳定性机制

1. 宿主级常驻
- `scripts/local_stack_launchd.sh` 托管本地桥接栈生命周期。
- 避免 sandbox 子进程在命令结束后被系统回收。

1.1 Stream 自愈参数（新增）
- `FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS=1800`
- `FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS=21600`
- `FQG_BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS=15`
- `FQG_BRIDGE_WATCHDOG_GRACE_SECONDS=90`
- `FQG_STREAM_RUNTIME_MAX_SECONDS=23400`

说明：
- 空闲超时（默认 30 分钟）会触发进程自退，由 systemd 自动拉起，避免“进程存活但入站僵死”。
- 最大运行时轮换（默认 6 小时）和 `RuntimeMaxSec`（6.5 小时）提供第二层兜底。

2. 主动健康检查
- `GET /healthz`
- `GET /v1/chat/routes`
- `GET /v1/chat/leader/snapshot`

3. 通道门禁
- `龟仙人` 作为唯一 ACTIVE 交互机器人。
- 其他通知机器人保持 PAUSED，不进入交互流。

## 操作基线

```bash
./scripts/local_stack_launchd.sh restart
curl -fsS http://8.140.215.219:3001/healthz
curl -sS http://8.140.215.219:3001/v1/chat/routes
curl -sS http://8.140.215.219:3001/v1/chat/leader/snapshot
```

## 证据要求

每轮实测至少落盘：

- `run_id`
- `summary.tsv`
- `report.json`
- 钉钉 START/RESULT 可见消息
