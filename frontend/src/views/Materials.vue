<template>
  <div class="page">
    <div class="page-head">
      <div><h1>物料</h1><p>“通用件”参与多订单冲填抢料；生产库与售后备件库各自设置安全库存上下限。</p></div>
      <div class="toolbar">
        <el-input v-model="q.keyword" placeholder="编码 / 名称 / 图号 / 规格" clearable style="width: 220px" @change="load" />
        <el-select v-model="q.type" clearable placeholder="类型" style="width: 100px" @change="load"><el-option v-for="(v, k) in MAT_TYPE" :key="k" :value="k" :label="v" /></el-select>
        <el-button v-if="auth.can('TECH', 'PLANNER')" type="primary" @click="edit()">新建物料</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list.items" size="small" @row-dblclick="edit">
        <el-table-column label="编码" width="150"><template #default="{ row }"><span class="no">{{ row.code }}</span></template></el-table-column>
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="spec" label="规格" min-width="140" />
        <el-table-column label="类型" width="60"><template #default="{ row }">{{ row.type_label }}</template></el-table-column>
        <el-table-column label="图号" width="150"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
        <el-table-column label="标记" width="110"><template #default="{ row }"><el-tag v-if="row.is_common" size="small" effect="plain">通用件</el-tag> <el-tag v-if="row.is_spare" size="small" effect="plain" type="info">备件</el-tag></template></el-table-column>
        <el-table-column label="提前期" width="70" align="right"><template #default="{ row }"><span class="num">{{ row.lead_time_days }}</span> 天</template></el-table-column>
        <el-table-column label="生产库安全库存" width="130" align="right"><template #default="{ row }"><span class="num">{{ num(row.safety_min) }} ~ {{ Number(row.safety_max) ? num(row.safety_max) : '不限' }}</span></template></el-table-column>
        <el-table-column label="备件库安全库存" width="130" align="right"><template #default="{ row }"><span class="num">{{ num(row.spare_min) }} ~ {{ Number(row.spare_max) ? num(row.spare_max) : '不限' }}</span></template></el-table-column>
        <el-table-column width="60"><template #default="{ row }"><el-button link size="small" @click="edit(row)">编辑</el-button></template></el-table-column>
      </el-table>
      <el-pagination v-if="list.total > 50" layout="prev, pager, next, total" :total="list.total" :page-size="50" v-model:current-page="q.page_no" @current-change="load" style="margin-top: 10px" />
    </div>

    <el-dialog v-model="open" :title="f.id ? '编辑物料' : '新建物料'" width="640px">
      <el-form :model="f" label-width="110px">
        <div class="grid-2">
          <el-form-item label="编码"><el-input v-model="f.code" :disabled="!!f.id" /></el-form-item>
          <el-form-item label="名称"><el-input v-model="f.name" /></el-form-item>
          <el-form-item label="规格"><el-input v-model="f.spec" /></el-form-item>
          <el-form-item label="单位"><el-input v-model="f.unit" /></el-form-item>
          <el-form-item label="类型"><el-select v-model="f.type"><el-option v-for="(v, k) in MAT_TYPE" :key="k" :value="k" :label="v" /></el-select></el-form-item>
          <el-form-item label="图号"><el-input v-model="f.drawing_no" :disabled="!!f.id" :placeholder="f.id ? '' : ''" /><div v-if="f.id" class="small muted">改图号请用【图纸 → 技改换图号】</div></el-form-item>
          <el-form-item label="提前期（天）"><el-input-number v-model="f.lead_time_days" :min="0" /></el-form-item>
          <el-form-item label="到货后检验/转运（天）"><el-input-number v-model="f.inbound_days" :min="0" :max="60" /></el-form-item>
          <el-form-item label="免 BOM"><el-switch v-model="f.bom_exempt" /><span class="small muted" style="margin-left:8px">自制/外协件确无物料投入的工艺才勾选；没有 BOM 又没勾选，不能开工/领料/报工</span></el-form-item>
          <el-form-item label="免图纸管控"><el-switch v-model="f.drawing_exempt" /><span class="small muted" style="margin-left:8px">确无受控图纸的标准件才勾选；填了图号又没发布图纸会拦开工</span></el-form-item>
          <el-form-item label="标记"><el-checkbox v-model="f.is_common">通用件</el-checkbox><el-checkbox v-model="f.is_spare">售后备件</el-checkbox></el-form-item>
          <el-form-item label="生产库下限"><el-input-number v-model="f.safety_min" :min="0" /></el-form-item>
          <el-form-item label="生产库上限"><el-input-number v-model="f.safety_max" :min="0" /><div class="small muted">0 = 不检查积压</div></el-form-item>
          <el-form-item label="备件库下限"><el-input-number v-model="f.spare_min" :min="0" /></el-form-item>
          <el-form-item label="备件库上限"><el-input-number v-model="f.spare_max" :min="0" /></el-form-item>
          <el-form-item label="默认供应商"><RefSelect v-model="f.default_supplier_id" kind="supplier" /></el-form-item>
          <el-form-item label="默认采购员"><RefSelect v-model="f.default_buyer_id" kind="user" /></el-form-item>
        </div>
      </el-form>
      <template #footer><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import RefSelect from '../components/RefSelect.vue'
import { MAT_TYPE, num } from '../utils'

defineOptions({ name: 'Materials' })
const auth = useAuth()
const q = ref({ keyword: '', type: '', page_no: 1, size: 50 })
const list = ref({ items: [], total: 0 })
async function load() { list.value = await get('/materials', q.value) }
const open = ref(false)
const f = ref({})
function edit(row) {
  f.value = row ? { ...row, safety_min: Number(row.safety_min), safety_max: Number(row.safety_max), spare_min: Number(row.spare_min), spare_max: Number(row.spare_max) }
    : { code: '', name: '', spec: '', unit: '件', type: 'BUY', drawing_no: '', drawing_exempt: false, bom_exempt: false, inbound_days: 1, is_common: false, is_spare: false, lead_time_days: 7, safety_min: 0, safety_max: 0, spare_min: 0, spare_max: 0, default_buyer_id: null, default_supplier_id: null, is_active: true }
  open.value = true
}
async function save() {
  const { id, type_label: _t, ...body } = f.value
  if (id) await put(`/materials/${id}`, body); else await post('/materials', body)
  open.value = false
  ElMessage.success('已保存')
  load()
}
onMounted(load)
onActivated(load)
</script>
