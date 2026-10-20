<template>
  <div class="page">
    <div class="page-head">
      <div><h1>BOM</h1><p>由技术统一维护。生效版本不可直接改，需复制为新版本修改后再生效；已下达订单仍按下达时的版本执行。</p></div>
      <div class="toolbar">
        <el-input v-model="kw" placeholder="机型 / 物料" clearable style="width: 200px" @change="load" />
        <el-button v-if="auth.can('TECH')" type="primary" @click="openNew">新建 BOM</el-button>
      </div>
    </div>
    <div class="split">
      <div class="panel">
        <el-table :data="list" size="small" highlight-current-row @current-change="pick">
          <el-table-column prop="material" label="物料" min-width="200" />
          <el-table-column prop="version" label="版本" width="60" />
          <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : row.status === 'DRAFT' ? 'warning' : 'info'">{{ { ACTIVE: '生效', DRAFT: '草稿', OBSOLETE: '作废' }[row.status] }}</el-tag></template></el-table-column>
          <el-table-column label="成熟" width="56"><template #default="{ row }">{{ row.is_mature ? '是' : '' }}</template></el-table-column>
          <el-table-column prop="line_count" label="子件" width="56" />
        </el-table>
      </div>
      <div class="panel" v-if="b">
        <div class="dh">
          <h2 style="margin: 0">{{ b.material }}　{{ b.version }}</h2>
          <div class="toolbar">
            <el-radio-group v-model="view" size="small"><el-radio-button value="edit">单层</el-radio-button><el-radio-button value="tree">多级展开</el-radio-button></el-radio-group>
            <template v-if="auth.can('TECH')">
              <el-button size="small" @click="copy">复制为新版本</el-button>
              <el-button v-if="b.status === 'DRAFT'" size="small" type="primary" @click="save">保存</el-button>
              <el-button v-if="b.status === 'DRAFT'" size="small" type="success" @click="activate">生效</el-button>
            </template>
          </div>
        </div>
        <template v-if="view === 'edit'">
          <el-checkbox v-model="b.is_mature" :disabled="b.status !== 'DRAFT'">成熟机型（技术统一维护）</el-checkbox>
          <el-table :data="b.lines" size="small" style="margin-top: 8px">
            <el-table-column label="子件" min-width="230">
              <template #default="{ row }"><MaterialSelect v-if="b.status === 'DRAFT'" v-model="row.child_id" /><span v-else><span class="no">{{ row.code }}</span> {{ row.name }}</span></template>
            </el-table-column>
            <el-table-column label="类型" width="56"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
            <el-table-column label="图号" width="140"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
            <el-table-column label="单位用量" width="120"><template #default="{ row }"><el-input-number v-if="b.status === 'DRAFT'" v-model="row.qty_per" :min="0.0001" size="small" controls-position="right" /><span v-else class="num">{{ num(row.qty_per, 4) }}</span></template></el-table-column>
            <el-table-column label="损耗率" width="110"><template #default="{ row }"><el-input-number v-if="b.status === 'DRAFT'" v-model="row.scrap_rate" :min="0" :max="1" :step="0.01" size="small" controls-position="right" /><span v-else class="num">{{ pct(row.scrap_rate) }}</span></template></el-table-column>
            <el-table-column v-if="b.status === 'DRAFT'" width="50"><template #default="{ $index }"><el-button link type="danger" @click="b.lines.splice($index, 1)">删</el-button></template></el-table-column>
          </el-table>
          <el-button v-if="b.status === 'DRAFT'" size="small" style="margin-top: 8px" @click="b.lines.push({ child_id: null, qty_per: 1, scrap_rate: 0, seq: (b.lines.length + 1) * 10 })">加子件</el-button>
        </template>
        <template v-else>
          <div class="toolbar" style="margin-bottom: 8px"><span class="small">按</span><el-input-number v-model="treeQty" :min="1" size="small" @change="loadTree" /><span class="small">台展开</span></div>
          <el-table :data="tree" row-key="id" default-expand-all size="small">
            <el-table-column label="物料" min-width="240"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
            <el-table-column label="类型" width="56"><template #default="{ row }">{{ MAT_TYPE[row.type] }}</template></el-table-column>
            <el-table-column label="图号" width="140"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
            <el-table-column label="总需求" width="100" align="right"><template #default="{ row }"><span class="num">{{ num(row.total, 3) }}</span> {{ row.unit }}</template></el-table-column>
          </el-table>
        </template>
      </div>
      <div class="panel muted" v-else>选择左侧 BOM</div>
    </div>

    <el-dialog v-model="newOpen" title="新建 BOM" width="420px">
      <el-form label-width="70px">
        <el-form-item label="父物料"><MaterialSelect v-model="nf.material_id" /></el-form-item>
        <el-form-item label="版本"><el-input v-model="nf.version" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveNew">创建</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import { MAT_TYPE, num, pct } from '../utils'

defineOptions({ name: 'Boms' })
const auth = useAuth()
const kw = ref('')
const list = ref([])
const b = ref(null)
const view = ref('edit')
const tree = ref([])
const treeQty = ref(1)
async function load() { list.value = await get('/boms', { keyword: kw.value }) }
async function pick(row) { if (row) { b.value = await get(`/boms/${row.id}`); if (view.value === 'tree') loadTree() } }
async function loadTree() { tree.value = await get(`/boms/${b.value.id}/tree`, { qty: treeQty.value }) }
watch(view, (v) => v === 'tree' && b.value && loadTree())
const body = () => ({ material_id: b.value.material_id, version: b.value.version, is_mature: b.value.is_mature, remark: b.value.remark,
  lines: b.value.lines.filter((l) => l.child_id).map((l, i) => ({ child_id: l.child_id, qty_per: l.qty_per, scrap_rate: l.scrap_rate || 0, seq: (i + 1) * 10 })) })
async function save() { b.value = await put(`/boms/${b.value.id}`, body()); ElMessage.success('已保存'); load() }
async function activate() {
  await put(`/boms/${b.value.id}`, body())
  b.value = await post(`/boms/${b.value.id}/activate`)
  ElMessage.success('已生效，齐套已按新 BOM 重算')
  load()
}
async function copy() {
  const { value } = await ElMessageBox.prompt('新版本号', '复制为新版本', { inputValue: 'V' + (Number(b.value.version.replace(/\D/g, '')) + 1) })
  b.value = await post(`/boms/${b.value.id}/copy`, null, { params: { version: value } })
  load()
}
const newOpen = ref(false)
const nf = ref({ material_id: null, version: 'V1' })
async function saveNew() { b.value = await post('/boms', { ...nf.value, lines: [] }); newOpen.value = false; load() }
function openNew() { nf.value = { material_id: null, version: 'V1' }; newOpen.value = true }
onMounted(load)
onActivated(load)
</script>

<style scoped>
.split { display: grid; grid-template-columns: minmax(0, .8fr) minmax(0, 1.4fr); gap: 16px; align-items: start; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
.split .panel + .panel { margin-top: 0; }
.dh { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
</style>
