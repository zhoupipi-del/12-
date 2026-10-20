<template>
  <div class="page">
    <div class="page-head"><div><h1>系统设置</h1><p>用户和角色、开放接口、AI 接口、操作日志。</p></div></div>
    <el-tabs v-model="tab" class="panel">
      <el-tab-pane label="用户与角色" name="users">
        <p class="muted small">一个账号可同时兼多个角色，不用为不同模块切换账号。</p>
        <el-button v-if="isAdmin" type="primary" size="small" @click="editUser()">新建用户</el-button>
        <el-table :data="users" size="small" style="margin-top: 10px">
          <el-table-column prop="username" label="用户名" width="120" />
          <el-table-column prop="name" label="姓名" width="100" />
          <el-table-column prop="department" label="部门" width="110" />
          <el-table-column label="角色" min-width="240"><template #default="{ row }"><el-tag v-for="r in row.roles" :key="r" size="small" effect="plain" style="margin-right: 4px">{{ auth.meta?.roles?.[r] || r }}</el-tag></template></el-table-column>
          <el-table-column label="状态" width="70"><template #default="{ row }">{{ row.is_active ? '启用' : '停用' }}</template></el-table-column>
          <el-table-column width="70" v-if="isAdmin"><template #default="{ row }"><el-button link size="small" @click="editUser(row)">编辑</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="开放接口" name="api" v-if="isAdmin">
        <p class="small">财务、CRM、MES 等外部系统用 API Key 调用 <code>/api/open/v1/*</code>（请求头 <code>X-API-Key</code>）。接口文档：<a href="/docs" target="_blank">/docs</a></p>
        <div class="toolbar"><el-input v-model="keyName" placeholder="用途，例如 财务系统" style="width: 200px" /><el-checkbox v-model="keyWrite">允许写入（推送订单）</el-checkbox><el-button type="primary" size="small" @click="newKey">生成密钥</el-button></div>
        <el-table :data="keys" size="small" style="margin-top: 10px">
          <el-table-column prop="name" label="用途" />
          <el-table-column label="前缀" width="130"><template #default="{ row }"><span class="no">{{ row.key_prefix }}…</span></template></el-table-column>
          <el-table-column prop="scopes" label="权限" width="100" />
          <el-table-column label="最近使用" width="150"><template #default="{ row }">{{ dt(row.last_used_at) }}</template></el-table-column>
          <el-table-column label="状态" width="70"><template #default="{ row }">{{ row.is_active ? '有效' : '已吊销' }}</template></el-table-column>
          <el-table-column width="70"><template #default="{ row }"><el-button v-if="row.is_active" link type="danger" size="small" @click="revokeKey(row)">吊销</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="AI 接口" name="ai" v-if="isAdmin">
        <div class="ai-note">
          <p><b>AI 只做辅助，默认关闭。</b>齐套、冲填、锁定、库存、审批都由确定性程序完成，不依赖 AI；AI 只读取数据生成缺料简报、判断来料异常类别、回答问题，不能改单或审批。未启用或调用失败时自动退回规则结果，业务不受影响。</p>
          <p>支持 OpenAI 兼容协议：DeepSeek <code>https://api.deepseek.com/v1</code>、通义千问 <code>https://dashscope.aliyuncs.com/compatible-mode/v1</code>、本地 Ollama <code>http://127.0.0.1:11434/v1</code>（本地模型可不填密钥，数据不出厂）。</p>
        </div>
        <el-form label-width="90px" style="max-width: 560px">
          <el-form-item label="启用"><el-switch v-model="aiOn" /></el-form-item>
          <el-form-item label="接口地址"><el-input v-model="ai.ai_base_url" /></el-form-item>
          <el-form-item label="模型名"><el-input v-model="ai.ai_model" placeholder="deepseek-chat / qwen-plus / qwen2.5:7b" /></el-form-item>
          <el-form-item label="密钥"><el-input v-model="ai.ai_api_key" type="password" show-password placeholder="留空表示不修改" /></el-form-item>
          <el-form-item><el-button type="primary" @click="saveAi">保存</el-button><el-button @click="testAi" :loading="testing">测试连接</el-button></el-form-item>
        </el-form>
        <h3>调用记录</h3>
        <el-table :data="aiLogs" size="small" max-height="300">
          <el-table-column label="时间" width="150"><template #default="{ row }">{{ dt(row.created_at) }}</template></el-table-column>
          <el-table-column prop="scene" label="场景" width="140" />
          <el-table-column label="结果" width="100"><template #default="{ row }">{{ row.ok ? '成功' : row.fallback ? '已降级' : '失败' }}</template></el-table-column>
          <el-table-column prop="error" label="说明" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="数据导入" name="import">
        <p class="small">上线初始化用。顺序：先导物料，再导 BOM，最后导期初库存。每次先“预检”，没有错误再正式导入；整批要么全部成功，要么全部不写入。</p>
        <div class="imp" v-for="k in IMPORTS" :key="k.kind">
          <div class="imp-h"><b>{{ k.label }}</b><span class="muted small">{{ k.tip }}</span></div>
          <div class="toolbar">
            <el-button size="small" @click="downloadTpl(k)">下载模板</el-button>
            <input type="file" accept=".xlsx" @change="(e) => (files[k.kind] = e.target.files[0])" />
            <el-button size="small" :disabled="!files[k.kind]" @click="doImport(k, true)">预检</el-button>
            <el-button size="small" type="primary" :disabled="!files[k.kind] || !checked[k.kind]" @click="doImport(k, false)">正式导入</el-button>
          </div>
          <div v-if="results[k.kind]" class="small" style="margin-top: 6px">
            <span :class="results[k.kind].error_count ? 'short' : 'ok'">
              {{ results[k.kind].committed ? '已导入' : '预检' }}：共 {{ results[k.kind].rows }} 行，新增 {{ results[k.kind].created }}，更新 {{ results[k.kind].updated }}，错误 {{ results[k.kind].error_count }}
            </span>
            <div v-for="e in results[k.kind].errors.slice(0, 20)" :key="e.row" class="short">第 {{ e.row }} 行：{{ e.error }}</div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="操作日志" name="audit">
        <el-table :data="audit.items" size="small">
          <el-table-column label="时间" width="150"><template #default="{ row }">{{ dt(row.created_at) }}</template></el-table-column>
          <el-table-column prop="user" label="操作人" width="90" />
          <el-table-column prop="action" label="动作" width="130" />
          <el-table-column label="对象" width="160"><template #default="{ row }">{{ row.entity }} #{{ row.entity_id }}</template></el-table-column>
          <el-table-column prop="detail" label="内容" show-overflow-tooltip />
        </el-table>
        <el-pagination layout="prev, pager, next, total" :total="audit.total" :page-size="100" v-model:current-page="auditPage" @current-change="loadAudit" style="margin-top: 10px" />
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="uOpen" :title="uf.id ? '编辑用户' : '新建用户'" width="480px">
      <el-form label-width="70px">
        <el-form-item label="用户名"><el-input v-model="uf.username" :disabled="!!uf.id" /></el-form-item>
        <el-form-item label="姓名"><el-input v-model="uf.name" /></el-form-item>
        <el-form-item label="部门"><el-input v-model="uf.department" /></el-form-item>
        <el-form-item label="角色"><el-checkbox-group v-model="uf.roles"><el-checkbox v-for="(v, k) in auth.meta?.roles" :key="k" :value="k">{{ v }}</el-checkbox></el-checkbox-group></el-form-item>
        <el-form-item label="密码"><el-input v-model="uf.password" type="password" show-password :placeholder="uf.id ? '留空不修改' : '至少 6 位'" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="uf.is_active" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="saveUser">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http, { get, post, put } from '../api'
import { useAuth } from '../store'
import { dt } from '../utils'

defineOptions({ name: 'System' })
const auth = useAuth()
const isAdmin = computed(() => auth.roles.includes('ADMIN'))
const tab = ref('users')
const users = ref([])
const keys = ref([])
const keyName = ref('')
const keyWrite = ref(false)
const ai = ref({})
const aiOn = ref(false)
const aiLogs = ref([])
const testing = ref(false)
const audit = ref({ items: [], total: 0 })
const IMPORTS = [
  { kind: 'materials', label: '物料', tip: '按编码新增或更新；类型填 整机/自制/外购/外协' },
  { kind: 'boms', label: 'BOM', tip: '每行一个子件；“生效”填是则导入后直接生效' },
  { kind: 'stock', label: '期初库存', tip: '按批次导入，入库日期决定先进先出顺序' },
]
const files = ref({})
const results = ref({})
const checked = ref({})
async function downloadTpl(k) {
  const b = await http.get(`/import/${k.kind}/template`, { responseType: 'blob' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = `${k.label}导入模板.xlsx`; a.click()
}
async function doImport(k, dry) {
  const fd = new FormData(); fd.append('file', files.value[k.kind])
  const r = await http.post(`/import/${k.kind}`, fd, { params: { dry_run: dry } })
  results.value[k.kind] = r
  checked.value[k.kind] = dry && !r.error_count
  if (r.committed) ElMessage.success(`${k.label}已导入`)
}
const auditPage = ref(1)

async function loadUsers() { users.value = await get('/users') }
async function loadKeys() { keys.value = await get('/api-keys') }
async function loadAi() {
  ai.value = await get('/ai/config'); aiOn.value = ai.value.enabled; ai.value.ai_base_url = ai.value.base_url; ai.value.ai_model = ai.value.model; ai.value.ai_api_key = ''
  aiLogs.value = await get('/ai/logs')
}
async function loadAudit() { audit.value = await get('/audit', { page_no: auditPage.value }) }
watch(tab, (t) => { if (t === 'api') loadKeys(); if (t === 'ai') loadAi(); if (t === 'audit') loadAudit() })

const uOpen = ref(false)
const uf = ref({})
function editUser(u) { uf.value = u ? { ...u, password: '' } : { username: '', name: '', department: '', roles: [], password: '', is_active: true }; uOpen.value = true }
async function saveUser() {
  if (uf.value.id) await put(`/users/${uf.value.id}`, uf.value); else await post('/users', uf.value)
  uOpen.value = false; loadUsers()
}
async function newKey() {
  if (!keyName.value) return ElMessage.warning('请填写用途')
  const r = await post('/api-keys', { name: keyName.value, scopes: keyWrite.value ? ['read', 'write'] : ['read'] })
  await ElMessageBox.alert(`<p>${r.tip}</p><p style="word-break:break-all"><b>${r.key}</b></p>`, '新密钥', { dangerouslyUseHTMLString: true })
  keyName.value = ''; loadKeys()
}
async function revokeKey(k) { await http.delete(`/api-keys/${k.id}`); loadKeys() }
async function saveAi() {
  await put('/ai/config', { ai_enabled: aiOn.value ? '1' : '0', ai_base_url: ai.value.ai_base_url, ai_model: ai.value.ai_model, ai_api_key: ai.value.ai_api_key || null })
  ElMessage.success('已保存，刷新页面后顶部出现 AI 助手入口'); loadAi()
}
async function testAi() {
  testing.value = true
  try { const r = await post('/ai/test'); r.ok ? ElMessage.success(`连接成功：${r.reply}`) : ElMessage.error(r.reply); loadAi() } finally { testing.value = false }
}
onMounted(loadUsers)
</script>

<style scoped>
code { font-family: var(--num); background: var(--bg); padding: 1px 5px; border-radius: 2px; font-size: 12px; }
.ai-note { background: var(--bg); padding: 12px 14px; font-size: 13px; line-height: 1.8; margin-bottom: 16px; max-width: 76ch; }
.ai-note p { margin: 0 0 6px; }
h3 { font-size: 14px; margin: 18px 0 8px; }
.imp { border: 1px solid var(--line-soft); padding: 12px 14px; margin-top: 10px; }
.imp-h { display: flex; gap: 12px; align-items: baseline; margin-bottom: 8px; }
</style>
