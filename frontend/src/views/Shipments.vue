<template>
  <div class="page">
    <div class="page-head">
      <div><h1>发货计划</h1><p>销售或计划提交，生产主管审批，仓库凭批准的计划出库。订单全部发完后自动标记已发货。</p></div>
      <el-select v-model="status" style="width: 130px" @change="load"><el-option value="DRAFT,SUBMITTED,APPROVED,REJECTED" label="未完成" /><el-option value="" label="全部" /></el-select>
    </div>
    <div class="panel">
      <el-table :data="list">
        <el-table-column label="发货单" width="140"><template #default="{ row }"><span class="no">{{ row.ship_no }}</span></template></el-table-column>
        <el-table-column label="订单" width="140"><template #default="{ row }"><router-link :to="`/orders/${row.order_id}`" class="no">{{ row.order_no }}</router-link></template></el-table-column>
        <el-table-column prop="customer" label="客户" min-width="130" />
        <el-table-column prop="product" label="机型" min-width="150" />
        <el-table-column label="数量" width="60"><template #default="{ row }">{{ num(row.qty) }}</template></el-table-column>
        <el-table-column label="计划日期" width="104"><template #default="{ row }"><span class="num">{{ row.plan_date }}</span></template></el-table-column>
        <el-table-column prop="address" label="地址" min-width="130" show-overflow-tooltip />
        <el-table-column label="状态" width="86"><template #default="{ row }"><StatusTag :map="SHIP_STATUS" :value="row.status" /></template></el-table-column>
        <el-table-column label="审批" width="130"><template #default="{ row }"><span class="small">{{ row.approver }}</span><div class="small muted">{{ row.approve_comment }}</div></template></el-table-column>
        <el-table-column width="180">
          <template #default="{ row }">
            <el-button v-if="['DRAFT', 'REJECTED'].includes(row.status) && auth.can('SALES', 'PLANNER')" link size="small" type="primary" @click="act(row, 'submit')">提交审批</el-button>
            <template v-if="row.status === 'SUBMITTED' && auth.can('MANAGER')">
              <el-button link size="small" type="primary" @click="act(row, 'approve')">批准</el-button>
              <el-button link size="small" type="danger" @click="rejectIt(row)">驳回</el-button>
            </template>
            <el-button v-if="row.status === 'APPROVED' && auth.can('WAREHOUSE')" link size="small" type="success" @click="act(row, 'ship')">发货出库</el-button>
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
import StatusTag from '../components/StatusTag.vue'
import { SHIP_STATUS, num } from '../utils'

defineOptions({ name: 'Shipments' })
const auth = useAuth()
const status = ref('DRAFT,SUBMITTED,APPROVED,REJECTED')
const list = ref([])
async function load() { list.value = (await get('/shipments', { status: status.value })).items }
async function act(row, a, comment = '') { await post(`/shipments/${row.id}/${a}`, { comment }); ElMessage.success('已处理'); load() }
async function rejectIt(row) { const { value } = await ElMessageBox.prompt('驳回原因', '驳回'); act(row, 'reject', value) }
onMounted(load)
onActivated(load)
</script>
