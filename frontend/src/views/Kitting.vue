<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>齐套中心</h1>
        <p>按冲填顺序排列：已下达优先 → 人工置顶 → 优先级 → 需求日。在途到货需早于需求日 {{ buffer }} 天才算“在途可齐”。</p>
      </div>
      <div class="toolbar">
        <el-radio-group v-model="color" size="small">
          <el-radio-button value="">全部 {{ items.length }}</el-radio-button>
          <el-radio-button value="RED">有风险 {{ summary.RED || 0 }}</el-radio-button>
          <el-radio-button value="YELLOW">在途可齐 {{ summary.YELLOW || 0 }}</el-radio-button>
          <el-radio-button value="GREEN">已齐 {{ summary.GREEN || 0 }}</el-radio-button>
        </el-radio-group>
        <el-checkbox v-model="soon">只看 {{ buffer }} 天内可齐套</el-checkbox>
        <el-button @click="recalc" :loading="busy"><el-icon><Refresh /></el-icon>重新计算</el-button>
      </div>
    </div>

    <div class="panel">
      <el-table :data="rows" @row-click="(r) => $router.push(`/orders/${r.id}`)" row-class-name="clickable" v-loading="loading">
        <el-table-column label="顺序" width="56" align="center"><template #default="{ row }"><span class="num muted">{{ row.seq }}</span></template></el-table-column>
        <el-table-column label="订单" min-width="200">
          <template #default="{ row }">
            <div><span class="no">{{ row.order_no }}</span>
              <el-tag v-if="row.priority <= 2" size="small" :type="row.priority === 1 ? 'danger' : 'warning'" effect="plain" style="margin-left: 6px">{{ PRIORITY[row.priority] }}</el-tag>
              <el-tag v-if="row.priority_pin > 0" size="small" effect="plain" style="margin-left: 4px">置顶</el-tag>
            </div>
            <div class="muted small">{{ row.customer }}，{{ row.product_name }} × {{ num(row.qty) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80"><template #default="{ row }"><StatusTag :map="ORDER_STATUS" :value="row.status" /></template></el-table-column>
        <el-table-column label="物料齐套" min-width="230">
          <template #default="{ row }">
            <KitStrip :lines="row.strip" />
            <div class="small" style="margin-top: 4px"><span class="dot" :class="row.kit_status" />{{ row.kit_label }}　<span class="num">{{ pct(row.kit_rate) }}</span> 品项已齐</div>
          </template>
        </el-table-column>
        <el-table-column label="需求日" width="104"><template #default="{ row }"><span class="num">{{ row.need_date }}</span><div class="muted small">{{ relDay(row.need_date) }}</div></template></el-table-column>
        <el-table-column label="预计齐套" width="110">
          <template #default="{ row }">
            <template v-if="row.kit_ready_date"><span class="num" :class="{ late: row.kit_ready_date > row.need_date }">{{ row.kit_ready_date }}</span>
              <div v-if="row.ready_in_2_days" class="small ok">{{ buffer }} 天内可齐</div></template>
            <span v-else class="short small">缺料，无法确定</span>
          </template>
        </el-table-column>
        <el-table-column label="交期" width="104"><template #default="{ row }"><span class="num" :class="{ short: row.due_date < todayStr }">{{ row.due_date }}</span></template></el-table-column>
        <el-table-column label="" width="150" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="simulate(row)">提为最急模拟</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="simOpen" :title="`模拟：把 ${simTarget?.order_no} 提为最高优先级`" width="720px">
      <p class="muted small">不会保存。确认后可在订单详情里“置顶”或“锁定预占”使其生效。</p>
      <el-table :data="sim" size="small" empty-text="对其他订单没有影响">
        <el-table-column label="订单" width="150"><template #default="{ row }"><span class="no">{{ row.order_no }}</span><el-tag v-if="row.is_target" size="small" style="margin-left: 4px">目标</el-tag></template></el-table-column>
        <el-table-column prop="customer" label="客户" />
        <el-table-column label="齐套状态"><template #default="{ row }"><span class="dot" :class="row.before" />{{ KIT[row.before]?.label }} → <span class="dot" :class="row.after" />{{ KIT[row.after]?.label }}</template></el-table-column>
        <el-table-column label="品项齐套率" width="120"><template #default="{ row }"><span class="num">{{ pct(row.before_rate) }} → {{ pct(row.after_rate) }}</span></template></el-table-column>
        <el-table-column label="预计齐套日" width="190"><template #default="{ row }"><span class="num">{{ row.before_ready || '—' }} → {{ row.after_ready || '—' }}</span></template></el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="simOpen = false">关闭</el-button>
        <el-button v-if="auth.can('PLANNER')" type="primary" @click="applyPin">置顶该订单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import KitStrip from '../components/KitStrip.vue'
import StatusTag from '../components/StatusTag.vue'
import { KIT, ORDER_STATUS, PRIORITY, num, pct, relDay, today } from '../utils'

defineOptions({ name: 'Kitting' })
const auth = useAuth()
const items = ref([])
const summary = ref({})
const buffer = ref(2)
const color = ref('')
const soon = ref(false)
const loading = ref(false)
const busy = ref(false)
const todayStr = today()
const rows = computed(() => items.value.filter((r) => (!color.value || r.kit_color === color.value) && (!soon.value || r.ready_in_2_days)))

async function load() {
  loading.value = true
  try {
    const r = await get('/kitting/overview')
    items.value = r.items; summary.value = r.summary; buffer.value = r.buffer_days
  } finally { loading.value = false }
}
async function recalc() {
  busy.value = true
  try { await post('/kitting/recalc'); await load(); ElMessage.success('齐套已重新计算') } finally { busy.value = false }
}
const simOpen = ref(false)
const sim = ref([])
const simTarget = ref(null)
async function simulate(row) {
  simTarget.value = row
  sim.value = await get(`/orders/${row.id}/simulate-top`)
  simOpen.value = true
}
async function applyPin() {
  await put(`/orders/${simTarget.value.id}`, { priority_pin: 1 })
  simOpen.value = false
  ElMessage.success('已置顶，齐套已按新顺序重算')
  load()
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
:deep(.clickable) { cursor: pointer; }
</style>
