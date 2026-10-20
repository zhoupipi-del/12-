<template>
  <div class="page">
    <div class="page-head">
      <div><h1>销售订单</h1><p>录入后选择机型和数量，下达时锁定当前生效 BOM 版本并参与齐套冲填。</p></div>
      <div class="toolbar">
        <el-input v-model="q.keyword" placeholder="单号 / 客户 / 机型" clearable style="width: 200px" @change="load" />
        <el-select v-model="q.status" placeholder="全部状态" clearable style="width: 130px" @change="load">
          <el-option v-for="(v, k) in ORDER_STATUS" :key="k" :value="k" :label="v[0]" />
        </el-select>
        <el-button v-if="auth.can('SALES', 'PLANNER')" type="primary" @click="openNew">新建订单</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list.items" v-loading="loading" @row-click="(r) => $router.push(`/orders/${r.id}`)" style="cursor: pointer">
        <el-table-column label="订单号" width="140"><template #default="{ row }"><span class="no">{{ row.order_no }}</span></template></el-table-column>
        <el-table-column prop="customer" label="客户" min-width="140" />
        <el-table-column label="机型" min-width="160"><template #default="{ row }">{{ row.product_name }}<el-tag v-if="row.is_nonstandard" size="small" effect="plain" style="margin-left: 6px">非标</el-tag></template></el-table-column>
        <el-table-column label="数量" width="70" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column label="优先级" width="70"><template #default="{ row }">{{ PRIORITY[row.priority] }}</template></el-table-column>
        <el-table-column label="状态" width="86"><template #default="{ row }"><StatusTag :map="ORDER_STATUS" :value="row.status" /></template></el-table-column>
        <el-table-column label="齐套" width="120"><template #default="{ row }"><template v-if="row.kit_status"><span class="dot" :class="row.kit_status" />{{ row.kit_label }}</template><span v-else class="muted">—</span></template></el-table-column>
        <el-table-column label="计划开工" width="104"><template #default="{ row }"><span class="num">{{ day(row.planned_start) }}</span></template></el-table-column>
        <el-table-column label="交期" width="104"><template #default="{ row }"><span class="num">{{ row.due_date }}</span></template></el-table-column>
        <el-table-column label="进度" width="110"><template #default="{ row }"><el-progress :percentage="row.progress" :stroke-width="6" /></template></el-table-column>
      </el-table>
      <el-pagination v-if="list.total > size" layout="prev, pager, next, total" :total="list.total" :page-size="size" v-model:current-page="q.page_no" @current-change="load" style="margin-top: 12px" />
    </div>

    <el-dialog v-model="open" title="新建销售订单" width="560px">
      <el-form :model="f" label-width="96px">
        <el-form-item label="客户" required><el-input v-model="f.customer" /></el-form-item>
        <el-form-item label="机型" required><MaterialSelect v-model="f.product_id" type="PRODUCT" placeholder="选择整机机型" @picked="onPick" /></el-form-item>
        <el-form-item label="数量" required><el-input-number v-model="f.qty" :min="1" /></el-form-item>
        <el-form-item label="客户交期" required><el-date-picker v-model="f.due_date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="计划开工"><el-date-picker v-model="f.planned_start" value-format="YYYY-MM-DD" placeholder="不填按交期减装配周期" /></el-form-item>
        <el-form-item label="优先级"><el-radio-group v-model="f.priority"><el-radio-button v-for="(v, k) in PRIORITY" :key="k" :value="Number(k)">{{ v }}</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="非标"><el-switch v-model="f.is_nonstandard" /></el-form-item>
        <el-form-item label="装配工位"><el-input v-model="f.station" placeholder="例如 2 号工位" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="f.remark" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <div v-if="preview.length" class="preview">
        <div class="muted small">BOM 展开预览（{{ f.qty }} 台，共 {{ preview.length }} 项一级物料）</div>
        <div v-for="p in preview" :key="p.id" class="pv"><span class="no">{{ p.code }}</span><span>{{ p.name }}</span><span class="num">{{ num(p.total) }} {{ p.unit }}</span></div>
      </div>
      <template #footer><el-button @click="open = false">取消</el-button><el-button type="primary" @click="save" :loading="saving">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { ORDER_STATUS, PRIORITY, day, num, today } from '../utils'

defineOptions({ name: 'Orders' })
const auth = useAuth()
const router = useRouter()
const size = 50
const q = ref({ keyword: '', status: '', page_no: 1, size })
const list = ref({ items: [], total: 0 })
const loading = ref(false)
async function load() {
  loading.value = true
  try { list.value = await get('/orders', q.value) } finally { loading.value = false }
}
const open = ref(false)
const saving = ref(false)
const f = ref({})
const preview = ref([])
let bomId = null
function openNew() {
  f.value = { customer: '', product_id: null, qty: 1, due_date: today(30), planned_start: null, priority: 3, is_nonstandard: false, station: '', remark: '' }
  preview.value = []
  open.value = true
}
async function onPick(m) {
  if (!m) return
  const boms = await get('/boms', { material_id: m.id })
  bomId = boms.find((b) => b.status === 'ACTIVE')?.id
  refreshPreview()
}
async function refreshPreview() {
  preview.value = bomId ? await get(`/boms/${bomId}/tree`, { qty: f.value.qty }) : []
}
watch(() => f.value.qty, () => bomId && refreshPreview())
async function save() {
  saving.value = true
  try {
    const o = await post('/orders', f.value)
    open.value = false
    router.push(`/orders/${o.id}`)
  } finally { saving.value = false }
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.preview { border-top: 1px solid var(--line-soft); padding-top: 10px; max-height: 200px; overflow: auto; }
.pv { display: grid; grid-template-columns: 140px 1fr 100px; font-size: 13px; padding: 3px 0; }
.pv .num { text-align: right; }
</style>
