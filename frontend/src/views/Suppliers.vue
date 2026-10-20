<template>
  <div class="page">
    <div class="page-head">
      <div><h1>供应商</h1><p>为供应商生成门户链接后，对方无需账号即可查看本厂采购订单并回复交期。</p></div>
      <div class="toolbar">
        <el-input v-model="kw" placeholder="编码 / 名称" clearable style="width: 200px" @change="load" />
        <el-button v-if="auth.can('BUYER')" type="primary" @click="edit()">新建供应商</el-button>
      </div>
    </div>
    <div class="panel">
      <el-table :data="list" size="small">
        <el-table-column label="编码" width="100"><template #default="{ row }"><span class="no">{{ row.code }}</span></template></el-table-column>
        <el-table-column prop="name" label="名称" min-width="180" />
        <el-table-column prop="contact" label="联系人" width="100" />
        <el-table-column prop="phone" label="电话" width="140" />
        <el-table-column label="外协厂" width="70"><template #default="{ row }">{{ row.is_outsourcer ? '是' : '' }}</template></el-table-column>
        <el-table-column label="门户" width="80"><template #default="{ row }"><span v-if="row.has_portal" class="ok small">已开通</span></template></el-table-column>
        <el-table-column width="220" v-if="auth.can('BUYER')">
          <template #default="{ row }">
            <el-button link size="small" @click="edit(row)">编辑</el-button>
            <el-button link size="small" @click="portal(row)">{{ row.has_portal ? '重置门户链接' : '开通门户' }}</el-button>
            <el-button v-if="row.has_portal" link size="small" type="danger" @click="revoke(row)">关闭门户</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <el-dialog v-model="open" :title="f.id ? '编辑供应商' : '新建供应商'" width="440px">
      <el-form label-width="70px">
        <el-form-item label="编码"><el-input v-model="f.code" :disabled="!!f.id" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="f.name" /></el-form-item>
        <el-form-item label="联系人"><el-input v-model="f.contact" /></el-form-item>
        <el-form-item label="电话"><el-input v-model="f.phone" /></el-form-item>
        <el-form-item label="外协厂"><el-switch v-model="f.is_outsourcer" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="f.is_active" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth } from '../store'

defineOptions({ name: 'Suppliers' })
const auth = useAuth()
const kw = ref('')
const list = ref([])
async function load() { list.value = await get('/suppliers', { keyword: kw.value }) }
const open = ref(false)
const f = ref({})
function edit(row) { f.value = row ? { ...row } : { code: '', name: '', contact: '', phone: '', is_outsourcer: false, is_active: true }; open.value = true }
async function save() {
  const { id, has_portal: _h, ...body } = f.value
  if (id) await put(`/suppliers/${id}`, body); else await post('/suppliers', body)
  open.value = false; load()
}
async function portal(row) {
  const r = await post(`/suppliers/${row.id}/portal-token`)
  await ElMessageBox.alert(`<p>发给 ${row.name}（旧链接已失效）：</p><p style="word-break:break-all"><b>${location.origin}${r.path}</b></p>`, '供应商门户链接', { dangerouslyUseHTMLString: true })
  load()
}
async function revoke(row) { await http.delete(`/suppliers/${row.id}/portal-token`); load() }
onMounted(load)
onActivated(load)
</script>
