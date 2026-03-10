# DingTalk Stream 稳步落地方案（龟仙人链路）

更新时间：2026-03-08  
Owner：`feiqiao-guard-delivery-lead`

## 1. 目标

把链路稳定到可持续交互状态：

`用户(钉钉@龟仙人) -> DingTalk Stream -> run_dingtalk_stream_bridge.py -> /v1/chat/leader/command -> 本地 tmux/codex -> 过程回传 -> 最终回传`

## 2. 官方约束（联网核对）

1. 群聊消息接收约束：机器人在群中只接收 **@机器人** 的消息。  
来源：DingTalk DeveloperPedia Stream 教程  
https://opensource.dingtalk.com/developerpedia/docs/explore/tutorials/stream/overview/

2. Stream 机器人消息 topic：应监听 `/v1.0/im/bot/messages/get`。  
来源：DingTalk Stream 协议文档  
https://open-dingtalk.github.io/developerpedia/docs/learn/stream/protocol/

3. 长连接机制：连接建立后需 ACK 回调；断线要重连。  
来源：同上协议文档 + Stream SDK Python README  
https://github.com/open-dingtalk/dingtalk-stream-sdk-python

## 3. 现状根因

1. 启动判定过松：只看 `bridge_started`，没有要求 endpoint 建立成功。  
2. 守卫判定粗糙：一次瞬时探测失败就重启，导致链路抖动。  
3. 可观测不足：没有 heartbeat，无法区分“桥接活着但无入站”与“进程假活着”。

## 4. 已落地修复

### 4.1 启动成功判定 fail-close

- 文件：`scripts/local_dingtalk_tmux_stack.sh`
- 变更：
  - `wait_bridge_ready()` 现在必须同时看到：
    - `bridge_started`
    - `endpoint is { ... }`
  - 默认 `FQG_BRIDGE_REQUIRE_AT=1`（与官方群聊约束对齐）

### 4.2 Bridge 心跳观测

- 文件：`scripts/run_dingtalk_stream_bridge.py`
- 变更：
  - 新增 heartbeat 参数：
    - `--heartbeat-file` / `FQG_BRIDGE_HEARTBEAT_FILE`
    - `--heartbeat-write-interval-seconds` / `FQG_BRIDGE_HEARTBEAT_WRITE_INTERVAL_SECONDS`
  - 心跳内容包括：
    - callback/inbound/reply/reject 计数
    - callback/inbound/reply age
    - last_reject_reason
  - watchdog 与关键事件（入站/拒绝/回复）都会刷新 heartbeat。

### 4.3 Supervisor 防抖与原因码

- 文件：`scripts/local_stack_supervisor.sh`
- 变更：
  - 新增连续失败阈值（默认 3 次）再触发重启；
  - 健康探测项拆分并输出原因码：
    - `api_session_missing`
    - `bridge_session_missing`
    - `healthz_unreachable`
    - `bridge_heartbeat_missing_or_stale`
  - 新增心跳新鲜度检测（默认 35 秒）。

## 5. 稳步执行流程（每轮）

1. 启动运行面（launchd 托管，不用临时后台）  
2. 核验 API 健康：`/healthz`、`/v1/chat/routes`、`/v1/chat/leader/snapshot`  
3. 核验 bridge 启动门禁：日志必须有 endpoint 建立  
4. 核验 heartbeat：文件存在且新鲜  
5. 用户在钉钉群 `@龟仙人` 发自然语言指令  
6. 验证日志与聊天可见证据：
  - `inbound msg_id=...`
  - `reply_sent tag=accepted`
  - `reply_sent tag=dispatch_summary`
  - `reply_sent tag=dispatch_final_result`
7. 若失败，按原因码回到对应层修复，不跨层拍脑袋重启。

## 6. 验收口径（硬）

只有同时满足以下条件才算 PASS：

1. 钉钉真实入站（不是本地模拟）；
2. 过程消息 + 最终消息都在钉钉可见；
3. bridge log 出现对应 `inbound` 与 `dispatch_final_result`；
4. 失败场景有可读原因码（不是笼统“超时/失败”）。
