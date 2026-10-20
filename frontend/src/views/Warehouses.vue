<template>
  <div class="page">
    <div class="page-head">
      <div><h1>仓库库位</h1><p>仓库用途决定分账：只有“生产物料”仓参与齐套；“售后备件”仓独立核算。</p></div>
      <el-button v-if="auth.can('WAREHOUSE')" type="primary" @click="openNew">新建仓库</el-button>
    </div>
    <div class="grid-3">
      <div v-for="w in list" :key="w.id" class="panel wh">
        <div class="wh-h"><b>{{ w.name }}</b><el-tag size="small" effect="plain">{{ w.purpose_label }}</el-tag></div>
        <div class="no small muted">{{ w.code }}</div>
        <div class="locs">
          <span v-for="l in w.locations" :key="l.id" class="loc" :title="l.name">{{ l.code }}</span>
          <span v-if="!w.locations.length" class="muted small">未设库位</span>
        </div>
        <el-button v-if="auth.can('WAREHOUSE')" size="small" @click="addLoc(w)">加库位</el-button>
      </div>
    </div>
    <el-dialog v-model="open" title="新建仓库" width="420px">
      <el-form label-width="70px">
        <el-form-item label="编码"><el-input v-model="f.code" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="f.name" /></el-form-item>
        <el-form-item label="用途"><el-select v-model="f.purpose"><el-option v-for="(v, k) in auth.meta?.purposes" :key="k" :value="k" :label="v" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { useAuth } from '../store'

defineOptions({ name: 'Warehouses' })
const auth = useAuth()
const list = ref([])
async function load() { list.value = await get('/warehouses') }
const open = ref(false)
const f = ref({})
function openNew() { f.value = { code: '', name: '', purpose: 'PRODUCTION', is_active: true }; open.value = true }
async function save() { await post('/warehouses', f.value); open.value = false; load() }
async function addLoc(w) {
  const { value } = await ElMessageBox.prompt('库位编码，例如 A-07', `给 ${w.name} 加库位`)
  await post(`/warehouses/${w.id}/locations`, { code: value, name: '' })
  load()
}
onMounted(load)
</script>

<style scoped>
.grid-3 .panel + .panel { margin-top: 0; }
.wh-h { display: flex; justify-content: space-between; align-items: center; }
.locs { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.loc { font-family: var(--num); font-size: 12px; border: 1px solid var(--line); padding: 2px 8px; border-radius: 2px; }
</style>
