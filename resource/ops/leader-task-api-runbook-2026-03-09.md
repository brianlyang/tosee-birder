# 飞桥守护 Leader 任务化接口操作手册（2026-03-09）

## 1. 目标

用任务化接口替代长阻塞调用，形成：

1. `POST /v1/chat/leader/tasks`：提交任务（立即返回 `task_id`）。
2. `GET /v1/chat/leader/tasks/{task_id}`：轮询状态与结果。

状态机：

- `accepted -> running -> final|failed|timeout`

## 2. 启动服务

```bash
cd /Users/yangxi/claude/codex_project/fqsh
python -m feiqiao_guard.main --host 127.0.0.1 --port 3001
```

## 3. 提交任务

```bash
curl -sS -X POST http://127.0.0.1:3001/v1/chat/leader/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "继续执行并在完成后回传证据路径",
    "auto_collab": true,
    "verify_seconds": 8
  }' | jq
```

返回示例（简化）：

```json
{
  "accepted": true,
  "task_id": "f6f9b8d5d2a74f48a08a9c0db7f0ad1b",
  "state": "accepted",
  "poll_url": "/v1/chat/leader/tasks/f6f9b8d5d2a74f48a08a9c0db7f0ad1b"
}
```

## 4. 轮询任务

```bash
TASK_ID="<上一步返回的task_id>"
curl -sS "http://127.0.0.1:3001/v1/chat/leader/tasks/${TASK_ID}" | jq
```

返回示例（终态）：

```json
{
  "task_id": "f6f9b8d5d2a74f48a08a9c0db7f0ad1b",
  "state": "final",
  "result": {
    "accepted": true,
    "leader_result": { "delivery_state": "confirmed" }
  }
}
```

## 5. 钉钉桥接说明

桥接客户端 `LeaderCommandClient` 已改为：

1. 优先走任务化接口；
2. 若目标服务暂未升级（404/旧版本），自动回退到旧接口 `/v1/chat/leader/command`；
3. 回退会在 `orchestration_notes` 写入 `task_api_fallback:*`，方便审计。

## 6. 排障

1. 若 `state=failed`：看 `error` 和 `result.orchestration_notes`。
2. 若长期 `running`：先看 `/v1/chat/leader/snapshot` 是否有会话推进。
3. 若查不到任务（404）：确认 `task_id` 与服务实例一致（同一进程内存态任务表）。
