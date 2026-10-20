<template>
  <div class="page">
    <div class="page-head">
      <div><h1>采购订单</h1><p>可按供应商、物料、单号快速查找。锁定只针对已到货的行，同一张单的其他行照常修改。</p></div>
      <div class="toolbar">
        <el-input v-model="q.keyword" placeholder="单号 / 供应商" clearable style="width: 180px" @change="load" />
        <RefSelect v-model="q.supplier_id" kind="supplier" placeholder="供应商" style="width: 180px" @update:model-value="load" />
        <div style="width: 200px"><MaterialSelect v-model="q.material_id" placeholder="含某物料" @update:model-value="load" /></div>
        <el-select v-model="q.status" clearable placeholder="状态" style="width: 110px" @change="load">
          <el-option v-for="(v, k) in PO_STATUS" :key="k" :value="k" :label="v[0]" />
        </el-select>
        <el-button v-if="auth.can('BUYER', 'PLANNER')" type="primary" @click="openNew">新建采购单</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list.items" @row-click="(r) => $router.push(`/purchase-orders/${r.id}`)" style="cursor: pointer">
        <el-table-column label="采购单" width="140"><template #default="{ row }"><span class="no">{{ row.po_no }}</span></template></el-table-column>
        <el-table-column prop="supplier" label="供应商" min-width="160" />
        <el-table-column prop="buyer" label="采购员" width="90" />
        <el-table-column label="下单日" width="104"><template #default="{ row }"><span class="num">{{ row.order_date }}</span></template></el-table-column>
        <el-table-column label="行" width="90"><template #default="{ row }"><span class="num">{{ row.line_count }}</span><span v-if="row.locked_lines" class="muted small">（{{ row.locked_lines }} 行已到货）</span></template></el-table-column>
        <el-table-column label="金额" width="130" align="right"><template #default="{ row }"><span class="num">{{ money(row.amount) }}</span></template></el-table-column>
        <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :map="PO_STATUS" :value="row.status" /></template></el-table-column>
        <el-table-column label="编辑中" width="90"><template #default="{ row }"><span v-if="row.edit_lock_user && row.edit_lock_until > now" class="small wait">{{ row.edit_lock_user }}</span></template></el-table-column>
        <el-table-column prop="remark" label="备注" min-width="140" show-overflow-tooltip />
      </el-table>
      <el-pagination v-if="list.total > 50" layout="prev, pager, next, total" :total="list.total" :page-size="50" v-model:current-page="q.page_no" @current-change="load" style="margin-top: 12px" />
    </div>

    <el-dialog v-model="open" title="新建采购单" width="860px">
      <el-form label-width="70px" inline>
        <el-form-item label="供应商"><RefSelect v-model="f.supplier_id" kind="supplier" style="width: 240px" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="f.remark" style="width: 300px" /></el-form-item>
      </el-form>
      <el-table :data="f.lines" size="small">
        <el-table-column label="物料" min-width="240"><template #default="{ row }"><MaterialSelect v-model="row.material_id" @picked="(m) => fill(row, m)" /></template></el-table-column>
        <el-table-column label="数量" width="130"><template #default="{ row }"><el-input-number v-model="row.qty" :min="0.01" size="small" controls-position="right" /></template></el-table-column>
        <el-table-column label="单价" width="140"><template #default="{ row }"><el-input-number v-model="row.price" :min="0" :precision="2" size="small" controls-position="right" /><div v-if="row.hint" class="small muted">{{ row.hint }}</div></template></el-table-column>
        <el-table-column label="需求日" width="150"><template #default="{ row }"><el-date-picker v-model="row.need_date" value-format="YYYY-MM-DD" size="small" style="width: 136px" /></template></el-table-column>
        <el-table-column width="50"><template #default="{ $index }"><el-button link type="danger" @click="f.lines.splice($index, 1)">删</el-button></template></el-table-column>
      </el-table>
      <el-button size="small" style="margin-top: 8px" @click="f.lines.push(blank())">加一行</el-button>
      <template #footer><el-button @click="open = false">取消</el-button><el-button type="primary" @click="save">保存草稿</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import RefSelect from '../components/RefSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { PO_STATUS, money, today } from '../utils'

defineOptions({ name: 'PurchaseOrders' })
const auth = useAuth()
const router = useRouter()
const now = new Date().toISOString().slice(0, 19)
const q = ref({ keyword: '', supplier_id: null, material_id: null, status: '', page_no: 1, size: 50 })
const list = ref({ items: [], total: 0 })
async function load() { list.value = await get('/purchase-orders', q.value) }
const open = ref(false)
const blank = () => ({ material_id: null, qty: 1, price: null, need_date: today(14), hint: '' })
const f = ref({})
function openNew() { f.value = { supplier_id: null, remark: '', lines: [blank()] }; open.value = true }
async function fill(row, m) {
  if (!m) return
  const s = await get(`/materials/${m.id}/suggest`, { supplier_id: f.value.supplier_id })
  if (!f.value.supplier_id && s.supplier_id) f.value.supplier_id = s.supplier_id
  if (s.price !== null) row.price = Number(s.price)
  row.hint = s.history.length ? `上次 ${s.history[0].supplier} ¥${Number(s.history[0].price)}` : '无历史价格'
}
async function save() {
  const p = await post('/purchase-orders', { supplier_id: f.value.supplier_id, remark: f.value.remark, lines: f.value.lines.filter((l) => l.material_id) })
  open.value = false
  router.push(`/purchase-orders/${p.id}`)
}
onMounted(load)
onActivated(load)
</script>
