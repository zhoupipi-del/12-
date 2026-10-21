<template>
  <div class="login">
    <form class="card" @submit.prevent="submit">
      <svg viewBox="0 0 32 32" width="40" height="40" aria-hidden="true">
        <path d="M4 26 L13 8 L18 17 L21 12 L28 26 Z" fill="var(--copper)" />
        <path d="M4 26 H28" stroke="var(--ink)" stroke-width="2" />
      </svg>
      <h1>U9 生产协同系统</h1>
      <p>订单、齐套、采购、仓储、车间一张网</p>
      <el-input v-model="username" size="large" placeholder="用户名" autocomplete="username" />
      <el-input v-model="password" size="large" type="password" show-password placeholder="密码" autocomplete="current-password" />
      <el-button type="primary" size="large" native-type="submit" :loading="busy">登录</el-button>
      <div class="hint">演示账号：admin / admin123；jihua、caigou、cangku、zhijian、chejian、jishu、zhuguan 密码 123456</div>
    </form>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '../store'

const auth = useAuth()
const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const busy = ref(false)
async function submit() {
  busy.value = true
  try {
    await auth.login(username.value, password.value)
    router.replace(route.query.next || '/')
  } finally { busy.value = false }
}
</script>

<style scoped>
.login { min-height: 100vh; display: grid; place-items: center; background: linear-gradient(180deg, #dfe4e8 0, #eceff1 60%); padding: 16px; }
.card { width: 360px; max-width: 100%; display: flex; flex-direction: column; gap: 14px; background: #fff; padding: 36px 32px 28px; border: 1px solid var(--line); border-top: 4px solid var(--primary); }
h1 { margin: 6px 0 0; font-size: 22px; letter-spacing: .06em; }
p { margin: 0 0 10px; color: var(--ink-3); font-size: 13px; }
.hint { font-size: 12px; color: var(--ink-3); line-height: 1.6; }
</style>
