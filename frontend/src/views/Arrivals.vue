<template>
  <div class="page">
    <div class="page-head">
      <div><h1>到货检验</h1><p>到货登记后进入待检；检验合格自动入库并触发齐套重算，不合格自动生成来料异常单。</p></div>
      <div class="toolbar">
        <el-radio-group v-model="status" size="small" @change="load">
          <el-radio-button value="PENDING">待检验</el-radio-button>
          <el-radio-button value="INSPECTED">已检验</el-radio-button>
          <el-radio-button value="">全部</el-radio-button>
        </el-radio-group>
        <el-button v-if="auth.can('WAREHOUSE')" type="primary" @click="openArr">登记到货</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list">
        <el-table-column label="到货单" width="140"><template #default="{ row }"><span class="no">{{ row.arrival_no }}</span></template></el-table-column>
        <el-table-column label="来源" width="70"><template #default="{ row }">{{ row.source_type === 'PO' ? '采购' : '外协' }}</template></el-table-column>
        <el-table-column prop="supplier" label="供应商" min-width="130" />
        <el-table-column label="物料" min-width="170"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column label="到货" width="80" align="right"><template #default="{ row }"><b class="num">{{ num(row.qty) }}</b> {{ row.unit }}</template></el-table-column>
        <el-table-column label="合格 / 不合格" width="110" align="right"><template #default="{ row }"><template v-if="row.status !== 'PENDING'"><span class="num ok">{{ num(row.qualified_qty) }}</span> / <span class="num short">{{ num(row.rejected_qty) }}</span></template></template></el-table-column>
        <el-table-column label="到货时间" width="140"><template #default="{ row }"><span class="num small">{{ dt(row.arrived_at) }}</span></template></el-table-column>
        <el-table-column prop="inspect_note" label="说明" min-width="140" show-overflow-tooltip />
        <el-table-column width="90"><template #default="{ row }"><el-button v-if="row.status === 'PENDING' && auth.can('QUALITY', 'WAREHOUSE')" type="primary" size="small" @click="openInsp(row)">检验</el-button></template></el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="arrOpen" title="登记到货" width="720px">
      <el-input v-model="kw" placeholder="搜索采购单号 / 供应商 / 物料" @change="loadSrc" clearable />
      <el-table :data="srcs" size="small" highlight-current-row @current-change="(r) => (sel = r)" height="260" style="margin-top: 8px">
        <el-table-column prop="no" label="单号" width="150" />
        <el-table-column prop="supplier" label="供应商" />
        <el-table-column label="物料"><template #default="{ row }">{{ row.code }} {{ row.name }}</template></el-table-column>
        <el-table-column label="未到" width="80"><template #default="{ row }">{{ num(row.open_qty) }}</template></el-table-column>
      </el-table>
      <el-form v-if="sel" label-width="110px" style="margin-top: 12px">
        <el-form-item label="实到数量"><el-input-number v-model="af.qty" :min="0.01" :max="Number(sel.open_qty)" /></el-form-item>
        <el-form-item label="送货单数量"><el-input-number v-model="af.expected_qty" :min="0" placeholder="与实到不符时填写" /><span class="muted small" style="margin-left: 8px">少于送货单数量会自动生成数量异常</span></el-form-item>
        <el-form-item label="备注"><el-input v-model="af.note" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" :disabled="!sel" @click="saveArr">登记</el-button></template>
    </el-dialog>

    <el-dialog v-model="inspOpen" :title="`检验 ${cur?.arrival_no}`" width="500px">
      <div class="muted small" style="margin-bottom: 10px">{{ cur?.code }} {{ cur?.name }}，到货 {{ num(cur?.qty) }} {{ cur?.unit }}</div>
      <el-form label-width="90px">
        <el-form-item label="合格数量"><el-input-number v-model="inf.qualified" :min="0" :max="Number(cur?.qty)" @change="(v) => (inf.rejected = Number(cur.qty) - v)" /></el-form-item>
        <el-form-item label="不合格"><el-input-number v-model="inf.rejected" :min="0" :max="Number(cur?.qty)" @change="(v) => (inf.qualified = Number(cur.qty) - v)" /></el-form-item>
        <el-form-item v-if="inf.rejected > 0" label="不合格原因" required>
          <el-input v-model="inf.reject_reason" type="textarea" :rows="2" placeholder="例如：表面锈蚀、尺寸超差" />
          <el-button v-if="aiOn" link size="small" @click="classify">让 AI 判断责任方</el-button>
          <div v-if="cls" class="small muted">建议：{{ cls.category === 'QTY' ? '数量异常' : '质量异常' }}，责任方 {{ RESP[cls.responsibility] }}，处置 {{ QUALITY_DISP[cls.disposition] || QTY_DISP[cls.disposition] }}（{{ cls.reason }}）</div>
        </el-form-item>
        <el-form-item label="入库仓库"><RefSelect v-model="inf.warehouse_id" kind="warehouse" placeholder="默认生产物料库" /></el-form-item>
        <el-form-item label="批次号"><el-input v-model="inf.lot_no" placeholder="默认用到货单号" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="inf.note" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveInsp">提交检验结果</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import RefSelect from '../components/RefSelect.vue'
import { QTY_DISP, QUALITY_DISP, RESP, dt, num } from '../utils'

defineOptions({ name: 'Arrivals' })
const auth = useAuth()
const status = ref('PENDING')
const list = ref([])
const aiOn = ref(true)
async function load() { list.value = (await get('/arrivals', { status: status.value })).items }
const arrOpen = ref(false)
const kw = ref('')
const srcs = ref([])
const sel = ref(null)
const af = ref({})
async function loadSrc() { srcs.value = await get('/arrival-sources', { keyword: kw.value }) }
function openArr() { sel.value = null; af.value = { qty: 1, expected_qty: undefined, note: '' }; loadSrc(); arrOpen.value = true }
async function saveArr() {
  const r = await post('/arrivals', { source_type: sel.value.source_type, source_id: sel.value.source_id, ...af.value })
  arrOpen.value = false
  ElMessage.success(r.exception_no ? `已登记，数量短少已生成异常单 ${r.exception_no}` : '已登记，等待检验')
  load()
}
const inspOpen = ref(false)
const cur = ref(null)
const inf = ref({})
const cls = ref(null)
function openInsp(row) { cur.value = row; cls.value = null; inf.value = { qualified: Number(row.qty), rejected: 0, reject_reason: '', warehouse_id: null, lot_no: '', note: '' }; inspOpen.value = true }
async function classify() { cls.value = await post('/exceptions/classify', { text: inf.value.reject_reason || '不合格' }) }
async function saveInsp() {
  const r = await post(`/arrivals/${cur.value.id}/inspect`, inf.value)
  inspOpen.value = false
  ElMessage.success(r.exception_no ? `合格品已入库；不合格已生成异常单 ${r.exception_no}` : '已入库，齐套已重算')
  load()
}
onMounted(load)
onActivated(load)
</script>
