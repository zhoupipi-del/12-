<template>
  <div class="portal">
    <div v-if="err" class="err"><b>链接不可用</b><p>{{ err }}</p></div>
    <template v-else-if="d">
      <header>
        <h1>{{ d.supplier }}</h1>
        <p>以下是湖南一二矿山科技向贵司下达、尚未交齐的采购明细。请填写可交货日期，我方会据此安排生产。</p>
      </header>
      <div v-for="l in d.lines" :key="l.id" class="card" :class="{ need: !l.promised_date }">
        <div class="c-h"><span class="no">{{ l.po_no }}</span><span class="muted">第 {{ l.line_no }} 行</span></div>
        <div class="c-n">{{ l.name }}<small>{{ l.spec }}{{ l.drawing_no ? `，图号 ${l.drawing_no}` : '' }}</small></div>
        <div class="c-g">
          <div><span>未交数量</span><b class="num">{{ Number(l.open_qty) }} {{ l.unit }}</b></div>
          <div><span>我方需求日</span><b class="num">{{ l.need_date || '—' }}</b></div>
          <div><span>已回复交期</span><b class="num" :class="{ late: l.promised_date && l.need_date && l.promised_date > l.need_date }">{{ l.promised_date || '未回复' }}</b></div>
        </div>
        <div class="c-f">
          <el-date-picker v-model="l._d" value-format="YYYY-MM-DD" placeholder="选择可交货日期" style="width: 160px" />
          <el-input v-model="l._n" placeholder="备注（可选），例如 分两批" />
          <el-button type="primary" :disabled="!l._d" @click="confirm(l)">提交交期</el-button>
        </div>
        <div v-if="l.supplier_note" class="muted small">上次备注：{{ l.supplier_note }}</div>
      </div>
      <div v-if="!d.lines.length" class="card">目前没有未交的采购明细，谢谢配合。</div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { ElMessage } from 'element-plus'

const route = useRoute()
const d = ref(null)
const err = ref('')
async function load() {
  try { d.value = (await axios.get(`/api/portal/${route.params.token}`)).data }
  catch (e) { err.value = e.response?.data?.detail || '网络异常，请稍后重试' }
}
async function confirm(l) {
  try {
    await axios.post(`/api/portal/${route.params.token}/lines/${l.id}/confirm`, { promised_date: l._d, note: l._n || '' })
    ElMessage.success('已提交，谢谢')
    load()
  } catch (e) { ElMessage.error(e.response?.data?.detail || '提交失败') }
}
onMounted(load)
</script>

<style scoped>
.portal { max-width: 760px; margin: 0 auto; padding: 24px 16px 60px; }
header h1 { font-size: 22px; margin: 0 0 6px; }
header p { color: var(--ink-2); line-height: 1.7; margin: 0 0 20px; }
.card { background: #fff; border: 1px solid var(--line); border-left: 4px solid var(--line); padding: 14px 16px; margin-bottom: 12px; }
.card.need { border-left-color: var(--wait); }
.c-h { display: flex; justify-content: space-between; font-size: 13px; }
.c-n { font-size: 16px; font-weight: 600; margin: 6px 0 10px; }
.c-n small { display: block; font-weight: 400; color: var(--ink-3); font-size: 12px; margin-top: 2px; }
.c-g { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.c-g div { display: flex; flex-direction: column; }
.c-g span { font-size: 12px; color: var(--ink-3); }
.c-f { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.c-f .el-input { flex: 1; min-width: 160px; }
.err { text-align: center; margin-top: 20vh; color: var(--ink-2); }
@media (max-width: 520px) { .c-g { grid-template-columns: 1fr 1fr; } }
</style>
