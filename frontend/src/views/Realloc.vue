<template>
  <div class="page">
    <div class="page-head">
      <div><h1>调料申请</h1><p>已分配给订单的库存是冻结的，重算不会抢。急单要动他单的料，先预演影响，由主管审批后才执行。</p></div>
      <el-radio-group v-model="status" size="small" @change="load">
        <el-radio-button value="PENDING">待审批</el-radio-button><el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
    </div>
    <div class="panel">
      <el-table :data="list" row-key="id">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="ex">
              <div class="small">来源：<span v-for="p in row.plan" :key="p.from_order_id" class="src">{{ p.order_no }} <b class="num">{{ num(p.qty) }}</b></span></div>
              <el-table :data="row.impacts" size="small" style="margin-top: 8px">
                <el-table-column prop="order_no" label="受影响订单" width="150" />
                <el-table-column prop="customer" label="客户" width="140" />
                <el-table-column label="状态变化" min-width="200"><template #default="{ row: i }">{{ KIT[i.before] || i.before }} → {{ KIT[i.after] || i.after }}　<span class="small muted">预计齐套 {{ i.before_ready || '—' }} → {{ i.after_ready || '无法确定' }}</span></template></el-table-column>
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="申请号" width="130"><template #default="{ row }"><span class="no">{{ row.req_no }}</span></template></el-table-column>
        <el-table-column label="调入订单" width="140"><template #default="{ row }"><router-link :to="`/orders/${row.to_order_id}`" class="no">{{ row.to_order_no }}</router-link></template></el-table-column>
        <el-table-column prop="material" label="物料" min-width="180" />
        <el-table-column label="数量" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.qty) }}</span></template></el-table-column>
        <el-table-column prop="reason" label="原因" min-width="160" />
        <el-table-column label="受影响" min-width="160"><template #default="{ row }"><span class="small">{{ row.impacts.filter((i) => !i.is_target).map((i) => i.order_no).join('、') || '无' }}</span></template></el-table-column>
        <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :map="ST" :value="row.status" /></template></el-table-column>
        <el-table-column width="150">
          <template #default="{ row }">
            <template v-if="row.status === 'PENDING' && auth.can('MANAGER')">
              <el-button type="primary" size="small" @click="decide(row, 'approve')">批准</el-button>
              <el-button size="small" @click="decide(row, 'reject')">驳回</el-button>
            </template>
            <span v-else class="muted small">{{ row.decision_comment }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'
import { num } from '../utils'
import StatusTag from '../components/StatusTag.vue'

defineOptions({ name: 'Realloc' })
const auth = useAuth()
const status = ref('PENDING')
const list = ref([])
const KIT = { READY: '已齐套', INCOMING_OK: '在途可齐', INCOMING_LATE: '在途延误', SHORT: '缺料' }
const ST = { PENDING: ['待审批', 'warning'], APPROVED: ['已批准', 'success'], REJECTED: ['已驳回', 'info'], EXPIRED: ['已作废', 'info'] }
async function load() { list.value = await get('/realloc', { status: status.value }) }
async function decide(r, act) {
  const { value } = await ElMessageBox.prompt(act === 'approve' ? '审批意见（可空）' : '驳回原因', act === 'approve' ? '批准调料' : '驳回',
    { inputPattern: act === 'approve' ? /.*/ : /\S+/, inputErrorMessage: '必填' })
  try { await post(`/realloc/${r.id}/${act}`, { comment: value || '' }); ElMessage.success('已处理') } finally { load() }
}
onMounted(load)
onActivated(load)
</script>

<style scoped>
.ex { padding: 8px 16px 12px 48px; }
.src { display: inline-block; margin-right: 12px; }
</style>
