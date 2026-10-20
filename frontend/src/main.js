import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as Icons from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import '@fontsource/barlow/400.css'
import '@fontsource/barlow/600.css'
import '@fontsource/barlow-condensed/500.css'
import '@fontsource/barlow-condensed/700.css'
import './styles.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
for (const [k, c] of Object.entries(Icons)) app.component(k, c)
app.use(createPinia()).use(router).use(ElementPlus, { locale: zhCn })
app.mount('#app')
