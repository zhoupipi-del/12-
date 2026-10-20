<template>
  <el-tag :type="pair[1] || undefined" size="small" effect="light" disable-transitions>{{ pair[0] }}</el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ map: Object, value: String })
const pair = computed(() => props.map?.[props.value] || [props.value || '—', 'info'])
</script>
