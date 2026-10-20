<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>{{ greet }}，{{ auth.user?.name }}</h1>
        <p>{{ todayText }}　齐套数据 {{ calcText }}</p>
      </div>
      <div class="toolbar">
        <el-button @click="recalc" :loading="busy"><el-icon><Refresh /></el-icon>重新计算齐套</el-button>
      </div>
    </div>

    <div class="board-row" v-if="d">
      <router-link to="/kitting" class="tile">
        <div class="lbl">生产中订单</div>
        <div class="big num">{{ d.orders.active }}</div>
        <div class="bar">
          <span :style="{ flexGrow: d.orders.green }" class="g" />
          <span :style="{ flexGrow: d.orders.yellow }" class="y" />
          <span :style="{ flexGrow: d.orders.red }" class="r" />
        </div>
        <div class="legend"><span><i class="dot GREEN" />已齐 {{ d.orders.green }}</span><span><i class="dot YELLOW" />在途可齐 {{ d.orders.yellow }}</span><span><i class="dot RED" />有风险 {{ d.orders.red }}</span></div>
      </router-link>
      <router-link to="/orders" class="tile">
        <div class="lbl">交期</div>
        <div class="pair"><div><b class="num short">{{ d.orders.overdue }}</b><span>已逾期</span></div><div><b class="num">{{ d.orders.due_7d }}</b><span>7 天内到期</span></div><div><b class="num">{{ d.orders.assembling }}</b><span>装配中</span></div></div>
      </router-link>
      <div class="tile todo">
        <div class="lbl">待我处理</div>
        <router-link v-for="t in todos" :key="t.k" :to="t.to" class="todo-i" v-show="d.todo[t.k] > 0">
          <span>{{ t.label }}</span><b class="num">{{ d.todo[t.k] }}</b>
        </router-link>
        <div v-if="!todos.some((t) => d.todo[t.k] > 0)" class="muted small">暂无待办</div>
      </div>
    </div>

    <div class="grid-2" style="margin-top: 16px">
      <div class="panel">
        <h2>缺料清单<small>按最早需求日排序</small></h2>
        <el-table :data="d?.shortages || []" size="small" empty-text="当前没有缺料">
          <el-table-column label="物料" min-width="180">
            <template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template>
          </el-table-column>
          <el-table-column label="类型" width="60"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
          <el-table-column label="缺口" width="80" align="right"><template #default="{ row }"><b class="num short">{{ num(row.short_qty) }}</b></template></el-table-column>
          <el-table-column label="需求日" width="100"><template #default="{ row }"><span class="num">{{ row.need_date }}</span></template></el-table-column>
          <el-table-column label="影响订单" min-width="140"><template #default="{ row }">{{ row.orders.map((o) => o.order_no).join('、') }}</template></el-table-column>
        </el-table>
        <div class="toolbar" style="margin-top: 10px" v-if="d?.shortages?.length && auth.can('BUYER', 'PLANNER')">
          <el-button type="primary" plain @click="toReq">缺料一键生成请购单</el-button>
        </div>
      </div>
      <div class="panel">
        <h2>最新预警</h2>
        <div v-for="a in alerts" :key="a.id" class="alert" :class="a.level">
          <div>{{ a.title }}</div>
          <div class="muted small">{{ a.detail }}</div>
        </div>
        <router-link to="/alerts" class="small">查看全部预警</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import { MAT_TYPE, num } from '../utils'

defineOptions({ name: 'Dashboard' })
const auth = useAuth()
const router = useRouter()
const d = ref(null)
const alerts = ref([])
const busy = ref(false)
const calcText = ref('')
const h = new Date().getHours()
const greet = h < 11 ? '早上好' : h < 14 ? '中午好' : h < 18 ? '下午好' : '晚上好'
const todayText = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })
const todos = [
  { k: 'pending_inspection', label: '到货待检', to: '/arrivals' },
  { k: 'exceptions_open', label: '来料异常未关闭', to: '/exceptions' },
  { k: 'po_draft', label: '采购单待审核', to: '/purchase-orders' },
  { k: 'po_lines_unconfirmed', label: '采购行供应商未回复交期', to: '/tracking' },
  { k: 'shipments_pending', label: '发货计划待审批', to: '/shipments' },
  { k: 'alerts_critical', label: '严重预警', to: '/alerts' },
]

async function load() {
  d.value = await get('/dashboard')
  alerts.value = (await get('/alerts', { size: 8 })).items
  const ov = await get('/kitting/overview')
  const t = ov.items[0]?.kit_calc_at
  calcText.value = t ? `更新于 ${String(t).slice(11, 16)}` : '尚未计算'
}
async function recalc() {
  busy.value = true
  try { await post('/alerts-scan'); await load(); ElMessage.success('已重新计算') } finally { busy.value = false }
}
async function toReq() {
  const r = await post('/kitting/shortages/to-requisition')
  const extra = r.make_suggestions.length + r.outsource_suggestions.length
  await ElMessageBox.alert(
    (r.requisition_no ? `已生成请购单 ${r.requisition_no}（外购件）。` : '') +
    (extra ? `另有 ${r.make_suggestions.length} 项自制件、${r.outsource_suggestions.length} 项外协件缺口，请在【自制任务】【外协单】中安排。` : ''),
    '已处理缺料', { confirmButtonText: r.requisition_id ? '打开请购单' : '知道了' })
  if (r.requisition_id) router.push('/requisitions')
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.board-row { display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 16px; }
@media (max-width: 1100px) { .board-row { grid-template-columns: 1fr; } }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 16px 18px; color: var(--ink); display: block; }
.tile:hover { text-decoration: none; border-color: var(--primary-soft); }
.lbl { color: var(--ink-3); font-size: 13px; }
.big { font-family: var(--num-c); font-size: 48px; font-weight: 700; line-height: 1.1; margin: 4px 0 10px; }
.bar { display: flex; height: 8px; gap: 2px; }
.bar span { flex-basis: 0; }
.bar .g { background: var(--ok); } .bar .y { background: var(--wait); } .bar .r { background: var(--short); }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--ink-2); margin-top: 8px; }
.pair { display: flex; gap: 28px; margin-top: 10px; }
.pair div { display: flex; flex-direction: column; }
.pair b { font-family: var(--num-c); font-size: 40px; font-weight: 700; line-height: 1.1; }
.pair span { font-size: 12px; color: var(--ink-3); }
.todo-i { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed var(--line-soft); color: var(--ink); font-size: 13px; }
.todo-i b { font-size: 16px; color: var(--primary); }
.alert { padding: 8px 10px; border-left: 3px solid var(--wait); background: #fafbfc; margin-bottom: 6px; font-size: 13px; }
.alert.CRITICAL { border-left-color: var(--short); }
.alert.INFO { border-left-color: var(--line); }
</style>
