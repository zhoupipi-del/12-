<template>
  <div class="page">
    <div class="page-head">
      <div><h1>订单备料工作台</h1><p>一张表核对全层级物料、库存占用、在途交期和历史采购，再分别安排采购、自制或外协。</p></div>
      <div class="toolbar">
        <el-select v-model="orderId" filterable placeholder="选择整机订单" style="width: 340px" @change="load">
          <el-option v-for="o in orders" :key="o.id" :value="o.id" :label="o.label" />
        </el-select>
        <el-button @click="load" :loading="loading"><el-icon><Refresh /></el-icon>刷新</el-button>
      </div>
    </div>

    <div v-if="plan.order" class="panel summary">
      <div><span>订单</span><b class="no">{{ plan.order.order_no }}</b></div>
      <div><span>客户/机型</span><b>{{ plan.order.customer }} · {{ plan.order.product_name }}</b></div>
      <div><span>计划开工</span><b class="num">{{ plan.order.planned_start || '按交期推算' }}</b></div>
      <div><span>齐套</span><b><span class="dot" :class="plan.order.kit_status" />{{ plan.order.kit_label }}</b></div>
    </div>

    <div class="panel" v-if="plan.order">
      <el-table :data="plan.lines" row-key="id" v-loading="loading" :row-class-name="({ row }) => 'lv' + row.level">
        <el-table-column label="物料" min-width="235">
          <template #default="{ row }"><span :style="{ paddingLeft: (row.level - 1) * 16 + 'px' }"><span class="no">{{ row.code }}</span> {{ row.name }}</span><el-tag v-if="row.is_common" size="small" effect="plain" style="margin-left:5px">通用件</el-tag></template>
        </el-table-column>
        <el-table-column label="类型" width="62"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
        <el-table-column label="需求/已领" width="100" align="right"><template #default="{ row }"><span class="num">{{ num(row.required) }} / {{ num(row.issued) }}</span></template></el-table-column>
        <el-table-column label="现存/本单/他单/空闲" width="180" align="right"><template #default="{ row }"><span class="num">{{ num(row.stock_total) }} / {{ num(row.own_reserved) }} / {{ num(row.occupied_by_others) }} / {{ num(row.stock_free) }}</span></template></el-table-column>
        <el-table-column label="本单供应" min-width="190">
          <template #default="{ row }">
            <span v-for="(s, i) in row.supplies" :key="i" class="src">{{ SOURCE[s.type] || s.type }} {{ num(s.qty) }}<template v-if="s.date"> @{{ s.date }}</template></span>
            <span v-if="row.from_children > 0" class="src">下级可制 {{ num(row.from_children) }}</span>
            <span v-if="!row.supplies.length && !row.from_children" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="需求/预计可用" width="115"><template #default="{ row }"><div class="num small">{{ row.need_date }}</div><div class="num small" :class="row.status === 'INCOMING_LATE' ? 'short' : 'muted'">{{ row.expected_date || '未确定' }}</div></template></el-table-column>
        <el-table-column label="历史供应商/价格" min-width="175">
          <template #default="{ row }"><div v-for="h in row.purchase_history" :key="`${h.supplier_id}-${h.price}`" class="small">{{ h.supplier }}　<span class="num">¥{{ num(h.price) }}</span></div><span v-if="!row.purchase_history.length" class="muted small">无历史记录</span></template>
        </el-table-column>
        <el-table-column label="缺口" width="78" align="right"><template #default="{ row }"><b class="num" :class="{ short: row.short_qty > 0 }">{{ num(row.short_qty) }}</b></template></el-table-column>
        <el-table-column label="安排" width="135" fixed="right">
          <template #default="{ row }">
            <template v-if="row.short_qty > 0">
              <el-button v-if="row.type === 'BUY'" link type="primary" size="small" @click="buy(row)">生成请购</el-button>
              <el-button v-else-if="row.type === 'MAKE'" link type="primary" size="small" @click="make(row)">建自制任务</el-button>
              <el-button v-else-if="row.type === 'OUTSOURCE'" link type="primary" size="small" @click="outsource(row)">建外协单</el-button>
            </template>
            <span v-else class="ok small">已有供应</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div v-else class="panel muted">请选择一个在制订单。</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { MAT_TYPE, num } from '../utils'

defineOptions({ name: 'MaterialPlan' })
const route = useRoute()
const router = useRouter()
const orders = ref([])
const orderId = ref(null)
const plan = ref({ lines: [] })
const loading = ref(false)
const SOURCE = { STOCK: '库存', LOCKED: '锁定', PO: '采购', WO: '自制', OUTSOURCE: '外协', ARRIVAL: '待检' }

async function load() {
  if (!orderId.value) { plan.value = { lines: [] }; return }
  loading.value = true
  try { plan.value = await get(`/orders/${orderId.value}/material-plan`) } finally { loading.value = false }
}
async function buy(row) {
  const h = row.purchase_history[0]
  const r = await post('/requisitions', { remark: `订单 ${plan.value.order.order_no} 备料工作台生成`, lines: [{ material_id: row.material_id, qty: row.short_qty, need_date: row.need_date, supplier_id: h?.supplier_id || null, price: h?.price ?? null, order_id: orderId.value }] })
  ElMessage.success(`已生成请购单 ${r.req_no}`)
  await load()
}
function make(row) { router.push({ path: '/work-orders', query: { order_id: orderId.value, material_id: row.material_id, qty: row.short_qty } }) }
function outsource(row) { router.push({ path: '/outsource', query: { order_id: orderId.value, material_id: row.material_id, qty: row.short_qty } }) }

onMounted(async () => {
  orders.value = await get('/orders/options')
  orderId.value = Number(route.query.order_id) || orders.value[0]?.id || null
  await load()
})
</script>

<style scoped>
.summary { display:grid; grid-template-columns: 1fr 2fr 1fr 1fr; gap:16px; }
.summary div { display:flex; flex-direction:column; gap:3px; }
.summary span { color:var(--ink-3); font-size:12px; }
:deep(.lv2 td:first-child), :deep(.lv3 td:first-child) { background:var(--bg); }
</style>
