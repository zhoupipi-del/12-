<template>
  <div class="page">
    <div class="page-head">
      <div><h1>请购计划单</h1><p>转采购前在这里改图号、供应商、数量和价格。换供应商时自动带出该供应商的历史价格。</p></div>
      <div class="toolbar">
        <el-select v-model="status" style="width: 130px" @change="load">
          <el-option value="DRAFT,CONFIRMED" label="待处理" /><el-option value="" label="全部" />
        </el-select>
        <el-button v-if="auth.can('BUYER', 'PLANNER')" @click="fromShort">从缺料生成</el-button>
        <el-button v-if="auth.can('BUYER', 'PLANNER')" type="primary" @click="newReq">新建请购</el-button>
      </div>
    </div>
    <div v-for="r in list" :key="r.id" class="panel">
      <div class="rh">
        <div><span class="no">{{ r.req_no }}</span>　<StatusTag :map="REQ_STATUS" :value="r.status" />
          <span class="muted small">　{{ r.source === 'KITTING' ? '由齐套缺料生成' : '手工' }}，{{ r.created_by }}，{{ dt(r.created_at) }}</span></div>
        <div class="toolbar" v-if="editable(r)">
          <el-button size="small" @click="addLine(r)">加一行</el-button>
          <el-button size="small" text type="danger" @click="cancel(r)">取消</el-button>
          <el-button size="small" type="primary" @click="convert(r)">按供应商拆分生成采购单</el-button>
        </div>
      </div>
      <el-table :data="r.lines" size="small">
        <el-table-column label="物料" min-width="180"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column label="图号" width="170">
          <template #default="{ row }"><el-input v-if="editable(r)" v-model="row.drawing_no" size="small" @change="save(row, { drawing_no: row.drawing_no })" /><span v-else class="no small">{{ row.drawing_no }}</span></template>
        </el-table-column>
        <el-table-column label="数量" width="120">
          <template #default="{ row }"><el-input-number v-if="editable(r)" v-model="row.qty" size="small" :min="0.01" controls-position="right" @change="save(row, { qty: row.qty })" /><span v-else class="num">{{ num(row.qty) }}</span></template>
        </el-table-column>
        <el-table-column label="需求日" width="140">
          <template #default="{ row }"><el-date-picker v-if="editable(r)" v-model="row.need_date" size="small" value-format="YYYY-MM-DD" style="width: 126px" @change="save(row, { need_date: row.need_date })" /><span v-else class="num">{{ row.need_date }}</span></template>
        </el-table-column>
        <el-table-column label="供应商" min-width="170">
          <template #default="{ row }"><RefSelect v-if="editable(r)" v-model="row.supplier_id" kind="supplier" @update:model-value="(v) => save(row, { supplier_id: v })" /><span v-else>{{ row.supplier }}</span></template>
        </el-table-column>
        <el-table-column label="单价" width="120">
          <template #default="{ row }"><el-input-number v-if="editable(r)" v-model="row.price" size="small" :min="0" :precision="2" controls-position="right" @change="save(row, { price: row.price })" /><span v-else class="num">{{ money(row.price) }}</span></template>
        </el-table-column>
        <el-table-column prop="buyer" label="采购员" width="80" />
        <el-table-column label="说明" min-width="160"><template #default="{ row }"><span class="small muted">{{ row.remark }}</span></template></el-table-column>
        <el-table-column width="70" v-if="editable(r)"><template #default="{ row }"><el-button link type="danger" size="small" @click="delLine(row)">删除</el-button></template></el-table-column>
      </el-table>
    </div>
    <div v-if="!list.length" class="panel muted">没有待处理的请购单。缺料时可点“从缺料生成”。</div>

    <el-dialog v-model="lineOpen" title="添加请购物料" width="480px">
      <el-form label-width="80px">
        <el-form-item label="物料"><MaterialSelect v-model="lf.material_id" type="BUY" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="lf.qty" :min="0.01" /></el-form-item>
        <el-form-item label="需求日"><el-date-picker v-model="lf.need_date" value-format="YYYY-MM-DD" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveLine">添加</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import RefSelect from '../components/RefSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { REQ_STATUS, dt, money, num, today } from '../utils'

defineOptions({ name: 'Requisitions' })
const auth = useAuth()
const router = useRouter()
const status = ref('DRAFT,CONFIRMED')
const list = ref([])
const editable = (r) => ['DRAFT', 'CONFIRMED'].includes(r.status) && auth.can('BUYER', 'PLANNER')
async function load() { list.value = (await get('/requisitions', { status: status.value })).items }
async function save(row, data) {
  const r = await put(`/requisition-lines/${row.id}`, data)
  const i = list.value.findIndex((x) => x.id === r.id)
  if (i >= 0) list.value[i] = r
}
async function delLine(row) { await http.delete(`/requisition-lines/${row.id}`); load() }
async function convert(r) {
  const pos = await post(`/requisitions/${r.id}/convert`)
  await ElMessageBox.alert(`已生成采购单：${pos.map((p) => `${p.po_no}（${p.supplier}）`).join('、')}。审核后供应商即可在门户查看。`, '已转采购')
  router.push('/purchase-orders')
}
async function cancel(r) { await post(`/requisitions/${r.id}/cancel`); load() }
async function fromShort() {
  const r = await post('/kitting/shortages/to-requisition')
  ElMessage.success(r.requisition_no ? `已生成 ${r.requisition_no}` : '缺料都是自制/外协件，请到对应页面安排')
  load()
}
async function newReq() {
  const { value } = await ElMessageBox.prompt('先填一个物料编码或名称，其余在列表里补', '新建请购', { inputPlaceholder: '可留空后再添加' }).catch(() => ({}))
  if (value === undefined) return
  const mats = (await get('/materials', { keyword: value || '', type: 'BUY', size: 1 })).items
  if (!mats.length) return ElMessage.warning('没找到外购物料')
  await post('/requisitions', { lines: [{ material_id: mats[0].id, qty: 1, need_date: today(14) }] })
  load()
}
const lineOpen = ref(false)
const lf = ref({})
let curReq = null
function addLine(r) { curReq = r; lf.value = { material_id: null, qty: 1, need_date: today(14) }; lineOpen.value = true }
async function saveLine() { await post(`/requisitions/${curReq.id}/lines`, lf.value); lineOpen.value = false; load() }
onMounted(load)
onActivated(load)
</script>

<style scoped>
.rh { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px; }
</style>
