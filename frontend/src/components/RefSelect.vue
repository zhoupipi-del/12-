<template>
  <!-- 供应商 / 用户 / 仓库 通用下拉 -->
  <el-select :model-value="modelValue" filterable clearable :placeholder="placeholder" style="width: 100%"
             @update:model-value="(v) => $emit('update:modelValue', v)">
    <el-option v-for="o in opts" :key="o.id" :value="o.id" :label="o.label" />
  </el-select>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { get } from '../api'

const props = defineProps({
  modelValue: Number,
  kind: { type: String, default: 'supplier' }, // supplier | outsourcer | user | warehouse
  purpose: String,
  placeholder: { type: String, default: '请选择' },
})
defineEmits(['update:modelValue'])
const opts = ref([])
onMounted(async () => {
  if (props.kind === 'user') opts.value = (await get('/users')).map((u) => ({ id: u.id, label: `${u.name}（${u.department}）` }))
  else if (props.kind === 'warehouse') {
    opts.value = (await get('/warehouses')).filter((w) => !props.purpose || w.purpose === props.purpose)
      .map((w) => ({ id: w.id, label: `${w.name}（${w.purpose_label}）` }))
  } else {
    const p = props.kind === 'outsourcer' ? { outsourcer: true } : {}
    opts.value = (await get('/suppliers', p)).filter((s) => s.is_active).map((s) => ({ id: s.id, label: `${s.code} ${s.name}` }))
  }
})
</script>
