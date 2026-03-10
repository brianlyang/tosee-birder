#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


DEFAULT_TEST_FILES = [
    "tests/test_guarded_session_control.py",
    "tests/test_watchdog_auto_continue.py",
    "tests/test_approval_flow.py",
    "tests/test_chat_bridge.py",
    "tests/test_dingtalk_client.py",
    "tests/test_chat_inbound.py",
]


def _load_module(path: Path):
    module_name = f"fqg_perfunc_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclass internals may need module registration before execution.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_kwargs(argnames: tuple[str, ...]) -> tuple[dict[str, Any], list[tempfile.TemporaryDirectory[str]], str]:
    holders: list[tempfile.TemporaryDirectory[str]] = []
    kwargs: dict[str, Any] = {}
    for name in argnames:
        if name == "tmp_path":
            holder = tempfile.TemporaryDirectory(dir="/tmp", prefix="fqgpf-")
            holders.append(holder)
            kwargs[name] = Path(holder.name)
            continue
        return {}, holders, f"unsupported arg: {name}"
    return kwargs, holders, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run test_* functions directly without pytest discovery/plugins.")
    parser.add_argument(
        "--workspace-root",
        default="",
        help="Workspace root path (default: auto-detect from this script).",
    )
    parser.add_argument(
        "--test-file",
        action="append",
        default=[],
        help="Relative test file path; can be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    args = parser.parse_args()

    root_dir = (
        Path(args.workspace_root).expanduser().resolve()
        if args.workspace_root
        else Path(__file__).resolve().parents[1]
    )
    src_dir = (root_dir / "src").resolve()
    for path in (root_dir, src_dir):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)

    test_files = args.test_file or DEFAULT_TEST_FILES

    results: list[dict[str, Any]] = []
    total = 0
    passed = 0
    failed = 0
    skipped = 0

    for rel in test_files:
        path = (root_dir / rel).resolve()
        if not path.exists():
            results.append(
                {
                    "test": rel,
                    "status": "SKIP",
                    "reason": f"file_not_found:{path}",
                }
            )
            skipped += 1
            continue

        module = _load_module(path)
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            obj = getattr(module, name)
            if not callable(obj):
                continue
            test_id = f"{path.name}::{name}"
            total += 1

            argcount = int(getattr(obj, "__code__", None).co_argcount or 0)
            argnames = tuple(getattr(obj, "__code__", None).co_varnames[:argcount] or ())
            kwargs, holders, unsupported_reason = _build_kwargs(argnames)
            if unsupported_reason:
                skipped += 1
                results.append(
                    {
                        "test": test_id,
                        "status": "SKIP",
                        "reason": unsupported_reason,
                    }
                )
                for holder in holders:
                    holder.cleanup()
                continue

            try:
                obj(**kwargs)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                results.append(
                    {
                        "test": test_id,
                        "status": "FAIL",
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
            else:
                passed += 1
                results.append({"test": test_id, "status": "PASS"})
            finally:
                for holder in holders:
                    holder.cleanup()

    summary = {
        "total": total,
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for item in results:
            line = f"{item['test']} -> {item['status']}"
            if item["status"] == "SKIP" and item.get("reason"):
                line += f" ({item['reason']})"
            if item["status"] == "FAIL" and item.get("error"):
                line += f" ({item['error']})"
            print(line)
        print(f"SUMMARY total={total} pass={passed} fail={failed} skip={skipped}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
