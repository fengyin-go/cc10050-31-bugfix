"""商品登记中心领域服务：聚合各平台商品为统一档案，负责重复档案的合并与疑似重复标记。

数据链路位置：主数据层，消费规格匹配器的匹配结果与各站点商品数据，
产出的统一商品档案供比价计算与价格基线引用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from typing import Any

from app.goods_flow import (
    BASELINE_LINK_TABLE,
    LINEAGE_TABLE,
    RECEIPT_TABLE,
    create_merge_baseline_links,
    find_goods,
    goods_code,
    mirror_row,
    next_id,
    now_iso,
    record_merge_in_recalc_jobs,
    resolve_attribution,
    resolve_baseline,
    snapshot_baseline,
    table_rows,
)
from app.services.base import BaseModuleService
from app.store import store


class GoodsRegistryService(BaseModuleService):
    MODULE = "goods_registry"
    ENTITY = "商品档案"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["商品标识", "商品名称", "品牌", "类目", "登记来源"]
    REQUIRED_FIELDS = ["商品标识", "商品名称", "品牌"]
    STATUS_ORDER = ["待合并", "已登记", "已合并", "疑似重复"]
    ACTION_RULES = {"登记商品": "已登记", "合并档案": "已合并", "标记重复": "疑似重复"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：档案聚合与重复治理 ----

    def register_goods(self, entry_id: int) -> dict[str, Any]:
        """登记商品档案，校验品牌与名称后进入已登记状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        name = entry.get("商品名称", "")
        brand = entry.get("品牌", "")
        if not name or not brand:
            return {"ok": False, "message": "商品名称与品牌必须齐备"}
        duplicates = [
            g.get("商品标识", "") for g in store.rows(self.MODULE)
            if g.get("id") != entry.get("id")
            and g.get("商品名称") == name and g.get("品牌") == brand
        ]
        if duplicates:
            entry["status"] = "疑似重复"
            entry["abnormal"] = True
            return {"ok": False, "message": "检测到重复档案", "duplicates": duplicates}
        entry["status"] = "已登记"
        entry["pending"] = False
        return {"ok": True, "message": f"商品 {name} 已登记"}

    def merge_duplicates(self, entry_id: Any, keep_id: Any = None, **options: Any) -> dict[str, Any]:
        """把疑似重复档案合并到目标档案，并建立可连续追踪的归并/基线链接。"""
        entry = find_goods(entry_id)
        if entry is None:
            return {"ok": False, "code": "source_not_found", "message": "档案不存在"}
        if keep_id is None:
            keep_id = options.get("target_id") or options.get("target_code") or options.get("keep_code")
        target = find_goods(keep_id)
        if target is None:
            return {"ok": False, "code": "target_not_found",
                    "message": f"目标档案 {keep_id} 不存在"}

        source_code = goods_code(entry)
        target_code = goods_code(target)
        source_id = entry.get("id")
        target_id = target.get("id")
        if source_id == target_id or source_code == target_code:
            return {"ok": False, "code": "self_merge", "message": "不能合并到自身"}

        before = resolve_attribution(source_code)
        current_source = before.get("current_code")
        target_attribution = resolve_attribution(target_code)
        final_target_code = target_attribution.get("current_code") or target_code
        if final_target_code == source_code or target_attribution.get("current_code") in before.get("former_codes", []):
            return {"ok": False, "code": "merge_cycle", "message": "合并会形成回到自身的循环，已拒绝"}

        existing_target = goods_code(entry.get("并入档案") or entry.get("merged_into_code")) or current_source
        if current_source != source_code:
            if current_source in {target_code, final_target_code} or existing_target in {target_code, final_target_code}:
                receipt = self.verify_goods(source_code)
                return {
                    "ok": True, "idempotent": True, "duplicate": True,
                    "message": "档案已归并到同一保留档案，跳过重复合并",
                    "kept": final_target_code, "target_code": target_code,
                    "final_code": receipt.get("final_code"), "moved_matches": 0,
                    "moved_specs": 0, "moved_baselines": 0,
                    "receipt": receipt,
                }
            return {
                "ok": False, "code": "multiple_final_profiles", "conflict": True,
                "message": f"{source_code} 已归并到 {current_source}，不能再解析成另一个最终档案 {target_code}",
                "current_code": current_source, "requested_target": target_code,
            }

        source_family = set(before.get("codes") or [source_code])
        sequence = next_id(LINEAGE_TABLE)
        merge_id = str(options.get("merge_id") or options.get("event_id") or f"MG-{sequence:04d}")
        merged_at = options.get("merged_at") or now_iso()
        merge_row = {
            "id": sequence,
            "merge_id": merge_id,
            "event_id": merge_id,
            "source_id": source_id,
            "source_code": source_code,
            "old_code": source_code,
            "from_code": source_code,
            "target_id": target_id,
            "target_code": target_code,
            "kept_id": target_id,
            "kept_code": target_code,
            "to_code": target_code,
            "final_code": final_target_code,
            "operator": options.get("operator") or options.get("值班员") or "值班员",
            "reason": options.get("reason", "重复档案合并"),
            "merged_at": merged_at,
            "created_at": merged_at,
            "timestamp": merged_at,
            "sequence": sequence,
            "status": "已确认",
        }
        mirror_row(LINEAGE_TABLE, merge_row, ("goods_merge_links", "merge_links", "goods_merges"))

        moved_specs = self._attribute_matches_on_merge(source_code, target_code, final_target_code, merge_row)

        entry["status"] = "已合并"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["并入档案"] = target_code
        entry["合并至"] = target_code
        entry["merged_into"] = target_code
        entry["merged_into_id"] = target_id
        entry["merged_into_code"] = target_code
        entry["final_goods_code"] = final_target_code
        entry["merged_at"] = merged_at
        entry["merge_id"] = merge_id

        baseline_links = create_merge_baseline_links(source_family, target_code, merge_row)
        record_merge_in_recalc_jobs(merge_row)
        receipt = self.verify_goods(source_code, spec=options.get("spec"))
        return {
            "ok": True,
            "message": "档案已合并，旧编号查验回执与规格基线链接已生成",
            "merge_id": merge_id,
            "source": source_code,
            "source_code": source_code,
            "kept": target_code,
            "kept_code": target_code,
            "target": target_code,
            "target_code": target_code,
            "final_code": receipt.get("final_code") or final_target_code,
            "final_goods_code": receipt.get("final_code") or final_target_code,
            "moved_matches": moved_specs,
            "moved_specs": moved_specs,
            "moved_baselines": len(baseline_links),
            "baseline_links_created": len(baseline_links),
            "baseline_links": baseline_links,
            "merge": merge_row,
            "receipt": receipt,
        }

    def apply_merge(self, source_code: Any, keep_code: Any, **options: Any) -> dict[str, Any]:
        """语义别名：按商品编号执行归并，兼容 lineage 风格调用。"""
        return self.merge_duplicates(source_code, keep_code, **options)

    def _match_is_historical(self, match: dict[str, Any]) -> bool:
        if match.get("confirmed") is True or match.get("结果已确认") is True:
            return True
        if str(match.get("确认状态", "")) == "已确认":
            return True
        if match.get("confirmed_baseline") or match.get("historical_basis") or match.get("历史基线依据"):
            return True
        return match.get("status") in {"已匹配", "已确认"}

    def _attribute_matches_on_merge(
        self,
        source_code: str,
        target_code: str,
        final_code: str,
        merge_row: dict[str, Any],
    ) -> int:
        moved = 0
        for match in store.rows("sku_match"):
            if match.get("商品档案") != source_code:
                continue
            original_code = str(
                match.get("原商品档案")
                or match.get("原始商品档案")
                or match.get("original_goods_code")
                or source_code
            )
            spec = match.get("归一规格") or match.get("候选规格")

            if self._match_is_historical(match) and not (
                match.get("confirmed_baseline") or match.get("historical_basis")
            ):
                basis_resolution = resolve_baseline(original_code or source_code, spec)
                original_baseline = basis_resolution.get("baseline")
                snap = snapshot_baseline(
                    original_baseline,
                    result_status=match.get("status"),
                    confirmed_at=match.get("confirmed_at") or match.get("matched_at") or merge_row["merged_at"],
                    source="confirmed_match_at_merge",
                    original_goods_code=original_code,
                )
                match["confirmed_baseline"] = snap
                match["historical_basis"] = {
                    "original_goods_code": original_code,
                    "goods_code": original_code,
                    "spec": spec,
                    "normalized_spec": basis_resolution.get("normalized_spec"),
                    "baseline": snap,
                    "baseline_id": snap.get("baseline_id") if snap else None,
                    "baseline_price": snap.get("price") if snap else None,
                    "immutable": True,
                    "confirmed": True,
                }
                match["历史基线依据"] = match["historical_basis"]
                match["confirmed_baseline_price"] = snap.get("price") if snap else None

            if not match.get("原商品档案"):
                match["原商品档案"] = source_code
            if not match.get("原始商品档案"):
                match["原始商品档案"] = original_code
            match["original_goods_code"] = original_code
            trail = match.setdefault("归属轨迹", [])
            if isinstance(trail, list):
                hop = {
                    "from": source_code,
                    "to": target_code,
                    "final_code": final_code,
                    "merge_id": merge_row.get("merge_id"),
                    "at": merge_row.get("merged_at"),
                }
                if hop not in trail:
                    trail.append(hop)
            match["商品档案"] = target_code
            match["当前商品档案"] = target_code
            match["生效商品档案"] = final_code
            match["effective_goods_code"] = final_code
            match["归并结果档案"] = final_code
            moved += 1
        return moved

    def verify_goods(self, code_or_id: Any = None, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        """按 id / 旧编号出具档案查验回执，并给登记页面打开真正生效的规格依据。"""
        if code_or_id is None:
            code_or_id = (
                kwargs.get("entry_id") or kwargs.get("code") or kwargs.get("goods_code")
                or kwargs.get("old_code") or kwargs.get("商品标识") or kwargs.get("编号")
            )
        goods = find_goods(code_or_id)
        queried_code = goods_code(goods) if goods else str(code_or_id or "")
        if goods is None and not queried_code:
            return {"ok": False, "code": "goods_not_found", "message": "档案不存在，无法出具查验回执"}

        attribution = resolve_attribution(queried_code or code_or_id)
        resolution = resolve_baseline(attribution.get("current_code") or queried_code, spec)
        effective = resolution.get("baselines", [])
        spec_groups: dict[str, dict[str, Any]] = {}
        for baseline in effective:
            key = str(baseline.get("normalized_spec") or baseline.get("归一规格") or baseline.get("id"))
            group = spec_groups.setdefault(key, {
                "spec": baseline.get("归一规格"),
                "normalized_spec": baseline.get("spec_key"),
                "baselines": [],
                "comparable": True,
            })
            group["baselines"].append(baseline)
            group["baseline"] = group["baselines"][0]
            group["price"] = group["baselines"][0].get("price")

        selected = resolution.get("baseline")
        receipt_id = next_id(RECEIPT_TABLE)
        receipt_no = f"GRR-{goods.get('id') if goods else 'X'}-{receipt_id:04d}"
        receipt = {
            "ok": True,
            "message": "档案查验回执已出具",
            "receipt_id": receipt_id,
            "id": receipt_id,
            "receipt_no": receipt_no,
            "回执编号": receipt_no,
            "receipt_type": "商品档案查验回执",
            "回执类型": "商品档案查验回执",
            "queried_id": goods.get("id") if goods else None,
            "queried_code": queried_code,
            "old_code": queried_code,
            "旧编号": queried_code,
            "original_goods": goods,
            "原档案": goods,
            "resolved": True,
            "is_merged": attribution.get("merge_count", 0) > 0,
            "final_goods_id": attribution.get("final_goods_id"),
            "final_goods_code": attribution.get("final_code"),
            "final_code": attribution.get("final_code"),
            "current_code": attribution.get("current_code"),
            "effective_goods_code": attribution.get("final_code"),
            "chain": attribution.get("chain", []),
            "merge_links": attribution.get("chain", []),
            "merge_points": attribution.get("merge_points", []),
            "former_codes": attribution.get("former_codes", []),
            "attribution": attribution,
            "spec": spec or "",
            "normalized_spec": resolution.get("normalized_spec"),
            "baseline": selected,
            "effective_baseline": selected,
            "effective_basis": selected,
            "规格依据": selected,
            "baseline_price": resolution.get("baseline_price"),
            "effective_baseline_price": resolution.get("baseline_price"),
            "baseline_count": len(resolution.get("candidates", [])) if spec else len(effective),
            "effective_baseline_count": len(effective),
            "comparable_baseline_count": len(resolution.get("candidates", [])),
            "same_spec_count": len(resolution.get("candidates", [])) if spec else None,
            "count": len(resolution.get("candidates", [])) if spec else len(effective),
            "has_effective": resolution.get("has_effective", False),
            "has_baseline": resolution.get("has_baseline", False),
            "has_available_result": resolution.get("has_available_result", False),
            "available": resolution.get("available", False),
            "specs": list(spec_groups.values()),
            "baselines": effective,
            "all_baselines": resolution.get("all_baselines", []),
            "independent_specs": resolution.get("independent_specs", []),
            "baseline_resolution": resolution,
            "basis_url": (
                f"/api/goods_registry/receipt/{queried_code}"
                + (f"?spec={spec}" if spec else "")
            ),
            "verified_at": now_iso(),
        }
        if spec and selected is None:
            receipt["message"] = f"档案唯一解析到 {attribution.get('final_code')}，但规格 {spec} 暂无同规格可用基线"
        elif selected is not None:
            receipt["message"] = (
                f"旧编号 {queried_code} 已唯一解析到 {attribution.get('final_code')}，"
                f"真正生效的规格依据为 {selected.get('归一规格')} / {selected.get('基线价格')}"
            )
        persisted_receipt = deepcopy(receipt)
        mirror_row(RECEIPT_TABLE, persisted_receipt, ("goods_verify_receipts", "registry_receipts"))
        return receipt

    def get_receipt(self, code_or_id: Any = None, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.verify_goods(code_or_id, spec, **kwargs)

    def receipt_by_old_code(self, old_code: Any, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.verify_goods(old_code, spec=spec, **kwargs)

    def get_registry_receipt_by_old_code(self, old_code: Any, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.verify_goods(old_code, spec=spec, **kwargs)

    def inspect_by_old_code(self, old_code: Any, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.verify_goods(old_code, spec=spec, **kwargs)

    def open_effective_basis(self, code_or_id: Any, spec: Any, **kwargs: Any) -> dict[str, Any]:
        return self.verify_goods(code_or_id, spec=spec, **kwargs)

    def resolve_baseline_link(self, entry_id: int, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        """检查档案是否有生效价格基线；连续归并后沿唯一归属链和规格链接重读。"""
        result = self.verify_goods(entry_id, spec=spec, **kwargs)
        if not result.get("ok"):
            result["message"] = "档案不存在"
            return result
        result["message"] = "基线关联检查完成"
        return result

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        payload = payload or {}
        nested = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if action == "合并档案":
            keeper = (
                payload.get("keep_id") or payload.get("keep_code")
                or payload.get("target_id") or payload.get("target_code")
                or payload.get("keep") or nested.get("keep_id") or nested.get("keep_code")
                or nested.get("target_id") or nested.get("target_code")
            )
            if keeper is not None:
                options = {
                    "operator": payload.get("operator") or nested.get("operator"),
                    "reason": payload.get("reason") or nested.get("reason"),
                    "spec": payload.get("spec") or nested.get("spec"),
                }
                result = self.merge_duplicates(entry_id, keeper, **{k: v for k, v in options.items() if v is not None})
                row = find_goods(entry_id)
                if result.get("ok") and row is not None:
                    return row, result["message"]
                return None, result.get("message", "合并失败")
        if action in {"查验回执", "档案查验", "基线查验", "查验基线", "verify", "receipt"}:
            result = self.verify_goods(entry_id, spec=payload.get("spec") or nested.get("spec"))
            row = store.find(self.MODULE, int(entry_id)) if str(entry_id).isdigit() else find_goods(entry_id)
            if row is not None:
                row["last_receipt_no"] = result.get("receipt_no")
                # 不把整张回执挂回行对象：回执中的 original_goods 会形成序列化环。
                row["last_receipt_final_code"] = result.get("final_code")
                row["last_receipt_baseline_price"] = result.get("baseline_price")
                return row, result.get("message", "查验完成")
            return None, result.get("message", "档案不存在")
        return super().run_action(entry_id, action, payload)

    def feed_from_match(self, entry_id: int) -> dict[str, Any]:
        """消费已匹配记录把站点商品回填到登记来源。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已登记":
            return {"ok": False, "message": "档案不存在或未登记"}
        goods_id = entry.get("商品标识", "")
        matched = [
            m for m in store.rows("sku_match")
            if m.get("商品档案") == goods_id and m.get("status") == "已匹配"
        ]
        sources = {str(m.get("匹配批次", "")) for m in matched}
        if sources:
            entry["登记来源"] = ",".join(sorted(sources))[:60]
        return {
            "ok": True,
            "message": "匹配来源已回填",
            "matched_count": len(matched),
            "sources": sorted(sources),
        }

    def mark_suspected(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记疑似重复并推送到人工复核队列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "档案不存在"}
        entry["status"] = "疑似重复"
        entry["abnormal"] = True
        entry["pending"] = True
        rows = store.rows("match_review")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": rid,
            "记录编号": f"GR-{entry_id}-{rid}",
            "匹配记录": entry.get("商品标识", ""),
            "复核结论": reason,
            "复核角色": "商品运营",
            "复核时间": datetime.now(timezone.utc).isoformat(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "疑似重复已标记并转人工复核", "review_id": rid}

    def registry_stats(self) -> dict[str, Any]:
        """输出档案中心统计快照供采集量指标消费。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for r in rows if r.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "ok": True,
            "message": "档案统计完成",
            "total": len(rows),
            "by_status": by_status,
            "duplicates": by_status.get("疑似重复", 0),
        }
