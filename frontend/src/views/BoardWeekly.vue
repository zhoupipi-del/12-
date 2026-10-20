<template>
  <div class="wk">
    <div v-if="err" class="err">{{ err }}</div>
    <template v-else-if="d">
      <header>
        <h1>本周计划</h1>
        <span class="range">{{ d.week_start.slice(5) }} 至 {{ d.week_end.slice(5) }}</span>
        <span class="done">完成 <b>{{ doneCount }}</b> / {{ allCount }}</span>
      </header>
      <div class="cols">
        <section v-for="(items, dept) in d.groups" :key="dept">
          <h2>{{ dept }}</h2>
          <div v-for="it in items" :key="it.id" class="it" :class="it.status">
            <i />
            <div><div>{{ it.content }}</div><small>{{ it.owner }}{{ it.due_date ? `，${it.due_date.slice(5)} 前` : '' }}</small></div>
          </div>
        </section>
      </div>
      <div class="bottom">
        <section>
          <h2>本周到期订单</h2>
          <div v-for="o in d.due_orders" :key="o.order_no" class="row"><span class="dot" :class="o.kit_color" /><b>{{ o.due_date.slice(5) }}</b>{{ o.product }}<small>{{ o.customer }}</small></div>
          <div v-if="!d.due_orders.length" class="muted">无</div>
        </section>
        <section>
          <h2>本周发货</h2>
          <div v-for="s in d.shipments" :key="s.ship_no" class="row"><b>{{ s.plan_date.slice(5) }}</b>{{ s.order_no }}<small>{{ { DRAFT: '草稿', SUBMITTED: '待审批', APPROVED: '已批准', SHIPPED: '已发货', REJECTED: '驳回' }[s.status] }}</small></div>
          <div v-if="!d.shipments.length" class="muted">无</div>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'

const route = useRoute()
const d = ref(null)
const err = ref('')
let timer = null
const allItems = computed(() => Object.values(d.value?.groups || {}).flat())
const allCount = computed(() => allItems.value.length)
const doneCount = computed(() => allItems.value.filter((x) => x.status === 'DONE').length)
async function load() {
  try {
    const t = localStorage.getItem('ps_token')
    const r = await axios.get('/api/board/weekly', { params: { key: route.query.key }, headers: t ? { Authorization: `Bearer ${t}` } : {} })
    d.value = r.data
    err.value = ''
  } catch (e) { err.value = e.response?.status === 401 ? '看板密钥无效' : '网络中断，稍后重试' }
  timer = setTimeout(load, 60000)
}
onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<style scoped>
.wk { min-height: 100vh; background: #eceff1; padding: 2vw; font-size: clamp(14px, 1.1vw, 22px); color: var(--ink); display: flex; flex-direction: column; gap: 1.4vw; }
.err { margin: auto; color: var(--ink-3); }
header { display: flex; align-items: baseline; gap: 1.2em; }
h1 { margin: 0; font-size: 2em; letter-spacing: .1em; }
.range { font-family: var(--num-c); font-size: 1.5em; color: var(--ink-2); }
.done { margin-left: auto; color: var(--ink-2); }
.done b { font-family: var(--num-c); font-size: 2em; color: var(--ok); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(16em, 1fr)); gap: 1.2vw; }
section { background: #fff; border: 1px solid var(--line); border-top: 3px solid var(--primary); padding: 1em 1.1em; }
h2 { font-size: 1.1em; margin: 0 0 .7em; }
.it { display: flex; gap: .6em; padding: .45em 0; border-bottom: 1px dashed var(--line-soft); }
.it i { width: .8em; height: .8em; border: 2px solid var(--ink-3); border-radius: 50%; margin-top: .3em; flex-shrink: 0; }
.it.DOING i { border-color: var(--wait); background: linear-gradient(90deg, var(--wait) 50%, transparent 50%); }
.it.DONE i { border-color: var(--ok); background: var(--ok); }
.it.DONE > div > div { color: var(--ink-3); text-decoration: line-through; }
.it small, .row small { color: var(--ink-3); display: block; font-size: .8em; }
.bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 1.2vw; }
.bottom section { border-top-color: var(--copper); }
.row { display: grid; grid-template-columns: auto 3.5em 1fr; gap: .6em; align-items: baseline; padding: .4em 0; border-bottom: 1px dashed var(--line-soft); }
.row b { font-family: var(--num-c); font-size: 1.2em; }
.row small { grid-column: 3; }
.bottom section:last-child .row { grid-template-columns: 3.5em 1fr; }
.bottom section:last-child .row small { grid-column: 2; }
</style>
