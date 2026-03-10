# Dual Server Baseline (2026-03-03)

## Scope
- Project: FeiQiao-Guard / 飞桥守护
- Purpose: collect usable server facts and define safe experimentation strategy
- Checked at: 2026-03-03 (local time)

## Server A (Integration host)
- Host: `8.140.215.219`
- Access: `root` login available with existing credential set
- Live status: reachable and verified by SSH health check

### Live check snapshot
- Hostname: `iZ2ze5294nx2qtw38si0eiZ`
- OS: `Ubuntu 24.04` kernel `6.8.0-64-generic`
- Runtime:
- `node`: `v20.19.5`
- `npm`: `10.8.2`
- `python3`: `3.12.3`
- n8n service:
- `systemctl is-active n8n` => `active`
- listening on `*:5678`
- n8n unit:
- active since `2026-03-01 06:24:16 CST`
- version reported in logs: `1.111.0`
- editor URL in logs: `http://8.140.215.219:5678`

### Resource snapshot
- Uptime: `143 days`
- Load average: `0.16 0.07 0.01`
- Memory: `1.6Gi total`, `~803Mi used`, no swap
- Disk `/`: `40G total`, `15G used`, `24G available`

### Risk notes
- This host is carrying active n8n workload and history.
- Treat as integration/staging host, not destructive sandbox.

## Server B (Experiment host)
- Host: `119.23.147.46`
- Expected role from docs: `claude-test-119` (test server)
- Doc baseline:
- OS: `CentOS 8`
- Runtime: `node v20.19.5`, `python 3.10.15`
- historical tests: `19/19 PASS`

### Current blocker
- Live SSH check with currently known password failed (`Permission denied`).
- SSH key auth from this machine also failed.
- Conclusion: operation is possible in principle, but current credential material in this workspace is insufficient for immediate login.

## Recommended role split
- `119.23.147.46`: destructive experiments, toolchain trials, Codex orchestration stress tests.
- `8.140.215.219`: integration verification only (Feishu callback wiring, n8n workflow handoff, end-to-end dry run).

## Immediate next actions
1. Obtain a valid credential path for `119.23.147.46` (password or private-key login).
2. Create isolated workspace roots on both servers:
- `/root/feiqiao-guard-sandbox` (119)
- `/root/feiqiao-guard-integration` (8.140)
3. Keep secrets out of git-tracked files; rotate any long-lived plaintext tokens discovered in local docs.
4. Run the same baseline script on both servers and store snapshots under `resource/ops/`.

## Baseline command pack
```bash
echo "[HOST]"; hostname
echo "[OS]"; uname -a
echo "[RUNTIME]"; node -v; npm -v; python3 --version || true; python --version || true
echo "[LOAD]"; uptime
echo "[MEM]"; free -h
echo "[DISK]"; df -h /
echo "[PORTS]"; ss -lntp | head -n 30
```

