<template>
  <div class="page" v-if="o">
    <div class="page-head">
      <div>
        <h1><span class="no">{{ o.order_no }}</span>　{{ o.customer }}</h1>
        <p>{{ o.product }} × {{ num(o.qty) }}　交期 {{ o.due_date }}（{{ relDay(o.due_date) }}）{{ o.is_nonstandard ? '　非标' : '' }}</p>
      </div>
      <div class="toolbar">
        <StatusTag :map="ORDER_STATUS" :value="o.status" />
        <el-button @click="$router.push(`/material-plan?order_id=${o.id}`)">订单备料</el-button>
        <el-button v-if="o.status === 'PLANNED' && auth.can('PLANNER')" type="primary" @click="act('release')">计划下达</el-button>
        <el-button v-if="o.status === 'RELEASED'" type="primary" @click="start()">开工装配</el-button>
        <el-button v-if="['RELEASED', 'ASSEMBLING'].includes(o.status) && auth.can('WAREHOUSE', 'WORKSHOP')" @click="newPick">按 BOM 领料</el-button>
        <el-button v-if="o.status === 'ASSEMBLING' && auth.can('WORKSHOP', 'WAREHOUSE')" type="success" @click="act('complete')">完工入库</el-button>
        <el-button v-if="['ASSEMBLING', 'COMPLETED'].includes(o.status) && auth.can('SALES', 'PLANNER')" @click="shipOpen = true">做发货计划</el-button>
        <el-button v-if="['PLANNED', 'RELEASED'].includes(o.status) && auth.can('PLANNER', 'SALES')" text type="danger" @click="cancel">取消订单</el-button>
      </div>
    </div>

    <div class="panel head">
      <div class="kit-sum">
        <div class="state"><span class="dot" :class="o.kit_status" />{{ o.kit_label }}</div>
        <KitStrip :lines="o.kit_lines" big />
        <div class="facts">
          <div><span>品项齐套率</span><b class="num">{{ pct(o.kit_rate) }}</b></div>
          <div><span>预计齐套</span><b class="num">{{ o.kit_ready_date || '缺料待补' }}</b></div>
          <div><span>需求日（计划开工）</span><b class="num">{{ o.planned_start || '按交期推算' }}</b></div>
          <div><span>优先级</span><b>{{ PRIORITY[o.priority] }}{{ o.priority_pin > 0 ? '（已置顶）' : '' }}</b></div>
          <div><span>装配进度</span><b class="num">{{ o.progress }}%</b></div>
          <div><span>工位</span><b>{{ o.station || '—' }}</b></div>
        </div>
      </div>
      <div class="toolbar edit" v-if="['PLANNED', 'RELEASED', 'ASSEMBLING'].includes(o.status)">
        <template v-if="auth.can('PLANNER')">
          <span class="muted small">优先级</span>
          <el-select :model-value="o.priority" size="small" style="width: 90px" @change="(v) => patch({ priority: v })">
            <el-option v-for="(v, k) in PRIORITY" :key="k" :value="Number(k)" :label="v" />
          </el-select>
          <el-button size="small" @click="patch({ priority_pin: o.priority_pin > 0 ? 0 : 1 })">{{ o.priority_pin > 0 ? '取消置顶' : '置顶' }}</el-button>
          <span class="muted small">计划开工</span>
          <el-date-picker :model-value="o.planned_start" size="small" value-format="YYYY-MM-DD" style="width: 140px" @change="(v) => patch({ planned_start: v })" />
        </template>
        <template v-if="o.status === 'ASSEMBLING' && auth.can('WORKSHOP', 'PLANNER')">
          <span class="muted small">进度</span>
          <el-slider :model-value="o.progress" :step="5" style="width: 160px" @change="(v) => patch({ progress: v })" />
        </template>
      </div>
    </div>

    <el-tabs v-model="tab" class="panel tabs">
      <el-tab-pane label="物料齐套明细" name="kit">
        <el-table :data="kitRows" size="small" row-key="id" :row-class-name="({ row }) => 'lv' + row.level">
          <el-table-column label="物料" min-width="230">
            <template #default="{ row }">
              <span :style="{ paddingLeft: (row.level - 1) * 18 + 'px' }"><span class="no">{{ row.code }}</span> {{ row.name }}</span>
              <el-tag v-if="row.is_common" size="small" effect="plain" style="margin-left: 6px">通用件</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="56"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
          <el-table-column label="图号" width="140"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
          <el-table-column label="需求" width="76" align="right"><template #default="{ row }"><span class="num">{{ num(row.required) }}</span></template></el-table-column>
          <el-table-column label="已领" width="66" align="right"><template #default="{ row }"><span class="num muted">{{ num(row.issued) }}</span></template></el-table-column>
          <el-table-column label="供应来源" min-width="220">
            <template #default="{ row }">
              <span v-for="(s, i) in row.supplies" :key="i" class="src">{{ SOURCE_LABEL[s.type] }} <b class="num">{{ num(s.qty) }}</b><template v-if="s.date"> @{{ s.date.slice(5) }}{{ s.confirmed === false ? '?' : '' }}</template></span>
              <span v-if="row.from_children > 0" class="src">由下级物料加工 <b class="num">{{ num(row.from_children) }}</b></span>
              <span v-if="row.short_qty > 0" class="src short">缺 <b class="num">{{ num(row.short_qty) }}</b></span>
            </template>
          </el-table-column>
          <el-table-column label="需求日" width="92"><template #default="{ row }"><span class="num small">{{ row.need_date }}</span></template></el-table-column>
          <el-table-column label="预计可用" width="92"><template #default="{ row }"><span class="num small">{{ row.expected_date || '—' }}</span></template></el-table-column>
          <el-table-column label="状态" width="96"><template #default="{ row }"><span class="dot" :class="row.status" />{{ row.status_label }}</template></el-table-column>
          <el-table-column v-if="auth.can('PLANNER', 'MANAGER')" label="" width="110">
            <template #default="{ row }">
              <el-button v-if="row.level === 1" link size="small" @click="openLock(row)">锁定</el-button>
              <el-button v-if="row.level === 1 && Number(row.short_qty) + Number(row.from_incoming) > 0" link size="small" type="warning" @click="openRealloc(row)">调料</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted small">“?”表示交期未经供应商确认。锁定：把现有库存固定给本订单，重新计算时不会被其他订单抢走。</p>
      </el-tab-pane>

      <el-tab-pane :label="`预占 ${o.reservations.length}`" name="res">
        <el-table :data="o.reservations" size="small">
          <el-table-column label="物料" min-width="200"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
          <el-table-column label="数量" width="90" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
          <el-table-column label="来源" width="80"><template #default="{ row }">{{ SOURCE_LABEL[row.source_type] }}</template></el-table-column>
          <el-table-column label="预计日期" width="104"><template #default="{ row }"><span class="num">{{ day(row.expected_date) }}</span></template></el-table-column>
          <el-table-column label="锁定" min-width="200"><template #default="{ row }"><template v-if="row.locked"><el-tag size="small" type="warning">人工锁定</el-tag> {{ row.locked_by }}：{{ row.lock_reason }}</template><span v-else class="muted small">自动冲填</span></template></el-table-column>
          <el-table-column width="80"><template #default="{ row }"><el-button v-if="row.locked && auth.can('PLANNER', 'MANAGER')" link type="danger" size="small" @click="unlock(row)">解除</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="dwg">
        <template #label>图纸确认 <el-badge v-if="pendingDwg" :value="pendingDwg" type="danger" /></template>
        <p class="muted small">开工装配前必须确认每张图纸的版本，确认后即为本订单的批准版本。图纸升版不会让本订单被动换版，由技术评估后指定是否切换；重大变更会强制暂停。</p>
        <el-table :data="o.drawings" size="small" @selection-change="(v) => (dwgSel = v)">
          <el-table-column type="selection" width="40" :selectable="(r) => !r.confirmed && !r.missing" />
          <el-table-column label="图号" width="170"><template #default="{ row }"><span class="no">{{ row.drawing_no }}</span></template></el-table-column>
          <el-table-column prop="material" label="物料" min-width="180" />
          <el-table-column label="批准版本" width="90"><template #default="{ row }"><b class="num">{{ row.version }}</b></template></el-table-column>
          <el-table-column label="查阅" width="80"><template #default="{ row }"><a v-if="row.pdf_path" @click.prevent="openFile(row.pdf_path)" href="#">打开</a><span v-else class="muted small">无文件</span></template></el-table-column>
          <el-table-column label="确认" min-width="180">
            <template #default="{ row }">
              <span v-if="row.hold" class="short">已暂停：{{ row.hold_reason }}</span>
              <template v-else>
                <span v-if="row.missing" class="bad">未建受控图纸，需技术先发布（或物料勾选免图纸管控）</span><span v-else-if="row.confirmed" class="ok">已确认 {{ dt(row.confirmed_at) }}</span>
                <span v-else class="wait">未确认</span>
                <span v-if="row.upgrade_available" class="muted small">　有新版 {{ row.current_version }}，是否切换由技术评估</span>
              </template>
            </template>
          </el-table-column>
        </el-table>
        <el-button v-if="auth.can('WORKSHOP', 'TECH', 'PLANNER')" type="primary" :disabled="!dwgSel.length" style="margin-top: 10px" @click="confirmDwg">确认所选图纸版本</el-button>
      </el-tab-pane>

      <el-tab-pane :label="`领料 ${o.picks.length}`" name="pick">
        <div v-for="p in o.picks" :key="p.id" class="pick">
          <div><span class="no">{{ p.pick_no }}</span>　<StatusTag :map="{ DRAFT: ['草稿', 'info'], POSTED: ['已过账', 'success'] }" :value="p.status" />　<span class="muted small">{{ dt(p.posted_at || p.created_at) }}</span>
            <el-button v-if="p.status === 'DRAFT' && auth.can('WAREHOUSE')" size="small" type="primary" style="margin-left: 12px" @click="postPick(p)">过账出库</el-button></div>
          <div class="pick-lines">
            <span v-for="l in p.lines" :key="l.id">{{ l.name }} <b class="num">{{ num(l.qty) }}</b>{{ l.unit }}<template v-if="l.lots.length"><span class="muted small">（{{ l.lots.map((x) => x.lot_no).join('、') }}）</span></template></span>
          </div>
        </div>
        <div v-if="!o.picks.length" class="muted small">还没有领料记录</div>
      </el-tab-pane>

      <el-tab-pane :label="`发货 ${o.shipments.length}`" name="ship">
        <el-table :data="o.shipments" size="small">
          <el-table-column label="发货单" width="140"><template #default="{ row }"><span class="no">{{ row.ship_no }}</span></template></el-table-column>
          <el-table-column label="数量" width="70"><template #default="{ row }">{{ num(row.qty) }}</template></el-table-column>
          <el-table-column prop="plan_date" label="计划日期" width="110" />
          <el-table-column prop="address" label="地址" />
          <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :map="SHIP_STATUS" :value="row.status" /></template></el-table-column>
        </el-table>
        <router-link to="/shipments" class="small">去发货计划处理审批</router-link>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="lockOpen" title="锁定库存给本订单" width="440px">
      <el-form label-width="80px">
        <el-form-item label="物料"><b>{{ lockRow?.code }} {{ lockRow?.name }}</b></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="lockF.qty" :min="0.01" /></el-form-item>
        <el-form-item label="原因"><el-input v-model="lockF.reason" placeholder="例如：客户催货，主管协调" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="doLock">锁定</el-button></template>
    </el-dialog>

    <el-dialog v-model="raOpen" title="申请调料（从他单已冻结的库存调给本订单）" width="680px">
      <p class="small muted">已分配给其他订单的库存不会被自动抢走。先预演影响，再提交审批；已领用的料不会被动。</p>
      <el-form label-width="80px" inline>
        <el-form-item label="物料"><b>{{ raRow?.code }} {{ raRow?.name }}</b></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="raF.qty" :min="0.01" /></el-form-item>
        <el-button @click="raPreview">预演影响</el-button>
      </el-form>
      <template v-if="raPv">
        <div class="small" style="margin: 6px 0">拟从：<span v-for="p in raPv.plan" :key="p.from_order_id" class="src">{{ p.order_no }} <b class="num">{{ num(p.qty) }}</b></span></div>
        <el-table :data="raPv.impacts" size="small">
          <el-table-column prop="order_no" label="订单" width="150" />
          <el-table-column label="变化" min-width="200"><template #default="{ row }">{{ KIT[row.before] || row.before }} → {{ KIT[row.after] || row.after }}<el-tag v-if="row.is_target" size="small" style="margin-left: 6px">本单</el-tag><el-tag v-else size="small" type="warning" style="margin-left: 6px">被调出</el-tag></template></el-table-column>
          <el-table-column label="预计齐套" width="170"><template #default="{ row }"><span class="num small">{{ row.before_ready || '—' }} → {{ row.after_ready || '无法确定' }}</span></template></el-table-column>
        </el-table>
        <el-input v-model="raF.reason" placeholder="调料原因（必填，例如：客户催货）" style="margin-top: 10px" />
      </template>
      <template #footer><el-button type="primary" :disabled="!raPv || !raF.reason" @click="raSubmit">提交审批</el-button></template>
    </el-dialog>

    <el-dialog v-model="pickOpen" title="按 BOM 领料" width="720px">
      <el-table :data="pickPlan" size="small">
        <el-table-column label="物料" min-width="180"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column label="BOM 需求" width="86" align="right"><template #default="{ row }"><span class="num">{{ num(row.required) }}</span></template></el-table-column>
        <el-table-column label="已领" width="70" align="right"><template #default="{ row }"><span class="num">{{ num(row.issued) }}</span></template></el-table-column>
        <el-table-column label="库存" width="80" align="right"><template #default="{ row }"><span class="num" :class="{ short: row.available < row.plan_qty }">{{ num(row.available) }}</span></template></el-table-column>
        <el-table-column label="本次领用" width="140"><template #default="{ row }"><el-input-number v-model="row.qty" :min="0" :max="Number(row.available)" size="small" controls-position="right" /></template></el-table-column>
      </el-table>
      <template #footer><el-button @click="pickOpen = false">取消</el-button><el-button type="primary" @click="savePick(false)">保存草稿</el-button><el-button v-if="auth.can('WAREHOUSE')" type="success" @click="savePick(true)">保存并过账</el-button></template>
    </el-dialog>

    <el-dialog v-model="shipOpen" title="发货计划" width="460px">
      <el-form label-width="80px">
        <el-form-item label="数量"><el-input-number v-model="shipF.qty" :min="1" :max="Number(o.qty)" /></el-form-item>
        <el-form-item label="计划日期"><el-date-picker v-model="shipF.plan_date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="收货地址"><el-input v-model="shipF.address" /></el-form-item>
        <el-form-item label="承运方"><el-input v-model="shipF.carrier" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveShip">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth, useTabs } from '../store'
import KitStrip from '../components/KitStrip.vue'
import StatusTag from '../components/StatusTag.vue'
import { MAT_TYPE, ORDER_STATUS, PRIORITY, SHIP_STATUS, SOURCE_LABEL, day, dt, num, pct, relDay, today } from '../utils'

defineOptions({ name: 'OrderDetail' })
const auth = useAuth()
const tabs = useTabs()
const route = useRoute()
const id = route.params.id
const o = ref(null)
const tab = ref('kit')
const kitRows = computed(() => o.value?.kit_lines || [])
const pendingDwg = computed(() => (o.value?.drawings || []).filter((d) => !d.confirmed && !d.missing).length)

async function load() {
  o.value = await get(`/orders/${id}`)
  tabs.rename(route.fullPath, o.value.order_no)
}
async function act(a) {
  await post(`/orders/${id}/${a}`)
  ElMessage.success('已完成')
  load()
}
async function start(reason = '') {
  try {
    await post(`/orders/${id}/start`, { override_reason: reason })
    ElMessage.success('已开工')
    load()
  } catch (e) {
    if (e.data?.need_override && auth.can('MANAGER')) {
      const { value } = await ElMessageBox.prompt('订单未齐套。如确需先开工，请填写原因（将记录在案）', '强制开工', { inputPlaceholder: '例如：先装主体，缺件后补' })
      if (value) start(value)
    } else if (e.data?.pending) tab.value = 'dwg'
  }
}
async function cancel() {
  const { value } = await ElMessageBox.prompt('请填写取消原因', '取消订单')
  await post(`/orders/${id}/cancel`, { reason: value })
  load()
}
async function patch(data) {
  await put(`/orders/${id}`, data)
  load()
}
const lockOpen = ref(false)
const lockRow = ref(null)
const lockF = ref({ qty: 1, reason: '' })
function openLock(row) {
  lockRow.value = row
  lockF.value = { qty: Number(row.required) || 1, reason: '' }
  lockOpen.value = true
}
async function doLock() {
  await post(`/orders/${id}/lock`, { material_id: lockRow.value.material_id, ...lockF.value })
  lockOpen.value = false
  ElMessage.success('已锁定')
  load()
}
const KIT = { READY: '已齐套', INCOMING_OK: '在途可齐', INCOMING_LATE: '在途延误', SHORT: '缺料' }
const raOpen = ref(false)
const raRow = ref(null)
const raF = ref({ qty: 1, reason: '' })
const raPv = ref(null)
function openRealloc(row) { raRow.value = row; raF.value = { qty: Number(row.short_qty) || 1, reason: '' }; raPv.value = null; raOpen.value = true }
async function raPreview() {
  raPv.value = await post('/realloc/preview', { to_order_id: Number(id), material_id: raRow.value.material_id, qty: raF.value.qty })
}
async function raSubmit() {
  await post('/realloc', { to_order_id: Number(id), material_id: raRow.value.material_id, qty: raF.value.qty, reason: raF.value.reason })
  raOpen.value = false
  ElMessage.success('已提交，等待主管审批（见【调料申请】）')
}
async function unlock(r) {
  await http.delete(`/reservations/${r.id}`)
  load()
}
const dwgSel = ref([])
async function confirmDwg() {
  await post('/drawings-confirm', { ref_type: 'SALES_ORDER', ref_id: Number(id), version_ids: dwgSel.value.filter((x) => x.version_id).map((x) => x.version_id) })
  ElMessage.success('已确认')
  load()
}
async function openFile(p) {
  const blob = await http.get(`/files/${p}`, { responseType: 'blob' })
  window.open(URL.createObjectURL(blob), '_blank')
}
const pickOpen = ref(false)
const pickPlan = ref([])
async function newPick() {
  pickPlan.value = (await get(`/orders/${id}/pick-plan`)).map((p) => ({ ...p, qty: Math.min(Number(p.plan_qty), Number(p.available)) }))
  pickOpen.value = true
}
async function savePick(postIt) {
  const lines = pickPlan.value.filter((p) => p.qty > 0).map((p) => ({ material_id: p.material_id, qty: p.qty }))
  if (!lines.length) return ElMessage.warning('没有要领用的物料')
  const pk = await post('/picks', { order_id: Number(id), lines })
  if (postIt) await post(`/picks/${pk.id}/post`)
  pickOpen.value = false
  ElMessage.success(postIt ? '已出库，齐套已重算' : '领料单已保存')
  tab.value = 'pick'
  load()
}
async function postPick(p) {
  await post(`/picks/${p.id}/post`)
  load()
}
const shipOpen = ref(false)
const shipF = ref({ qty: 1, plan_date: today(3), address: '', carrier: '' })
async function saveShip() {
  await post('/shipments', { order_id: Number(id), ...shipF.value })
  shipOpen.value = false
  ElMessage.success('发货计划已建，请提交审批')
  tab.value = 'ship'
  load()
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.head { padding-bottom: 10px; }
.kit-sum .state { font-size: 15px; font-weight: 600; margin-bottom: 10px; }
.facts { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
@media (max-width: 1100px) { .facts { grid-template-columns: repeat(3, 1fr); } }
.facts div { display: flex; flex-direction: column; }
.facts span { font-size: 12px; color: var(--ink-3); }
.facts b { font-size: 16px; font-weight: 600; }
.edit { margin-top: 14px; padding-top: 10px; border-top: 1px dashed var(--line-soft); }
.tabs { margin-top: 16px; }
.src { display: inline-block; margin-right: 10px; font-size: 12px; color: var(--ink-2); }
:deep(.lv2) td { background: #fafbfc; }
:deep(.lv3) td { background: #f5f7f9; }
.pick { padding: 10px 0; border-bottom: 1px solid var(--line-soft); }
.pick-lines { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; }
</style>
