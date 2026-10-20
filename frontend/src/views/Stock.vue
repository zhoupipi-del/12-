<template>
  <div class="page">
    <div class="page-head">
      <div><h1>库存</h1><p>生产物料库与售后备件库分开记账；售后库不参与生产齐套。出库一律先进先出。</p></div>
      <div class="toolbar" v-if="auth.can('WAREHOUSE', 'AFTERSALES')">
        <el-button v-if="auth.can('WAREHOUSE')" @click="openTransfer()">调拨</el-button>
        <el-button type="primary" @click="openSpare()">售后备件出库</el-button>
      </div>
    </div>
    <el-tabs v-model="tab" class="panel">
      <el-tab-pane label="现存量" name="onhand">
        <div class="toolbar" style="margin-bottom: 10px">
          <el-radio-group v-model="purpose" size="small" @change="loadOnhand">
            <el-radio-button value="PRODUCTION">生产物料</el-radio-button>
            <el-radio-button value="AFTERSALES">售后备件</el-radio-button>
            <el-radio-button value="FINISHED">成品</el-radio-button>
            <el-radio-button value="">全部</el-radio-button>
          </el-radio-group>
          <el-input v-model="kw" placeholder="编码 / 名称 / 图号" clearable style="width: 200px" @change="loadOnhand" />
          <el-checkbox v-model="flagOnly">只看高低预警</el-checkbox>
        </div>
        <el-table :data="onhandRows" size="small">
          <el-table-column label="物料" min-width="200"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}<div class="muted small">{{ row.spec }}</div></template></el-table-column>
          <el-table-column label="类型" width="60"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
          <el-table-column prop="warehouse" label="仓库" width="110" />
          <el-table-column label="现存" width="100" align="right"><template #default="{ row }"><b class="num" :class="{ short: row.flag === 'LOW', wait: row.flag === 'HIGH' }">{{ num(row.qty) }}</b> {{ row.unit }}</template></el-table-column>
          <el-table-column label="安全库存" width="130" align="right"><template #default="{ row }"><span class="num muted">{{ num(row.safety_min) }} ~ {{ Number(row.safety_max) ? num(row.safety_max) : '不限' }}</span></template></el-table-column>
          <el-table-column label="预警" width="80"><template #default="{ row }"><el-tag v-if="row.flag === 'LOW'" type="danger" size="small">低于下限</el-tag><el-tag v-if="row.flag === 'HIGH'" type="warning" size="small">积压</el-tag></template></el-table-column>
          <el-table-column label="最早批次" width="110"><template #default="{ row }"><span class="num small">{{ day(row.oldest_lot) }}</span></template></el-table-column>
          <el-table-column label="最近出入库" width="110"><template #default="{ row }"><span class="num small">{{ day(row.last_move_at) }}</span></template></el-table-column>
          <el-table-column width="140"><template #default="{ row }"><el-button link size="small" @click="showLots(row)">批次</el-button><el-button link size="small" @click="showTxn(row)">流水</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="出入库明细" name="txn">
        <div class="toolbar" style="margin-bottom: 10px">
          <div style="width: 220px"><MaterialSelect v-model="tq.material_id" @update:model-value="loadTxn" /></div>
          <el-select v-model="tq.txn_type" clearable placeholder="类型" style="width: 130px" @change="loadTxn"><el-option v-for="(v, k) in auth.meta?.txn_types" :key="k" :value="k" :label="v" /></el-select>
          <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始" end-placeholder="结束" @change="loadTxn" />
        </div>
        <el-table :data="txns.items" size="small">
          <el-table-column label="时间" width="140"><template #default="{ row }"><span class="num small">{{ dt(row.created_at) }}</span></template></el-table-column>
          <el-table-column prop="type_label" label="类型" width="90" />
          <el-table-column label="物料" min-width="180"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
          <el-table-column prop="warehouse" label="仓库" width="110" />
          <el-table-column label="数量" width="90" align="right"><template #default="{ row }"><b class="num" :class="row.qty > 0 ? 'ok' : 'short'">{{ row.qty > 0 ? '+' : '' }}{{ num(row.qty) }}</b></template></el-table-column>
          <el-table-column label="关联" width="130"><template #default="{ row }"><span class="no small">{{ row.order_no }}</span> {{ row.customer }}</template></el-table-column>
          <el-table-column prop="user" label="操作人" width="80" />
          <el-table-column prop="remark" label="备注" min-width="140" show-overflow-tooltip />
        </el-table>
        <el-pagination layout="prev, pager, next, total" :total="txns.total" :page-size="100" v-model:current-page="tq.page_no" @current-change="loadTxn" style="margin-top: 10px" />
      </el-tab-pane>
      <el-tab-pane label="售后备件统计" name="spare">
        <p class="muted small">售后备件“对外销售”与“维修领用”分开统计。</p>
        <el-table :data="spare" size="small">
          <el-table-column label="物料" min-width="200"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
          <el-table-column label="销售出库" width="120" align="right"><template #default="{ row }"><span class="num">{{ num(row.sale) }}</span></template></el-table-column>
          <el-table-column label="维修领用" width="120" align="right"><template #default="{ row }"><span class="num">{{ num(row.use) }}</span></template></el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="lotsOpen" :title="`批次：${lotsTitle}`" size="620px">
      <el-table :data="lots" size="small">
        <el-table-column prop="lot_no" label="批次" width="150" />
        <el-table-column prop="warehouse" label="仓库" width="100" />
        <el-table-column prop="location" label="库位" width="70" />
        <el-table-column label="数量" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column label="入库" width="100"><template #default="{ row }"><span class="num small">{{ day(row.received_at) }}</span></template></el-table-column>
        <el-table-column v-if="auth.can('WAREHOUSE')" width="70"><template #default="{ row }"><el-button link size="small" @click="adjust(row)">盘点</el-button></template></el-table-column>
      </el-table>
      <p class="muted small">按入库时间从早到晚排列，领料时从上往下扣（先进先出）。</p>
    </el-drawer>

    <el-dialog v-model="trOpen" title="库存调拨" width="460px">
      <el-form label-width="80px">
        <el-form-item label="物料"><MaterialSelect v-model="tr.material_id" /></el-form-item>
        <el-form-item label="调出"><RefSelect v-model="tr.from_warehouse_id" kind="warehouse" /></el-form-item>
        <el-form-item label="调入"><RefSelect v-model="tr.to_warehouse_id" kind="warehouse" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="tr.qty" :min="0.01" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="tr.remark" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveTr">调拨</el-button></template>
    </el-dialog>

    <el-dialog v-model="spOpen" title="售后备件出库" width="460px">
      <el-form label-width="80px">
        <el-form-item label="用途"><el-radio-group v-model="sp.kind"><el-radio-button value="SALE">对外销售</el-radio-button><el-radio-button value="USE">维修领用</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="物料"><MaterialSelect v-model="sp.material_id" /></el-form-item>
        <el-form-item label="仓库"><RefSelect v-model="sp.warehouse_id" kind="warehouse" purpose="AFTERSALES" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="sp.qty" :min="0.01" /></el-form-item>
        <el-form-item v-if="sp.kind === 'SALE'" label="客户" required><el-input v-model="sp.customer" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="sp.remark" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveSp">出库</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import RefSelect from '../components/RefSelect.vue'
import { MAT_TYPE, day, dt, num } from '../utils'

defineOptions({ name: 'Stock' })
const auth = useAuth()
const tab = ref('onhand')
const purpose = ref('PRODUCTION')
const kw = ref('')
const flagOnly = ref(false)
const onhand = ref([])
const onhandRows = computed(() => onhand.value.filter((r) => !flagOnly.value || r.flag))
async function loadOnhand() { onhand.value = await get('/stock/onhand', { purpose: purpose.value, keyword: kw.value }) }
const tq = ref({ material_id: null, txn_type: '', page_no: 1, size: 100 })
const range = ref(null)
const txns = ref({ items: [], total: 0 })
async function loadTxn() {
  txns.value = await get('/stock/txns', { ...tq.value, material_id: tq.value.material_id || undefined, date_from: range.value?.[0], date_to: range.value?.[1] })
}
const spare = ref([])
watch(tab, (t) => { if (t === 'txn') loadTxn(); if (t === 'spare') get('/stock/spare-report').then((r) => (spare.value = r)) })
function showTxn(row) { tq.value.material_id = row.material_id; tab.value = 'txn'; loadTxn() }
const lotsOpen = ref(false)
const lots = ref([])
const lotsTitle = ref('')
let lotRow = null
async function showLots(row) {
  lotRow = row
  lotsTitle.value = `${row.code} ${row.name}（${row.warehouse}）`
  lots.value = await get('/stock/lots', { material_id: row.material_id, warehouse_id: row.warehouse_id })
  lotsOpen.value = true
}
async function adjust(lot) {
  const { value } = await ElMessageBox.prompt(`批次 ${lot.lot_no} 实盘数量（当前 ${num(lot.qty)}）`, '盘点调整', { inputValue: String(Number(lot.qty)) })
  const { value: why } = await ElMessageBox.prompt('调整原因', '盘点调整')
  await post('/stock/adjust', { lot_id: lot.id, new_qty: Number(value), remark: why })
  ElMessage.success('已调整')
  showLots(lotRow); loadOnhand()
}
const trOpen = ref(false)
const tr = ref({})
function openTransfer() { tr.value = { material_id: null, from_warehouse_id: null, to_warehouse_id: null, qty: 1, remark: '' }; trOpen.value = true }
async function saveTr() { await post('/stock/transfer', tr.value); trOpen.value = false; ElMessage.success('已调拨'); loadOnhand() }
const spOpen = ref(false)
const sp = ref({})
function openSpare() { sp.value = { kind: 'SALE', material_id: null, warehouse_id: null, qty: 1, customer: '', remark: '' }; spOpen.value = true }
async function saveSp() { await post('/stock/spare-out', sp.value); spOpen.value = false; ElMessage.success('已出库'); loadOnhand() }
onMounted(loadOnhand)
onActivated(loadOnhand)
</script>
