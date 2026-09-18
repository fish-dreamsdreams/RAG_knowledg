/** 认证与系统状态接口。 */

import request from './request'

export function fetchHealth() {
  return request.get('/health')
}

export function fetchCurrentUser() {
  return request.get('/auth/me')
}

/** 登录：返回 { access_token, expires_in, user }（TECH_SPEC §4.5）。 */
export function login(credentials) {
  return request.post('/auth/login', credentials)
}
