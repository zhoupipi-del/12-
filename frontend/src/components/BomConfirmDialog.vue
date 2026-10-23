<template>
  <el-dialog :model-value="modelValue" title="技术确认 BOM" width="640px" @update:model-value="(v) => emit('update:modelValue', v)" @open="load">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px"
      title="这张单据没有冻结的批准 BOM（迁移前的旧单据，或建单时尚无 BOM）。确认前不能开工、领料、报工、退料。请对照工艺卡/图纸，选定这张单据实际采用的版本。" />
    <template v-if="info">
      <div class="small muted" style="margin-bottom: 8px">
        数量 {{ num(info.qty) }}，{{ info.kind === 'WORK_ORDER' ? '已完工' : '已回厂' }} {{ num(info.done_qty) }}
        <span v-if="info.issued.length">；已领未退：{{ info.issued.map((x) => `${x.code} ${num(x.issued_net)}`).join('、') }}</span>
      </div>
      <el-radio-group v-model="pick" style="display: block">
        <div v-for="b in info.boms" :key="b.id" class="opt">
          <el-radio :value="`B${b.id}`">
            <b>{{ b.version }}</b> <el-tag size="small" :type="b.status === 'ACTIVE' ? 'success' : 'info'">{{ b.status === 'ACTIVE' ? '当前生效' : '已作废' }}</el-tag>
          </el-radio>
          <div class="small muted lines">{{ b.lines.map((l) => `${l.code}×${num(l.per_unit)}`).join('，') }}</div>
        </div>
        <div v-if="info.order_bom" class="opt">
          <el-radio value="ORDER"><b>沿用关联订单的批准版本</b></el-radio>
          <div class="small muted lines">{{ info.order_bom.map((l) => `${l.code}×${num(l.per_unit)}`).join('，') }}</div>
        </div>
      </el-radio-group>
      <div v-if="!info.boms.length && !info.order_bom" class="muted">该物料还没有已发布的 BOM，请先让技术维护并生效 BOM。</div>
      <el-input v-model="reason" type="textarea" :rows="2" maxlength="300" show-word-limit style="margin-top: 12px"
        placeholder="确认依据（必填），例如：对照工艺卡 GY-0231，该任务 2026-09 按 V1 开出" />
    </template>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :disabled="!pick || reason.trim().length < 2" :loading="saving" @click="submit">确认并冻结</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { num } from '../utils'

const props = defineProps({ modelValue: Boolean, kind: String, row: Object })
const emit = defineEmits(['update:modelValue', 'done'])
const info = ref(null)
const pick = ref('')
const reason = ref('')
const saving = ref(false)
const base = () => `${props.kind === 'WORK_ORDER' ? '/work-orders' : '/outsource'}/${props.row.id}`

async function load() {
  info.value = null
  pick.value = ''
  reason.value = ''
  info.value = await get(`${base()}/bom`)
  const act = info.value.boms.find((b) => b.status === 'ACTIVE')
  pick.value = info.value.order_bom ? 'ORDER' : act ? `B${act.id}` : ''
}
async function submit() {
  saving.value = true
  try {
    const body = pick.value === 'ORDER' ? { use_order_bom: true } : { bom_id: Number(pick.value.slice(1)) }
    const r = await post(`${base()}/bom-confirm`, { ...body, reason: reason.value.trim() })
    if (r.warnings?.length) await ElMessageBox.alert(r.warnings.join('\n'), '已确认，但请注意', { type: 'warning' })
    else ElMessage.success('已确认并冻结 BOM')
    emit('update:modelValue', false)
    emit('done')
  } finally { saving.value = false }
}
</script>

<style scoped>
.opt { padding: 6px 0; border-bottom: 1px solid var(--el-border-color-lighter, #eee) }
.lines { margin: 2px 0 0 24px; word-break: break-all }
</style>
