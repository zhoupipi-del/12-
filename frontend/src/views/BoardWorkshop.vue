<template>
  <div class="screen" :class="d?.mode">
    <div v-if="err" class="err">{{ err }}</div>

    <!-- 车间模式 -->
    <template v-else-if="d && d.mode === 'WORKSHOP'">
      <header>
        <div class="brand"><svg viewBox="0 0 32 32" width="34" height="34"><path d="M4 26 L13 8 L18 17 L21 12 L28 26 Z" fill="#c07a3c" /><path d="M4 26 H28" stroke="#e8edf0" stroke-width="2" /></svg>装配车间</div>
        <div class="stats">
          <div><b>{{ d.stats.assembling }}</b><span>在装</span></div>
          <div><b>{{ d.stats.planned }}</b><span>待装</span></div>
          <div class="g"><b>{{ d.stats.ready }}</b><span>已齐套</span></div>
          <div class="y"><b>{{ d.stats.ready_in_2_days }}</b><span>2 天内可齐</span></div>
          <div class="r"><b>{{ d.stats.short_orders }}</b><span>有缺料风险</span></div>
          <div><b>{{ d.stats.pending_inspection }}</b><span>到货待检</span></div>
        </div>
        <div class="clock"><b>{{ clock }}</b><span>{{ dateStr }}</span></div>
      </header>

      <main>
        <section class="col-a">
          <h2>在装设备</h2>
          <div v-for="o in d.assembling" :key="o.id" class="unit">
            <div class="u-h"><span class="st">{{ o.station || '未分工位' }}</span><span class="pn">{{ o.product }}</span><span class="cu">{{ o.customer }}</span></div>
            <div class="u-p"><div class="track"><div class="fill" :style="{ width: o.progress + '%' }" /></div><b>{{ o.progress }}%</b></div>
            <div class="u-f">
              <KitStrip :lines="o.strip" big />
              <span class="u-d" :class="{ r: o.overdue }">交期 {{ o.due_date.slice(5) }}</span>
            </div>
          </div>
          <div v-if="!d.assembling.length" class="empty">当前没有在装设备</div>

          <h2 class="mt">装配计划</h2>
          <div class="plan">
            <div v-for="o in d.plan.slice(0, 7)" :key="o.id" class="p-row">
              <span class="p-date">{{ (o.planned_start || o.due_date).slice(5) }}</span>
              <span class="p-name">{{ o.product }}<small>{{ o.customer }}</small></span>
              <KitStrip :lines="o.strip" />
              <span class="p-st" :class="o.kit_color">{{ o.kit_color === 'GREEN' ? '已齐套' : o.ready_in_2_days ? '2 天内齐' : o.kit_ready_date ? `预计 ${o.kit_ready_date.slice(5)}` : '缺料' }}</span>
            </div>
          </div>
        </section>

        <section class="col-b">
          <h2>缺件清单 <small>{{ d.shortages.length }} 项</small></h2>
          <div class="list">
            <div v-for="(s, i) in d.shortages.slice(0, 12)" :key="i" class="sh">
              <span class="sh-n">{{ s.name }}<small>{{ s.drawing_no || s.code }}</small></span>
              <span class="sh-q">{{ fmt(s.short_qty) }}</span>
              <span class="sh-o">{{ s.order_no.slice(-6) }}</span>
              <span class="sh-s" :class="s.status">{{ s.status === 'SHORT' ? '无供应' : `迟到 ${s.expected_date?.slice(5)}` }}</span>
            </div>
            <div v-if="!d.shortages.length" class="empty ok">没有缺件</div>
          </div>
        </section>

        <section class="col-c">
          <h2>7 天内到货</h2>
          <div class="list">
            <div v-for="(x, i) in d.incoming.slice(0, 10)" :key="i" class="in">
              <span class="in-d" :class="{ r: x.late }">{{ x.date.slice(5) }}</span>
              <span class="in-n">{{ x.name }}<small>{{ x.supplier }}</small></span>
              <span class="in-q">{{ fmt(x.qty) }}</span>
              <span class="in-c">{{ x.type }}{{ x.confirmed ? '' : '，未确认' }}</span>
            </div>
          </div>
          <h2 class="mt">已到货待检</h2>
          <div class="list">
            <div v-for="a in d.arrived.slice(0, 5)" :key="a.no" class="in">
              <span class="in-d">今</span><span class="in-n">{{ a.name }}<small>{{ a.supplier }}</small></span><span class="in-q">{{ fmt(a.qty) }}</span><span class="in-c">待检</span>
            </div>
            <div v-if="!d.arrived.length" class="empty">无</div>
          </div>
        </section>
      </main>

      <footer v-if="d.notes.length">
        <span class="f-l">注意事项</span>
        <div class="ticker"><div class="t-in" :style="{ animationDuration: d.notes.length * 12 + 's' }">
          <span v-for="(n, i) in [...d.notes, ...d.notes]" :key="i" :class="{ imp: n.level === 'IMPORTANT' }">{{ n.order_no ? `【${n.order_no}】` : '' }}{{ n.content }}</span>
        </div></div>
      </footer>
    </template>

    <!-- 展示模式：客户参观 -->
    <template v-else-if="d && d.mode === 'SHOWCASE'">
      <div class="show">
        <div class="s-head">
          <svg viewBox="0 0 32 32" width="64" height="64"><path d="M4 26 L13 8 L18 17 L21 12 L28 26 Z" fill="#c07a3c" /><path d="M4 26 H28" stroke="#e8edf0" stroke-width="2" /></svg>
          <div><h1>{{ d.title }}</h1><p>{{ d.slogan }}</p></div>
        </div>
        <p class="s-intro">{{ d.intro }}</p>
        <div class="s-stats">
          <div><b>{{ d.stats.assembling }}</b><span>台设备正在装配</span></div>
          <div><b>{{ d.stats.shipped_year }}</b><span>台本年度已交付</span></div>
          <div><b>{{ d.stats.on_time_rate === null ? '—' : Math.round(d.stats.on_time_rate * 100) + '%' }}</b><span>交付准时率</span></div>
        </div>
        <div class="s-list">
          <div v-for="(o, i) in d.assembling" :key="i" class="s-u">
            <span class="pn">{{ o.product }}</span><span class="cu">{{ o.customer }}</span>
            <div class="track"><div class="fill" :style="{ width: o.progress + '%' }" /></div><b>{{ o.progress }}%</b>
          </div>
        </div>
      </div>
      <div class="s-clock">{{ dateStr }} {{ clock }}</div>
    </template>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import KitStrip from '../components/KitStrip.vue'

const route = useRoute()
const d = ref(null)
const err = ref('')
const clock = ref('')
const dateStr = ref('')
let timer = null
let ct = null
const fmt = (v) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
async function load() {
  try {
    const headers = {}
    const t = localStorage.getItem('ps_token')
    if (t) headers.Authorization = `Bearer ${t}`
    const r = await axios.get('/api/board/workshop', { params: { key: route.query.key, mode: route.query.mode }, headers })
    d.value = r.data
    err.value = ''
    clearTimeout(timer)
    timer = setTimeout(load, (d.value.refresh || 30) * 1000)
  } catch (e) {
    err.value = e.response?.status === 401 ? '看板密钥无效，请从系统里重新打开大屏' : '网络中断，30 秒后重试'
    timer = setTimeout(load, 30000)
  }
}
function tick() {
  const n = new Date()
  clock.value = n.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  dateStr.value = n.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' })
}
onMounted(() => { load(); tick(); ct = setInterval(tick, 10000) })
onBeforeUnmount(() => { clearTimeout(timer); clearInterval(ct) })
</script>

<style scoped>
.screen { --bg: #16222c; --bg2: #1d2c38; --ln: #2b3d4b; --tx: #e8edf0; --tx2: #9fb0bc; --ok: #3fa56a; --wait: #d4a017; --late: #e0703f; --short: #e0483b;
  min-height: 100vh; background: var(--bg); color: var(--tx); font-family: var(--ui); padding: 1.4vw 1.8vw; display: flex; flex-direction: column; gap: 1.2vw; font-size: clamp(14px, 1.05vw, 22px); }
.err { margin: auto; font-size: 1.6em; color: var(--tx2); }
header { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 2vw; }
.brand { display: flex; align-items: center; gap: .6em; font-size: 1.6em; font-weight: 600; letter-spacing: .12em; }
.stats { display: flex; gap: 2.4vw; justify-content: center; }
.stats div { display: flex; flex-direction: column; align-items: center; }
.stats b { font-family: var(--num-c); font-size: 2.6em; line-height: 1; font-weight: 700; }
.stats span { color: var(--tx2); font-size: .85em; }
.stats .g b { color: var(--ok); } .stats .y b { color: var(--wait); } .stats .r b { color: var(--short); }
.clock { text-align: right; }
.clock b { font-family: var(--num-c); font-size: 2.6em; display: block; line-height: 1; }
.clock span { color: var(--tx2); font-size: .85em; }
main { flex: 1; display: grid; grid-template-columns: 1.35fr 1fr 1fr; gap: 1.4vw; min-height: 0; }
section { background: var(--bg2); border: 1px solid var(--ln); padding: 1vw 1.2vw; overflow: hidden; }
h2 { font-size: 1.05em; font-weight: 600; margin: 0 0 .8em; color: var(--tx2); letter-spacing: .08em; }
h2 small { font-weight: 400; margin-left: .4em; }
h2.mt { margin-top: 1.4em; }
.unit { padding: .7em 0 .9em; border-bottom: 1px solid var(--ln); }
.u-h { display: flex; gap: .8em; align-items: baseline; }
.st { background: #c07a3c; color: #16222c; font-weight: 700; padding: .05em .5em; font-size: .85em; }
.pn { font-size: 1.25em; font-weight: 600; }
.cu { color: var(--tx2); font-size: .9em; }
.u-p { display: flex; align-items: center; gap: .8em; margin: .6em 0; }
.u-p b { font-family: var(--num-c); font-size: 1.8em; width: 3em; text-align: right; }
.track { flex: 1; height: .7em; background: #0f1820; }
.fill { height: 100%; background: linear-gradient(90deg, #4f7c9a, #7fb0cc); }
.u-f { display: flex; align-items: center; gap: 1em; }
.u-f > :first-child { flex: 1; }
.u-d { font-family: var(--num); color: var(--tx2); font-size: .9em; white-space: nowrap; }
.u-d.r, .in-d.r { color: var(--short); }
.p-row { display: grid; grid-template-columns: 3.4em 1fr 9em 6.5em; gap: .8em; align-items: center; padding: .45em 0; border-bottom: 1px dashed var(--ln); }
.p-date { font-family: var(--num-c); font-size: 1.15em; }
.p-name small, .sh-n small, .in-n small { display: block; color: var(--tx2); font-size: .75em; }
.p-st { font-size: .85em; text-align: right; }
.p-st.GREEN { color: var(--ok); } .p-st.YELLOW { color: var(--wait); } .p-st.RED { color: var(--short); }
.sh, .in { display: grid; align-items: center; gap: .7em; padding: .45em 0; border-bottom: 1px dashed var(--ln); }
.sh { grid-template-columns: 1fr 3.5em 4.2em 6em; }
.in { grid-template-columns: 3em 1fr 3.5em 5em; }
.sh-q, .in-q { font-family: var(--num-c); font-size: 1.3em; text-align: right; font-weight: 700; }
.sh-q { color: var(--short); }
.sh-o { font-family: var(--num); color: var(--tx2); font-size: .85em; }
.sh-s { font-size: .8em; text-align: right; }
.sh-s.SHORT { color: var(--short); } .sh-s.INCOMING_LATE { color: var(--late); }
.in-d { font-family: var(--num-c); font-size: 1.15em; }
.in-c { color: var(--tx2); font-size: .8em; text-align: right; }
.empty { color: var(--tx2); padding: 1em 0; }
.empty.ok { color: var(--ok); }
footer { display: flex; align-items: center; gap: 1em; background: var(--bg2); border: 1px solid var(--ln); padding: .6em 1em; overflow: hidden; }
.f-l { color: #c07a3c; font-weight: 700; white-space: nowrap; }
.ticker { overflow: hidden; flex: 1; }
.t-in { display: inline-flex; gap: 4em; white-space: nowrap; animation: scroll linear infinite; }
.t-in .imp { color: var(--wait); font-weight: 600; }
@keyframes scroll { from { transform: translateX(0); } to { transform: translateX(-50%); } }
@media (prefers-reduced-motion: reduce) { .t-in { animation: none; flex-wrap: wrap; white-space: normal; } }
:deep(.strip) { min-width: 0; }
:deep(.seg) { background: var(--ok); }
:deep(.seg.INCOMING_OK) { background: var(--wait); }
:deep(.seg.INCOMING_LATE) { background: var(--late); }
:deep(.seg.SHORT) { background: var(--short); }

.show { flex: 1; display: flex; flex-direction: column; justify-content: center; padding: 0 6vw; gap: 2.5vw; }
.s-head { display: flex; align-items: center; gap: 1.5vw; }
.s-head h1 { font-size: 3em; margin: 0; letter-spacing: .1em; font-weight: 600; }
.s-head p { margin: .3em 0 0; font-size: 1.5em; color: #c07a3c; letter-spacing: .2em; }
.s-intro { color: var(--tx2); font-size: 1.3em; margin: 0; }
.s-stats { display: flex; gap: 6vw; }
.s-stats div { display: flex; flex-direction: column; }
.s-stats b { font-family: var(--num-c); font-size: 6em; line-height: 1; font-weight: 700; }
.s-stats span { color: var(--tx2); font-size: 1.1em; }
.s-list { display: grid; gap: 1em; max-width: 1100px; }
.s-u { display: grid; grid-template-columns: 14em 10em 1fr 4em; gap: 1em; align-items: center; font-size: 1.15em; }
.s-u b { font-family: var(--num-c); font-size: 1.6em; text-align: right; }
.s-clock { text-align: right; color: var(--tx2); font-family: var(--num); }
@media (max-width: 1000px) { main { grid-template-columns: 1fr; } .stats { flex-wrap: wrap; gap: 1.2em; } header { grid-template-columns: 1fr; } }
</style>
