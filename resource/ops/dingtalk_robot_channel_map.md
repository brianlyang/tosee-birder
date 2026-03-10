# DingTalk Robot Channel Map (FQG)

Updated: 2026-03-08

## 1) n8n 自动化通知机器人（单向）

- Status: `PAUSED`（暂停使用，不删除）
- Purpose: 通知播报（outbound only）
- Transport: `webhook`
- Example webhook token: `0e266a46f8df1494...`
- Can receive user command: `NO`
- Can route to `run_dingtalk_stream_bridge.py`: `NO`

## 2) 龟仙人（可接收/可回复）

- Status: `ACTIVE`（唯一允许的交互机器人）
- Purpose: 交互式指令桥接（inbound + outbound）
- Transport: `DingTalk Stream`
- Config keys:
  - `FQG_DINGTALK_STREAM_CLIENT_ID`
  - `FQG_DINGTALK_STREAM_CLIENT_SECRET`
  - `FQG_ENABLE_DINGTALK_STREAM_BRIDGE=1`
- Can receive user command: `YES`
- Can route to `run_dingtalk_stream_bridge.py`: `YES`

## Hard Rule

- 回归 `Stream` 链路时，禁止使用 webhook 机器人发测试命令。
- 仅使用“用户在龟仙人会话中 @机器人 发消息”的方式触发 end-to-end 测试。
- 运行时硬拦截：`FQG_DINGTALK_PAUSED_WEBHOOK_TOKENS` 包含 `0e266...`，命中即拒绝发送。
