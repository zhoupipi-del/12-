<template>
  <div class="page">
    <div class="page-head">
      <div><h1>领料</h1><p>在订单详情点“按 BOM 领料”自动带出物料，这里集中查看和过账。</p></div>
      <el-radio-group v-model="status" size="small" @change="load">
        <el-radio-button value="DRAFT">待过账</el-radio-button><el-radio-button value="POSTED">已过账</el-radio-button><el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
    </div>
    <div class="panel">
      <el-table :data="list">
        <el-table-column label="领料单" width="140"><template #default="{ row }"><span class="no">{{ row.pick_no }}</span></template></el-table-column>
        <el-table-column label="来源" width="160"><template #default="{ row }"><router-link v-if="row.order_id" :to="`/orders/${row.order_id}`" class="no">{{ row.source }}</router-link><span v-else class="no">{{ row.source }}</span></template></el-table-column>
        <el-table-column label="物料" min-width="320"><template #default="{ row }"><span v-for="l in row.lines" :key="l.id" class="ln">{{ l.name }} <b class="num">{{ num(l.qty) }}</b><i v-if="Number(l.returned_qty) > 0" class="muted small">（已退 {{ num(l.returned_qty) }}）</i></span></template></el-table-column>
        <el-table-column label="时间" width="140"><template #default="{ row }"><span class="num small">{{ dt(row.posted_at || row.created_at) }}</span></template></el-table-column>
        <el-table-column width="160"><template #default="{ row }"><el-button v-if="row.status === 'DRAFT' && auth.can('WAREHOUSE')" type="primary" size="small" @click="postIt(row)">过账出库</el-button><template v-else><span class="ok small">已出库</span><el-button v-if="auth.can('WAREHOUSE')" link size="small" @click="returnIt(row)">退料</el-button></template></template></el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import { dt, num } from '../utils'

defineOptions({ name: 'Picks' })
const auth = useAuth()
const status = ref('DRAFT')
const list = ref([])
async function load() { list.value = (await get('/picks', { status: status.value })).items }
async function postIt(r) { await post(`/picks/${r.id}/post`); ElMessage.success('已出库'); load() }
async function returnIt(r) {
  const lines = r.lines.filter((l) => Number(l.qty) - Number(l.returned_qty) > 0)
  if (!lines.length) return ElMessage.info('该领料单已全部退回')
  const { value } = await ElMessageBox.prompt(
    `可退：${lines.map((l) => `${l.name} ${num(Number(l.qty) - Number(l.returned_qty))}`).join('、')}。\n输入“物料序号:数量”，如 1:2（序号按上面顺序）；原因另填。`,
    '退料（退回原批次）', { inputPlaceholder: '1:2', inputPattern: /^\d+:\d+(\.\d+)?$/, inputErrorMessage: '格式如 1:2' })
  const [i, q] = value.split(':')
  const l = lines[Number(i) - 1]
  if (!l) return ElMessage.error('序号无效')
  const { value: reason } = await ElMessageBox.prompt('退料原因', '退料', { inputPattern: /\S+/, inputErrorMessage: '必填' })
  await post(`/picks/${r.id}/return`, { reason, lines: [{ material_id: l.material_id, qty: Number(q) }] })
  ElMessage.success('已退回原批次'); load()
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.ln { display: inline-block; margin-right: 14px; font-size: 13px; }
</style>
