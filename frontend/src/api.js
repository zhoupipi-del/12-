import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from './router'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

http.interceptors.request.use((cfg) => {
  const t = localStorage.getItem('ps_token')
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

http.interceptors.response.use(
  (r) => r.data,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? '请检查填写内容：' + detail.map((d) => d.loc?.slice(-1)[0]).join('、') : '网络异常，请稍后重试'
    if (status === 401 && !err.config.url.startsWith('/portal') && !err.config.url.startsWith('/board')) {
      localStorage.removeItem('ps_token')
      router.push('/login')
    }
    if (!err.config.silent) ElMessage.error(msg)
    err.message = msg
    err.data = err.response?.data?.data
    return Promise.reject(err)
  },
)

export default http
export const get = (url, params, cfg = {}) => http.get(url, { params, ...cfg })
export const post = (url, data, cfg = {}) => http.post(url, data, cfg)
export const put = (url, data, cfg = {}) => http.put(url, data, cfg)
export const del = (url, cfg = {}) => http.delete(url, cfg)
