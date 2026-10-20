<template>
  <div class="page">
    <div class="page-head">
      <div><h1>来料异常</h1><p>发起 → 质检审核定处置和责任方 → 退换/返修复检 → 关闭。每一步留痕，超时自动预警。</p></div>
      <div class="toolbar">
        <el-select v-model="status" style="width: 140px" @change="load">
          <el-option value="DRAFT,SUBMITTED,REJECTED,DISPOSED,AWAIT_REINSPECT,REINSPECTED" label="未关闭" />
          <el-option value="CLOSED" label="已关闭" /><el-option value="" label="全部" />
        </el-select>
        <el-input v-model="kw" placeholder="单号 / 物料 / 描述" clearable style="width: 180px" @change="load" />
        <el-button type="primary" @click="openNew">发起异常</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list" @row-click="openDetail" style="cursor: pointer">
        <el-table-column label="单号" width="140"><template #default="{ row }"><span class="no">{{ row.exc_no }}</span></template></el-table-column>
        <el-table-column label="类别" width="70"><template #default="{ row }">{{ row.category === 'QTY' ? '数量' : '质量' }}</template></el-table-column>
        <el-table-column label="物料" min-width="160"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column prop="supplier" label="供应商" min-width="120" />
        <el-table-column label="数量" width="70" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        <el-table-column label="处置" width="120"><template #default="{ row }">{{ row.disposition_label }}<div class="small muted">{{ row.responsibility_label }}</div></template></el-table-column>
        <el-table-column label="时限" width="100"><template #default="{ row }"><span class="num" :class="{ short: row.status !== 'CLOSED' && row.deadline < todayStr }">{{ row.deadline }}</span></template></el-table-column>
        <el-table-column label="状态" width="86"><template #default="{ row }"><StatusTag :map="EXC_STATUS" :value="row.status" /></template></el-table-column>
      </el-table>
    </div>

    <el-drawer v-model="detailOpen" :title="e ? `${e.exc_no}　${e.category === 'QTY' ? '数量异常' : '质量异常'}` : ''" size="560px">
      <template v-if="e">
        <div class="kv">
          <div><span>物料</span><b>{{ e.code }} {{ e.name }}</b></div>
          <div><span>供应商</span><b>{{ e.supplier || '—' }}</b></div>
          <div><span>数量</span><b class="num">{{ num(e.qty) }}</b></div>
          <div><span>关联到货</span><b class="no">{{ e.arrival_no || '—' }}</b></div>
          <div><span>状态</span><b><StatusTag :map="EXC_STATUS" :value="e.status" /></b></div>
          <div><span>处理时限</span><b class="num">{{ e.deadline }}</b></div>
          <div><span>处置 / 责任方</span><b>{{ e.disposition_label || '—' }} / {{ e.responsibility_label || '—' }}</b></div>
          <div><span>责任人</span><b>{{ e.owner || '—' }}</b></div>
        </div>
        <p class="desc">{{ e.description }}</p>

        <div class="act" v-if="e.status !== 'CLOSED'">
          <template v-if="['DRAFT', 'REJECTED'].includes(e.status)">
            <el-button type="primary" @click="go('submit', { comment: '' })">提交审核</el-button>
          </template>
          <template v-if="e.status === 'SUBMITTED' && auth.can('QUALITY', 'MANAGER')">
            <el-form label-width="70px">
              <el-form-item label="处置">
                <el-radio-group v-model="dp.disposition">
                  <el-radio-button v-for="(v, k) in (e.category === 'QTY' ? QTY_DISP : QUALITY_DISP)" :key="k" :value="k">{{ v }}</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="责任方"><el-radio-group v-model="dp.responsibility"><el-radio v-for="(v, k) in RESP" :key="k" :value="k">{{ v }}</el-radio></el-radio-group></el-form-item>
              <el-form-item label="责任人"><RefSelect v-model="dp.owner_id" kind="user" /></el-form-item>
              <el-form-item label="意见"><el-input v-model="dp.comment" /></el-form-item>
            </el-form>
            <el-button @click="suggest">{{ aiLabel }}</el-button>
            <el-button type="primary" :disabled="!dp.disposition || !dp.responsibility" @click="go('dispose', dp)">审核通过并处置</el-button>
            <el-button type="danger" plain @click="rejectIt">驳回</el-button>
          </template>
          <template v-if="e.status === 'AWAIT_REINSPECT' && auth.can('QUALITY')">
            <span class="small">复检合格数</span>
            <el-input-number v-model="rq" :min="0" :max="Number(e.qty)" />
            <el-button type="primary" @click="go('reinspect', { qualified: rq })">提交复检</el-button>
          </template>
          <template v-if="['DISPOSED', 'REINSPECTED'].includes(e.status) && auth.can('QUALITY', 'MANAGER')">
            <el-input v-model="result" placeholder="处理结果，例如：供应商已换货，复检合格入库" />
            <el-button type="success" :disabled="!result" @click="go('close', { result })" style="margin-top: 8px">关闭</el-button>
          </template>
        </div>
        <div v-else class="panel"><b>处理结果：</b>{{ e.result }}</div>

        <h3>处理记录</h3>
        <el-timeline>
          <el-timeline-item v-for="(l, i) in e.logs" :key="i" :timestamp="dt(l.created_at)">
            <b>{{ l.user }}</b> {{ ACT[l.action] || l.action }}<span v-if="l.to_status">　→ {{ EXC_STATUS[l.to_status]?.[0] }}</span>
            <div v-if="l.comment" class="muted small">{{ l.comment }}</div>
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-drawer>

    <el-dialog v-model="newOpen" title="发起来料异常" width="500px">
      <el-form label-width="80px">
        <el-form-item label="类别"><el-radio-group v-model="nf.category"><el-radio-button value="QUALITY">质量</el-radio-button><el-radio-button value="QTY">数量</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="物料"><MaterialSelect v-model="nf.material_id" /></el-form-item>
        <el-form-item label="供应商"><RefSelect v-model="nf.supplier_id" kind="supplier" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="nf.qty" :min="0.01" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="nf.description" type="textarea" :rows="3" placeholder="在哪个环节发现、什么问题" /></el-form-item>
        <el-form-item label="处理时限"><el-input-number v-model="nf.deadline_days" :min="1" /> 天</el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveNew">保存草稿</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import RefSelect from '../components/RefSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { EXC_STATUS, QTY_DISP, QUALITY_DISP, RESP, dt, num, today } from '../utils'

defineOptions({ name: 'Exceptions' })
const ACT = { CREATE: '发起', SUBMIT: '提交', REJECT: '驳回', DISPOSE: '审核处置', REINSPECT: '复检', CLOSE: '关闭' }
const auth = useAuth()
const todayStr = today()
const status = ref('DRAFT,SUBMITTED,REJECTED,DISPOSED,AWAIT_REINSPECT,REINSPECTED')
const kw = ref('')
const list = ref([])
async function load() { list.value = (await get('/exceptions', { status: status.value, keyword: kw.value })).items }
const detailOpen = ref(false)
const e = ref(null)
const dp = ref({})
const rq = ref(0)
const result = ref('')
const aiLabel = ref('智能建议')
async function openDetail(row) {
  e.value = await get(`/exceptions/${row.id}`)
  dp.value = { disposition: '', responsibility: '', owner_id: null, comment: '' }
  rq.value = Number(e.value.qty)
  result.value = ''
  detailOpen.value = true
}
async function go(action, body) {
  e.value = await post(`/exceptions/${e.value.id}/${action}`, body)
  ElMessage.success('已处理')
  load()
}
async function rejectIt() {
  const { value } = await ElMessageBox.prompt('驳回原因', '驳回')
  go('reject', { comment: value })
}
async function suggest() {
  const r = await post('/exceptions/classify', { text: e.value.description })
  // 只作参考：不自动填责任方；处置方式预填后仍由审批人确认
  if (r.suggested_disposition) dp.value.disposition = r.suggested_disposition
  const causes = (r.possible_causes || []).join('；')
  const ev = (r.evidence_to_verify || []).join('、')
  ElMessageBox.alert(`可能原因：${causes || '—'}\n\n待核实证据：${ev || '—'}\n\n${r.disclaimer || ''}`,
    `${r.source === 'AI' ? 'AI' : '规则'}辅助提示（责任方请自行判定）`)
}
const newOpen = ref(false)
const nf = ref({})
function openNew() { nf.value = { category: 'QUALITY', material_id: null, supplier_id: null, qty: 1, description: '', deadline_days: 3 }; newOpen.value = true }
async function saveNew() { const r = await post('/exceptions', nf.value); newOpen.value = false; load(); openDetail(r) }
onMounted(load)
onActivated(load)
</script>

<style scoped>
.desc { background: var(--bg); padding: 10px 12px; border-radius: 3px; line-height: 1.6; margin: 14px 0; }
.act { border-top: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); padding: 14px 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.act .el-form { width: 100%; }
h3 { font-size: 14px; margin: 18px 0 10px; }
</style>
