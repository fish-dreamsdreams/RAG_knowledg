/** 路由装配（tasklist 14.2 / 14.3）：登录页 + 控制台壳 + 权限守卫。 */

import { Suspense, lazy } from 'react'
import { Spin } from 'antd'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import ChatPage from './pages/ChatPage'
import KnowledgePage from './pages/KnowledgePage'
import LoginPage from './pages/LoginPage'
import OperationsPage from './pages/OperationsPage'
import OrganizationPage from './pages/OrganizationPage'
import SystemPage from './pages/SystemPage'
import ConsoleLayout from './layouts/ConsoleLayout'
import { NAV_ITEM_BY_PATH } from './config/nav'
import {
  LandingRedirect,
  PermissionEmpty,
  RequireAuth,
  RequirePermission,
} from './routes/guards'

/**
 * 看板是唯一带 ECharts 的页面，改成按路由加载：图表库单独成一个 chunk，不进首屏。
 * 其余页面保持同步导入——它们都是登录后马上要用的，拆开只会多一轮往返。
 */
const DashboardPage = lazy(() => import('./pages/DashboardPage'))

function RouteLoading() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
      <Spin />
    </div>
  )
}

/** 按路径查表挂上页面级权限守卫，路由表与菜单表不会各写一份权限码。 */
function guarded(path, element) {
  return (
    <RequirePermission codes={NAV_ITEM_BY_PATH[path].requires}>{element}</RequirePermission>
  )
}

const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: <RequireAuth />,
    children: [
      {
        element: <ConsoleLayout />,
        children: [
          { index: true, element: <LandingRedirect /> },
          { path: 'chat', element: guarded('/chat', <ChatPage />) },
          { path: 'knowledge', element: guarded('/knowledge', <KnowledgePage />) },
          { path: 'operations', element: guarded('/operations', <OperationsPage />) },
          { path: 'dashboard', element: guarded('/dashboard', <Suspense fallback={<RouteLoading />}><DashboardPage /></Suspense>) },
          { path: 'org', element: guarded('/org', <OrganizationPage />) },
          { path: 'system', element: guarded('/system', <SystemPage />) },
        ],
      },
    ],
  },
  // 不存在的路径与无权限共用同一套空态：区别本身就是信息
  { path: '*', element: <PermissionEmpty /> },
])

export default function App() {
  return <RouterProvider router={router} />
}
