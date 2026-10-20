export const KIT = {
  READY: { label: '已齐', color: 'var(--ok)', tag: 'success' },
  INCOMING_OK: { label: '在途可齐', color: 'var(--wait)', tag: 'warning' },
  INCOMING_LATE: { label: '在途延误', color: 'var(--late)', tag: 'danger' },
  SHORT: { label: '缺料', color: 'var(--short)', tag: 'danger' },
}
export const COLOR_LABEL = { GREEN: '已齐套', YELLOW: '在途可齐', RED: '有风险' }

export const ORDER_STATUS = {
  PLANNED: ['已录入', 'info'], RELEASED: ['已下达', ''], ASSEMBLING: ['装配中', 'warning'],
  COMPLETED: ['已完工', 'success'], SHIPPED: ['已发货', 'success'], CLOSED: ['已关闭', 'info'], CANCELLED: ['已取消', 'info'],
}
export const PO_STATUS = { DRAFT: ['草稿', 'info'], APPROVED: ['已审核', ''], CLOSED: ['已完成', 'success'], CANCELLED: ['已取消', 'info'] }
export const LINE_STATUS = { OPEN: ['未到货', ''], PARTIAL: ['部分到货', 'warning'], RECEIVED: ['已入库', 'success'], CLOSED: ['已关闭', 'info'], CANCELLED: ['已取消', 'info'] }
export const REQ_STATUS = { DRAFT: ['草稿', 'info'], CONFIRMED: ['已确认', ''], CONVERTED: ['已转采购', 'success'], CANCELLED: ['已取消', 'info'] }
export const WO_STATUS = { PLANNED: ['待开工', 'info'], STARTED: ['加工中', 'warning'], DONE: ['已完工', 'success'], CANCELLED: ['已取消', 'info'] }
export const OS_STATUS = { PLANNED: ['待发出', 'info'], SENT: ['外协中', 'warning'], PARTIAL: ['部分回厂', 'warning'], RETURNED: ['已回厂', 'success'], CANCELLED: ['已取消', 'info'] }
export const EXC_STATUS = { DRAFT: ['草稿', 'info'], SUBMITTED: ['待审核', 'danger'], REJECTED: ['已驳回', 'info'], DISPOSED: ['处置中', 'warning'], AWAIT_REINSPECT: ['待复检', 'warning'], REINSPECTED: ['已复检', ''], CLOSED: ['已关闭', 'success'] }
export const SHIP_STATUS = { DRAFT: ['草稿', 'info'], SUBMITTED: ['待审批', 'warning'], APPROVED: ['已批准', ''], REJECTED: ['已驳回', 'danger'], SHIPPED: ['已发货', 'success'], CANCELLED: ['已取消', 'info'] }
export const MAT_TYPE = { PRODUCT: '整机', MAKE: '自制', BUY: '外购', OUTSOURCE: '外协' }
export const PRIORITY = { 1: '特急', 2: '急', 3: '普通', 4: '低' }
export const SOURCE_LABEL = { STOCK: '库存', LOCKED: '锁定', PO: '采购', OUTSOURCE: '外协', WO: '自制', ARRIVAL: '待检' }
export const QUALITY_DISP = { RETURN: '退货', EXCHANGE: '换货', REWORK: '返修/挑选', CONCESSION: '让步接收' }
export const QTY_DISP = { SUPPLEMENT: '供应商补发', CLOSE_SHORT: '按实收关闭余量' }
export const RESP = { SUPPLIER: '供应商', INTERNAL: '内部', LOGISTICS: '物流' }

export function num(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return v
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}
export function money(v) {
  if (v === null || v === undefined) return '—'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
export function day(v) {
  if (!v) return '—'
  return String(v).slice(0, 10)
}
export function dt(v) {
  if (!v) return '—'
  return String(v).replace('T', ' ').slice(0, 16)
}
export function daysFrom(v) {
  if (!v) return null
  const d = new Date(String(v).slice(0, 10) + 'T00:00:00')
  const t = new Date(); t.setHours(0, 0, 0, 0)
  return Math.round((d - t) / 86400000)
}
export function relDay(v) {
  const n = daysFrom(v)
  if (n === null) return ''
  if (n === 0) return '今天'
  if (n === 1) return '明天'
  if (n === -1) return '昨天'
  return n > 0 ? `${n} 天后` : `超 ${-n} 天`
}
export function today(offset = 0) {
  const d = new Date(); d.setDate(d.getDate() + offset)
  return d.toISOString().slice(0, 10)
}
export function pct(v) {
  return v === null || v === undefined ? '—' : Math.round(Number(v) * 100) + '%'
}
