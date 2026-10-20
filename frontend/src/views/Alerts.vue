<template>
  <div class="page">
    <div class="page-head">
      <div><h1>预警中心</h1><p>缺料、在途延误、交期风险、库存高低、呆滞、异常超时、图纸升版。问题消除后预警自动关闭。</p></div>
      <div class="toolbar">
        <el-radio-group v-model="mine" size="small" @change="load"><el-radio-button value="mine">与我相关</el-radio-button><el-radio-button value="">全部</el-radio-button></el-radio-group>
        <el-select v-model="type" clearable placeholder="全部类型" style="width: 140px" @change="load"><el-option v-for="(v, k) in TYPES" :key="k" :value="k" :label="v" /></el-select>
        <el-button @click="scan" :loading="busy">立即扫描</el-button>
      </div>
    </div>
    <div class="panel">
      <div v-for="a in list" :key="a.id" class="row" :class="a.level">
        <div class="t">
          <el-tag size="small" :type="a.level === 'CRITICAL' ? 'danger' : a.level === 'WARN' ? 'warning' : 'info'" effect="plain">{{ TYPES[a.type] || a.type }}</el-tag>
          <b>{{ a.title }}</b>
          <span class="muted small">{{ dt(a.created_at) }}</span>
        </div>
        <div class="d">{{ a.detail }}</div>
        <div class="ops">
          <router-link v-if="a.ref_type === 'ORDER'" :to="`/orders/${a.ref_id}`">查看订单</router-link>
          <router-link v-if="a.ref_type === 'PO'" :to="`/purchase-orders/${a.ref_id}`">查看采购单</router-link>
          <router-link v-if="a.type === 'SHORTAGE'" to="/requisitions">去请购</router-link>
          <router-link v-if="a.ref_type === 'EXCEPTION'" to="/exceptions">处理异常</router-link>
          <a v-if="a.status === 'OPEN'" href="#" @click.prevent="act(a, 'ack')">已知悉</a>
          <span v-else class="muted small">已知悉</span>
        </div>
      </div>
      <div v-if="!list.length" class="muted">没有预警</div>
    </div>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { get, post } from '../api'
import { dt } from '../utils'

defineOptions({ name: 'Alerts' })
const TYPES = { SHORTAGE: '缺料', LATE_SUPPLY: '在途延误', DUE_RISK: '交期风险', ORDER_OVERDUE: '订单逾期', STOCK_LOW: '库存不足', STOCK_HIGH: '库存积压', SPARE_LOW: '备件不足', SPARE_HIGH: '备件积压', STAGNANT: '呆滞', EXC_OVERDUE: '异常超时', DRAWING_CHANGED: '图纸变更', SUPPLIER_LATE: '供应商交期晚' }
const mine = ref('mine')
const type = ref('')
const list = ref([])
const busy = ref(false)
async function load() { list.value = (await get('/alerts', { role: mine.value, type: type.value })).items }
async function act(a, x) { await post(`/alerts/${a.id}/${x}`); load() }
async function scan() { busy.value = true; try { await post('/alerts-scan'); await load() } finally { busy.value = false } }
onMounted(load)
onActivated(load)
</script>

<style scoped>
.row { padding: 10px 12px; border-left: 3px solid var(--wait); border-bottom: 1px solid var(--line-soft); }
.row.CRITICAL { border-left-color: var(--short); }
.row.INFO { border-left-color: var(--line); }
.t { display: flex; gap: 10px; align-items: center; }
.d { color: var(--ink-2); font-size: 13px; margin: 4px 0; }
.ops { display: flex; gap: 14px; font-size: 13px; }
</style>
