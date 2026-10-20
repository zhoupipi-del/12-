<template>
  <div class="page" v-if="po">
    <div class="page-head">
      <div>
        <h1><span class="no">{{ po.po_no }}</span>　{{ po.supplier }}</h1>
        <p>采购员 {{ po.buyer }}　下单 {{ po.order_date }}　金额 <span class="num">{{ money(po.amount) }}</span>　版本 <span class="num">{{ po.version }}</span></p>
      </div>
      <div class="toolbar">
        <StatusTag :map="PO_STATUS" :value="po.status" />
        <el-button v-if="po.status === 'DRAFT' && auth.can('BUYER', 'MANAGER')" type="primary" @click="approve">审核</el-button>
        <template v-if="canEdit">
          <el-button v-if="!mine" @click="lock()">开始编辑</el-button>
          <el-button v-else type="primary" plain @click="unlock">完成编辑</el-button>
        </template>
      </div>
    </div>

    <el-alert v-if="lockedByOther" type="warning" :closable="false" show-icon style="margin-bottom: 12px"
              :title="`${po.edit_lock_user} 正在编辑这张采购单（${dt(po.edit_lock_until)} 前），你现在只能查看`">
      <el-button v-if="auth.can('MANAGER')" size="small" @click="lock(true)">主管强制接管</el-button>
    </el-alert>

    <div class="panel">
      <h2>明细<small>浅黄色行已有到货，只锁这一行；要更正物料或价格请用“行级冲销”</small></h2>
      <el-table :data="po.lines" :row-class-name="({ row }) => (row.locked ? 'locked-row' : '')">
        <el-table-column label="行" width="56"><template #default="{ row }"><span class="num">{{ row.line_no }}</span></template></el-table-column>
        <el-table-column label="物料" min-width="200"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}<div class="muted small">{{ row.spec }}</div></template></el-table-column>
        <el-table-column label="图号" width="150"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
        <el-table-column label="数量" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column label="单价" width="100" align="right"><template #default="{ row }"><span class="num">{{ money(row.price) }}</span></template></el-table-column>
        <el-table-column label="需求日" width="100"><template #default="{ row }"><span class="num">{{ day(row.need_date) }}</span></template></el-table-column>
        <el-table-column label="承诺交期" width="110"><template #default="{ row }"><span class="num" :class="{ late: row.promised_date && row.need_date && row.promised_date > row.need_date }">{{ day(row.promised_date) }}</span><div v-if="row.supplier_note" class="small muted">{{ row.supplier_note }}</div></template></el-table-column>
        <el-table-column label="到货 / 入库" width="100" align="right"><template #default="{ row }"><span class="num">{{ num(row.arrived_qty) }} / {{ num(row.received_qty) }}</span></template></el-table-column>
        <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :map="LINE_STATUS" :value="row.status" /><el-icon v-if="row.locked" class="wait" style="margin-left: 4px" title="该行已锁定"><Lock /></el-icon></template></el-table-column>
        <el-table-column v-if="canEdit && editing" label="" width="250">
          <template #default="{ row }">
            <template v-if="!['CLOSED', 'CANCELLED', 'RECEIVED'].includes(row.status) || row.locked">
              <el-button link size="small" @click="openEdit(row)" v-if="!['CLOSED', 'CANCELLED'].includes(row.status)">修改</el-button>
              <el-button link size="small" type="warning" v-if="row.locked" @click="openReverse(row)">行级冲销</el-button>
              <el-button link size="small" v-if="['OPEN', 'PARTIAL'].includes(row.status) && po.status === 'APPROVED'" @click="closeRemain(row)">关闭余量</el-button>
              <el-button link size="small" type="danger" v-if="!row.locked && !['CLOSED', 'CANCELLED'].includes(row.status)" @click="delLine(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <el-button v-if="canEdit && editing" size="small" style="margin-top: 10px" @click="addOpen = true">加一行</el-button>
    </div>

    <div class="panel">
      <h2>修订记录<small>谁、什么时候、改了什么、为什么</small></h2>
      <el-table :data="po.revisions" size="small" empty-text="还没有修改记录">
        <el-table-column label="时间" width="140"><template #default="{ row }"><span class="num small">{{ dt(row.created_at) }}</span></template></el-table-column>
        <el-table-column label="行" width="56"><template #default="{ row }">{{ row.line_no }}</template></el-table-column>
        <el-table-column label="动作" width="100"><template #default="{ row }">{{ REV[row.action] || row.action }}</template></el-table-column>
        <el-table-column label="改前" min-width="160"><template #default="{ row }"><code>{{ short(row.before) }}</code></template></el-table-column>
        <el-table-column label="改后" min-width="160"><template #default="{ row }"><code>{{ short(row.after) }}</code></template></el-table-column>
        <el-table-column prop="reason" label="原因" min-width="140" />
        <el-table-column prop="user" label="操作人" width="90" />
      </el-table>
    </div>

    <el-dialog v-model="editOpen" :title="`修改第 ${editRow?.line_no} 行`" width="480px">
      <el-alert v-if="editRow?.locked" type="info" :closable="false" title="该行已有到货，只能改数量（不少于已到货量）、日期和备注" style="margin-bottom: 12px" />
      <el-form label-width="90px">
        <el-form-item v-if="!editRow?.locked" label="物料"><MaterialSelect v-model="ef.material_id" /></el-form-item>
        <el-form-item v-if="!editRow?.locked" label="图号"><el-input v-model="ef.drawing_no" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="ef.qty" :min="Number(editRow?.arrived_qty) || 0.01" /></el-form-item>
        <el-form-item v-if="!editRow?.locked" label="单价"><el-input-number v-model="ef.price" :min="0" :precision="2" /></el-form-item>
        <el-form-item label="需求日"><el-date-picker v-model="ef.need_date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="承诺交期"><el-date-picker v-model="ef.promised_date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="修改原因" :required="po.status === 'APPROVED'"><el-input v-model="ef.reason" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveEdit">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="revOpen" :title="`第 ${revRow?.line_no} 行冲销`" width="560px">
      <p class="small">冲销会冲回这一行的入库库存、作废这一行的到货记录，然后该行可重新修改。<b>同单其他行不受影响，不需要弃审整张单。</b></p>
      <el-table :data="impact?.items || []" size="small" empty-text="无下游单据">
        <el-table-column prop="type" label="下游单据" width="100" />
        <el-table-column prop="no" label="单号" />
        <el-table-column label="数量" width="80"><template #default="{ row }">{{ num(row.qty) }}</template></el-table-column>
        <el-table-column label="状态 / 剩余" width="110"><template #default="{ row }">{{ row.status || num(row.remain) }}</template></el-table-column>
      </el-table>
      <el-alert v-for="b in impact?.blockers || []" :key="b" type="error" :closable="false" :title="b" style="margin-top: 8px" />
      <el-input v-model="revReason" placeholder="冲销原因（必填）" style="margin-top: 12px" />
      <template #footer><el-button type="warning" :disabled="!impact?.can_reverse || !revReason" @click="doReverse">确认冲销这一行</el-button></template>
    </el-dialog>

    <el-dialog v-model="addOpen" title="加一行" width="480px">
      <el-form label-width="80px">
        <el-form-item label="物料"><MaterialSelect v-model="af.material_id" @picked="sugAdd" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="af.qty" :min="0.01" /></el-form-item>
        <el-form-item label="单价"><el-input-number v-model="af.price" :min="0" :precision="2" /></el-form-item>
        <el-form-item label="需求日"><el-date-picker v-model="af.need_date" value-format="YYYY-MM-DD" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveAdd">添加</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth, useTabs } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { LINE_STATUS, PO_STATUS, day, dt, money, num, today } from '../utils'

defineOptions({ name: 'PurchaseOrderDetail' })
const REV = { ADD: '新增行', EDIT: '修改', REVERSE: '行级冲销', CLOSE_REMAIN: '关闭余量', CANCEL: '取消行', SUPPLIER_DATE: '供应商回复交期' }
const auth = useAuth()
const tabs = useTabs()
const route = useRoute()
const id = route.params.id
const po = ref(null)
const editing = ref(false)
const canEdit = computed(() => po.value && !['CLOSED', 'CANCELLED'].includes(po.value.status) && auth.can('BUYER', 'PLANNER'))
const mine = computed(() => editing.value)
const lockedByOther = computed(() => po.value?.edit_lock_user && po.value.edit_lock_user !== auth.user?.name &&
  po.value.edit_lock_until > new Date().toISOString().slice(0, 19))

const short = (s) => { try { const o = JSON.parse(s); return Object.entries(o).map(([k, v]) => `${k}: ${v}`).join('，') } catch { return s } }
async function load() {
  po.value = await get(`/purchase-orders/${id}`)
  tabs.rename(route.fullPath, po.value.po_no)
}
async function lock(force = false) {
  await post(`/purchase-orders/${id}/lock`, null, { params: { force } })
  editing.value = true
  load()
}
async function unlock() {
  await http.delete(`/purchase-orders/${id}/lock`)
  editing.value = false
  load()
}
async function approve() { await post(`/purchase-orders/${id}/approve`); ElMessage.success('已审核'); load() }

const editOpen = ref(false)
const editRow = ref(null)
const ef = ref({})
function openEdit(row) {
  editRow.value = row
  ef.value = { material_id: row.material_id, drawing_no: row.drawing_no, qty: Number(row.qty), price: Number(row.price), need_date: row.need_date, promised_date: row.promised_date, reason: '' }
  editOpen.value = true
}
async function saveEdit() {
  const r = editRow.value
  const ch = {}
  const fields = r.locked ? ['qty', 'need_date', 'promised_date'] : ['material_id', 'drawing_no', 'qty', 'price', 'need_date', 'promised_date']
  fields.forEach((k) => { if (String(ef.value[k] ?? '') !== String(k === 'qty' || k === 'price' ? Number(r[k]) : r[k] ?? '')) ch[k] = ef.value[k] })
  if (!Object.keys(ch).length) { editOpen.value = false; return }
  po.value = await put(`/po-lines/${r.id}`, { changes: ch, reason: ef.value.reason, version: po.value.version })
  editOpen.value = false
  ElMessage.success('已修改')
  load()
}
const revOpen = ref(false)
const revRow = ref(null)
const impact = ref(null)
const revReason = ref('')
async function openReverse(row) {
  revRow.value = row
  revReason.value = ''
  impact.value = await get(`/po-lines/${row.id}/impact`)
  revOpen.value = true
}
async function doReverse() {
  await post(`/po-lines/${revRow.value.id}/reverse`, { reason: revReason.value, version: po.value.version })
  revOpen.value = false
  ElMessage.success('该行已冲销，现在可以修改')
  load()
}
async function closeRemain(row) {
  const { value } = await ElMessageBox.prompt('关闭该行剩余未到数量，原因：', '关闭余量')
  await post(`/po-lines/${row.id}/close`, { reason: value, version: po.value.version })
  load()
}
async function delLine(row) {
  await ElMessageBox.confirm(`删除第 ${row.line_no} 行？`, '确认')
  await http.delete(`/po-lines/${row.id}`, { params: { version: po.value.version } })
  load()
}
const addOpen = ref(false)
const af = ref({ material_id: null, qty: 1, price: null, need_date: today(14) })
async function sugAdd(m) { if (m) { const s = await get(`/materials/${m.id}/suggest`, { supplier_id: po.value.supplier_id }); af.value.price = s.price !== null ? Number(s.price) : null } }
async function saveAdd() {
  await post(`/purchase-orders/${id}/lines`, af.value, { params: { version: po.value.version } })
  addOpen.value = false
  af.value = { material_id: null, qty: 1, price: null, need_date: today(14) }
  load()
}
onMounted(load)
onActivated(load)
const release = () => { if (editing.value) { http.delete(`/purchase-orders/${id}/lock`, { silent: true }).catch(() => {}); editing.value = false } }
onDeactivated(release)
onBeforeUnmount(release)
</script>

<style scoped>
code { font-family: var(--num); font-size: 12px; color: var(--ink-2); }
</style>
