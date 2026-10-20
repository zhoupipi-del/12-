<template>
  <!-- 齐套条：一张订单的每个一级物料一节，颜色即状态。一眼看出卡在哪几个件上。 -->
  <div class="strip" :class="{ big }" role="img" :aria-label="aria">
    <el-tooltip v-for="(s, i) in segs" :key="i" :content="s.tip" placement="top" :show-after="150">
      <span class="seg" :class="s.status" :style="{ flexGrow: 1 }" />
    </el-tooltip>
    <span v-if="!segs.length" class="seg empty" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { KIT, num } from '../utils'

const props = defineProps({
  lines: { type: Array, default: () => [] }, // kit_lines（只取 level=1）
  big: Boolean,
})
const order = { SHORT: 0, INCOMING_LATE: 1, INCOMING_OK: 2, READY: 3 }
const segs = computed(() => props.lines.filter((l) => l.level === 1)
  .sort((a, b) => order[a.status] - order[b.status])
  .map((l) => ({
    status: l.status,
    tip: `${l.name}  ${KIT[l.status]?.label}` + (l.short_qty > 0 ? `  缺 ${num(l.short_qty)}` : '') +
      (l.expected_date && l.status !== 'READY' ? `  预计 ${l.expected_date}` : ''),
  })))
const aria = computed(() => {
  const c = {}
  segs.value.forEach((s) => { c[s.status] = (c[s.status] || 0) + 1 })
  return Object.entries(c).map(([k, n]) => `${KIT[k].label} ${n} 项`).join('，')
})
</script>

<style scoped>
.strip { display: flex; gap: 2px; height: 10px; min-width: 120px; }
.strip.big { height: 18px; gap: 3px; }
.seg { flex-basis: 0; border-radius: 1px; background: var(--ok); }
.seg.INCOMING_OK { background: var(--wait); }
.seg.INCOMING_LATE { background: var(--late); }
.seg.SHORT { background: var(--short); }
.seg.empty { background: var(--line); flex-grow: 1; }
</style>
