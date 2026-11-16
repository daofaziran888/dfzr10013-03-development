<template>
  <section class="page" data-module="funnel">
    <header class="page-head">
      <div>
        <h2>事件驱动漏斗转化分析</h2>
        <p class="page-desc">
          对带商机编号与发生时间的阶段事件去重、还原多轮漏斗路径，按入组周期重建队列转化与停留时长数据集，
          供经营预测模型跨模块消费。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="rebuild">批量重建选中队列</button>
        <button class="btn" type="button" @click="loadAll">刷新数据</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="infoMessage" class="info-text">{{ infoMessage }}</p>

    <!-- 1. 多选入组周期 + 各自观测截止点，一次提交批量重建 -->
    <div class="panel">
      <h3>① 多选入组周期批量重建（每条队列独立固定观测截止点）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th><th>入组周期</th><th>观测截止时间</th>
            <th>有效样本量</th><th>版本</th><th>事件水位时间</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in cohortRows" :key="row.period">
            <td><input v-model="row.selected" type="checkbox" /></td>
            <td>{{ row.period }}</td>
            <td><input v-model="row.cutoff" type="text" placeholder="YYYY-MM-DDTHH:mm:ssZ" /></td>
            <td>{{ row.sample ?? '—' }}</td>
            <td>{{ row.version ?? '—' }}</td>
            <td>{{ row.watermark ?? '—' }}</td>
            <td>
              <span v-if="row.stale" class="tag warn">待重建（迟到更正）</span>
              <span v-else-if="row.sample !== null" class="tag ok">已生成</span>
              <span v-else class="tag">未重建</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="hint">周期之间成员集合严格隔离：缺少阶段事件的周期返回空成员，不会借用其它周期的成员。</p>
      <div v-if="rebuildResults.length" class="result-box">
        <div v-for="r in rebuildResults" :key="r.入组周期" :class="['result-line', r.ok ? 'ok' : 'bad']">
          <strong>{{ r.入组周期 }}</strong>（{{ r.观测截止时间 || '—' }}）：{{ r.message }}
          <template v-if="r.ok">
            有效样本量 {{ r.有效样本量 }}，被排除事件 {{ r.被排除事件数 }}，截止点之外 {{ r.截止点之外事件数 }}
          </template>
        </div>
      </div>
    </div>

    <!-- 2. 事件库与去重审计 -->
    <div class="panel">
      <h3>② 阶段事件库（重复事件只计一次，接收顺序颠倒按发生时间裁决）</h3>
      <form class="filter-bar" @submit.prevent="loadEvents">
        <label class="filter-item">
          <span>商机编号</span>
          <input v-model="eventFilter.opportunity_id" placeholder="按商机编号过滤" />
        </label>
        <label class="filter-item">
          <span>事件类型</span>
          <input v-model="eventFilter.event_type" placeholder="进入/退出/成交/流失/重新开启" />
        </label>
        <button class="btn" type="submit">查询事件</button>
      </form>
      <div class="ingest-box">
        <h4>接入新事件（迟到事件会标记受影响队列待重建）</h4>
        <div class="ingest-grid">
          <input v-model="ingestForm.事件编号" placeholder="事件编号" />
          <input v-model="ingestForm.商机编号" placeholder="商机编号" />
          <select v-model="ingestForm.事件类型">
            <option v-for="t in eventTypes" :key="t" :value="t">{{ t }}</option>
          </select>
          <select v-model="ingestForm.阶段">
            <option value="">（终止/重开不填阶段）</option>
            <option v-for="s in stages" :key="s" :value="s">{{ s }}</option>
          </select>
          <input v-model="ingestForm.发生时间" placeholder="发生时间 2026-09-01T09:00:00Z" />
          <input v-model="ingestForm.接收时间" placeholder="接收时间（可晚于发生时间）" />
          <button class="btn primary" type="button" @click="ingestEvent">接入事件</button>
        </div>
      </div>
      <table class="data-table compact">
        <thead>
          <tr><th>事件编号</th><th>商机编号</th><th>类型</th><th>阶段</th><th>发生时间</th><th>接收时间</th><th>迟到</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in events" :key="String(e.事件编号)">
            <td>{{ e.事件编号 }}</td><td>{{ e.商机编号 }}</td><td>{{ e.事件类型 }}</td>
            <td>{{ e.阶段 ?? '—' }}</td><td>{{ e.发生时间 }}</td><td>{{ e.接收时间 }}</td>
            <td><span v-if="e.迟到" class="tag warn">迟到</span><span v-else>—</span></td>
          </tr>
          <tr v-if="!events.length"><td colspan="7" class="empty-state">暂无事件</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 3. 数据集：分阶段指标 + 对账 + 排除事件 -->
    <div class="panel" v-if="activeDataset">
      <h3>③ 队列转化分析数据集：{{ activeDataset.入组周期 }} ｜ 截止 {{ activeDataset.观测截止时间 }}</h3>
      <div class="reconcile">
        <span v-for="(v, k) in activeDataset.对账" :key="k" class="reconcile-item">
          <em>{{ k }}</em><strong>{{ v }}</strong>
        </span>
      </div>
      <h4>分阶段指标（多轮经历按阶段周期分别计数，未知停留时长不补零）</h4>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>阶段</th><th>商机数(去重)</th><th>阶段周期数(含多轮)</th><th>有效转化数</th>
            <th>转化率</th><th>流失周期</th><th>未结束(右删失)</th><th>平均停留秒</th><th>停留样本量</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in activeDataset.分阶段指标" :key="m.阶段">
            <td>{{ m.阶段 }}</td>
            <td>{{ m['商机数（去重成员）'] }}</td>
            <td>{{ m['阶段周期数（含多轮）'] }}</td>
            <td>{{ m.有效转化数 }}</td>
            <td>{{ m.转化率 ?? '—' }}</td>
            <td>{{ m.流失周期数 }}</td>
            <td>{{ m['未结束周期数（右删失）'] }}</td>
            <td>{{ m.停留时长.平均停留秒 ?? '未知（不补零）' }}</td>
            <td>{{ m.停留时长.样本量 }}</td>
          </tr>
        </tbody>
      </table>

      <h4>成员与多轮经历（重开商机不重复计为新客户，流失经历保留）</h4>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>商机编号</th><th>经历轮次</th><th>阶段周期数</th>
            <th>历史流失</th><th>历史成交</th><th>截止点未结束</th><th>路径</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="mem in activeDataset.成员" :key="mem.商机编号">
            <td>{{ mem.商机编号 }}</td><td>{{ mem.经历轮次 }}</td><td>{{ mem.阶段周期数 }}</td>
            <td>{{ mem.历史流失 ? '是' : '否' }}</td>
            <td>{{ mem.历史成交 ? '是' : '否' }}</td>
            <td>{{ mem.截止点未结束 ? '观察期不足' : '已结束' }}</td>
            <td><button class="link" type="button" @click="showPath(mem.商机编号)">查看路径</button></td>
          </tr>
        </tbody>
      </table>

      <h4>被排除事件（逐条返回原因，同刻矛盾不靠数组次序裁决）</h4>
      <table class="data-table compact">
        <thead><tr><th>事件编号</th><th>商机编号</th><th>类型</th><th>发生时间</th><th>排除原因</th></tr></thead>
        <tbody>
          <tr v-for="ex in activeDataset.被排除事件" :key="ex.事件编号">
            <td>{{ ex.事件编号 }}</td><td>{{ ex.商机编号 }}</td><td>{{ ex.事件类型 }}</td>
            <td>{{ ex.发生时间 }}</td><td>{{ ex.排除原因 }}</td>
          </tr>
          <tr v-if="!activeDataset.被排除事件.length"><td colspan="5" class="empty-state">无被排除事件</td></tr>
        </tbody>
      </table>

      <div v-if="currentPath" class="path-box">
        <h4>路径明细：{{ currentPath.商机编号 }}（轮次 {{ currentPath.经历轮次 }}，
          历史流失 {{ currentPath.历史流失 ? '是' : '否' }}）</h4>
        <table class="data-table compact">
          <thead><tr><th>轮次</th><th>阶段</th><th>进入</th><th>退出</th><th>关闭方式</th><th>停留秒</th><th>跨日</th><th>右删失</th><th>有效转化</th></tr></thead>
          <tbody>
            <tr v-for="(v, i) in currentPath.阶段周期" :key="i">
              <td>{{ v.轮次 }}</td><td>{{ v.阶段 }}</td><td>{{ v.进入时间 }}</td><td>{{ v.退出时间 ?? '—' }}</td>
              <td>{{ v.关闭方式 ?? '未结束' }}</td>
              <td>{{ v.停留秒 ?? '未知' }}</td><td>{{ v.跨日 ? '是' : '否' }}</td>
              <td>{{ v.右删失 ? '是' : '否' }}</td><td>{{ v.有效转化 ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="consume-box">
        <h4>④ 经营预测模型跨模块消费（固化周期、截止点、有效样本量）</h4>
        <div class="ingest-grid">
          <input v-model="consumeForm.任务编号" placeholder="预测任务编号，如 FC-2026-09" />
          <button class="btn primary" type="button" @click="consume">消费当前数据集</button>
        </div>
        <table v-if="consumptions.length" class="data-table compact">
          <thead><tr><th>记录编号</th><th>任务编号</th><th>入组周期</th><th>截止时间</th><th>有效样本量</th><th>右删失</th><th>数据集待重建</th></tr></thead>
          <tbody>
            <tr v-for="c in consumptions" :key="String(c.记录编号)">
              <td>{{ c.记录编号 }}</td><td>{{ c.任务编号 }}</td><td>{{ c.入组周期 }}</td>
              <td>{{ c.观测截止时间 }}</td><td>{{ c.有效样本量 }}</td><td>{{ c.右删失样本量 }}</td>
              <td>{{ c.数据集待重建 ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/funnel'
const stages = ['线索', '商机确认', '方案报价', '商务谈判', '成交']
const eventTypes = ['进入', '退出', '成交', '流失', '重新开启']
const DEFAULT_PERIODS = [
  { period: '2026-08', cutoff: '2026-08-31T23:59:59Z' },
  { period: '2026-09', cutoff: '2026-09-30T23:59:59Z' },
  { period: '2026-10', cutoff: '2026-10-31T23:59:59Z' },
]

const errorMessage = ref('')
const infoMessage = ref('')
const events = ref<Row[]>([])
const datasets = ref<Row[]>([])
const activeDataset = ref<Record<string, any> | null>(null)
const currentPath = ref<Record<string, any> | null>(null)
const rebuildResults = ref<Record<string, any>[]>([])
const consumptions = ref<Row[]>([])
const eventFilter = reactive({ opportunity_id: '', event_type: '' })
const ingestForm = reactive({
  事件编号: '', 商机编号: '', 事件类型: '进入', 阶段: '线索',
  发生时间: '', 接收时间: '',
})
const consumeForm = reactive({ 任务编号: '' })

const cohortRows = reactive(
  DEFAULT_PERIODS.map((item) => ({ ...item, selected: item.period === '2026-09', sample: null as number | null, version: null as number | null, watermark: null as string | null, stale: false })),
)

const stats = computed(() => [
  { label: '事件总数', value: events.value.length },
  { label: '已生成数据集', value: datasets.value.length },
  { label: '当前队列有效样本量', value: activeDataset.value?.有效样本量 ?? '—' },
  { label: '当前队列右删失', value: activeDataset.value?.对账?.['观察期不足商机数（右删失，未当最终流失）'] ?? '—' },
])

async function loadAll() {
  await Promise.all([loadEvents(), loadDatasets(), loadConsumptions()])
}

async function loadEvents() {
  errorMessage.value = ''
  const query = new URLSearchParams(
    Object.entries(eventFilter).filter(([, v]) => v) as [string, string][],
  ).toString()
  const response = await request(`${ENDPOINT}/events?${query}`)
  if (!response.ok) {
    errorMessage.value = '事件库读取失败'
    return
  }
  const payload = await response.json()
  events.value = payload.items ?? []
}

async function loadDatasets() {
  const response = await request(`${ENDPOINT}/cohorts/datasets`)
  if (!response.ok) return
  const payload = await response.json()
  datasets.value = payload.items ?? []
  for (const row of cohortRows) {
    const match = datasets.value.find((d) => d.入组周期 === row.period && d.观测截止时间 === row.cutoff)
    row.sample = match ? Number(match.有效样本量) : null
    row.version = match ? Number(match.版本) : null
    row.watermark = match ? String(match.事件水位时间) : null
    row.stale = Boolean(match?.待重建)
  }
  if (!activeDataset.value && datasets.value.length) {
    await openDataset(datasets.value[0].入组周期 as string, datasets.value[0].观测截止时间 as string)
  }
}

async function rebuild() {
  errorMessage.value = ''
  infoMessage.value = ''
  const cohorts = cohortRows.filter((r) => r.selected).map((r) => ({ 入组周期: r.period, 观测截止时间: r.cutoff }))
  if (!cohorts.length) {
    errorMessage.value = '请至少勾选一个入组周期'
    return
  }
  const response = await request(`${ENDPOINT}/cohorts/rebuild`, {
    method: 'POST',
    body: JSON.stringify({ cohorts }),
  })
  const payload = await response.json()
  rebuildResults.value = payload.results ?? []
  infoMessage.value = payload.message
  await loadDatasets()
}

async function openDataset(period: string, cutoff: string) {
  const response = await request(`${ENDPOINT}/cohorts/${period}/datasets/${encodeURIComponent(cutoff)}`)
  if (!response.ok) {
    errorMessage.value = '数据集读取失败，请先重建该队列'
    activeDataset.value = null
    return
  }
  activeDataset.value = await response.json()
  currentPath.value = null
}

async function showPath(opportunityId: string) {
  if (!activeDataset.value) return
  const { 入组周期, 观测截止时间 } = activeDataset.value
  const response = await request(
    `${ENDPOINT}/cohorts/${入组周期}/datasets/${encodeURIComponent(观测截止时间)}/paths/${opportunityId}`,
  )
  if (response.ok) currentPath.value = await response.json()
}

async function ingestEvent() {
  errorMessage.value = ''
  const values: Record<string, string> = {
    事件编号: ingestForm.事件编号,
    商机编号: ingestForm.商机编号,
    事件类型: ingestForm.事件类型,
    发生时间: ingestForm.发生时间,
  }
  if (ingestForm.阶段) values.阶段 = ingestForm.阶段
  if (ingestForm.接收时间) values.接收时间 = ingestForm.接收时间
  const response = await request(`${ENDPOINT}/events`, { method: 'POST', body: JSON.stringify({ values }) })
  const payload = await response.json()
  infoMessage.value = payload.message
  await loadEvents()
  await loadDatasets()
}

async function consume() {
  if (!activeDataset.value) return
  errorMessage.value = ''
  const response = await request('/api/forecast/funnel-dataset/consume', {
    method: 'POST',
    body: JSON.stringify({
      values: {
        入组周期: activeDataset.value.入组周期,
        观测截止时间: activeDataset.value.观测截止时间,
        任务编号: consumeForm.任务编号 || `FC-${activeDataset.value.入组周期}`,
      },
    }),
  })
  const payload = await response.json()
  if (!payload.ok) errorMessage.value = payload.message
  else infoMessage.value = payload.message
  await loadConsumptions()
}

async function loadConsumptions() {
  const response = await request(`${ENDPOINT}/forecast/consumptions`)
  if (response.ok) {
    const payload = await response.json()
    consumptions.value = payload.items ?? []
  }
}

onMounted(loadAll)
</script>

<style scoped>
.panel {
  background: var(--card-bg, #fff);
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 18px;
}
.panel h3 { margin: 0 0 12px; font-size: 16px; }
.panel h4 { margin: 16px 0 8px; font-size: 14px; color: #374151; }
.hint { color: #6b7280; font-size: 12px; margin: 8px 0 0; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #f3f4f6; font-size: 12px; }
.tag.warn { background: #fef3c7; color: #92400e; }
.tag.ok { background: #d1fae5; color: #065f46; }
.result-box { margin-top: 12px; }
.result-line { padding: 6px 10px; border-radius: 6px; margin-bottom: 6px; font-size: 13px; }
.result-line.ok { background: #ecfdf5; }
.result-line.bad { background: #fef2f2; }
.ingest-box, .consume-box, .path-box {
  border: 1px dashed #d1d5db; border-radius: 8px; padding: 12px; margin: 12px 0;
}
.ingest-grid { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.ingest-grid input, .ingest-grid select { min-width: 180px; padding: 6px 8px; }
.reconcile { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }
.reconcile-item {
  display: flex; flex-direction: column; background: #f9fafb; border-radius: 8px;
  padding: 8px 12px; min-width: 130px;
}
.reconcile-item em { font-style: normal; font-size: 12px; color: #6b7280; }
.reconcile-item strong { font-size: 18px; margin-top: 2px; }
.compact :deep(td), .compact :deep(th) { font-size: 12px; padding: 6px 8px; }
.info-text { color: #065f46; }
</style>
