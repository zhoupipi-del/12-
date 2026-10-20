<template>
  <div class="page">
    <div class="page-head">
      <div><h1>图纸版本</h1><p>每张图同一时刻只有一个生效版本。订单/任务绑定自己的批准版本：升版不会让在制单据被动换版，由技术看完影响后逐单指定切换；重大安全/质量变更可强制暂停。</p></div>
      <div class="toolbar">
        <el-input v-model="kw" placeholder="图号 / 名称" clearable style="width: 200px" @change="load" />
        <el-button v-if="auth.can('TECH')" @click="chgOpen = true">技改换图号</el-button>
        <el-button v-if="auth.can('TECH')" type="primary" @click="newOpen = true">新建图纸</el-button>
      </div>
    </div>
    <div class="split">
      <div class="panel">
        <el-table :data="list" highlight-current-row @current-change="pick" size="small">
          <el-table-column label="图号" min-width="170"><template #default="{ row }"><span class="no">{{ row.drawing_no }}</span></template></el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="生效版" width="70"><template #default="{ row }"><b class="num">{{ row.current_version || '—' }}</b></template></el-table-column>
          <el-table-column width="60"><template #default="{ row }"><el-tag v-if="row.has_draft" size="small" type="warning">草稿</el-tag></template></el-table-column>
        </el-table>
      </div>
      <div class="panel" v-if="d">
        <div class="dh">
          <div><h2 style="margin: 0"><span class="no">{{ d.drawing_no }}</span>　{{ d.name }}</h2></div>
          <el-button v-if="auth.can('TECH')" type="primary" size="small" @click="upOpen = true">上传新版本</el-button>
        </div>
        <div class="ver" v-for="v in d.versions" :key="v.id" :class="v.status">
          <div class="vh">
            <b class="num vnum">{{ v.version }}</b>
            <el-tag size="small" :type="v.status === 'RELEASED' ? 'success' : v.status === 'DRAFT' ? 'warning' : 'info'">{{ { RELEASED: '生效', DRAFT: '草稿', SUPERSEDED: '已被替代(在制仍可用)', OBSOLETE: '已作废' }[v.status] }}</el-tag>
            <span class="muted small">{{ v.uploader }} 上传 {{ dt(v.created_at) }}<template v-if="v.released_at">，{{ v.releaser }} 发布 {{ dt(v.released_at) }}</template></span>
          </div>
          <div class="small">{{ v.change_note }}</div>
          <div class="toolbar" style="margin-top: 6px">
            <el-button v-if="v.pdf_path" link size="small" @click="openFile(v.pdf_path)">查阅 PDF</el-button>
            <el-button v-if="v.file_path && v.file_path !== v.pdf_path" link size="small" @click="openFile(v.file_path)">下载原图 {{ v.file_name }}</el-button>
            <el-button v-if="v.status === 'DRAFT' && auth.can('TECH')" size="small" type="primary" @click="release(v)">发布生效</el-button>
          </div>
        </div>
        <div class="aff" v-if="binds.length">
          <b>在制单据绑定的批准版本（升版影响评估）</b>
          <el-table :data="binds" size="small" style="margin-top: 8px">
            <el-table-column prop="no" label="单据" width="150" />
            <el-table-column label="类型" width="80"><template #default="{ row }">{{ { SALES_ORDER: '订单', WORK_ORDER: '自制', OUTSOURCE: '外协' }[row.ref_type] }}</template></el-table-column>
            <el-table-column label="批准版" width="70"><template #default="{ row }"><b class="num">{{ row.bound_version }}</b></template></el-table-column>
            <el-table-column label="状态" min-width="150">
              <template #default="{ row }">
                <el-tag v-if="row.hold" type="danger" size="small">已暂停：{{ row.hold_reason }}</el-tag>
                <el-tag v-else-if="row.upgrade_available" type="warning" size="small">有新版 {{ row.current_version }} 可切换</el-tag>
                <span v-else class="muted small">与生效版一致</span>
              </template>
            </el-table-column>
            <el-table-column width="170" v-if="auth.can('TECH')">
              <template #default="{ row }">
                <el-button v-if="row.upgrade_available" link size="small" type="primary" @click="doSwitch(row)">切换到 {{ row.current_version }}</el-button>
                <el-button v-if="row.hold" link size="small" @click="doResolve(row)">按原版继续</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div v-if="hasAffected" class="aff">
          <b>在制单据使用此图</b>
          <div v-if="d.affected.orders.length">订单：{{ d.affected.orders.join('、') }}</div>
          <div v-if="d.affected.work_orders.length">自制任务：{{ d.affected.work_orders.join('、') }}</div>
          <div v-if="d.affected.outsource.length">外协单：{{ d.affected.outsource.join('、') }}</div>
          <div v-if="d.affected.po_lines.length">采购行：{{ d.affected.po_lines.join('、') }}</div>
        </div>
      </div>
      <div class="panel muted" v-else>选择左侧图纸查看版本</div>
    </div>

    <el-dialog v-model="relOpen" title="发布生效" width="460px">
      <p class="small">发布 <b>{{ rel?.version }}</b> 版。在制单据继续按各自批准版本执行，不会被动换版。</p>
      <el-form label-width="90px">
        <el-form-item label="变更级别">
          <el-radio-group v-model="rf.change_level">
            <el-radio value="MINOR">一般变更</el-radio>
            <el-radio value="MAJOR">重大（安全/质量）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="rf.change_level === 'MAJOR'" label="暂停原因">
          <el-input v-model="rf.hold_reason" type="textarea" :rows="2" placeholder="将强制暂停所有仍绑定旧版的在制单据，直到技术逐单裁定" />
        </el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="doRelease">确认发布</el-button></template>
    </el-dialog>

    <el-dialog v-model="newOpen" title="新建图纸" width="440px">
      <el-form label-width="70px">
        <el-form-item label="图号"><el-input v-model="nf.drawing_no" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="nf.name" /></el-form-item>
        <el-form-item label="物料"><MaterialSelect v-model="nf.material_id" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveNew">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="upOpen" title="上传新版本" width="460px">
      <el-form label-width="80px">
        <el-form-item label="版本号"><el-input v-model="uf.version" placeholder="例如 C" /></el-form-item>
        <el-form-item label="变更说明"><el-input v-model="uf.change_note" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="原图"><input type="file" @change="(e) => (uf.file = e.target.files[0])" accept=".dwg,.dxf,.step,.stp,.igs,.zip,.pdf" /></el-form-item>
        <el-form-item label="受控 PDF"><input type="file" @change="(e) => (uf.pdf = e.target.files[0])" accept=".pdf" /><div class="small muted">车间在线查阅用，建议必传</div></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="upload">上传为草稿</el-button></template>
    </el-dialog>

    <el-dialog v-model="chgOpen" title="技改换图号" width="480px">
      <p class="small muted">更新物料图号，引用该物料的 BOM、未到货采购行、请购行、未发外协单自动同步。</p>
      <el-form label-width="80px">
        <el-form-item label="物料"><MaterialSelect v-model="cf.material_id" /></el-form-item>
        <el-form-item label="新图号"><el-input v-model="cf.new_drawing_no" /></el-form-item>
        <el-form-item label="技改原因"><el-input v-model="cf.reason" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveChg">执行</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http, { get, post } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import { dt } from '../utils'

defineOptions({ name: 'Drawings' })
const auth = useAuth()
const kw = ref('')
const list = ref([])
const d = ref(null)
const hasAffected = computed(() => d.value && Object.values(d.value.affected).some((x) => x.length))
async function load() { list.value = await get('/drawings', { keyword: kw.value }) }
const binds = ref([])
async function pick(row) {
  if (!row) return
  d.value = await get(`/drawings/${row.id}`)
  binds.value = await get(`/drawings/${row.id}/bindings`)
}
async function openFile(p) { const b = await http.get(`/files/${p}`, { responseType: 'blob' }); window.open(URL.createObjectURL(b), '_blank') }
const relOpen = ref(false)
const rel = ref(null)
const rf = ref({ change_level: 'MINOR', hold_reason: '' })
function release(v) { rel.value = v; rf.value = { change_level: 'MINOR', hold_reason: '' }; relOpen.value = true }
async function doRelease() {
  const r = await post(`/drawings/versions/${rel.value.id}/release`, rf.value)
  relOpen.value = false
  const n = Object.values(r.affected).reduce((s, x) => s + x.length, 0)
  ElMessage.success(r.held ? `已发布，${r.held} 张在制单据被强制暂停` : n ? `已发布；${n} 张在制单据仍按原批准版本，请评估后指定切换` : '已发布')
  pick(d.value); load()
}
async function doSwitch(row) {
  const { value } = await ElMessageBox.prompt(`${row.no} 切换到 ${row.current_version} 版，切换后需重新确认。请填写评估结论`, '指定切换', { inputPattern: /\S+/, inputErrorMessage: '必填' })
  await post('/drawings-switch', { ref_type: row.ref_type, ref_id: row.ref_id, drawing_id: d.value.id, new_version_id: row.current_version_id, reason: value })
  ElMessage.success('已切换，请车间重新确认新版')
  pick(d.value)
}
async function doResolve(row) {
  const { value } = await ElMessageBox.prompt(`${row.no} 解除暂停并按原版 ${row.bound_version} 继续。请填写裁定理由`, '技术裁定', { inputPattern: /\S+/, inputErrorMessage: '必填' })
  await post('/drawings-resolve-hold', { ref_type: row.ref_type, ref_id: row.ref_id, drawing_id: d.value.id, reason: value })
  ElMessage.success('已解除暂停')
  pick(d.value)
}
const newOpen = ref(false)
const nf = ref({ drawing_no: '', name: '', material_id: null })
async function saveNew() { await post('/drawings', nf.value); newOpen.value = false; load() }
const upOpen = ref(false)
const uf = ref({ version: '', change_note: '', file: null, pdf: null })
async function upload() {
  const fd = new FormData()
  fd.append('version', uf.value.version); fd.append('change_note', uf.value.change_note)
  if (uf.value.file) fd.append('file', uf.value.file)
  if (uf.value.pdf) fd.append('pdf', uf.value.pdf)
  await http.post(`/drawings/${d.value.id}/versions`, fd)
  upOpen.value = false
  uf.value = { version: '', change_note: '', file: null, pdf: null }
  pick(d.value); load()
}
const chgOpen = ref(false)
const cf = ref({ material_id: null, new_drawing_no: '', reason: '' })
async function saveChg() {
  const r = await post('/drawings-change-no', cf.value)
  chgOpen.value = false
  ElMessage.success(`${r.old} → ${r.new}：同步 BOM ${r.boms} 处、采购行 ${r.po_lines}、请购行 ${r.req_lines}、外协单 ${r.outsource}`)
  load()
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr); gap: 16px; align-items: start; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
.split .panel + .panel { margin-top: 0; }
.dh { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.ver { border-left: 3px solid var(--line); padding: 8px 12px; margin-bottom: 10px; }
.ver.RELEASED { border-left-color: var(--ok); background: #f4f9f6; }
.ver.DRAFT { border-left-color: var(--wait); }
.ver.OBSOLETE, .ver.SUPERSEDED { opacity: .65; }
.vh { display: flex; gap: 10px; align-items: center; }
.vnum { font-family: var(--num-c); font-size: 22px; }
.aff { margin-top: 16px; font-size: 13px; line-height: 1.8; background: var(--bg); padding: 10px 12px; }
</style>
