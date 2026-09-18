/**
 * 登录态 Provider：Token 存 localStorage，身份（含 permissions）刷新页面时向 /auth/me 取回。
 *
 * 前端不缓存权限：角色改完服务端立即生效，刷新一次身份即可，不必重新登录（design.md §3.6）。
 * Token 过期由请求层广播 401，这里统一退出——否则界面停在控制台，点什么都失败。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchCurrentUser, login as loginRequest } from '../api/auth'
import { UNAUTHORIZED_EVENT } from '../api/request'
import { clearToken, getToken, setToken } from '../utils/token'
import { AuthContext } from './context'

const WILDCARD = '*'

export default function AuthProvider({ children }) {
  const [identity, setIdentity] = useState(null)
  // 有 Token 才需要恢复身份；没有就直接是未登录态，避免闪一下登录页
  const [loading, setLoading] = useState(() => Boolean(getToken()))

  useEffect(() => {
    if (!getToken()) {
      return undefined
    }
    let cancelled = false
    fetchCurrentUser()
      .then((me) => {
        if (!cancelled) setIdentity(me)
      })
      .catch(() => {
        // Token 失效/账号被停用：清掉本地凭据，回到未登录
        clearToken()
        if (!cancelled) setIdentity(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const logout = useCallback(() => {
    clearToken()
    setIdentity(null)
  }, [])

  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, logout)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, logout)
  }, [logout])

  const login = useCallback(async (credentials) => {
    const data = await loginRequest(credentials)
    setToken(data.access_token)
    setIdentity(data.user)
    return data.user
  }, [])

  const value = useMemo(
    () => ({
      identity,
      loading,
      login,
      logout,
      /** 权限码命中任一即放行；`*`（system_admin）全部放行。 */
      hasAny: (codes) => {
        const owned = identity?.permissions ?? []
        return owned.includes(WILDCARD) || codes.some((code) => owned.includes(code))
      },
    }),
    [identity, loading, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
