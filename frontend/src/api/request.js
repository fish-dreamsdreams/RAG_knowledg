/**
 * axios 统一封装：注入 JWT、拆解统一响应 {code, message, data}，失败抛 ApiError。
 * baseURL 走 Vite 代理到 http://127.0.0.1:8000（见 vite.config.js）。
 */

import axios from 'axios'
import { clearToken, getToken } from '../utils/token'

/** 401 广播事件名：AuthProvider 监听它统一退出到登录页。 */
export const UNAUTHORIZED_EVENT = 'kb:unauthorized'

export class ApiError extends Error {
  constructor(code, message, httpStatus) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.httpStatus = httpStatus
  }
}

const request = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

request.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

request.interceptors.response.use(
  // 成功：直接返回 data 字段，调用方不再关心信封
  (response) => response.data?.data ?? null,
  (error) => {
    const httpStatus = error.response?.status
    const body = error.response?.data
    // 登录接口的 401 是「口令不对」，不是「登录态失效」，不能触发退出
    const isLoginAttempt = (error.config?.url || '').includes('/auth/login')
    if (httpStatus === 401 && !isLoginAttempt) {
      clearToken()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    return Promise.reject(
      new ApiError(
        body?.code || 'SYS_NETWORK',
        body?.message || '网络异常，请稍后重试',
        httpStatus,
      ),
    )
  },
)

export default request
