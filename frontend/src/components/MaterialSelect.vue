<template>
  <el-select :model-value="modelValue" filterable remote clearable :remote-method="search" :loading="loading"
             :placeholder="placeholder" style="width: 100%" @update:model-value="pick" @visible-change="(v) => v && !opts.length && search('')">
    <el-option v-for="m in opts" :key="m.id" :value="m.id" :label="`${m.code} ${m.name}`">
      <span class="no">{{ m.code }}</span>
      <span style="margin-left: 8px">{{ m.name }}</span>
      <span class="muted small" style="margin-left: 8px">{{ m.spec }}</span>
      <span class="muted small" style="float: right">{{ MAT_TYPE[m.type] }}</span>
    </el-option>
  </el-select>
</template>

<script setup>
import { ref, watch } from 'vue'
import { get } from '../api'
import { MAT_TYPE } from '../utils'

const props = defineProps({ modelValue: Number, type: String, placeholder: { type: String, default: '编码 / 名称 / 图号' } })
const emit = defineEmits(['update:modelValue', 'picked'])
const opts = ref([])
const loading = ref(false)

async function search(q) {
  loading.value = true
  try { opts.value = (await get('/materials', { keyword: q, type: props.type || '', size: 30 })).items } finally { loading.value = false }
}
function pick(v) {
  emit('update:modelValue', v)
  emit('picked', opts.value.find((m) => m.id === v))
}
watch(() => props.modelValue, async (v) => {
  if (v && !opts.value.find((m) => m.id === v)) {
    const all = (await get('/materials', { size: 500, type: props.type || '' })).items
    opts.value = all.filter((m) => m.id === v).concat(opts.value)
  }
}, { immediate: true })
</script>
