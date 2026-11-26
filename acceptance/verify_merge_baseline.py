#!/usr/bin/env python3 -I
"""商品档案连续归并 / 规格基线跟随 / 历史依据冻结验收脚本。

默认访问 http://127.0.0.1:8020，可用 BASE_URL 覆盖。仅使用标准库。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8020").rstrip("/")
CHECKS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise AssertionError(f"{method} {path} -> {exc.code}: {body}") from exc


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))


def main() -> int:
    call("POST", "/api/dev/reset")
    scenario = call("POST", "/api/dev/merge-baseline-scenario", {})
    check("场景创建成功", scenario.get("ok") is True, str(scenario))
    ids = scenario["goods"]
    match_id = scenario["match"]["id"]

    job = call("POST", f"/api/sku_match/{match_id}/recalculations", {"spec": "500g"})["job"]
    check("重算启动时冻结 A 的 500g 基线 32", float(job["locked_baseline_price"]) == 32.0)

    first = call("POST", f"/api/goods_registry/{ids['A']['id']}/merge", {"keep_id": ids["B"]["id"]})
    second = call("POST", f"/api/goods_registry/{ids['B']['id']}/merge", {"keep_id": ids["C"]["id"]})
    check("A→B 成功", first.get("ok") is True, first.get("message", ""))
    check("B→C 成功", second.get("ok") is True, second.get("message", ""))
    check("连续归并唯一最终档案为 C", second.get("final_code") == "C", str(second.get("final_code")))

    receipt = call("GET", "/api/goods_registry/receipt/A?spec=500g")
    check("旧编号 A 可取得查验回执", bool(receipt.get("receipt_no")), str(receipt))
    check("旧 A 的 500g 当前跟随到 C 的 34", receipt.get("final_code") == "C"
          and float(receipt.get("baseline_price")) == 34.0, str(receipt.get("baseline_price")))
    check("500g 核对不串用 C 的 250g", len(receipt.get("independent_specs", [])) == 1
          and float(receipt["independent_specs"][0]["price"]) == 16.0)

    receipt_250 = call("GET", "/api/goods_registry/by-code/A/receipt?spec=250g")
    check("250g 作为独立有效规格可回查 16", float(receipt_250.get("baseline_price")) == 16.0)

    sku_baseline = call("GET", f"/api/sku_match/{match_id}/baseline?spec=500g")
    check("规格匹配器读到实际基线 34", float(sku_baseline.get("baseline_price")) == 34.0)
    check("规格匹配器同时保留已确认价 32",
          float(sku_baseline.get("confirmed_baseline_price")) == 32.0)
    check("匹配依据保存最终档案 C", sku_baseline.get("final_goods") == "C")

    history = call("GET", f"/api/sku_match/{match_id}/confirmed-result")
    check("已确认历史结果仍给 A/32",
          history.get("goods_code") == "A" and float(history.get("baseline_price")) == 32.0,
          str(history))

    continued = call("POST", f"/api/sku_match/recalculations/{job['job_id']}/continue",
                     {"strategy": "original", "operator": "值班员"})
    basis = continued.get("basis", {})
    check("同一笔重算记录两个归并时点冲突", continued.get("job", {}).get("conflict") is True
          and len(continued.get("job", {}).get("merge_points", [])) == 2)
    check("继续原重算可采用 32 且依据完整",
          continued.get("ok") and float(basis.get("adopted_price")) == 32.0
          and basis.get("evidence_complete") is True and basis.get("baseline_before_merge")
          and basis.get("baseline_after_merge"))
    replayed = call("POST", f"/api/sku_match/{match_id}/recompute", {})
    check("保存的依据可复算上次采用的 32", float(replayed.get("baseline_price")) == 32.0)
    check("复算不覆盖已确认 32", float(replayed.get("confirmed_baseline_price")) == 32.0)

    latest_job = call("POST", f"/api/sku_match/{match_id}/recalculations", {"spec": "500g"})["job"]
    followed = call("POST", f"/api/sku_match/recalculations/{latest_job['job_id']}/continue",
                    {"strategy": "latest"})
    check("在途工作也可完整留痕地改采最终基线 34",
          float(followed.get("basis", {}).get("adopted_price")) == 34.0
          and followed.get("basis", {}).get("baseline_link") is not None)
    replayed_34 = call("POST", f"/api/sku_match/{match_id}/recompute", {})
    check("新保存的上次结果可复算 34", float(replayed_34.get("baseline_price")) == 34.0)
    check("新结果仍不覆盖历史确认价 32",
          float(call("GET", f"/api/sku_match/{match_id}/confirmed-result").get("baseline_price")) == 32.0)

    spec_check = call("GET", "/api/sku_match/spec-check/A?spec=500g")
    check("两个入口核对同一份 500g 基线",
          spec_check.get("final_code") == "C" and float(spec_check.get("baseline_price")) == 34.0)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n=== {len(CHECKS) - len(failed)} passed, {len(failed)} failed ===")
    if failed:
        print("FAILED:", "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
