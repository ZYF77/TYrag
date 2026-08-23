#!/usr/bin/env python3
"""Print or run the frozen reasoning-mode eval matrix.

Without ENTERPRISE_EVAL_BASE_URL this is a dry run: it only loads the case
file and prints the mode × question matrix. Live scoring still needs a
human-labelled answer key after the Gateway call.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CASES = Path(__file__).resolve().parents[1] / "eval" / "reasoning_mode_cases.json"


def load_cases() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def dry_run(payload: dict) -> None:
    print(f"prompt={payload['prompt']} cases={len(payload['cases'])}")
    print("mode\tcase_id\tquestion")
    for mode in payload["modes"]:
        for case in payload["cases"]:
            print(f"{mode}\t{case['id']}\t{case['question']}")
    print("metrics: " + ", ".join(payload["metrics"]))
    print("live scores: see docs/eval/reasoning-mode-eval.md")


def post_message(base_url: str, token: str, conversation_id: str, question: str, mode: str) -> dict:
    body = json.dumps(
        {
            "clientMessageId": f"eval-{mode}-{int(time.time() * 1000)}",
            "question": question,
            "reasoningMode": mode,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/conversations/{conversation_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))
    payload["_total_ms"] = int((time.perf_counter() - started) * 1000)
    return payload


def live_run(payload: dict, base_url: str, token: str, conversation_id: str) -> None:
    print("mode\tcase_id\tstatus\ttotal_ms\tanswer_len")
    for mode in payload["modes"]:
        for case in payload["cases"]:
            result = post_message(base_url, token, conversation_id, case["question"], mode)
            answer = str(result.get("answer") or "")
            print(
                f"{mode}\t{case['id']}\t{result.get('status')}\t{result.get('_total_ms')}\t{len(answer)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    payload = load_cases()
    if not args.live:
        dry_run(payload)
        return 0
    base_url = os.environ.get("ENTERPRISE_EVAL_BASE_URL", "").strip()
    token = os.environ.get("ENTERPRISE_EVAL_TOKEN", "").strip()
    conversation_id = os.environ.get("ENTERPRISE_EVAL_CONVERSATION_ID", "").strip()
    if not base_url or not token or not conversation_id:
        raise SystemExit(
            "Set ENTERPRISE_EVAL_BASE_URL, ENTERPRISE_EVAL_TOKEN, ENTERPRISE_EVAL_CONVERSATION_ID"
        )
    live_run(payload, base_url, token, conversation_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
