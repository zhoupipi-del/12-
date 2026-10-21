<template>
  <div class="shell" :class="{ collapsed }">
    <aside class="side">
      <div class="brand" @click="$router.push('/')">
        <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
          <path d="M4 26 L13 8 L18 17 L21 12 L28 26 Z" fill="var(--copper)" />
          <path d="M4 26 H28" stroke="#fff" stroke-width="2" />
        </svg>
        <div v-if="!collapsed">
          <b>U9 生产协同</b>
          <span>一二矿山科技</span>
        </div>
      </div>
      <nav>
        <div v-for="g in nav" :key="g.group" class="group">
          <div v-if="!collapsed" class="gname">{{ g.group }}</div>
          <router-link v-for="i in g.items" :key="i.path" :to="i.path" class="item"
                       :class="{ on: isOn(i.path) }" :title="i.title">
            <el-icon><component :is="i.icon" /></el-icon>
            <span v-if="!collapsed">{{ i.title }}</span>
          </router-link>
        </div>
      </nav>
      <button class="collapse" @click="collapsed = !collapsed" :aria-label="collapsed ? '展开菜单' : '收起菜单'">
        <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
      </button>
    </aside>

    <section class="main">
      <header class="top">
        <div class="tabs" role="tablist">
          <div v-for="t in tabs.tabs" :key="t.path" class="tab" :class="{ on: t.path === $route.fullPath }"
               role="tab" @click="$router.push(t.path)">
            {{ t.title }}
            <el-icon v-if="t.path !== '/'" class="x" @click.stop="closeTab(t.path)"><Close /></el-icon>
          </div>
        </div>
        <div class="right">
          <el-button v-if="aiEnabled" text @click="aiOpen = true"><el-icon><MagicStick /></el-icon>AI 助手</el-button>
          <a href="/board/workshop?key=" class="screen" @click.prevent="openBoard">车间大屏</a>
          <el-dropdown @command="onUser">
            <span class="user">{{ auth.user?.name }}<small>{{ roleText }}</small></span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="pwd">修改密码</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>
      <div class="content">
        <router-view v-slot="{ Component, route }">
          <keep-alive :include="cached">
            <component :is="Component" :key="route.fullPath" />
          </keep-alive>
        </router-view>
      </div>
    </section>

    <el-drawer v-model="aiOpen" title="AI 助手" size="460px">
      <p class="muted small">只读取系统数据给建议，不会修改任何单据。</p>
      <el-button @click="brief" :loading="aiBusy">生成今日缺料简报</el-button>
      <el-input v-model="question" class="ask" placeholder="例如：哪些订单本周可能逾期？" @keyup.enter="ask">
        <template #append><el-button @click="ask" :loading="aiBusy">提问</el-button></template>
      </el-input>
      <pre v-if="aiText" class="ai-out">{{ aiText }}</pre>
    </el-drawer>

    <el-dialog v-model="pwdOpen" title="修改密码" width="380px">
      <el-form label-width="80px">
        <el-form-item label="原密码"><el-input v-model="pwd.old_password" type="password" show-password /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="pwd.new_password" type="password" show-password placeholder="至少 6 位" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="savePwd">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { NAV } from '../router'
import { useAuth, useTabs } from '../store'
import { get, post } from '../api'

const nav = NAV
const auth = useAuth()
const tabs = useTabs()
const route = useRoute()
const router = useRouter()
const collapsed = ref(localStorage.getItem('ps_nav') === '1')
watch(collapsed, (v) => localStorage.setItem('ps_nav', v ? '1' : '0'))

const cached = computed(() => [...new Set(tabs.tabs.map((t) => t.name))])
const roleText = computed(() => auth.roles.map((r) => auth.meta?.roles?.[r] || r).join(' / '))
const isOn = (p) => (p === '/' ? route.path === '/' : route.path.startsWith(p))

watch(() => route.fullPath, () => tabs.open(route), { immediate: true })
function closeTab(p) {
  const next = tabs.close(p)
  if (route.fullPath === p) router.push(next)
}

async function openBoard() {
  const s = await get('/board/settings')
  window.open(`/board/workshop?key=${encodeURIComponent(s.board_key)}`, '_blank')
}

function onUser(c) {
  if (c === 'logout') { auth.logout(); router.push('/login') }
  if (c === 'pwd') pwdOpen.value = true
}
const pwdOpen = ref(false)
const pwd = ref({ old_password: '', new_password: '' })
async function savePwd() {
  await post('/auth/password', pwd.value)
  ElMessage.success('密码已修改')
  pwdOpen.value = false
}

const aiEnabled = ref(false)
const aiOpen = ref(false)
const aiBusy = ref(false)
const aiText = ref('')
const question = ref('')
onMounted(async () => { aiEnabled.value = (await get('/ai/status', null, { silent: true }).catch(() => ({}))).enabled })
async function brief() {
  aiBusy.value = true
  try { aiText.value = (await post('/ai/brief')).text } finally { aiBusy.value = false }
}
async function ask() {
  if (!question.value) return
  aiBusy.value = true
  try { aiText.value = (await post('/ai/ask', { question: question.value })).text } finally { aiBusy.value = false }
}
</script>

<style scoped>
.shell { display: grid; grid-template-columns: 208px 1fr; height: 100vh; }
.shell.collapsed { grid-template-columns: 60px 1fr; }
.side { background: #1e2a33; color: #c8d1d8; display: flex; flex-direction: column; overflow: hidden; }
.brand { display: flex; align-items: center; gap: 10px; padding: 16px 16px 14px; cursor: pointer; border-bottom: 1px solid #2c3a45; }
.brand b { display: block; color: #fff; font-size: 15px; letter-spacing: .08em; }
.brand span { font-size: 11px; color: #8a98a3; }
nav { flex: 1; overflow-y: auto; padding: 8px 0; }
.group { padding: 6px 0; }
.gname { font-size: 11px; color: #6f7f8b; padding: 6px 18px 4px; }
.item { display: flex; align-items: center; gap: 10px; padding: 7px 18px; color: #c8d1d8; font-size: 13.5px; border-left: 3px solid transparent; }
.item:hover { background: #26343f; text-decoration: none; color: #fff; }
.item.on { background: #2a3b48; color: #fff; border-left-color: var(--copper); }
.collapsed .item { justify-content: center; padding: 10px 0; }
.collapse { background: none; border: 0; border-top: 1px solid #2c3a45; color: #8a98a3; padding: 10px; cursor: pointer; }
.main { display: flex; flex-direction: column; min-width: 0; }
.top { height: 44px; background: var(--panel); border-bottom: 1px solid var(--line); display: flex; align-items: stretch; justify-content: space-between; }
.tabs { display: flex; overflow-x: auto; min-width: 0; }
.tab { display: flex; align-items: center; gap: 6px; padding: 0 14px; font-size: 13px; color: var(--ink-2); border-right: 1px solid var(--line-soft); cursor: pointer; white-space: nowrap; }
.tab.on { color: var(--ink); background: var(--bg); box-shadow: inset 0 -2px 0 var(--primary); }
.tab .x { font-size: 12px; color: var(--ink-3); }
.tab .x:hover { color: var(--short); }
.right { display: flex; align-items: center; gap: 14px; padding: 0 16px; flex-shrink: 0; }
.screen { font-size: 13px; }
.user { cursor: pointer; display: flex; flex-direction: column; line-height: 1.2; font-size: 13px; }
.user small { color: var(--ink-3); font-size: 11px; }
.content { flex: 1; overflow: auto; }
.ask { margin: 14px 0; }
.ai-out { white-space: pre-wrap; font-family: var(--ui); background: var(--bg); padding: 12px; border-radius: 4px; line-height: 1.7; font-size: 13px; }
</style>
