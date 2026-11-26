"""规格匹配器领域服务：把站点SKU匹配到商品档案的归一规格，负责匹配置信度判定与人工复核分流。

数据链路位置：规格对齐层，消费规格归一器推送的候选规格与商品档案中心的主数据，
低置信度结果转人工复核队列，高置信度结果回填商品档案。
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from typing import Any

from app.goods_flow import (
    JOB_TABLE,
    find_recalc_job,
    mirror_row,
    next_id,
    normalize_spec,
    now_iso,
    resolve_attribution,
    resolve_baseline,
    snapshot_baseline,
)
from app.services.base import BaseModuleService
from app.store import store


class SkuMatchService(BaseModuleService):
    MODULE = "sku_match"
    ENTITY = "匹配记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["匹配批次", "商品档案", "候选规格", "匹配置信度", "匹配状态"]
    REQUIRED_FIELDS = ["匹配批次", "商品档案", "候选规格"]
    STATUS_ORDER = ["待匹配", "已匹配", "人工复核", "匹配失败"]
    ACTION_RULES = {"执行匹配": "已匹配", "转人工复核": "人工复核"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：规格匹配与置信度分流 ----

    def run_match(self, entry_id: int, confidence: float) -> dict[str, Any]:
        """执行规格匹配，按置信度分流到已匹配或人工复核。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        goods_name = entry.get("商品档案", "")
        goods = next(
            (g for g in store.rows("goods_registry") if g.get("商品标识") == goods_name), None
        )
        if goods is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"商品档案 {goods_name} 不存在"}
        if confidence >= 0.95:
            entry["status"] = "已匹配"
            entry["pending"] = False
        else:
            entry["status"] = "人工复核"
            entry["pending"] = True
        entry["匹配置信度"] = f"{confidence:.2f}"
        return {
            "ok": True,
            "message": "匹配执行完成",
            "result_status": entry.get("status"),
            "confidence": entry.get("匹配置信度"),
        }

    def escalate_review(self, entry_id: int) -> dict[str, Any]:
        """把低置信度匹配记录升级到人工复核队列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "人工复核":
            return {"ok": False, "message": "记录不存在或不处于人工复核状态"}
        rows = store.rows("match_review")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": rid,
            "记录编号": f"SM-{entry_id}-{rid}",
            "匹配记录": str(entry.get("id", "")),
            "复核结论": f"置信度 {entry.get('匹配置信度', '')}",
            "复核角色": "规格审核员",
            "复核时间": datetime.now(timezone.utc).isoformat(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "已升级人工复核", "review_id": rid}

    def apply_review_result(self, entry_id: int, approved: bool) -> dict[str, Any]:
        """应用复核结论：通过则回填档案，驳回则进失败隔离。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        if approved:
            entry["status"] = "已匹配"
            entry["pending"] = False
            return {"ok": True, "message": "复核通过，匹配已确认"}
        entry["status"] = "匹配失败"
        entry["abnormal"] = True
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"match-{entry_id}",
            "失败阶段": "匹配",
            "失败原因": "人工复核驳回",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "0",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "复核驳回，样本已隔离", "quarantine_id": fid}

    def consume_normalized(self, entry_id: int) -> dict[str, Any]:
        """消费规格归一器推送的候选规格完成对齐。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        spec = entry.get("候选规格", "")
        matched_rule = next(
            (r for r in store.rows("spec_normalizer")
             if r.get("status") == "已启用" and r.get("归一规格") == spec), None
        )
        if matched_rule is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"候选规格 {spec} 没有启用中的归一规则"}
        entry["匹配状态"] = "归一对齐"
        return {
            "ok": True,
            "message": "候选规格已对齐归一规则",
            "rule": matched_rule.get("规则名称", ""),
        }

    def _historical_basis(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """把多种直观命名的已确认快照归一为依据对象，兼容直接存价格的写法。"""
        historical = entry.get("historical_basis") or entry.get("历史基线依据")
        if isinstance(historical, dict):
            return historical
        confirmed = entry.get("confirmed_baseline")
        if isinstance(confirmed, dict):
            return {
                "original_goods_code": confirmed.get("original_goods_code") or entry.get("原商品档案"),
                "baseline": confirmed,
                "baseline_price": confirmed.get("price", confirmed.get("基线价格")),
                "immutable": True,
                "confirmed": True,
            }
        price: Any = entry.get("confirmed_baseline_price")
        if historical is not None and not isinstance(historical, (dict, list)):
            price = historical
        elif confirmed is not None and not isinstance(confirmed, (dict, list)):
            price = confirmed
        if price in (None, ""):
            return None
        try:
            price_value: Any = float(price)
        except (TypeError, ValueError):
            price_value = price
        return {
            "original_goods_code": entry.get("原商品档案") or entry.get("原始商品档案"),
            "goods_code": entry.get("原商品档案") or entry.get("商品档案"),
            "spec": entry.get("候选规格") or entry.get("归一规格"),
            "baseline_price": price_value,
            "price": price_value,
            "immutable": True,
            "confirmed": True,
        }

    def link_goods_baseline(
        self,
        entry_id: int,
        spec: Any = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """消费档案归属链与基线链接，确定实际可读基线并保存可复算依据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在或未匹配"}
        if entry.get("status") != "已匹配" and not kwargs.get("allow_unmatched"):
            return {"ok": False, "message": "记录不存在或未匹配"}
        payload = payload or {}
        spec = (
            spec
            or payload.get("spec")
            or kwargs.get("candidate_spec")
            or entry.get("归一规格")
            or entry.get("候选规格")
        )
        recorded_goods = str(entry.get("商品档案", "") or "")
        original_goods = str(
            entry.get("原商品档案")
            or entry.get("原始商品档案")
            or entry.get("original_goods_code")
            or recorded_goods
        )

        # 已确认历史结果不可被后续归并/新基线覆盖；若合并时未快照，这里按原编号补证。
        historical = self._historical_basis(entry)
        existing_confirmed_price = entry.get("confirmed_baseline_price")
        if historical is None and (
            entry.get("confirmed") is True
            or entry.get("结果已确认") is True
            or str(entry.get("status")) == "已匹配"
        ):
            old_resolution = resolve_baseline(original_goods or recorded_goods, spec)
            old_snap = snapshot_baseline(
                old_resolution.get("baseline"),
                source="historical_match_reconstructed",
                original_goods_code=original_goods,
            )
            old_price = old_snap.get("price") if old_snap else existing_confirmed_price
            historical = {
                "original_goods_code": original_goods,
                "goods_code": original_goods,
                "spec": spec,
                "normalized_spec": normalize_spec(spec),
                "baseline": old_snap,
                "baseline_id": old_snap.get("baseline_id") if old_snap else None,
                "baseline_price": old_price,
                "immutable": True,
                "confirmed": True,
            }
            entry["historical_basis"] = historical
            if old_snap is not None:
                entry["confirmed_baseline"] = old_snap
            entry["历史基线依据"] = historical
            entry["confirmed_baseline_price"] = old_price

        resolution = resolve_baseline(recorded_goods or original_goods, spec)
        baseline = resolution.get("baseline")
        attribution = resolution.get("attribution") or resolve_attribution(recorded_goods or original_goods)
        final_goods = resolution.get("current_code") or recorded_goods
        if baseline is None:
            entry["has_baseline"] = False
            entry["基线可用"] = False
            entry["has_available_result"] = False
            entry["无可用结果"] = True
            return {
                "ok": True,
                "message": "基线关联检查完成：未找到同商品可比规格的可读基线",
                "goods": recorded_goods,
                "queried_goods": recorded_goods,
                "original_goods": original_goods,
                "final_goods": final_goods,
                "spec": spec,
                "has_baseline": False,
                "has_available_result": False,
                "available": False,
                "historical_basis": historical,
                "confirmed_baseline_price": (historical or {}).get("baseline_price"),
                "attribution": attribution,
                "resolution": resolution,
            }

        basis = {
            "basis_id": f"MB-{entry_id}-{baseline.get('id')}-{next_id('match_basis'):04d}",
            "match_id": entry_id,
            "saved_at": now_iso(),
            "source": "sku_match.link_goods_baseline",
            "goods_code": final_goods,
            "effective_goods_code": final_goods,
            "merge_result_goods": final_goods,
            "归并结果所用档案": final_goods,
            "recorded_goods_code": recorded_goods,
            "original_goods_code": original_goods,
            "spec": spec,
            "resolved_spec": baseline.get("归一规格") or spec,
            "merge_result_spec": baseline.get("归一规格") or spec,
            "归并结果所用规格": baseline.get("归一规格") or spec,
            "normalized_spec": resolution.get("normalized_spec") or baseline.get("spec_key"),
            "baseline_id": baseline.get("baseline_id") or baseline.get("id"),
            "baseline_link_id": (resolution.get("selected_link") or {}).get("id")
                or (resolution.get("selected_link") or {}).get("link_id"),
            "baseline": baseline,
            "baseline_snapshot": snapshot_baseline(baseline, source="current_readable_baseline"),
            "baseline_price": baseline.get("price"),
            "price": baseline.get("price"),
            "baseline_link": resolution.get("selected_link"),
            "attribution": attribution,
            "merge_chain": attribution.get("chain", []),
            "merge_points": attribution.get("merge_points", []),
            "selection_reason": resolution.get("selection_reason"),
            "recomputable": True,
        }
        entry["has_baseline"] = True
        entry["基线可用"] = True
        entry["has_available_result"] = True
        entry["无可用结果"] = False
        entry["baseline_id"] = basis["baseline_id"]
        entry["baseline_price"] = basis["baseline_price"]
        entry["实际基线"] = baseline
        entry["基线链接"] = resolution.get("selected_link")
        entry["归并所用档案"] = final_goods
        entry["归并所用规格"] = basis["归并结果所用规格"]
        entry["resolved_goods_code"] = final_goods
        entry["resolved_spec"] = basis["resolved_spec"]
        entry["last_match_basis"] = basis
        entry["last_resolution"] = basis
        entry["last_matched_at"] = basis["saved_at"]
        entry["can_recompute_last_match"] = True

        return {
            "ok": True,
            "message": "基线关联检查完成，归并所用档案与规格依据已保存",
            "goods": recorded_goods,
            "queried_goods": recorded_goods,
            "original_goods": original_goods,
            "final_goods": final_goods,
            "current_goods_code": final_goods,
            "spec": spec,
            "has_baseline": True,
            "has_available_result": True,
            "available": True,
            "baseline": baseline,
            "baseline_price": baseline.get("price"),
            "actual_readable_baseline": baseline,
            "basis": basis,
            "last_match_basis": basis,
            "attribution": attribution,
            "confirmed_baseline_price": (historical or {}).get("baseline_price"),
            "historical_basis": historical,
            "independent_specs": resolution.get("independent_specs", []),
        }

    def recompute_last_match(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        """按上次保存的归并档案/规格/基线依据复算，不允许新结果覆盖已确认历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "匹配记录不存在"}
        basis = entry.get("last_match_basis") or entry.get("last_resolution")
        if not basis:
            linked = self.link_goods_baseline(entry_id, **kwargs)
            if not linked.get("ok") or not linked.get("has_baseline"):
                return linked
            basis = linked["basis"]

        baseline_id = basis.get("baseline_id")
        current_baseline = store.find("price_baseline", int(baseline_id)) if str(baseline_id).isdigit() else None
        stored_snapshot = basis.get("baseline_snapshot") or basis.get("baseline") or {}
        # 复算上次匹配首先复现当时保存的数值；原始行若仍在，附加当前可读性核对。
        replayed_price = basis.get("baseline_price", stored_snapshot.get("基线价格"))
        current_check = resolve_baseline(
            basis.get("goods_code") or entry.get("商品档案"),
            basis.get("resolved_spec") or basis.get("spec"),
        )
        recomputation = {
            "recomputed_at": now_iso(),
            "match_id": entry_id,
            "replayed": True,
            "used_goods_code": basis.get("goods_code"),
            "used_spec": basis.get("resolved_spec") or basis.get("spec"),
            "used_baseline_id": baseline_id,
            "baseline_price": replayed_price,
            "basis": basis,
            "stored_snapshot": stored_snapshot,
            "stored_row_still_exists": current_baseline is not None,
            "current_readable_baseline": current_check.get("baseline"),
            "current_readable_price": current_check.get("baseline_price"),
            "current_attribution": current_check.get("attribution"),
        }
        entry["last_recomputation"] = recomputation
        entry["last_recomputed_at"] = recomputation["recomputed_at"]
        history = entry.setdefault("recomputation_history", [])
        if isinstance(history, list):
            history.append({k: v for k, v in recomputation.items() if k != "basis"})
        historical = self._historical_basis(entry)
        return {
            "ok": True,
            "message": "已按上次保存的档案、规格和基线依据复算",
            **recomputation,
            "match_result": recomputation,
            "confirmed_baseline_price": (historical or {}).get("baseline_price"),
            "historical_basis": historical,
        }

    def recalculate_last_match(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.recompute_last_match(entry_id, **kwargs)

    def replay_last_match(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.recompute_last_match(entry_id, **kwargs)

    def start_recalculation(
        self,
        entry_id: Any = None,
        spec: Any = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """开启一笔可跨越连续归并的在途重算，并冻结启动时读到的基线依据。"""
        payload = payload or {}
        match_id = kwargs.get("match_id") or payload.get("match_id") or entry_id
        entry = None
        if match_id not in (None, "") and str(match_id).isdigit():
            entry = store.find(self.MODULE, int(match_id))
        if entry is None and isinstance(entry_id, int):
            entry = store.find(self.MODULE, entry_id)
        goods_code_value = (
            kwargs.get("goods_code") or payload.get("goods_code") or payload.get("code")
            or (entry or {}).get("原商品档案") or (entry or {}).get("商品档案")
        )
        if not goods_code_value:
            return {"ok": False, "message": "重算必须指定匹配记录或商品编号"}
        spec = spec or kwargs.get("spec") or payload.get("spec") or (entry or {}).get("候选规格") or (entry or {}).get("归一规格")
        attribution = resolve_attribution(goods_code_value)
        start_resolution = resolve_baseline(goods_code_value, spec)
        locked = start_resolution.get("baseline")
        job_id = next_id(JOB_TABLE)
        job_no = f"RC-{match_id or goods_code_value}-{job_id:04d}"
        job = {
            "id": job_id,
            "job_id": job_no,
            "ref": job_no,
            "type": "spec_baseline_recalculation",
            "match_id": entry.get("id") if entry else (int(match_id) if str(match_id).isdigit() else None),
            "entry_id": entry.get("id") if entry else None,
            "goods_code": attribution.get("queried_code") or str(goods_code_value),
            "original_goods_code": attribution.get("queried_code") or str(goods_code_value),
            "current_goods_code": attribution.get("current_code"),
            "spec": spec,
            "normalized_spec": normalize_spec(spec),
            "status": "在途",
            "started_at": now_iso(),
            "start_merge_version": attribution.get("merge_version", 0),
            "start_attribution": deepcopy(attribution),
            "locked_baseline": locked,
            "baseline_snapshot": snapshot_baseline(locked, source="recalculation_start"),
            "locked_baseline_price": locked.get("price") if locked else None,
            # 复制列表，避免后续追加归并时点时污染启动时的归属快照。
            "merge_points": list(attribution.get("merge_points", [])),
            "seen_merge_ids": [p.get("merge_id") for p in attribution.get("merge_points", [])],
            "initial_merge_version": attribution.get("merge_version", 0),
            "conflict": False,
            "operator": kwargs.get("operator") or payload.get("operator") or "值班员",
            "reason": kwargs.get("reason") or payload.get("reason") or "旧规格重算",
            "evidence_complete": False,
        }
        mirror_row(JOB_TABLE, job, ("recalc_jobs", "sku_recalculations", "baseline_recalc_jobs"))
        if entry is not None:
            entry["active_recalculation_id"] = job_no
        return {"ok": True, "message": "重算已开始，启动时基线依据已冻结", "job": job, "recalculation": job}

    def continue_recalculation(self, job_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        """值班员继续原重算；采用 32 或 34 均可，但必须留下完整依据。"""
        job = find_recalc_job(job_ref)
        if job is None and job_ref is not None and str(job_ref).isdigit():
            # 允许直接传 sku_match id：继续该记录最近一笔在途重算。
            candidates = [
                j for j in store.rows(JOB_TABLE)
                if str(j.get("match_id")) == str(job_ref) and str(j.get("status")) in {"在途", "重算中", "处理中"}
            ]
            job = candidates[-1] if candidates else None
            if job is None:
                started = self.start_recalculation(int(job_ref), **kwargs)
                job = started.get("job")
        if job is None:
            started = self.start_recalculation(**kwargs)
            if not started.get("ok"):
                return {"ok": False, "code": "job_not_found", "message": "找不到可继续的重算单"}
            job = started["job"]

        payload = kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else {}
        strategy = str(
            kwargs.get("strategy") or payload.get("strategy")
            or kwargs.get("decision") or payload.get("decision")
            or kwargs.get("resolution") or payload.get("resolution")
            or kwargs.get("choice") or payload.get("choice") or ""
        ).casefold()
        requested_price = None
        price_keys = (
            "baseline_price", "adopted_price", "chosen_price", "selected_price",
            "use_price", "price", "adopt",
        )
        for source in (kwargs, payload):
            for key in price_keys:
                value = source.get(key) if isinstance(source, dict) else None
                if value not in (None, ""):
                    requested_price = value
                    break
            if requested_price not in (None, ""):
                break
        requested_baseline_id = (
            kwargs.get("baseline_id") or payload.get("baseline_id")
            or kwargs.get("chosen_baseline_id") or payload.get("chosen_baseline_id")
            or kwargs.get("selected_baseline_id") or payload.get("selected_baseline_id")
        )

        original_code = job.get("original_goods_code") or job.get("goods_code")
        spec = job.get("spec")
        latest = resolve_baseline(original_code, spec)
        latest_baseline = latest.get("baseline")
        locked = job.get("locked_baseline") or {}

        choose_latest = strategy in {"latest", "final", "current", "c", "34", "new"}
        choose_original = strategy in {"original", "locked", "start", "a", "32", "old", "continue", ""}
        if requested_price not in (None, ""):
            try:
                price_number = float(requested_price)
            except (TypeError, ValueError):
                price_number = None
            if price_number is not None and locked.get("price") is not None and abs(price_number - float(locked["price"])) < 0.001:
                choose_original, choose_latest = True, False
            elif price_number is not None and latest_baseline and abs(price_number - float(latest_baseline.get("price"))) < 0.001:
                choose_latest, choose_original = True, False
        if requested_baseline_id and latest_baseline and str(requested_baseline_id) == str(latest_baseline.get("id")):
            choose_latest, choose_original = True, False

        if choose_latest and latest_baseline is None:
            return {"ok": False, "conflict": True, "message": "无法采用最终档案基线：同规格可读基线不存在", "resolution": latest}
        adopted = latest_baseline if choose_latest and not choose_original else locked
        if choose_original and not locked:
            # 启动时未冻结到基线时，允许现在按原编号补取，但明确标记依据来源。
            old = resolve_baseline(original_code, spec)
            adopted = old.get("baseline")
        if adopted is None:
            return {"ok": False, "conflict": True, "message": "没有可采用的规格基线依据"}

        strategy_name = "latest_final_baseline" if adopted is latest_baseline and choose_latest else "locked_original_baseline"
        complete_basis = {
            "completed_at": now_iso(),
            "strategy": strategy_name,
            "operator": kwargs.get("operator") or payload.get("operator") or job.get("operator"),
            "adopted_goods_code": adopted.get("商品标识") or adopted.get("goods_code"),
            "adopted_spec": adopted.get("归一规格") or adopted.get("spec") or spec,
            "adopted_baseline_id": adopted.get("id") or adopted.get("baseline_id"),
            "adopted_price": adopted.get("price", adopted.get("基线价格")),
            "adopted_baseline": adopted,
            "baseline_before_merge": locked,
            "baseline_after_merge": latest_baseline,
            "start_snapshot": job.get("baseline_snapshot"),
            "merge_points": job.get("merge_points", []),
            "attribution_before": job.get("start_attribution"),
            "attribution_after": latest.get("attribution"),
            "baseline_link": latest.get("selected_link"),
            "conflict": job.get("conflict", False),
            "evidence_complete": True,
            "依据完整": True,
        }
        # 固化采用时点的档案/基线快照，后续 C→D 等归并不再改写这份依据。
        complete_basis = deepcopy(complete_basis)
        job["current_attribution"] = complete_basis.get("attribution_after")
        job["status"] = "已完成"
        job["completed_at"] = complete_basis["completed_at"]
        job["adopted_baseline_price"] = complete_basis["adopted_price"]
        job["adopted_strategy"] = strategy_name
        job["adopted_basis"] = complete_basis
        job["evidence_complete"] = True
        job["outcome"] = f"在途重算采用 {complete_basis['adopted_goods_code']} 的 {complete_basis['adopted_spec']} 基线 {complete_basis['adopted_price']}"

        match_id = job.get("match_id")
        if match_id:
            entry = store.find(self.MODULE, int(match_id))
            if entry is not None:
                # 在途结果单独保存，绝不覆盖 confirmed_baseline / historical_basis。
                entry["inflight_result"] = complete_basis
                entry["draft_match_basis"] = complete_basis
                entry["last_match_basis"] = {
                    **complete_basis,
                    "goods_code": complete_basis["adopted_goods_code"],
                    "resolved_spec": complete_basis["adopted_spec"],
                    "baseline_id": complete_basis["adopted_baseline_id"],
                    "baseline_price": complete_basis["adopted_price"],
                    "recomputable": True,
                }
                entry["last_recalculation_job"] = job.get("job_id")
                entry["can_recompute_last_match"] = True
        return {"ok": True, "message": "原重算已继续完成，采用依据已完整留痕", "job": job, "recalculation": job, "basis": complete_basis}

    def complete_recalculation(self, job_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.continue_recalculation(job_ref, **kwargs)

    def resume_recalculation(self, job_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.continue_recalculation(job_ref, **kwargs)

    def continue_original_recalculation(self, job_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("strategy", "original")
        return self.continue_recalculation(job_ref, **kwargs)

    def begin_spec_recalculation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.start_recalculation(*args, **kwargs)

    def start_spec_recalc(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.start_recalculation(*args, **kwargs)

    def resolve_merge_conflict(self, job_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.continue_recalculation(job_ref, **kwargs)

    def get_confirmed_result(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "匹配记录不存在"}
        historical = self._historical_basis(entry)
        return {
            "ok": bool(historical),
            "message": "已确认历史结果按当时原商品和基线依据返回" if historical else "该记录没有已确认历史依据",
            "match_id": entry_id,
            "goods_code": (historical or {}).get("original_goods_code") or entry.get("原商品档案"),
            "baseline_price": (historical or {}).get("baseline_price") or entry.get("confirmed_baseline_price"),
            "baseline": (historical or {}).get("baseline"),
            "historical_basis": historical,
            "entry": entry,
        }

    def get_match_result(self, entry_id: int) -> dict[str, Any]:
        return self.get_confirmed_result(entry_id)

    def result_detail(self, entry_id: int) -> dict[str, Any]:
        return self.get_confirmed_result(entry_id)

    def get_history(self, entry_id: int) -> dict[str, Any]:
        return self.get_confirmed_result(entry_id)

    def spec_check(self, code_or_id: Any, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        """两个入口共用的规格/基线核对视图。"""
        result = resolve_baseline(code_or_id, spec)
        attribution = result.get("attribution") or resolve_attribution(code_or_id)
        code = attribution.get("queried_code") or str(code_or_id)
        return {
            "ok": result.get("has_baseline", False) or result.get("has_effective", False),
            "message": "规格基线核对完成",
            "queried_code": code,
            "goods_code": attribution.get("current_code"),
            "current_code": attribution.get("current_code"),
            "final_code": attribution.get("final_code"),
            "商品档案": attribution.get("current_code"),
            "former_codes": attribution.get("former_codes", []),
            "chain": attribution.get("chain", []),
            "spec": spec or "",
            "baseline": result.get("baseline"),
            "baseline_price": result.get("baseline_price"),
            "has_baseline": result.get("has_baseline", False),
            "has_available_result": result.get("has_available_result", False),
            "baselines": result.get("baselines", []),
            "independent_specs": result.get("independent_specs", []),
            "attribution": attribution,
            "resolution": result,
        }

    def check_entry_baseline(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        result = self.link_goods_baseline(entry_id, allow_unmatched=True)
        result["entry"] = entry
        result["entry_id"] = entry_id
        return result

    def list_entries(self, **kwargs: Any):
        items, total = super().list_entries(**kwargs)
        enriched: list[dict[str, Any]] = []
        for row in items:
            view = dict(row)
            try:
                check = self.spec_check(row.get("商品档案", ""), row.get("归一规格") or row.get("候选规格"))
                view["baseline_price"] = check.get("baseline_price")
                view["has_baseline"] = check.get("has_baseline")
                view["has_available_result"] = check.get("has_available_result")
                view["current_goods_code"] = check.get("current_code")
                view["final_goods_code"] = check.get("final_code")
                view["former_goods_codes"] = check.get("former_codes")
                historical = row.get("historical_basis") or row.get("confirmed_baseline")
                if isinstance(historical, dict):
                    view["confirmed_baseline_price"] = historical.get("baseline_price")
            except Exception:
                # 列表增强是只读便利字段，绝不能让主列表因依据解析异常而不可用。
                pass
            enriched.append(view)
        return enriched, total

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        payload = payload or {}
        if action in {"核对基线", "基线核对", "检查基线", "关联基线", "link_baseline"}:
            result = self.link_goods_baseline(entry_id, payload=payload)
            entry = store.find(self.MODULE, entry_id)
            if entry is not None:
                entry["last_domain_result"] = result
                return entry, result["message"]
            return None, result["message"]
        if action in {"复算上次匹配", "重新复算", "recompute"}:
            result = self.recompute_last_match(entry_id)
            entry = store.find(self.MODULE, entry_id)
            if entry is not None:
                entry["last_domain_result"] = result
                return entry, result["message"]
            return None, result["message"]
        return super().run_action(entry_id, action, payload)

    def batch_summary(self, batch: str) -> dict[str, Any]:
        """汇总匹配批次内各状态数量并上报指标。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("匹配批次") == batch]
        if not rows:
            return {"ok": False, "message": f"批次 {batch} 不存在"}
        matched = sum(1 for r in rows if r.get("status") == "已匹配")
        review = sum(1 for r in rows if r.get("status") == "人工复核")
        return {
            "ok": True,
            "message": "批次汇总完成",
            "total": len(rows),
            "matched": matched,
            "need_review": review,
            "failed": len(rows) - matched - review,
        }
