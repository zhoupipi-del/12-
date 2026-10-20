<template>
  <div class="page">
    <div class="page-head">
      <div><h1>看板与周计划</h1><p>车间大屏和部门周计划小屏都用这里的数据。客户来参观时切到展示模式：隐藏缺料、供应商等内部信息，客户名脱敏。</p></div>
      <div class="toolbar">
        <el-button @click="openScreen('workshop')">打开车间大屏</el-button>
        <el-button @click="openScreen('weekly')">打开周计划屏</el-button>
      </div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h2>大屏模式</h2>
        <div class="modes">
          <button class="mode" :class="{ on: s.board_mode === 'WORKSHOP' }" @click="setMode('WORKSHOP')">
            <b>车间模式</b><span>在装设备、装配计划、缺件、到货、注意事项</span>
          </button>
          <button class="mode" :class="{ on: s.board_mode === 'SHOWCASE' }" @click="setMode('SHOWCASE')">
            <b>展示模式</b><span>公司介绍、在装进度、交付准时率，不显示内部数据</span>
          </button>
        </div>
        <el-form label-width="90px" style="margin-top: 16px">
          <el-form-item label="展示标题"><el-input v-model="s.showcase_title" /></el-form-item>
          <el-form-item label="标语"><el-input v-model="s.showcase_slogan" /></el-form-item>
          <el-form-item label="介绍"><el-input v-model="s.showcase_intro" /></el-form-item>
          <el-form-item label="刷新间隔"><el-input v-model="s.board_refresh_seconds" style="width: 120px"><template #append>秒</template></el-input></el-form-item>
          <el-form-item><el-button type="primary" @click="save">保存</el-button></el-form-item>
        </el-form>
        <p class="muted small">大屏地址带看板密钥，车间电视打开后无需登录。密钥泄露可在下方重置。</p>
        <el-button v-if="auth.can('ADMIN')" size="small" @click="resetKey">重置看板密钥</el-button>
      </div>

      <div class="panel">
        <h2>装配注意事项<small>显示在车间大屏</small></h2>
        <div class="toolbar"><el-input v-model="note.content" placeholder="例如：主轴已升版 B，装配前核对" style="flex: 1" /><el-checkbox v-model="noteImp">重要</el-checkbox><el-button type="primary" @click="addNote">添加</el-button></div>
        <div v-for="n in notes" :key="n.id" class="note" :class="{ off: !n.is_active }">
          <span :class="{ short: n.level === 'IMPORTANT' }">{{ n.content }}</span>
          <el-switch :model-value="n.is_active" size="small" @change="(v) => toggleNote(n, v)" />
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="wh">
        <h2 style="margin: 0">部门周计划</h2>
        <div class="toolbar">
          <el-date-picker v-model="week" type="week" format="YYYY 第 ww 周" value-format="YYYY-MM-DD" @change="loadWeek" />
          <el-button type="primary" size="small" @click="editItem()">添加计划</el-button>
        </div>
      </div>
      <el-table :data="items" size="small" style="margin-top: 10px">
        <el-table-column prop="department" label="部门" width="100" />
        <el-table-column prop="content" label="事项" min-width="260" />
        <el-table-column prop="owner" label="负责人" width="90" />
        <el-table-column label="截止" width="100"><template #default="{ row }"><span class="num">{{ day(row.due_date) }}</span></template></el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }"><el-select :model-value="row.status" size="small" @change="(v) => saveItem({ ...row, status: v })"><el-option value="TODO" label="待办" /><el-option value="DOING" label="进行中" /><el-option value="DONE" label="完成" /></el-select></template>
        </el-table-column>
        <el-table-column width="100"><template #default="{ row }"><el-button link size="small" @click="editItem(row)">编辑</el-button><el-button link size="small" type="danger" @click="delItem(row)">删除</el-button></template></el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="itemOpen" title="周计划事项" width="440px">
      <el-form label-width="70px">
        <el-form-item label="部门"><el-select v-model="itf.department" filterable allow-create><el-option v-for="d in DEPTS" :key="d" :value="d" /></el-select></el-form-item>
        <el-form-item label="事项"><el-input v-model="itf.content" /></el-form-item>
        <el-form-item label="负责人"><el-input v-model="itf.owner" /></el-form-item>
        <el-form-item label="截止"><el-date-picker v-model="itf.due_date" value-format="YYYY-MM-DD" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveItem(itf); itemOpen = false">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth } from '../store'
import { day, today } from '../utils'

defineOptions({ name: 'BoardAdmin' })
const DEPTS = ['生产部', '采购部', '仓库', '质量部', '技术部', '销售部', '售后部']
const auth = useAuth()
const s = ref({})
async function load() { s.value = await get('/board/settings') }
async function setMode(m) { await put('/board/settings', { board_mode: m }); s.value.board_mode = m; ElMessage.success(m === 'SHOWCASE' ? '已切到展示模式' : '已切回车间模式') }
async function save() {
  const { board_key: _k, ...body } = s.value
  await put('/board/settings', body); ElMessage.success('已保存')
}
async function resetKey() { const r = await post('/board/key'); s.value.board_key = r.board_key; ElMessage.success('已重置，请重新打开大屏') }
function openScreen(k) { window.open(`/board/${k}?key=${encodeURIComponent(s.value.board_key)}`, '_blank') }
const notes = ref([])
const note = ref({ content: '' })
const noteImp = ref(false)
async function loadNotes() { notes.value = await get('/assembly-notes') }
async function addNote() { if (!note.value.content) return; await post('/assembly-notes', { content: note.value.content, level: noteImp.value ? 'IMPORTANT' : 'INFO' }); note.value.content = ''; loadNotes() }
async function toggleNote(n, v) { await put(`/assembly-notes/${n.id}`, { content: n.content, level: n.level, order_id: n.order_id, is_active: v }); loadNotes() }
const week = ref(today())
const items = ref([])
async function loadWeek() { items.value = await get('/weekly-items', { week: week.value }) }
const itemOpen = ref(false)
const itf = ref({})
function editItem(row) { itf.value = row ? { ...row } : { department: '生产部', content: '', owner: '', due_date: null, status: 'TODO', week_start: week.value }; itemOpen.value = true }
async function saveItem(it) {
  const body = { week_start: it.week_start || week.value, department: it.department, content: it.content, owner: it.owner, due_date: it.due_date, status: it.status, order_id: it.order_id || null }
  if (it.id) await put(`/weekly-items/${it.id}`, body); else await post('/weekly-items', body)
  loadWeek()
}
async function delItem(row) { await http.delete(`/weekly-items/${row.id}`); loadWeek() }
onMounted(() => { load(); loadNotes(); loadWeek() })
</script>

<style scoped>
.modes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.mode { text-align: left; border: 1px solid var(--line); background: #fff; padding: 12px 14px; border-radius: 3px; cursor: pointer; font: inherit; color: inherit; display: flex; flex-direction: column; gap: 4px; }
.mode span { font-size: 12px; color: var(--ink-3); }
.mode.on { border-color: var(--primary); box-shadow: inset 3px 0 0 var(--primary); background: var(--primary-soft); }
.note { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--line-soft); font-size: 13px; gap: 10px; }
.note.off { opacity: .5; }
.wh { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
.grid-2 .panel + .panel { margin-top: 0; }
.grid-2 + .panel { margin-top: 16px; }
</style>
