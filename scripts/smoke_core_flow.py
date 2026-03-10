#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base.rstrip('/') + path,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip('/') + path, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> None:
    parser = argparse.ArgumentParser(description='FeiQiao-Guard core flow smoke test')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--approver', default='zhouqihang')
    args = parser.parse_args()

    created = post(
        args.base_url,
        '/v1/approvals',
        {
            'command': 'ls -la',
            'terminal_session_id': 'smoke-session-1',
            'source': 'smoke_script',
            'extra_context': {'suite': 'core'},
        },
    )
    request_id = created['request_id']

    callback = post(
        args.base_url,
        '/v1/callback/decision',
        {
            'request_id': request_id,
            'action': 'approve',
            'token': created['callback_token'],
            'nonce': f'smoke-nonce-{int(time.time())}',
            'approver': args.approver,
            'timestamp': int(time.time()),
            'signature': '',
            'source_ip': '127.0.0.1',
            'raw_payload': {'suite': 'core'},
        },
    )

    state = get(args.base_url, f'/v1/approvals/{request_id}')

    risk = post(
        args.base_url,
        '/v1/approvals',
        {
            'command': 'rm -rf /tmp/demo',
            'terminal_session_id': 'smoke-session-2',
            'source': 'smoke_script',
            'extra_context': {'suite': 'core'},
        },
    )

    assert callback['status'] == 'APPROVED', callback
    assert state['status'] == 'APPROVED', state
    assert risk['status'] == 'REJECTED', risk

    print('SMOKE_PASS')
    print('created_request_id=', request_id)
    print('approved_terminal_action=', state.get('terminal_action'))
    print('high_risk_reason=', risk.get('reason'))


if __name__ == '__main__':
    main()
