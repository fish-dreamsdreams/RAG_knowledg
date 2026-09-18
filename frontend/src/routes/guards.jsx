/**
 * 路由守卫（tasklist 14.2 / 14.3）。
 *
 * 三条规则：
 * 1. 未登录 → 跳登录页，并记住原目标，登录后能回到原处。
 * 2. 已登录但无权限码 → 通用空态，**不写模块名**——写"你无权访问运营看板"等于把未授权
 *    模块的名字告诉了他（design.md §3.6）。
 * 3. 不存在的路径 → 与无权限同一套空态：两者的区别本身就是信息。
 */

import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { Result, Spin } from 'antd'
import { useAuth } from '../auth/context'
import { landingPath } from '../config/nav'

/** 通用空态：无权限与不存在共用，不留任何模块线索。 */
export function PermissionEmpty() {
  return (
    <Result
      status="403"
      title="无法访问"
      subTitle="该页面不存在，或当前账号没有访问权限。"
    />
  )
}

function RestoringIdentity() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}>
      <Spin tip="正在恢复登录态…" size="large">
        <div style={{ width: 200, height: 60 }} />
      </Spin>
    </div>
  )
}

/** 登录守卫：包住整个控制台。 */
export function RequireAuth() {
  const { identity, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return <RestoringIdentity />
  }
  if (!identity) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}

/** 页面级权限守卫：权限不足时给通用空态。 */
export function RequirePermission({ codes, children }) {
  const { hasAny } = useAuth()
  if (!hasAny(codes)) {
    return <PermissionEmpty />
  }
  return children
}

/** `/` 落点：按角色默认落点（PRD §4）或第一个有权限的模块。 */
export function LandingRedirect() {
  const { identity, hasAny } = useAuth()
  const path = landingPath(identity, hasAny)
  if (!path) {
    return <PermissionEmpty />
  }
  return <Navigate to={path} replace />
}
