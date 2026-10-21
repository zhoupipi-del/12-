<template>
  <div class="page">
    <div class="page-head">
      <div><h1>自制任务</h1><p>自制件的加工任务。计划完工日即齐套计算里的预计可用日；报工后完工数量直接入生产库。</p></div>
      <div class="toolbar">
        <el-select v-model="status" style="width: 120px" @change="load"><el-option value="PLANNED,STARTED" label="未完工" /><el-option value="" label="全部" /></el-select>
        <el-button v-if="auth.can('PLANNER', 'WORKSHOP')" type="primary" @click="openNew()">新建任务</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list">
        <el-table-column label="任务" width="140"><template #default="{ row }"><span class="no">{{ row.wo_no }}</span></template></el-table-column>
        <el-table-column label="自制件" min-width="180"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}<div class="no small muted">{{ row.drawing_no }}</div></template></el-table-column>
        <el-table-column prop="order_no" label="任务来源" width="145"><template #default="{ row }"><span v-if="row.source_mode === 'STOCK'">备库生产</span><span v-else class="no">{{ row.order_no }}</span></template></el-table-column>
        <el-table-column label="完工 / 计划" width="110" align="right"><template #default="{ row }"><span class="num">{{ num(row.done_qty) }} / {{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column label="计划开工" width="104"><template #default="{ row }"><span class="num">{{ day(row.planned_start) }}</span></template></el-table-column>
        <el-table-column label="计划完工" width="150">
          <template #default="{ row }">
            <el-date-picker v-if="['PLANNED', 'STARTED'].includes(row.status) && auth.can('PLANNER', 'WORKSHOP')" :model-value="row.planned_finish" size="small" value-format="YYYY-MM-DD" style="width: 130px" @change="(v) => upd(row, { planned_finish: v })" />
            <span v-else class="num">{{ day(row.planned_finish) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="owner" label="负责" width="90" />
        <el-table-column label="状态" width="86"><template #default="{ row }"><StatusTag :map="WO_STATUS" :value="row.status" /></template></el-table-column>
        <el-table-column width="170">
          <template #default="{ row }">
            <el-button v-if="row.status === 'PLANNED' && auth.can('WORKSHOP', 'PLANNER')" link size="small" type="primary" @click="startIt(row)">开工</el-button>
            <el-button v-if="['PLANNED', 'STARTED'].includes(row.status) && auth.can('WAREHOUSE', 'WORKSHOP')" link size="small" @click="pickRaw(row)">领原料</el-button>
            <el-button v-if="row.status === 'STARTED' && auth.can('WORKSHOP')" link size="small" type="primary" @click="report(row)">报工</el-button>
            <el-button v-if="['PLANNED', 'STARTED'].includes(row.status) && auth.can('PLANNER')" link size="small" type="danger" @click="cancel(row)">取消</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="open" title="新建自制任务" width="460px">
      <el-form label-width="80px">
        <el-form-item label="任务来源" required><el-radio-group v-model="f.source_mode" @change="sourceChanged"><el-radio value="ORDER">整机订单</el-radio><el-radio value="STOCK">备库生产</el-radio></el-radio-group></el-form-item>
        <el-form-item v-if="f.source_mode === 'ORDER'" label="整机订单" required><el-select v-model="f.order_id" filterable style="width:100%"><el-option v-for="o in orderOpts" :key="o.id" :value="o.id" :label="o.label" /></el-select></el-form-item>
        <el-alert v-else type="info" :closable="false" title="备库生产不占用某张整机订单，仍会参与原料占用保护。" style="margin-bottom:12px" />
        <el-form-item label="自制件"><MaterialSelect v-model="f.material_id" type="MAKE" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="f.qty" :min="1" /></el-form-item>
        <el-form-item label="计划开工"><el-date-picker v-model="f.planned_start" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="计划完工"><el-date-picker v-model="f.planned_finish" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="负责"><el-input v-model="f.owner" placeholder="班组" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="dwgOpen" title="开工前确认图纸版本" width="520px">
      <el-table :data="dwgList" size="small">
        <el-table-column prop="drawing_no" label="图号" />
        <el-table-column prop="version" label="生效版本" width="90" />
        <el-table-column label="状态" width="140"><template #default="{ row }">{{ row.missing ? '未建受控图纸（需技术发布）' : row.confirmed ? '已确认' : row.outdated_confirm ? '已升版，需重新确认' : '未确认' }}</template></el-table-column>
      </el-table>
      <template #footer><el-button type="primary" @click="confirmAndStart">确认并开工</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { WO_STATUS, day, num, today } from '../utils'

defineOptions({ name: 'WorkOrders' })
const auth = useAuth()
const route = useRoute()
const status = ref('PLANNED,STARTED')
const list = ref([])
async function load() { list.value = (await get('/work-orders', { status: status.value })).items }
const open = ref(false)
const f = ref({})
const orderOpts = ref([])
function openNew(prefill = {}) { f.value = { source_mode: 'ORDER', order_id: null, material_id: null, qty: 1, planned_start: today(), planned_finish: today(5), owner: '', ...prefill }; open.value = true }
function sourceChanged(v) { if (v === 'STOCK') f.value.order_id = null }
async function save() { await post('/work-orders', f.value); open.value = false; load() }
async function upd(row, d) { await put(`/work-orders/${row.id}`, d); load() }
const dwgOpen = ref(false)
const dwgList = ref([])
let cur = null
async function pickRaw(row) {
  await post(`/work-orders/${row.id}/pick`)
  ElMessage.success('已生成领料单，请到【领料】过账出库')
}
async function startIt(row) {
  cur = row
  dwgList.value = await get('/drawings-required', { ref_type: 'WORK_ORDER', ref_id: row.id })
  if (dwgList.value.some((d) => !d.confirmed)) { dwgOpen.value = true; return }
  await post(`/work-orders/${row.id}/start`); ElMessage.success('已开工'); load()
}
async function confirmAndStart() {
  await post('/drawings-confirm', { ref_type: 'WORK_ORDER', ref_id: cur.id, version_ids: dwgList.value.filter((d) => !d.confirmed && !d.missing).map((d) => d.version_id) })
  await post(`/work-orders/${cur.id}/start`)
  dwgOpen.value = false
  ElMessage.success('已开工')
  load()
}
async function report(row) {
  const { value } = await ElMessageBox.prompt(`本次完工数量（剩余 ${num(row.qty - row.done_qty)}）`, '报工', { inputValue: String(Number(row.qty) - Number(row.done_qty)) })
  await post(`/work-orders/${row.id}/report`, { qty: Number(value) })
  ElMessage.success('已报工入库')
  load()
}
async function cancel(row) { await post(`/work-orders/${row.id}/cancel`); load() }
onMounted(async () => {
  orderOpts.value = await get('/orders/options')
  await load()
  if (route.query.order_id) openNew({ order_id: Number(route.query.order_id), material_id: Number(route.query.material_id), qty: Number(route.query.qty || 1) })
})
onActivated(load)
</script>
