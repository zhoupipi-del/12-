<template>
  <div class="page">
    <div class="page-head">
      <div><h1>采购跟单</h1><p>所有未到货采购行，按需求日排序。供应商可在门户链接里自行回复交期。</p></div>
      <div class="toolbar">
        <RefSelect v-model="supplier" kind="supplier" placeholder="按供应商" style="width: 200px" @update:model-value="load" />
        <el-checkbox v-model="onlyRisk">只看有风险</el-checkbox>
        <el-button v-if="supplier && auth.can('BUYER')" @click="portal">生成该供应商门户链接</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="rows">
        <el-table-column label="采购单" width="150"><template #default="{ row }"><router-link :to="`/purchase-orders/${row.po_id}`" class="no">{{ row.po_no }}-{{ row.line_no }}</router-link></template></el-table-column>
        <el-table-column prop="supplier" label="供应商" min-width="140" />
        <el-table-column label="物料" min-width="180"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column label="未到" width="80" align="right"><template #default="{ row }"><b class="num">{{ num(row.open_qty) }}</b></template></el-table-column>
        <el-table-column label="需求日" width="110"><template #default="{ row }"><span class="num">{{ day(row.need_date) }}</span><div class="small muted">{{ relDay(row.need_date) }}</div></template></el-table-column>
        <el-table-column label="承诺交期" width="130">
          <template #default="{ row }">
            <span v-if="row.promised_date" class="num" :class="{ late: risk(row) }">{{ row.promised_date }}</span>
            <span v-else class="wait small">供应商未回复</span>
            <div v-if="row.supplier_note" class="small muted">{{ row.supplier_note }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="buyer" label="采购员" width="80" />
        <el-table-column v-if="auth.can('BUYER')" width="120">
          <template #default="{ row }"><el-date-picker v-model="row._d" size="small" value-format="YYYY-MM-DD" placeholder="登记交期" style="width: 110px" @change="(v) => setDate(row, v)" /></template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import RefSelect from '../components/RefSelect.vue'
import { day, num, relDay } from '../utils'

defineOptions({ name: 'Tracking' })
const auth = useAuth()
const supplier = ref(null)
const onlyRisk = ref(false)
const list = ref([])
const risk = (r) => !r.promised_date || (r.need_date && r.promised_date > r.need_date)
const rows = computed(() => list.value.filter((r) => !onlyRisk.value || risk(r)))
async function load() { list.value = await get('/po-lines', { supplier_id: supplier.value || undefined }) }
async function setDate(row, v) {
  if (!v) return
  const po = await get(`/purchase-orders/${row.po_id}`)
  await put(`/po-lines/${row.id}`, { changes: { promised_date: v }, reason: '电话确认交期', version: po.version })
  load()
}
async function portal() {
  const r = await post(`/suppliers/${supplier.value}/portal-token`)
  const url = `${location.origin}${r.path}`
  await ElMessageBox.alert(`<p>把这个链接发给供应商（旧链接已失效）：</p><p style="word-break:break-all"><b>${url}</b></p>`, '供应商门户链接', { dangerouslyUseHTMLString: true })
}
onMounted(load)
onActivated(load)
</script>
