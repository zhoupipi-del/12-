<template>
  <div class="page">
    <div class="page-head">
      <div><h1>外协单</h1><p>发出前须确认图纸当前版本；回厂后在【到货检验】登记，合格品入库参与齐套。</p></div>
      <div class="toolbar">
        <el-select v-model="status" style="width: 130px" @change="load"><el-option value="PLANNED,SENT,PARTIAL" label="未完成" /><el-option value="" label="全部" /></el-select>
        <el-button v-if="auth.can('BUYER', 'PLANNER')" type="primary" @click="openNew">新建外协单</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list">
        <el-table-column label="外协单" width="140"><template #default="{ row }"><span class="no">{{ row.os_no }}</span></template></el-table-column>
        <el-table-column prop="supplier" label="外协厂" min-width="140" />
        <el-table-column label="物料" min-width="170"><template #default="{ row }"><span class="no">{{ row.code }}</span> {{ row.name }}</template></el-table-column>
        <el-table-column label="图号" width="150"><template #default="{ row }"><span class="no small">{{ row.drawing_no }}</span></template></el-table-column>
        <el-table-column label="数量 / 已回" width="100" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }} / {{ num(row.received_qty) }}</span></template></el-table-column>
        <el-table-column label="发出" width="100"><template #default="{ row }"><span class="num">{{ day(row.sent_date) }}</span></template></el-table-column>
        <el-table-column label="预计回厂" width="120"><template #default="{ row }"><span class="num">{{ day(row.expected_return) }}</span><div class="small muted">{{ relDay(row.expected_return) }}</div></template></el-table-column>
        <el-table-column label="状态" width="110"><template #default="{ row }"><StatusTag :map="OS_STATUS" :value="row.status" /><div class="small muted">{{ row.supply_mode === 'CONSIGNED' ? '本厂供料' : '包工包料' }}</div></template></el-table-column>
        <el-table-column width="170">
          <template #default="{ row }">
            <template v-if="row.status === 'PLANNED'">
              <el-button link size="small" @click="dwg(row)">确认图纸</el-button>
              <el-button v-if="row.supply_mode === 'CONSIGNED'" link size="small" @click="pickRaw(row)">领料给外协</el-button>
              <el-button link size="small" type="primary" @click="send(row)">发出</el-button>
            </template>
            <el-date-picker v-if="['SENT', 'PARTIAL'].includes(row.status)" v-model="row._d" size="small" value-format="YYYY-MM-DD" placeholder="改回厂日" style="width: 120px" @change="(v) => upd(row, v)" />
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="open" title="新建外协单" width="480px">
      <el-form label-width="80px">
        <el-form-item label="外协件"><MaterialSelect v-model="f.material_id" type="OUTSOURCE" /></el-form-item>
        <el-form-item label="外协厂"><RefSelect v-model="f.supplier_id" kind="outsourcer" /></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="f.qty" :min="1" /></el-form-item>
        <el-form-item label="单价"><el-input-number v-model="f.price" :min="0" :precision="2" /></el-form-item>
        <el-form-item label="预计回厂"><el-date-picker v-model="f.expected_return" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="供料方式">
          <el-radio-group v-model="f.supply_mode"><el-radio value="TURNKEY">包工包料</el-radio><el-radio value="CONSIGNED">本厂供料</el-radio></el-radio-group>
          <div class="small muted">本厂供料：原料先计入需求并扣出库存，发出前需领料过账</div>
        </el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="dwgOpen" title="确认图纸版本" width="560px">
      <el-table :data="dwgList" size="small">
        <el-table-column prop="drawing_no" label="图号" />
        <el-table-column prop="version" label="生效版本" width="90" />
        <el-table-column label="状态" width="120"><template #default="{ row }">{{ row.missing ? '未建受控图纸（需技术发布）' : row.confirmed ? '已确认' : '未确认' }}</template></el-table-column>
      </el-table>
      <template #footer><el-button type="primary" @click="confirmDwg">确认以上为当前版本</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post, put } from '../api'
import { useAuth } from '../store'
import MaterialSelect from '../components/MaterialSelect.vue'
import RefSelect from '../components/RefSelect.vue'
import StatusTag from '../components/StatusTag.vue'
import { OS_STATUS, day, num, relDay, today } from '../utils'

defineOptions({ name: 'Outsource' })
const auth = useAuth()
const status = ref('PLANNED,SENT,PARTIAL')
const list = ref([])
async function load() { list.value = (await get('/outsource', { status: status.value })).items }
const open = ref(false)
const f = ref({})
function openNew() { f.value = { material_id: null, supplier_id: null, qty: 1, price: 0, expected_return: today(14), supply_mode: 'TURNKEY' }; open.value = true }
async function save() { await post('/outsource', f.value); open.value = false; load() }
async function send(row) { await post(`/outsource/${row.id}/send`); ElMessage.success('已发出'); load() }
async function pickRaw(row) { await post(`/outsource/${row.id}/pick`); ElMessage.success('已生成领料单，请到【领料】过账出库') }
async function upd(row, v) { if (v) { await put(`/outsource/${row.id}`, { expected_return: v }); load() } }
const dwgOpen = ref(false)
const dwgList = ref([])
let cur = null
async function dwg(row) {
  cur = row
  dwgList.value = await get('/drawings-required', { ref_type: 'OUTSOURCE', ref_id: row.id })
  if (!dwgList.value.length) return ElMessage.info('该物料没有受控图纸，可直接发出')
  dwgOpen.value = true
}
async function confirmDwg() {
  await post('/drawings-confirm', { ref_type: 'OUTSOURCE', ref_id: cur.id, version_ids: dwgList.value.filter((d) => !d.missing).map((d) => d.version_id) })
  dwgOpen.value = false
  ElMessage.success('已确认')
}
onMounted(load)
onActivated(load)
</script>
