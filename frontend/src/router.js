import { createRouter, createWebHistory } from 'vue-router'

const v = (n) => () => import(`./views/${n}.vue`)

export const NAV = [
  { group: '计划与齐套', items: [
    { path: '/', name: 'Dashboard', title: '工作台', icon: 'Odometer' },
    { path: '/kitting', name: 'Kitting', title: '齐套中心', icon: 'Grid' },
    { path: '/material-plan', name: 'MaterialPlan', title: '订单备料', icon: 'List' },
    { path: '/orders', name: 'Orders', title: '销售订单', icon: 'Tickets' },
    { path: '/realloc', name: 'Realloc', title: '调料申请', icon: 'Switch' },
    { path: '/alerts', name: 'Alerts', title: '预警中心', icon: 'Bell' },
  ] },
  { group: '采购与外协', items: [
    { path: '/requisitions', name: 'Requisitions', title: '请购计划单', icon: 'DocumentAdd' },
    { path: '/purchase-orders', name: 'PurchaseOrders', title: '采购订单', icon: 'Document' },
    { path: '/tracking', name: 'Tracking', title: '采购跟单', icon: 'Van' },
    { path: '/outsource', name: 'Outsource', title: '外协单', icon: 'Connection' },
  ] },
  { group: '仓储与质量', items: [
    { path: '/arrivals', name: 'Arrivals', title: '到货检验', icon: 'Box' },
    { path: '/exceptions', name: 'Exceptions', title: '来料异常', icon: 'Warning' },
    { path: '/stock', name: 'Stock', title: '库存', icon: 'Coin' },
    { path: '/picks', name: 'Picks', title: '领料', icon: 'ShoppingCart' },
  ] },
  { group: '车间与交付', items: [
    { path: '/work-orders', name: 'WorkOrders', title: '自制任务', icon: 'SetUp' },
    { path: '/shipments', name: 'Shipments', title: '发货计划', icon: 'Promotion' },
    { path: '/board-admin', name: 'BoardAdmin', title: '看板与周计划', icon: 'Monitor' },
  ] },
  { group: '技术与基础数据', items: [
    { path: '/drawings', name: 'Drawings', title: '图纸版本', icon: 'Picture' },
    { path: '/boms', name: 'Boms', title: 'BOM', icon: 'Share' },
    { path: '/materials', name: 'Materials', title: '物料', icon: 'Goods' },
    { path: '/suppliers', name: 'Suppliers', title: '供应商', icon: 'OfficeBuilding' },
    { path: '/warehouses', name: 'Warehouses', title: '仓库库位', icon: 'House' },
  ] },
  { group: '系统', items: [
    { path: '/system', name: 'System', title: '系统设置', icon: 'Setting' },
  ] },
]

const pages = NAV.flatMap((g) => g.items).map((i) => ({
  path: i.path, name: i.name, component: v(i.name), meta: { title: i.title },
}))

const routes = [
  { path: '/login', name: 'Login', component: v('Login'), meta: { public: true } },
  { path: '/board/workshop', name: 'BoardWorkshop', component: v('BoardWorkshop'), meta: { public: true } },
  { path: '/board/weekly', name: 'BoardWeekly', component: v('BoardWeekly'), meta: { public: true } },
  { path: '/portal/:token', name: 'Portal', component: v('Portal'), meta: { public: true } },
  {
    path: '/', component: () => import('./layout/Layout.vue'),
    children: [
      ...pages,
      { path: '/orders/:id', name: 'OrderDetail', component: v('OrderDetail'), meta: { title: '订单详情' } },
      { path: '/purchase-orders/:id', name: 'PurchaseOrderDetail', component: v('PurchaseOrderDetail'), meta: { title: '采购单' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  const { useAuth } = await import('./store')
  const auth = useAuth()
  if (!auth.user && !(await auth.load())) return { path: '/login', query: { next: to.fullPath } }
  return true
})

export default router
