"""商品档案归并、基线链接与规格可比判定的共享领域工具。

这里不依赖 FastAPI，服务层和语义化路由都调用同一套解析逻辑，避免登记中心
与规格匹配器对“哪个才是最终档案 / 哪条基线真的可读”给出不同答案。
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.store import store

GOODS_MODULE = "goods_registry"
BASELINE_MODULE = "price_baseline"
MATCH_MODULE = "sku_match"

LINEAGE_TABLE = "merge_lineage"
LINEAGE_ALIASES = ("goods_merge_links", "merge_links", "goods_merges")
LINEAGE_TABLES = (LINEAGE_TABLE, *LINEAGE_ALIASES)

BASELINE_LINK_TABLE = "baseline_links"
BASELINE_LINK_ALIASES = ("baseline_link", "price_baseline_links", "baseline_chain")
BASELINE_LINK_TABLES = (BASELINE_LINK_TABLE, *BASELINE_LINK_ALIASES)

JOB_TABLE = "match_recalculations"
JOB_ALIASES = ("recalc_jobs", "sku_recalculations", "baseline_recalc_jobs")
JOB_TABLES = (JOB_TABLE, *JOB_ALIASES)

RECEIPT_TABLE = "goods_receipts"

ACTIVE_JOB_STATUSES = {"在途", "重算中", "处理中", "open", "running", "pending"}
CONFIRMED_LINK_STATUSES = {"已确认", "有效", "active", "confirmed", ""}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def table_rows(name: str) -> list[dict[str, Any]]:
    return store.rows(name)


def next_id(name: str) -> int:
    return max((int(row.get("id", 0) or 0) for row in table_rows(name)), default=0) + 1


def mirror_row(canonical_table: str, row: dict[str, Any], aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    """写入主台账并镜像到直观别名表；各表保存独立 dict，避免后续修改互相串扰。"""
    table_rows(canonical_table).append(row)
    for alias in aliases:
        table_rows(alias).append(deepcopy(row))
    return row


def find_goods(code_or_id: Any) -> dict[str, Any] | None:
    """按数字 id、商品标识（A / G-1）或常见旧编号字段找档案。"""
    if code_or_id is None:
        return None
    if isinstance(code_or_id, dict):
        code_or_id = code_or_id.get("id") or code_or_id.get("商品标识")
    if isinstance(code_or_id, int):
        found = store.find(GOODS_MODULE, code_or_id)
        if found is not None:
            return found
    token = str(code_or_id).strip()
    if not token:
        return None
    if token.isdigit():
        found = store.find(GOODS_MODULE, int(token))
        if found is not None:
            return found
    rows = table_rows(GOODS_MODULE)
    for key in ("商品标识", "旧编号", "old_code", "goods_code", "code"):
        for row in rows:
            if str(row.get(key, "")).strip() == token:
                return row
    folded = token.casefold()
    for row in rows:
        if str(row.get("商品标识", "")).strip().casefold() == folded:
            return row
    return None


def goods_code(code_or_row: Any) -> str:
    if isinstance(code_or_row, dict):
        return str(code_or_row.get("商品标识", "") or code_or_row.get("code", "") or "")
    goods = find_goods(code_or_row)
    if goods is not None:
        return str(goods.get("商品标识", ""))
    return "" if code_or_row is None else str(code_or_row)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _link_time(link: dict[str, Any]) -> datetime | None:
    for key in ("merged_at", "created_at", "timestamp", "time", "归并时间", "合并时间"):
        parsed = _parse_time(link.get(key))
        if parsed is not None:
            return parsed
    return None


def merge_link_rows(at: Any = None) -> list[dict[str, Any]]:
    """读取所有已知台账名并按业务幂等键去重。"""
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    cutoff = _parse_time(at) if at is not None else None
    for table in LINEAGE_TABLES:
        for link in table_rows(table):
            status = str(link.get("status", "已确认"))
            if status in {"已撤销", "已取消", "rejected", "cancelled", "canceled"}:
                continue
            when = _link_time(link)
            if cutoff is not None and when is not None and when > cutoff:
                continue
            key = (
                link.get("merge_id") or link.get("event_id") or link.get("id"),
                _link_source_code(link),
                _link_target_code(link),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(link)
    result.sort(key=lambda row: (_link_time(row) or datetime.min.replace(tzinfo=timezone.utc),
                                 int(row.get("sequence", row.get("id", 0)) or 0)))
    return result


def _link_source_code(link: dict[str, Any]) -> str:
    for key in ("source_code", "old_code", "from_code", "source_goods_code",
                "被并档案", "旧商品标识", "source"):
        value = link.get(key)
        if value not in (None, ""):
            return goods_code(value) or str(value)
    source_id = link.get("source_id") or link.get("old_id") or link.get("entry_id")
    if source_id is not None:
        row = find_goods(source_id)
        if row:
            return goods_code(row)
    return ""


def _link_target_code(link: dict[str, Any]) -> str:
    for key in ("target_code", "kept_code", "to_code", "target_goods_code",
                "保留档案", "并入档案", "target"):
        value = link.get(key)
        if value not in (None, ""):
            return goods_code(value) or str(value)
    target_id = link.get("target_id") or link.get("keep_id") or link.get("kept_id")
    if target_id is not None:
        row = find_goods(target_id)
        if row:
            return goods_code(row)
    return ""


def goods_direct_target(row: dict[str, Any]) -> str:
    for key in ("并入档案", "合并至", "merged_into_code", "target_code", "final_code"):
        value = row.get(key)
        if value not in (None, ""):
            code = goods_code(value) or str(value)
            if code and code != goods_code(row):
                return code
    merged_id = row.get("merged_into_id") or row.get("target_id")
    if merged_id is not None:
        target = find_goods(merged_id)
        if target is not None:
            return goods_code(target)
    return ""


def resolve_attribution(code_or_id: Any, at: Any = None) -> dict[str, Any]:
    """把旧编号解析成唯一最终档案，并保留连续归并链。"""
    start_row = find_goods(code_or_id)
    start = goods_code(start_row) if start_row else goods_code(code_or_id)
    if not start and code_or_id is not None:
        start = str(code_or_id)

    links = merge_link_rows(at=at)
    outgoing: dict[str, set[str]] = {}
    link_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        source = _link_source_code(link)
        target = _link_target_code(link)
        if not source or not target:
            continue
        outgoing.setdefault(source, set()).add(target)
        link_by_pair[(source, target)] = link

    # 档案行上的直接字段也作为链接来源，兼容只更新行、未写台账的旧数据。
    for row in table_rows(GOODS_MODULE):
        source = goods_code(row)
        target = goods_direct_target(row)
        if source and target:
            outgoing.setdefault(source, set()).add(target)

    ambiguous = {code: sorted(targets) for code, targets in outgoing.items() if len(targets) > 1}

    chain: list[dict[str, Any]] = []
    current = start
    seen = {current}
    cycle = False
    while current in outgoing and outgoing[current]:
        targets = outgoing[current]
        if len(targets) > 1:
            ambiguous[current] = sorted(targets)
            break
        nxt = next(iter(targets))
        if nxt == current or nxt in seen:
            cycle = True
            break
        link = link_by_pair.get((current, nxt), {})
        chain.append({
            "from_code": current,
            "to_code": nxt,
            "from": current,
            "to": nxt,
            "merge_id": link.get("merge_id") or link.get("event_id") or link.get("id"),
            "merged_at": link.get("merged_at") or link.get("created_at") or link.get("时间"),
            "operator": link.get("operator", ""),
            "link": link,
        })
        seen.add(nxt)
        current = nxt

    final_row = find_goods(current) or find_goods(start)
    former = {hop["from_code"] for hop in chain}

    def terminal_of(code: str) -> tuple[str, bool]:
        walked: set[str] = set()
        current_code = code
        while current_code in outgoing and outgoing[current_code]:
            targets = outgoing[current_code]
            if len(targets) != 1 or current_code in walked or current_code in targets:
                return current_code, True
            walked.add(current_code)
            current_code = next(iter(targets))
        return current_code, False

    # 反向补齐所有多跳旧编号；不把其他分支塞进本次查询的 chain。
    for candidate_row in table_rows(GOODS_MODULE):
        candidate = goods_code(candidate_row)
        if not candidate or candidate == current:
            continue
        terminal, bad = terminal_of(candidate)
        if not bad and terminal == current:
            former.add(candidate)
    former.discard(current)

    merge_points = [
        {
            "merge_id": hop.get("merge_id"),
            "from": hop["from_code"],
            "to": hop["to_code"],
            "at": hop.get("merged_at"),
            "operator": hop.get("operator", ""),
        }
        for hop in chain
    ]
    return {
        "ok": not ambiguous and not cycle,
        "ambiguous": bool(ambiguous),
        "ambiguous_finals": ambiguous,
        "cycle": cycle,
        "queried_code": start,
        "old_code": start,
        "current_code": current,
        "final_code": current,
        "final_goods_code": current,
        "current_id": final_row.get("id") if final_row else None,
        "final_goods_id": final_row.get("id") if final_row else None,
        "final_goods": final_row,
        "chain": chain,
        "merge_points": merge_points,
        "merge_count": len(chain),
        "merge_version": len(chain),
        "former_codes": sorted(former),
        "codes": sorted({start, current, *former}),
    }


def normalize_spec(spec: Any) -> str:
    """规格归一：500g / 500克 可比；500g 与 250g 一定不同。"""
    text = str(spec or "").strip().casefold()
    if not text:
        return ""
    text = text.replace("千克", "kg").replace("公斤", "kg")
    text = text.replace("毫克", "mg").replace("毫升", "ml")
    text = text.replace("克", "g").replace("升", "l")
    text = re.sub(r"\s+", "", text)

    total_mass = 0.0
    total_volume = 0.0
    found_unit = False
    unit_to_g = {"kg": 1000.0, "g": 1.0, "mg": 0.001, "斤": 500.0}
    unit_to_ml = {"l": 1000.0, "ml": 1.0}
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(kg|mg|g|ml|l|斤)", text):
        found_unit = True
        amount = float(value)
        if unit in unit_to_g:
            total_mass += amount * unit_to_g[unit]
        else:
            total_volume += amount * unit_to_ml[unit]
    if found_unit:
        if total_mass > 0:
            return f"mass:{total_mass:g}g"
        if total_volume > 0:
            return f"volume:{total_volume:g}ml"
    return re.sub(r"[^0-9a-z一-鿿]+", "", text)


def specs_comparable(left: Any, right: Any, explicit: list[dict[str, Any]] | None = None) -> bool:
    left_key = normalize_spec(left)
    right_key = normalize_spec(right)
    if left_key and left_key == right_key:
        return True
    left_text = re.sub(r"\s+", "", str(left or "").casefold())
    right_text = re.sub(r"\s+", "", str(right or "").casefold())
    if left_text and left_text == right_text:
        return True
    for link in explicit or []:
        a = normalize_spec(link.get("source_spec") or link.get("源规格"))
        b = normalize_spec(link.get("target_spec") or link.get("目标规格"))
        keys = {left_key, right_key}
        if a in keys and b in keys and a != b and link.get("comparable", True):
            return True
    return False


def baseline_price(baseline: dict[str, Any] | None) -> float | None:
    if not baseline:
        return None
    try:
        return float(baseline.get("基线价格", baseline.get("price")))
    except (TypeError, ValueError):
        return None


def baseline_view(baseline: dict[str, Any], **extra: Any) -> dict[str, Any]:
    goods = str(baseline.get("商品标识", "") or baseline.get("goods_code", ""))
    spec = baseline.get("归一规格") or baseline.get("spec") or ""
    view = deepcopy(baseline)
    view.update({
        "baseline_id": baseline.get("id"),
        "goods_code": goods,
        "商品标识": goods,
        "spec": spec,
        "归一规格": spec,
        "price": baseline_price(baseline),
        "基线价格": baseline.get("基线价格"),
        "status": baseline.get("status", ""),
        "spec_key": normalize_spec(spec),
        "original_goods_code": baseline.get("原商品标识") or baseline.get("original_goods_code") or goods,
    })
    view.update(extra)
    return view


def explicit_spec_relations() -> list[dict[str, Any]]:
    """读取可能存在的显式规格可比关系表；没有该表时返回空。"""
    relations: list[dict[str, Any]] = []
    for table in ("spec_comparability", "comparable_specs", "spec_relations"):
        for row in table_rows(table):
            if str(row.get("status", "有效")) in {"停用", "无效", "cancelled"}:
                continue
            relations.append({
                "source_spec": row.get("source_spec") or row.get("源规格") or row.get("spec_a"),
                "target_spec": row.get("target_spec") or row.get("目标规格") or row.get("spec_b"),
                "comparable": row.get("comparable", True),
            })
    return relations


def baseline_link_rows() -> list[dict[str, Any]]:
    seen = set()
    rows: list[dict[str, Any]] = []
    for table in BASELINE_LINK_TABLES:
        for link in table_rows(table):
            status = str(link.get("status", "active"))
            if status in {"cancelled", "canceled", "已撤销", "已取消"}:
                continue
            key = (link.get("id"), link.get("source_baseline_id"), link.get("target_baseline_id"),
                   link.get("source_goods_code"), link.get("target_goods_code"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(link)
    return rows


def _baseline_by_id(baseline_id: Any) -> dict[str, Any] | None:
    if baseline_id in (None, ""):
        return None
    if isinstance(baseline_id, int) or str(baseline_id).isdigit():
        found = store.find(BASELINE_MODULE, int(baseline_id))
        if found is not None:
            return found
    return next((b for b in table_rows(BASELINE_MODULE)
                 if str(b.get("baseline_id", "")) == str(baseline_id)), None)


def resolve_baseline(
    code_or_id: Any,
    spec: Any = None,
    *,
    at: Any = None,
    effective_only: bool = True,
) -> dict[str, Any]:
    """沿档案链和基线链接确定当前真正可读的规格基线。"""
    attribution = resolve_attribution(code_or_id, at=at)
    codes = set(attribution.get("codes") or [])
    queried = goods_code(code_or_id) or attribution.get("queried_code", "")
    if queried:
        codes.add(queried)

    raw: list[dict[str, Any]] = []
    for baseline in table_rows(BASELINE_MODULE):
        owner = str(baseline.get("商品标识", "") or baseline.get("goods_code", ""))
        original = str(baseline.get("原商品标识", "") or baseline.get("original_goods_code", ""))
        if owner in codes or original in codes:
            raw.append(baseline)

    links = baseline_link_rows()
    known_ids = {b.get("id") for b in raw}
    for link in links:
        if link.get("source_goods_code") in codes or link.get("target_goods_code") in codes:
            for bid in (link.get("source_baseline_id"), link.get("target_baseline_id")):
                if bid not in known_ids:
                    extra = _baseline_by_id(bid)
                    if extra is not None:
                        raw.append(extra)
                        known_ids.add(bid)

    wanted_key = normalize_spec(spec)
    explicit = [l for l in links if l.get("comparable") or l.get("relation") == "comparable_spec"]
    explicit.extend(explicit_spec_relations())

    all_views = [baseline_view(b) for b in raw]
    effective_raw = [b for b in raw if not effective_only or b.get("status") == "已生效"]
    effective_views = [baseline_view(b) for b in effective_raw]

    comparable_raw: list[dict[str, Any]] = []
    independent_views: list[dict[str, Any]] = []
    for baseline in effective_raw:
        baseline_spec = baseline.get("归一规格") or baseline.get("spec")
        if spec in (None, "") or specs_comparable(baseline_spec, spec, explicit):
            comparable_raw.append(baseline)
        else:
            independent_views.append(baseline_view(baseline, comparable=False))

    selected: dict[str, Any] | None = None
    selected_link: dict[str, Any] | None = None
    reason = ""

    final_code = attribution.get("final_code")

    def terminal_of_baseline_link(link: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        target = _baseline_by_id(link.get("target_baseline_id"))
        if target is None:
            source = _baseline_by_id(link.get("source_baseline_id"))
            if source is not None and link.get("readable", True):
                return source, link
            return None, None
        terminal = target
        terminal_link = link
        seen_baseline: set[Any] = set()
        while True:
            next_link = next((
                item for item in links
                if item.get("source_baseline_id") == terminal.get("id")
                and item.get("target_baseline_id") not in (None, "")
                and item.get("id") not in seen_baseline
            ), None)
            if next_link is None:
                break
            seen_baseline.add(next_link.get("id"))
            nxt = _baseline_by_id(next_link.get("target_baseline_id"))
            if nxt is None:
                break
            terminal = nxt
            terminal_link = next_link
        if terminal.get("status") != "已生效":
            return None, None
        return terminal, terminal_link

    # 先汇总显式基线链接可到达的终端基线。
    link_choices: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for link in links:
        if str(link.get("status", "active")) in {"superseded", "replaced", "inactive"} or link.get("active") is False:
            continue
        source_match = (
            link.get("source_goods_code") in codes
            or link.get("source_baseline_id") in {b.get("id") for b in comparable_raw}
        )
        if not source_match:
            continue
        link_spec = link.get("spec") or link.get("归一规格") or spec
        if spec and not specs_comparable(link_spec, spec, explicit):
            continue
        terminal, used_link = terminal_of_baseline_link(link)
        if terminal is None:
            continue
        if spec and not specs_comparable(terminal.get("归一规格"), spec, explicit):
            continue
        link_choices.append((terminal, used_link or link))

    # 第一优先级：链接或最终档案上的同规格生效基线。
    final_link_choice = next((
        choice for choice in link_choices
        if str(choice[0].get("商品标识", "")) == final_code
    ), None)
    final_matches = [
        b for b in comparable_raw
        if str(b.get("商品标识", "")) == final_code
    ]
    if final_link_choice is not None:
        selected, selected_link = final_link_choice
        reason = "baseline_link_to_final_profile"
    elif final_matches:
        selected = sorted(final_matches, key=_baseline_sort_key, reverse=True)[0]
        reason = "final_profile_same_spec"
        selected_link = next((
            link for link in links
            if link.get("target_baseline_id") == selected.get("id")
            and (link.get("source_goods_code") in codes or link.get("source_baseline_id") in known_ids)
        ), None)
    elif link_choices:
        selected, selected_link = sorted(
            link_choices,
            key=lambda choice: _baseline_sort_key(choice[0]),
            reverse=True,
        )[0]
        reason = "baseline_link_readable"

    # 第二优先级：归属链上最近旧档案遗留的同规格基线，仍可回查使用。
    if selected is None and comparable_raw:
        chain_order = [final_code, *(hop["from_code"] for hop in reversed(attribution.get("chain", [])))]
        rank = {code: i for i, code in enumerate(chain_order)}
        selected = sorted(
            comparable_raw,
            key=lambda b: (rank.get(str(b.get("商品标识", "")), 99), *_baseline_sort_key(b)),
        )[0]
        reason = "inherited_chain_same_spec"

    selected_view = baseline_view(
        selected,
        selected_by=reason,
        requested_spec=spec or "",
        requested_spec_key=wanted_key,
        link=selected_link,
    ) if selected is not None else None

    return {
        "ok": attribution.get("ok", True),
        "attribution": attribution,
        "codes": attribution.get("codes", sorted(codes)),
        "queried_code": queried,
        "current_code": final_code,
        "final_code": final_code,
        "spec": spec or "",
        "normalized_spec": wanted_key,
        "baseline": selected_view,
        "selected": selected_view,
        "baseline_price": baseline_price(selected) if selected is not None else None,
        "price": baseline_price(selected) if selected is not None else None,
        "baseline_id": selected.get("id") if selected is not None else None,
        "has_baseline": selected is not None,
        "has_effective": bool(effective_views),
        "has_available_result": selected is not None,
        "available": selected is not None,
        "selection_reason": reason,
        "selected_link": selected_link,
        "candidates": [baseline_view(b) for b in comparable_raw],
        "baselines": effective_views,
        "all_baselines": all_views,
        "independent_specs": independent_views,
    }


def _baseline_sort_key(baseline: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(baseline.get("更新时间", "") or ""),
        int(baseline.get("id", 0) or 0),
    )


def snapshot_baseline(baseline: dict[str, Any] | None, **extra: Any) -> dict[str, Any] | None:
    if baseline is None:
        return None
    snap = baseline_view(baseline)
    snap["snapshot_at"] = now_iso()
    snap["immutable"] = True
    snap.update(extra)
    return snap


def create_merge_baseline_links(
    source_codes: set[str] | list[str],
    target_code: str,
    merge_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """为来源族的每条规格建立可追溯基线链接；不同规格绝不串链。"""
    source_set = set(source_codes)
    target_attribution = resolve_attribution(target_code)
    final_target = target_attribution.get("final_code") or target_code
    target_family = set(target_attribution.get("codes") or [target_code]) - source_set
    created: list[dict[str, Any]] = []
    existing = {
        (str(l.get("source_baseline_id")), str(l.get("target_baseline_id")), normalize_spec(l.get("spec")))
        for l in baseline_link_rows()
    }

    source_baselines = [
        b for b in table_rows(BASELINE_MODULE)
        if str(b.get("商品标识", "") or b.get("goods_code", "")) in source_set
        or str(b.get("原商品标识", "")) in source_set
    ]
    for source_baseline in source_baselines:
        spec = source_baseline.get("归一规格") or source_baseline.get("spec") or ""
        wanted = normalize_spec(spec)
        target_candidates = [
            b for b in table_rows(BASELINE_MODULE)
            if str(b.get("商品标识", "") or b.get("goods_code", "")) in target_family
            and b.get("status") == "已生效"
            and (not wanted or normalize_spec(b.get("归一规格")) == wanted)
        ]
        target_baseline_row = next(
            (b for b in target_candidates if str(b.get("商品标识", "")) == final_target),
            None,
        ) or (sorted(target_candidates, key=_baseline_sort_key, reverse=True)[0] if target_candidates else None)
        target_baseline = baseline_view(target_baseline_row) if target_baseline_row else None
        target_id = target_baseline.get("id") if target_baseline else None
        key = (str(source_baseline.get("id")), str(target_id), normalize_spec(spec))
        if key in existing:
            continue
        link = {
            "id": next_id(BASELINE_LINK_TABLE),
            "link_id": None,  # set after id is known below
            "merge_id": merge_row.get("merge_id"),
            "merge_event_id": merge_row.get("event_id") or merge_row.get("merge_id"),
            "source_baseline_id": source_baseline.get("id"),
            "source_goods_code": source_baseline.get("商品标识"),
            "source_spec": spec,
            "target_baseline_id": target_id,
            "target_goods_code": target_baseline.get("商品标识") if target_baseline else final_target,
            "target_spec": target_baseline.get("归一规格") if target_baseline else spec,
            "final_goods_code": final_target,
            "spec": spec,
            "归一规格": spec,
            "normalized_spec": normalize_spec(spec),
            "relation": "same_spec" if target_id else "inherited_same_spec",
            "comparable": True,
            "readable": True,
            "effective": source_baseline.get("status") == "已生效" or bool(target_baseline),
            "created_at": now_iso(),
            "merged_at": merge_row.get("merged_at"),
            "status": "active",
        }
        link["link_id"] = f"BL-{link['id']:04d}"
        if target_id is not None:
            # 连续归并产生直达最终同规格基线的新链接后，旧的过渡链接只留痕、
            # 不再参与“当前实际可读基线”的选择，避免解析出两条生效路径。
            for old_link in baseline_link_rows():
                if (
                    old_link.get("source_baseline_id") == source_baseline.get("id")
                    and normalize_spec(old_link.get("spec")) == normalize_spec(spec)
                    and old_link.get("target_baseline_id") != target_id
                    and old_link.get("status") not in {"superseded", "replaced"}
                ):
                    old_link["status"] = "superseded"
                    old_link["active"] = False
                    old_link["readable"] = False
                    old_link["superseded_by"] = link["link_id"]
                    old_link["superseded_by_id"] = link["id"]
        mirror_row(BASELINE_LINK_TABLE, link, BASELINE_LINK_ALIASES)
        created.append(link)
        existing.add(key)
    return created


def recalc_job_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen = set()
    for table in JOB_TABLES:
        for job in table_rows(table):
            key = job.get("job_id") or job.get("id")
            if key in seen:
                continue
            seen.add(key)
            rows.append(job)
    return rows


def record_merge_in_recalc_jobs(merge_row: dict[str, Any]) -> None:
    """把连续归并时点追加到同一笔在途重算；不替值班员选择 32 或 34。"""
    source = _link_source_code(merge_row)
    target = _link_target_code(merge_row)
    for job in recalc_job_rows():
        if str(job.get("status", "")) not in ACTIVE_JOB_STATUSES:
            continue
        original_code = job.get("original_goods_code") or job.get("goods_code") or job.get("queried_code")
        attribution = resolve_attribution(original_code)
        codes = set(attribution.get("codes") or [])
        codes.update({source, target, _link_target_code(merge_row)})
        related = (
            source in codes or target in codes
            or job.get("match_id") == merge_row.get("match_id")
            or str(job.get("entry_id", "")) == str(merge_row.get("entry_id", ""))
        )
        if not related:
            continue
        point = {
            "merge_id": merge_row.get("merge_id") or merge_row.get("id"),
            "from": source,
            "to": target,
            "at": merge_row.get("merged_at") or now_iso(),
            "operator": merge_row.get("operator", ""),
        }
        points = job.setdefault("merge_points", [])
        if not any(p.get("merge_id") == point["merge_id"] and p.get("from") == source for p in points):
            points.append(point)
        seen_ids = set(job.get("seen_merge_ids", []))
        seen_ids.add(point["merge_id"])
        job["seen_merge_ids"] = sorted(x for x in seen_ids if x is not None)
        job["current_goods_code"] = attribution.get("current_code")
        job["current_attribution"] = attribution
        if len(points) >= 2:
            job["conflict"] = True
            job["conflict_code"] = "multiple_merge_points_in_one_recalculation"
            job["冲突"] = True
            job["冲突说明"] = "同一笔重算跨越两个归并时点，继续时必须留存采用旧基线或最终基线的完整依据"


def find_recalc_job(ref: Any) -> dict[str, Any] | None:
    if ref is None:
        return None
    if isinstance(ref, int) or str(ref).isdigit():
        rid = int(ref)
        for table in JOB_TABLES:
            found = store.find(table, rid)
            if found is not None:
                return found
    token = str(ref)
    return next((j for j in recalc_job_rows()
                 if str(j.get("job_id", "")) == token or str(j.get("ref", "")) == token), None)
