import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T
}

export async function getJson<T = any>(path: string): Promise<T> {
  const response = await request(path)
  return readJson<T>(response)
}

export async function postJson<T = any>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return readJson<T>(response)
}

export function goodsReceiptUrl(code: string, spec = ''): string {
  const query = new URLSearchParams()
  if (spec) query.set('spec', spec)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return `/api/goods_registry/receipt/${encodeURIComponent(code)}${suffix}`
}

export async function getGoodsReceipt(code: string, spec = ''): Promise<any> {
  return getJson(goodsReceiptUrl(code, spec))
}

export async function mergeGoods(id: number | string, keeper: number | string): Promise<any> {
  const key = /^\d+$/.test(String(keeper)) ? 'keep_id' : 'keep_code'
  const value = /^\d+$/.test(String(keeper)) ? Number(keeper) : keeper
  return postJson(`/api/goods_registry/${id}/merge`, { [key]: value })
}

export async function seedMergeScenario(): Promise<any> {
  return postJson('/api/dev/merge-baseline-scenario', {})
}

export async function getSkuBaseline(id: number | string, spec = ''): Promise<any> {
  const suffix = spec ? `?spec=${encodeURIComponent(spec)}` : ''
  return getJson(`/api/sku_match/${id}/baseline${suffix}`)
}

export async function recomputeSkuMatch(id: number | string): Promise<any> {
  return postJson(`/api/sku_match/${id}/recompute`, {})
}

export async function startSkuRecalculation(id: number | string, spec = ''): Promise<any> {
  return postJson(`/api/sku_match/${id}/recalculations`, spec ? { spec } : {})
}

export async function continueSkuRecalculation(jobId: string, strategy = ''): Promise<any> {
  return postJson(`/api/sku_match/recalculations/${encodeURIComponent(jobId)}/continue`, strategy ? { strategy } : {})
}

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
