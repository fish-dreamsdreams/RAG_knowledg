/**
 * 登录页（tasklist 14.2）：左侧品牌面 + 右侧表单，样式在 `login.css`。
 *
 * 三处刻意的选择：
 * 1. 品牌面画的是这个产品自己的事——一张知识图底纹，不是通用渐变插画；图对屏幕阅读器
 *    `aria-hidden`，正文照常可读。
 * 2. 整体浅色系，与内部控制台同一套观感（两边都是白底 + antd 默认蓝），不另立一套主题。
 *    左栏靠一层淡蓝渐变与点阵网格与右栏分开。
 * 3. 演示账号点一行就把账号与口令填进表单，演示时少一次手打；口令提示只在开发模式出现，
 *    与后端种子（data/seeds/users.json）一致。
 * 3. 登录逻辑一行没动：成功后按角色默认落点跳转（PRD §4），带过来的 `from` 只在它确实是当前
 *    身份可访问的模块时才用。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Form, Input } from 'antd'
import {
  BulbOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Navigate, useLocation } from 'react-router-dom'
import { fetchHealth } from '../api/auth'
import { ApiError } from '../api/request'
import { useAuth } from '../auth/context'
import BrandMark from '../components/BrandMark'
import { NAV_ITEM_BY_PATH, landingPath } from '../config/nav'
import './login.css'

// 演示账号只在开发模式提示；口令统一 Demo@123456（种子文件 data/seeds/users.json）
const DEMO_PASSWORD = 'Demo@123456'
const DEMO_ACCOUNTS = [
  ['admin', '系统管理员'],
  ['kbadm', '知识管理员'],
  ['finance01', '普通员工（财务部）'],
]

// 三条卖点（文案由用户指定）
const POINTS = [
  { icon: SearchOutlined, text: '提问 → 命中知识 → 引用回答' },
  { icon: SafetyCertificateOutlined, text: '角色权限划分，资料按权访问' },
  { icon: BulbOutlined, text: '支持知识缺口管理' },
]

// 装饰用知识图：坐标写死（不用随机数），铺满左栏做底纹。
const GRAPH_NODES = [
  [520, 400, 5],
  [430, 560, 6],
  [300, 680, 5],
  [170, 760, 5],
  [140, 300, 6],
  [110, 520, 6],
  [420, 120, 4],
  [560, 620, 4],
  [330, 840, 4],
  [90, 200, 4],
  [250, 520, 4],
  [250, 180, 5],
]
const GRAPH_EDGES = [
  [300, 430, 470, 250],
  [300, 430, 520, 400],
  [300, 430, 430, 560],
  [300, 430, 300, 680],
  [300, 430, 170, 760],
  [300, 430, 140, 300],
  [300, 430, 110, 520],
  [300, 430, 250, 180],
  [300, 430, 420, 120],
  [300, 430, 560, 620],
  [300, 430, 330, 840],
  [300, 430, 90, 200],
  [300, 430, 250, 520],
  [250, 180, 420, 120],
  [140, 300, 250, 180],
  [470, 250, 520, 400],
  [430, 560, 560, 620],
  [300, 680, 330, 840],
  [110, 520, 170, 760],
  [90, 200, 140, 300],
]

/** 装饰图：问句节点 → 核心 → 命中节点；两道检索波走不同路线。 */
function KnowledgeGraph() {
  return (
    <svg
      className="kb-login__graph"
      viewBox="0 0 640 900"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
      focusable="false"
    >
      {GRAPH_EDGES.map(([x1, y1, x2, y2], index) => (
        <line
          key={`${x1}-${y1}-${x2}-${y2}`}
          className="kb-login__graph-edge"
          /* pathLength=1 把每条线归一到 0—1，自绘动画就不必逐条量长度；
             再按序号错开一点延迟，线条是一根根长出来的，不是一下子全亮 */
          pathLength="1"
          style={{ animationDelay: `${0.12 + (index % 7) * 0.09}s` }}
          x1={x1}
          y1={y1}
          x2={x2}
          y2={y2}
        />
      ))}

      {/* 一道从问句节点出发，一道从另一份制度汇入核心 */}
      <path className="kb-login__graph-flow" d="M140 300 L300 430 L470 250" fill="none" />
      <path
        className="kb-login__graph-flow kb-login__graph-flow--b"
        d="M250 180 L300 430 L470 250"
        fill="none"
      />

      {/* 命中节点两圈涟漪错开，像持续在响应 */}
      <circle className="kb-login__graph-ring" cx="470" cy="250" r="10" />
      <circle className="kb-login__graph-ring kb-login__graph-ring--b" cx="470" cy="250" r="10" />

      {GRAPH_NODES.map(([cx, cy, r], index) => (
        <circle
          key={`${cx}-${cy}`}
          className="kb-login__graph-node"
          style={{ animationDelay: `${(index % 6) * 0.5}s` }}
          cx={cx}
          cy={cy}
          r={r}
        />
      ))}
      <circle className="kb-login__graph-node kb-login__graph-node--core" cx="300" cy="430" r="9" />
      <circle className="kb-login__graph-node kb-login__graph-node--hit" cx="470" cy="250" r="7" />
    </svg>
  )
}

export default function LoginPage() {
  const { identity, hasAny, login } = useAuth()
  const location = useLocation()
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [health, setHealth] = useState(null)

  const brandRef = useRef(null)
  const brandRect = useRef(null)

  const checkHealth = useCallback(() => {
    fetchHealth()
      .then((data) => setHealth(data?.status === 'ok'))
      .catch(() => setHealth(false))
  }, [])

  useEffect(checkHealth, [checkHealth])

  if (identity) {
    const from = location.state?.from
    const item = from ? NAV_ITEM_BY_PATH[from] : undefined
    const target = item && hasAny(item.requires) ? from : landingPath(identity, hasAny) || '/chat'
    return <Navigate to={target} replace />
  }

  const onFinish = async (values) => {
    setSubmitting(true)
    setError(null)
    try {
      await login(values)
    } catch (apiError) {
      // 登录失败不区分「用户不存在 / 口令错 / 已停用」，后端也是同一句提示
      setError(apiError instanceof ApiError ? apiError.message : '登录失败，请稍后重试')
      setSubmitting(false)
    }
  }

  // 点一行演示账号即填入：口令对不上时也不用回头翻文档
  const fillDemo = (username) => {
    form.setFieldsValue({ username, password: DEMO_PASSWORD })
    setError(null)
  }

  // 指针在左栏移动时图形做几像素的视差跟随：只改 CSS 变量，不触发 React 重渲染
  const onBrandEnter = () => {
    brandRect.current = brandRef.current?.getBoundingClientRect() ?? null
  }

  const onBrandMove = (event) => {
    const element = brandRef.current
    const rect = brandRect.current
    if (!element || !rect) return
    const x = (event.clientX - rect.left) / rect.width - 0.5
    const y = (event.clientY - rect.top) / rect.height - 0.5
    element.style.setProperty('--kb-px', `${(x * 16).toFixed(1)}px`)
    element.style.setProperty('--kb-py', `${(y * 12).toFixed(1)}px`)
  }

  const onBrandLeave = () => {
    const element = brandRef.current
    if (!element) return
    element.style.setProperty('--kb-px', '0px')
    element.style.setProperty('--kb-py', '0px')
  }

  const healthLabel = health === null ? '检测中' : health ? '已连接' : '未连接'
  const healthDot =
    health === null ? ' kb-login__dot--wait' : health ? ' kb-login__dot--ok' : ' kb-login__dot--bad'

  return (
    <div className="kb-login">
      <aside
        className="kb-login__brand"
        ref={brandRef}
        onPointerEnter={onBrandEnter}
        onPointerMove={onBrandMove}
        onPointerLeave={onBrandLeave}
      >
        {/* 两层极慢漂移的柔光：让浅色面是活的，但慢到不会吸引注意力 */}
        <span className="kb-login__blob kb-login__blob--a" aria-hidden="true" />
        <span className="kb-login__blob kb-login__blob--b" aria-hidden="true" />

        <div className="kb-login__stack">
          <div className="kb-login__mark">
            <BrandMark size={48} />
            <span className="kb-login__mark-name">知识库管理平台</span>
          </div>

          <div className="kb-login__say">
            <h1 className="kb-login__headline">AI比TA更懂你</h1>
            <p className="kb-login__sub">企业内部知识检索与智能问答</p>
            <ul className="kb-login__points">
              {POINTS.map(({ icon: Icon, text }) => (
                <li className="kb-login__point" key={text}>
                  <Icon className="kb-login__point-icon" />
                  <span>{text}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <KnowledgeGraph />
      </aside>

      <main className="kb-login__panel">
        <div className="kb-login__top">
          <button
            type="button"
            className="kb-login__pill"
            onClick={() => {
              // 重置回「检测中」放在事件里而不是 effect 里：effect 里同步 setState 会多推一轮渲染
              setHealth(null)
              checkHealth()
            }}
            title="点击重新检测后端连通性"
          >
            <span className={`kb-login__dot${healthDot}`} />
            后端：{healthLabel}
          </button>
        </div>

        <div className="kb-login__body">
          <div className="kb-login__form">
            <h2 className="kb-login__title">登录</h2>
            <p className="kb-login__hint">使用企业账号继续，权限按部门自动生效</p>

            <Form form={form} layout="vertical" size="large" onFinish={onFinish} requiredMark={false}>
              <Form.Item
                name="username"
                label="账号"
                rules={[{ required: true, message: '请输入账号' }]}
              >
                <Input prefix={<UserOutlined />} placeholder="请输入账号" autoComplete="username" />
              </Form.Item>
              <Form.Item
                name="password"
                label="密码"
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <Input.Password
                  prefix={<LockOutlined />}
                  placeholder="请输入密码"
                  autoComplete="current-password"
                />
              </Form.Item>

              {error ? (
                <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
              ) : null}

              <Button type="primary" htmlType="submit" block loading={submitting}>
                登录
              </Button>
            </Form>

            {import.meta.env.DEV ? (
              <section className="kb-login__demo" aria-label="演示账号">
                <div className="kb-login__demo-title">
                  <span>演示账号</span>
                  <span>口令均为 {DEMO_PASSWORD}</span>
                </div>
                <div className="kb-login__demo-list">
                  {DEMO_ACCOUNTS.map(([username, role]) => (
                    <button
                      key={username}
                      type="button"
                      className="kb-login__demo-row"
                      onClick={() => fillDemo(username)}
                    >
                      <span className="kb-login__demo-user">{username}</span>
                      <span className="kb-login__demo-role">{role}</span>
                      <span className="kb-login__demo-fill">填入</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            <p className="kb-login__foot">企业内部系统 · 请使用公司账号登录</p>
          </div>
        </div>
      </main>
    </div>
  )
}
