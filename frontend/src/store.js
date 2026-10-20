import { defineStore } from 'pinia'
import { get, post } from './api'

export const useAuth = defineStore('auth', {
  state: () => ({ user: null, meta: null }),
  getters: {
    roles: (s) => s.user?.roles || [],
  },
  actions: {
    async login(username, password) {
      const r = await post('/auth/login', { username, password })
      localStorage.setItem('ps_token', r.token)
      this.user = r.user
      await this.loadMeta()
    },
    async load() {
      if (!localStorage.getItem('ps_token')) return false
      try {
        this.user = await get('/auth/me', null, { silent: true })
        await this.loadMeta()
        return true
      } catch {
        return false
      }
    },
    async loadMeta() {
      if (!this.meta) this.meta = await get('/meta')
    },
    logout() {
      localStorage.removeItem('ps_token')
      this.user = null
    },
    can(...roles) {
      const mine = this.roles
      return mine.includes('ADMIN') || roles.some((r) => mine.includes(r))
    },
  },
})

// 多页签工作区：模块之间跳转保留原页面状态
export const useTabs = defineStore('tabs', {
  state: () => ({ tabs: [{ path: '/', title: '工作台', name: 'Dashboard' }] }),
  actions: {
    open(route) {
      if (route.meta?.public || route.meta?.bare) return
      const title = route.meta?.title || route.name
      const ex = this.tabs.find((t) => t.path === route.fullPath)
      if (!ex) this.tabs.push({ path: route.fullPath, title: route.meta?.tabTitle?.(route) || title, name: route.name })
    },
    close(path) {
      const i = this.tabs.findIndex((t) => t.path === path)
      if (i > 0) this.tabs.splice(i, 1)
      return this.tabs[Math.max(0, i - 1)]?.path || '/'
    },
    rename(path, title) {
      const t = this.tabs.find((x) => x.path === path)
      if (t) t.title = title
    },
  },
})
