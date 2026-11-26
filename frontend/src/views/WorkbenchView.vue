<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <!-- 商品登记中心：旧编号查验回执与真正生效的规格依据 -->
    <section v-if="meta.key === 'goods_registry'" class="flow-desk" data-testid="goods-flow-desk">
      <div class="flow-head">
        <h3>旧编号查验与基线回执</h3>
        <button class="btn ghost" type="button" data-testid="seed-merge-scenario" @click="seedScenario">
          构造 A→B→C / 500g·250g 场景
        </button>
      </div>
      <form class="filter-bar" @submit.prevent="fetchReceipt()">
        <label class="filter-item">
          <span>旧编号 / 档案ID</span>
          <input v-model="receiptCode" data-testid="goods-receipt-code" placeholder="例如 A 或 4" />
        </label>
        <label class="filter-item">
          <span>可比规格</span>
          <input v-model="receiptSpec" data-testid="goods-receipt-spec" placeholder="例如 500g" />
        </label>
        <button class="btn primary" type="submit" data-testid="goods-receipt-fetch" :disabled="flowBusy">
          取得档案查验回执
        </button>
      </form>
      <p v-if="flowMessage" class="flow-message" data-testid="flow-message">{{ flowMessage }}</p>

      <article v-if="receipt" class="receipt-card" data-testid="goods-receipt" :data-ok="String(Boolean(receipt.ok))">
        <template v-if="receipt.ok">
          <header class="receipt-head">
            <div>
              <h4>商品档案查验回执</h4>
              <span data-testid="receipt-id" class="receipt-no">{{ receipt.receipt_no }}</span>
            </div>
            <span class="badge" data-testid="receipt-final">最终档案：{{ receipt.final_code }}</span>
          </header>
          <p data-testid="receipt-chain" class="chain-line">
            归并链：<b>{{ (receipt.chain || []).map((h: any) => `${h.from_code}→${h.to_code}`).join('，') || '无归并' }}</b>
            <span v-if="receipt.former_codes?.length">；旧编号：{{ receipt.former_codes.join('、') }}</span>
          </p>

          <div class="basis-box" data-testid="effective-basis">
            <template v-if="receipt.baseline">
              <span>真正生效的规格依据：</span>
              <b data-testid="effective-spec">{{ receipt.baseline['归一规格'] }}</b>
              <strong class="baseline-price" data-testid="effective-price">¥{{ receipt.baseline_price }}</strong>
              <small>基线#{{ receipt.baseline.id }} / {{ receipt.baseline.status }}</small>
            </template>
            <p v-else class="empty-state" data-testid="receipt-no-available">无可用结果：该规格没有同商品可比的生效基线</p>
          </div>

          <div v-if="(receipt.independent_specs || []).length" class="independent-box" data-testid="independent-specs">
            <span>其他规格保持独立：</span>
            <i v-for="b in receipt.independent_specs" :key="`ind-${b.id}`" :data-testid="`independent-${b.id}`">
              {{ b['归一规格'] }} ¥{{ b.price }}
            </i>
          </div>

          <ul class="baseline-list" data-testid="receipt-baseline-list">
            <li v-for="b in receipt.baselines || []" :key="`rb-${b.id}`" :data-testid="`receipt-baseline-row-${b.id}`">
              <span>{{ b['商品标识'] }} · {{ b['归一规格'] }}</span>
              <b>¥{{ b.price }}</b>
              <em>{{ b.status }}</em>
            </li>
          </ul>
        </template>
        <p v-else class="error-text">{{ receipt.message || '查验失败' }}</p>
      </article>
    </section>

    <!-- 规格匹配器：消费基线链接、保存依据、复算上次匹配 -->
    <section v-if="meta.key === 'sku_match'" class="flow-desk" data-testid="sku-flow-desk">
      <div class="flow-head">
        <h3>基线链接、归并依据与上次匹配复算</h3>
        <button class="btn ghost" type="button" data-testid="seed-sku-scenario" @click="seedScenario">
          载入 A/B/C 验收场景
        </button>
      </div>
      <form class="filter-bar" @submit.prevent="checkSku()">
        <label class="filter-item">
          <span>匹配记录ID</span>
          <input v-model="skuMatchId" data-testid="sku-match-id" placeholder="例如 4" />
        </label>
        <label class="filter-item">
          <span>规格</span>
          <input v-model="skuSpec" data-testid="sku-spec" placeholder="500g" />
        </label>
        <button class="btn primary" type="submit" data-testid="sku-baseline-fetch">读取实际基线</button>
        <button class="btn" type="button" data-testid="sku-recompute" @click="recomputeSku()">复算上次匹配</button>
        <button class="btn" type="button" data-testid="sku-start-recalc" @click="startRecalc()">开始旧A重算</button>
      </form>

      <div v-if="skuJob" class="job-box" data-testid="recalc-job">
        <p>重算单 <b data-testid="recalc-job-id">{{ skuJob.job_id }}</b>；启动锁定价：
          <b data-testid="recalc-locked-price">{{ skuJob.locked_baseline_price }}</b>
          <span v-if="skuJob.conflict" class="error-text" data-testid="recalc-conflict">两个归并时点冲突</span>
        </p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="continue-original-32"
                  @click="continueRecalc('original')">继续原重算并保留启动依据（如32）</button>
          <button class="btn primary" type="button" data-testid="continue-latest-34"
                  @click="continueRecalc('latest')">跟随最终档案依据（如34）</button>
        </div>
      </div>

      <article v-if="skuResult" class="receipt-card" data-testid="sku-baseline-result">
        <h4>规格匹配基线依据</h4>
        <div class="basis-grid">
          <div>
            <span>实际可读取基线</span>
            <strong data-testid="readable-price">
              {{ skuResult.baseline_price ?? skuResult.current_readable_price ?? '无可用结果' }}
            </strong>
          </div>
          <div>
            <span>已确认历史结果</span>
            <strong data-testid="confirmed-price">{{ skuResult.confirmed_baseline_price ?? '—' }}</strong>
          </div>
          <div>
            <span>归并所用档案</span>
            <strong data-testid="used-goods">
              {{ skuResult.final_goods || skuResult.used_goods_code || skuResult.basis?.['归并结果所用档案'] || '—' }}
            </strong>
          </div>
          <div>
            <span>归并所用规格</span>
            <strong data-testid="used-spec">
              {{ skuResult.basis?.['归并结果所用规格'] || skuResult.used_spec || skuResult.spec || '—' }}
            </strong>
          </div>
        </div>
        <p class="flow-message" data-testid="sku-result-message">{{ skuResult.message }}</p>
        <pre v-if="skuResult.basis" class="basis-json" data-testid="basis-json">{{ JSON.stringify(skuResult.basis, null, 2) }}</pre>
      </article>
    </section>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'goods_registry'"
              class="link"
              type="button"
              :data-testid="`receipt-${row.id}`"
              @click="fetchReceipt(row.id, '')"
            >
              查验回执
            </button>
            <template v-if="meta.key === 'sku_match'">
              <button class="link" type="button" :data-testid="`spec-check-${row.id}`" @click="checkSku(row)">
                基线依据
              </button>
              <button class="link" type="button" :data-testid="`recompute-${row.id}`" @click="recomputeSku(row)">
                复算
              </button>
            </template>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="mergeRow" class="modal-backdrop" data-testid="merge-modal">
      <form class="modal" @submit.prevent="confirmMerge">
        <h3>合并 {{ mergeRow['商品标识'] }} 到保留档案</h3>
        <label class="create-field">
          <span>保留档案 ID 或商品编号</span>
          <input
            v-model="mergeKeeper"
            data-testid="merge-keep-input"
            placeholder="例如 C 或档案数字 ID"
            required
          />
        </label>
        <p v-if="mergeError" class="error-text" data-testid="merge-error">{{ mergeError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="mergeRow = null">取消</button>
          <button class="btn primary" type="submit" data-testid="merge-keep-confirm" :disabled="flowBusy">
            确认合并并生成回执
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  continueSkuRecalculation,
  createEntry,
  downloadEntries,
  getGoodsReceipt,
  getSkuBaseline,
  listEntries,
  mergeGoods,
  moduleMeta,
  recomputeSkuMatch,
  runAction as applyAction,
  seedMergeScenario,
  startSkuRecalculation,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const receiptCode = ref('A')
const receiptSpec = ref('500g')
const receipt = ref<any>(null)
const mergeRow = ref<EntryRow | null>(null)
const mergeKeeper = ref('')
const mergeError = ref('')
const skuMatchId = ref<number | string>('')
const skuSpec = ref('500g')
const skuResult = ref<any>(null)
const skuJob = ref<any>(null)
const flowBusy = ref(false)
const flowMessage = ref('')

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  if (action === '合并档案') {
    openMerge(row)
    return
  }
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function openMerge(row: EntryRow) {
  mergeRow.value = row
  mergeKeeper.value = ''
  mergeError.value = ''
}

async function confirmMerge() {
  if (!mergeRow.value || !mergeKeeper.value.trim()) return
  flowBusy.value = true
  mergeError.value = ''
  try {
    const result = await mergeGoods(mergeRow.value.id, mergeKeeper.value.trim())
    if (!result.ok) throw new Error(result.message || '合并失败')
    receipt.value = result.receipt || null
    if (receipt.value && !receiptCode.value) receiptCode.value = String(receipt.value.queried_code || '')
    mergeRow.value = null
    flowMessage.value = result.message || '合并完成'
    await reload()
  } catch (error) {
    mergeError.value = error instanceof Error ? error.message : '合并失败'
  } finally {
    flowBusy.value = false
  }
}

async function fetchReceipt(target?: string | number, specOverride?: string) {
  const code = String(target ?? receiptCode.value ?? '').trim()
  if (!code) {
    errorMessage.value = '请输入旧编号或档案 ID'
    return
  }
  const spec = specOverride === undefined ? receiptSpec.value : specOverride
  flowBusy.value = true
  errorMessage.value = ''
  try {
    receipt.value = await getGoodsReceipt(code, spec)
    if (!receipt.value.ok) throw new Error(receipt.value.message || '查验失败')
  } catch (error) {
    receipt.value = null
    errorMessage.value = error instanceof Error ? error.message : '查验失败'
  } finally {
    flowBusy.value = false
  }
}

async function seedScenario() {
  flowBusy.value = true
  errorMessage.value = ''
  try {
    const result = await seedMergeScenario()
    if (!result.ok) throw new Error(result.message || '场景构造失败')
    flowMessage.value = result.message
    receiptCode.value = 'A'
    receiptSpec.value = '500g'
    if (result.match?.id) skuMatchId.value = result.match.id
    await reload()
    receipt.value = await getGoodsReceipt('A', '500g')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '场景构造失败'
  } finally {
    flowBusy.value = false
  }
}

async function checkSku(row?: EntryRow) {
  const id = row?.id ?? skuMatchId.value
  if (id === '' || id === null || id === undefined) {
    errorMessage.value = '请填写匹配记录 ID'
    return
  }
  if (row) skuMatchId.value = row.id
  const spec = row ? String(row['归一规格'] || row['候选规格'] || '') : skuSpec.value
  flowBusy.value = true
  try {
    skuResult.value = await getSkuBaseline(id, spec)
    if (!skuResult.value.ok) throw new Error(skuResult.value.message || '基线读取失败')
  } catch (error) {
    skuResult.value = null
    errorMessage.value = error instanceof Error ? error.message : '基线读取失败'
  } finally {
    flowBusy.value = false
  }
}

async function recomputeSku(row?: EntryRow) {
  const id = row?.id ?? skuMatchId.value
  if (id === '' || id === null || id === undefined) {
    errorMessage.value = '请填写匹配记录 ID'
    return
  }
  if (row) skuMatchId.value = row.id
  flowBusy.value = true
  try {
    skuResult.value = await recomputeSkuMatch(id)
    if (!skuResult.value.ok) throw new Error(skuResult.value.message || '复算失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复算失败'
  } finally {
    flowBusy.value = false
  }
}

async function startRecalc() {
  if (skuMatchId.value === '') return
  flowBusy.value = true
  try {
    const result = await startSkuRecalculation(skuMatchId.value, skuSpec.value)
    if (!result.ok) throw new Error(result.message || '重算启动失败')
    skuJob.value = result.job
    flowMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算启动失败'
  } finally {
    flowBusy.value = false
  }
}

async function continueRecalc(strategy: 'original' | 'latest') {
  if (!skuJob.value?.job_id) {
    errorMessage.value = '请先开始一笔重算'
    return
  }
  flowBusy.value = true
  try {
    const result = await continueSkuRecalculation(skuJob.value.job_id, strategy)
    if (!result.ok) throw new Error(result.message || '继续重算失败')
    skuJob.value = result.job
    skuResult.value = result
    flowMessage.value = result.job?.outcome || result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '继续重算失败'
  } finally {
    flowBusy.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.flow-desk {
  background: #fff;
  border: 1px solid var(--border, #e4e7ec);
  border-radius: 12px;
  padding: 16px;
  margin: 14px 0;
}
.flow-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.flow-head h3,
.receipt-card h4 {
  margin: 0;
}
.flow-message {
  color: #475467;
  font-size: 13px;
  margin: 8px 0;
}
.receipt-card {
  border-top: 1px dashed #d0d5dd;
  margin-top: 12px;
  padding-top: 12px;
}
.receipt-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
}
.receipt-no {
  color: #667085;
  font-size: 12px;
}
.badge {
  background: #ecfdf3;
  color: #027a48;
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 12px;
  white-space: nowrap;
}
.chain-line {
  font-size: 13px;
  color: #344054;
}
.basis-box,
.basis-grid > div {
  background: #fffaeb;
  border: 1px solid #fedf89;
  border-radius: 10px;
  padding: 12px;
  margin: 10px 0;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.basis-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.basis-grid > div {
  flex-direction: column;
  align-items: flex-start;
  margin: 0;
}
.baseline-price {
  color: #b54708;
  font-size: 24px;
}
.independent-box {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 13px;
}
.independent-box i {
  font-style: normal;
  background: #f2f4f7;
  border-radius: 6px;
  padding: 3px 8px;
}
.baseline-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.baseline-list li {
  display: flex;
  gap: 12px;
  border-top: 1px dashed #e4e7ec;
  padding-top: 6px;
}
.baseline-list em {
  color: #667085;
  font-style: normal;
}
.job-box {
  background: #eff8ff;
  border: 1px solid #b2ddff;
  border-radius: 10px;
  padding: 10px 12px;
  margin: 10px 0;
}
.basis-json {
  max-height: 260px;
  overflow: auto;
  background: #101828;
  color: #d0d5dd;
  border-radius: 8px;
  padding: 10px;
  font-size: 12px;
}
</style>
