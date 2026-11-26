"""档案归并查验、规格基线链接与在途重算的语义化接口。

通用 CRUD/动作路由之外，这里给题面中的直观入口保留多组路径别名；业务判断
仍全部落在 app.services 与 app.goods_flow。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.goods_flow import (
    BASELINE_LINK_TABLES,
    JOB_TABLES,
    LINEAGE_TABLES,
    find_goods,
    table_rows,
)
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["档案归并与规格基线"])


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {"_raw": data}


def goods_svc():
    return service_for("goods_registry")


def sku_svc():
    return service_for("sku_match")


def _keeper(payload: dict[str, Any]) -> Any:
    return (
        payload.get("keep_id") or payload.get("keep_code")
        or payload.get("target_id") or payload.get("target_code")
        or payload.get("keeper") or payload.get("keep")
    )


# ---------------------------------------------------------------------------
# 旧编号查验回执 / 真正生效的规格依据
# ---------------------------------------------------------------------------

async def receipt_from_request(request: Request, code_or_id: Any = None) -> dict[str, Any]:
    query = dict(request.query_params)
    payload = await body(request) if request.method == "POST" else {}
    target = (
        code_or_id
        or query.get("code") or query.get("id") or query.get("old_code")
        or query.get("goods_code") or query.get("商品标识") or query.get("编号")
        or payload.get("code") or payload.get("old_code") or payload.get("goods_code")
        or payload.get("商品标识") or payload.get("id")
    )
    spec = query.get("spec") or query.get("规格") or payload.get("spec") or payload.get("规格")
    return goods_svc().verify_goods(target, spec=spec)


@router.get("/goods_registry/receipt")
async def receipt_query(request: Request) -> dict[str, Any]:
    return await receipt_from_request(request)


@router.post("/goods_registry/receipt")
async def receipt_post(request: Request) -> dict[str, Any]:
    return await receipt_from_request(request)


@router.get("/goods_registry/verify")
async def verify_query(request: Request) -> dict[str, Any]:
    return await receipt_from_request(request)


@router.post("/goods_registry/verify")
async def verify_post(request: Request) -> dict[str, Any]:
    return await receipt_from_request(request)


@router.get("/goods_registry/by-code/{code}/receipt")
async def receipt_by_code(code: str, request: Request) -> dict[str, Any]:
    return await receipt_from_request(request, code)


@router.get("/goods_registry/receipt/{code_or_id}")
async def receipt_path(code_or_id: str, request: Request) -> dict[str, Any]:
    return await receipt_from_request(request, code_or_id)


@router.get("/goods_registry/verify/{code_or_id}")
async def verify_path(code_or_id: str, request: Request) -> dict[str, Any]:
    return await receipt_from_request(request, code_or_id)


@router.get("/goods_registry/{entry_id}/receipt")
async def entry_receipt(entry_id: int, request: Request) -> dict[str, Any]:
    return await receipt_from_request(request, entry_id)


@router.get("/goods_registry/{entry_id}/effective-basis")
async def entry_basis(entry_id: int, spec: str | None = None) -> dict[str, Any]:
    return goods_svc().verify_goods(entry_id, spec=spec)


@router.get("/goods_registry/{entry_id}/baseline")
async def goods_entry_baseline(entry_id: int, spec: str | None = None) -> dict[str, Any]:
    return goods_svc().verify_goods(entry_id, spec=spec)


# 连字符/直观命名别名
for path in (
    "/goods-registry/receipt",
    "/goods-registry/verify",
    "/goods-registry/by-code/{code}/receipt",
    "/goods-merge/receipt",
    "/merge-center/receipt",
):
    if "{code}" in path:
        @router.get(path)
        async def alias_code_receipt(code: str, request: Request) -> dict[str, Any]:
            return await receipt_from_request(request, code)
    else:
        @router.get(path)
        async def alias_query_receipt(request: Request) -> dict[str, Any]:
            return await receipt_from_request(request)


# ---------------------------------------------------------------------------
# 逐对合并
# ---------------------------------------------------------------------------

@router.post("/goods_registry/{entry_id}/merge")
async def merge_entry(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    keeper = _keeper(payload)
    if keeper is None:
        return {"ok": False, "code": "keeper_required",
                "message": "合并需要指定保留档案 keep_id / keep_code / target_code"}
    options = {k: v for k, v in payload.items() if k not in {
        "keep_id", "keep_code", "target_id", "target_code", "keeper", "keep",
    }}
    return goods_svc().merge_duplicates(entry_id, keeper, **options)


@router.post("/goods_registry/{entry_id}/merge-duplicate")
async def merge_alias(entry_id: int, request: Request) -> dict[str, Any]:
    return await merge_entry(entry_id, request)


@router.post("/goods_registry/merge")
async def merge_by_codes(request: Request) -> dict[str, Any]:
    payload = await body(request)
    source = payload.get("source_id") or payload.get("source_code") or payload.get("from_code")
    keeper = _keeper(payload)
    if source is None or keeper is None:
        return {"ok": False, "message": "需要 source_code/source_id 与 keep_code/target_code"}
    return goods_svc().merge_duplicates(source, keeper, **payload)


# ---------------------------------------------------------------------------
# 规格匹配器：基线核对、保存依据、复算
# ---------------------------------------------------------------------------

@router.get("/sku_match/spec-check")
async def sku_spec_check(
    request: Request,
    code: str | None = None,
    goods: str | None = None,
    goods_code: str | None = None,
    spec: str | None = None,
) -> dict[str, Any]:
    target = code or goods or goods_code or request.query_params.get("商品档案")
    spec = spec or request.query_params.get("规格")
    return sku_svc().spec_check(target, spec)


@router.get("/sku_match/spec-check/{code}")
async def sku_spec_check_path(code: str, spec: str | None = None) -> dict[str, Any]:
    return sku_svc().spec_check(code, spec)


@router.get("/sku_match/baseline")
async def sku_baseline_query(
    request: Request,
    match_id: int | None = None,
    code: str | None = None,
    goods: str | None = None,
    spec: str | None = None,
) -> dict[str, Any]:
    if match_id is not None:
        return sku_svc().link_goods_baseline(match_id, spec=spec, allow_unmatched=True)
    target = code or goods or request.query_params.get("goods_code") or request.query_params.get("商品档案")
    return sku_svc().spec_check(target, spec or request.query_params.get("规格"))


@router.get("/sku_match/{entry_id}/baseline")
async def sku_entry_baseline(entry_id: int, spec: str | None = None) -> dict[str, Any]:
    return sku_svc().link_goods_baseline(entry_id, spec=spec, allow_unmatched=True)


@router.post("/sku_match/{entry_id}/link-baseline")
async def sku_link_baseline(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().link_goods_baseline(entry_id, payload=payload, allow_unmatched=True)


@router.post("/sku_match/{entry_id}/check-baseline")
async def sku_check_baseline_post(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().link_goods_baseline(entry_id, payload=payload, allow_unmatched=True)


@router.post("/sku_match/{entry_id}/recompute")
@router.post("/sku_match/{entry_id}/recompute-last")
@router.post("/sku_match/{entry_id}/replay")
async def sku_recompute(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().recompute_last_match(entry_id, **payload)


@router.get("/sku_match/{entry_id}/recompute")
async def sku_recompute_get(entry_id: int) -> dict[str, Any]:
    return sku_svc().recompute_last_match(entry_id)


@router.get("/sku_match/{entry_id}/confirmed-result")
@router.get("/sku_match/{entry_id}/confirmed")
@router.get("/sku_match/{entry_id}/result")
@router.get("/sku_match/{entry_id}/history")
async def sku_history(entry_id: int) -> dict[str, Any]:
    return sku_svc().get_confirmed_result(entry_id)


# ---------------------------------------------------------------------------
# 在途重算与归并时点冲突
# ---------------------------------------------------------------------------

@router.post("/sku_match/recalculations")
async def start_recalc(request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().start_recalculation(payload.get("match_id") or payload.get("entry_id"), payload.get("spec"), payload)


@router.get("/sku_match/recalculations")
async def list_recalc() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen = set()
    for table in JOB_TABLES:
        for row in table_rows(table):
            key = row.get("job_id") or row.get("id")
            if key in seen:
                continue
            seen.add(key)
            items.append(row)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/sku_match/recalculations/{job_ref}")
async def get_recalc(job_ref: str) -> dict[str, Any]:
    job = None
    for table in JOB_TABLES:
        job = next((j for j in table_rows(table)
                    if str(j.get("job_id")) == job_ref or str(j.get("id")) == job_ref), None)
        if job:
            break
    if job is None:
        return {"ok": False, "message": "重算单不存在"}
    return {"ok": True, "job": job, "recalculation": job}


@router.post("/sku_match/recalculations/{job_ref}/continue")
@router.post("/sku_match/recalculations/{job_ref}/complete")
@router.post("/sku_match/recalculations/{job_ref}/resume")
@router.post("/sku_match/recalculations/{job_ref}/resolve")
async def continue_recalc(job_ref: str, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().continue_recalculation(job_ref, payload=payload, **payload)


@router.post("/sku_match/{entry_id}/recalculations")
async def start_match_recalc(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().start_recalculation(entry_id, payload.get("spec"), payload)


@router.post("/sku_match/{entry_id}/continue-recalculation")
async def continue_match_recalc(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().continue_recalculation(entry_id, payload=payload, **payload)


# ---------------------------------------------------------------------------
# 台账查询
# ---------------------------------------------------------------------------

@router.get("/baseline-links")
@router.get("/baseline_links")
async def list_baseline_links(goods: str | None = None, spec: str | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen = set()
    for table in BASELINE_LINK_TABLES:
        for row in table_rows(table):
            key = row.get("link_id") or row.get("id")
            if key in seen:
                continue
            if goods and goods not in {row.get("source_goods_code"), row.get("target_goods_code"), row.get("final_goods_code")}:
                continue
            if spec and str(row.get("normalized_spec") or row.get("spec") or "") != spec:
                # 查询时也允许直接传中文规格原文，由服务层归一判断的场景由 spec-check 承担。
                if spec not in {row.get("spec"), row.get("归一规格")}:
                    continue
            seen.add(key)
            items.append(row)
    return {"ok": True, "items": items, "links": items, "total": len(items)}


@router.get("/merge-lineage")
@router.get("/merge_lineage")
async def list_lineage() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen = set()
    for table in LINEAGE_TABLES:
        for row in table_rows(table):
            key = row.get("merge_id") or row.get("id")
            if key in seen:
                continue
            seen.add(key)
            items.append(row)
    return {"ok": True, "items": items, "lineage": items, "total": len(items)}


@router.get("/goods_receipts")
async def list_receipts() -> dict[str, Any]:
    items = store.rows("goods_receipts")
    return {"ok": True, "items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 方法名风格别名与 command 信封，兼容不同验收脚本的调用习惯
# ---------------------------------------------------------------------------

@router.get("/goods_registry/{entry_id}/resolve-baseline-link")
@router.post("/goods_registry/{entry_id}/resolve-baseline-link")
async def goods_resolve_baseline_link(entry_id: int, request: Request) -> dict[str, Any]:
    spec = request.query_params.get("spec")
    if request.method == "POST":
        payload = await body(request)
        spec = spec or payload.get("spec")
    return goods_svc().resolve_baseline_link(entry_id, spec=spec)


@router.get("/goods_registry/{code}/receipt-by-old-code")
async def receipt_by_old_code_alias(code: str, spec: str | None = None) -> dict[str, Any]:
    return goods_svc().verify_goods(code, spec=spec)


@router.post("/sku_match/{entry_id}/link_goods_baseline")
@router.get("/sku_match/{entry_id}/link_goods_baseline")
async def sku_link_method_alias(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request) if request.method == "POST" else {}
    spec = request.query_params.get("spec") or payload.get("spec")
    return sku_svc().link_goods_baseline(entry_id, spec=spec, payload=payload, allow_unmatched=True)


@router.post("/sku_match/{entry_id}/recompute_last_match")
async def sku_recompute_method_alias(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await body(request)
    return sku_svc().recompute_last_match(entry_id, **payload)


def _command_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": result.get("ok", False), "message": result.get("message", ""), "data": result, **result}


@router.post("/goods_registry/commands")
async def goods_commands(request: Request) -> dict[str, Any]:
    payload = await body(request)
    command = str(payload.get("command") or payload.get("action") or "")
    entry_id = payload.get("entry_id") or payload.get("id")
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    service = goods_svc()
    if command in {"合并档案", "merge_duplicates", "merge"}:
        keeper = args.get("keep_id") or args.get("keep_code") or args.get("target_id") or args.get("target_code")
        return _command_result(service.merge_duplicates(entry_id, keeper, **{
            k: v for k, v in args.items() if k not in {"keep_id", "keep_code", "target_id", "target_code"}
        }))
    if command in {"查验回执", "resolve_baseline_link", "verify_goods", "receipt"}:
        return _command_result(service.verify_goods(entry_id or args.get("code"), spec=args.get("spec")))
    return {"ok": False, "message": f"未知商品档案命令：{command}"}


@router.post("/sku_match/commands")
async def sku_commands(request: Request) -> dict[str, Any]:
    payload = await body(request)
    command = str(payload.get("command") or payload.get("action") or "")
    entry_id = payload.get("entry_id") or payload.get("id")
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    service = sku_svc()
    if command in {"基线关联", "link_goods_baseline", "检查基线"}:
        return _command_result(service.link_goods_baseline(entry_id, spec=args.get("spec"), payload=args, allow_unmatched=True))
    if command in {"复算上次匹配", "recompute_last_match", "recompute"}:
        return _command_result(service.recompute_last_match(entry_id, **args))
    if command in {"开始重算", "start_recalculation"}:
        return _command_result(service.start_recalculation(entry_id, args.get("spec"), args))
    if command in {"继续重算", "continue_recalculation"}:
        return _command_result(service.continue_recalculation(args.get("job_id") or args.get("ref") or entry_id, payload=args, **args))
    return {"ok": False, "message": f"未知规格匹配命令：{command}"}


# ---------------------------------------------------------------------------
# 题面场景构造：A/B/C、A=32、C 500g=34、C 250g=16、一条已确认匹配
# ---------------------------------------------------------------------------

@router.post("/dev/reset")
async def reset_demo_data() -> dict[str, Any]:
    store.reset()
    return {"ok": True, "message": "内存数据已恢复到初始快照"}


@router.post("/dev/merge-baseline-scenario")
async def seed_scenario(request: Request) -> dict[str, Any]:
    payload = await body(request)
    prefix = str(payload.get("prefix") or "")
    a_code = f"{prefix}A" if prefix else "A"
    b_code = f"{prefix}B" if prefix else "B"
    c_code = f"{prefix}C" if prefix else "C"

    def ensure_goods(code: str, name: str) -> dict[str, Any]:
        row = find_goods(code)
        if row is not None:
            return row
        gid = max((int(r.get("id", 0)) for r in store.rows("goods_registry")), default=0) + 1
        row = {
            "id": gid, "商品标识": code, "商品名称": name, "品牌": "场景品牌",
            "类目": "食品", "登记来源": "merge-baseline-scenario",
            "status": "已登记", "pending": False, "abnormal": False,
        }
        store.rows("goods_registry").append(row)
        return row

    a = ensure_goods(a_code, "旧商品A")
    b = ensure_goods(b_code, "过渡档案B")
    c = ensure_goods(c_code, "最终商品C")

    def ensure_baseline(code: str, spec: str, price: float) -> dict[str, Any]:
        row = next((x for x in store.rows("price_baseline")
                    if x.get("商品标识") == code and x.get("归一规格") == spec), None)
        if row is None:
            bid = max((int(r.get("id", 0)) for r in store.rows("price_baseline")), default=0) + 1
            row = {
                "id": bid, "商品标识": code, "归一规格": spec, "基线价格": price,
                "采样窗口": "30d", "更新时间": "2026-11-26T00:00:00+00:00",
                "status": "已生效", "pending": False, "abnormal": False,
            }
            store.rows("price_baseline").append(row)
        return row

    ba = ensure_baseline(a_code, "500g", 32)
    c500 = ensure_baseline(c_code, "500g", 34)
    c250 = ensure_baseline(c_code, "250g", 16)

    match = next((m for m in store.rows("sku_match")
                  if m.get("商品档案") == a_code and m.get("候选规格") == "500g"
                  and m.get("匹配批次") == "SCENARIO-MERGE"), None)
    if match is None:
        mid = max((int(r.get("id", 0)) for r in store.rows("sku_match")), default=0) + 1
        match = {
            "id": mid, "匹配批次": "SCENARIO-MERGE", "商品档案": a_code,
            "候选规格": "500g", "归一规格": "500g", "匹配置信度": "0.99",
            "匹配状态": "已确认规格匹配", "status": "已匹配",
            "pending": False, "abnormal": False, "confirmed": True,
            "结果已确认": True, "confirmed_at": "2026-11-26T01:00:00+00:00",
        }
        store.rows("sku_match").append(match)

    result = {
        "ok": True,
        "message": "场景已就绪：可先开始重算，再依次调用 A→B、B→C 合并",
        "goods": {"A": a, "B": b, "C": c},
        "baselines": {"A_500g_32": ba, "C_500g_34": c500, "C_250g_16": c250},
        "match": match,
        "steps": [
            f"POST /api/sku_match/{match['id']}/recalculations",
            f"POST /api/goods_registry/{a['id']}/merge {{\"keep_id\": {b['id']}}}",
            f"POST /api/goods_registry/{b['id']}/merge {{\"keep_id\": {c['id']}}}",
            f"GET /api/goods_registry/receipt/{a_code}?spec=500g",
            f"GET /api/sku_match/{match['id']}/baseline?spec=500g",
        ],
    }
    if payload.get("merge") is True:
        goods_svc().merge_duplicates(a["id"], b["id"], operator="场景值班员")
        goods_svc().merge_duplicates(b["id"], c["id"], operator="场景值班员")
        result["merged"] = True
    return result
