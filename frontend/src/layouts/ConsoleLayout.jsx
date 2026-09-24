/**
 * 控制台壳（tasklist 14.3）：侧栏 + 顶栏 + 内容区。
 *
 * 侧栏只渲染当前身份有权限的模块（PRD §3「无菜单权限不展示」），权限来自 /auth/me 的
 * permission 列表，因此管理员改完角色，用户刷新一次即可生效（不必重新登录）。
 *
 * 尺寸、配色与字号梯度都在 `config/theme.js`（令牌）和 `console.css`（令牌表达不了的部分）。
 * 外面套一层 ConfigProvider：令牌只作用于控制台子树——登录页另有一套设计，不该被这里改到；
 * 而弹窗、抽屉挂在 portal 上，只有令牌够得着它们。
 */

import { useState } from 'react'
import { Avatar, Button, ConfigProvider, Dropdown, Layout, Menu, Typography } from 'antd'
import { LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/context'
import AssistantWidget from '../components/assistant/AssistantWidget'
import BrandMark from '../components/BrandMark'
import { NAV_ITEMS } from '../config/nav'
import { CONSOLE_THEME } from '../config/theme'
import './console.css'

const { Content, Header, Sider } = Layout
const { Text, Title } = Typography

export default function ConsoleLayout() {
  return (
    <ConfigProvider theme={CONSOLE_THEME}>
      <ConsoleShell />
    </ConfigProvider>
  )
}

function ConsoleShell() {
  const { identity, hasAny, logout } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)

  const allowed = NAV_ITEMS.filter((item) => hasAny(item.requires))
  const menuItems = allowed.map(({ key, label, icon: Icon }) => ({
    key,
    label,
    icon: <Icon />,
  }))
  const selected = allowed.filter((item) => location.pathname.startsWith(item.key)).map((i) => i.key)
  const currentLabel =
    allowed.find((item) => location.pathname.startsWith(item.key))?.label ?? ''

  const onLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    // 用 vh 而不是 %：antd 的 App 包装层没有高度，百分比链会断在它那里，侧栏就只剩内容高
    <Layout className="kb-console">
      <Sider
        collapsible
        collapsed={collapsed}
        collapsedWidth={64}
        onCollapse={setCollapsed}
        theme="light"
        width={180}
      >
        <div className="kb-console__logo">
          <BrandMark size={22} />
          {collapsed ? null : <span className="kb-console__logo-text">知识库管理平台</span>}
        </div>
        <Menu
          className="kb-console__menu"
          mode="inline"
          items={menuItems}
          selectedKeys={selected}
          onClick={({ key }) => navigate(key)}
        />
        {collapsed ? null : <AssistantWidget docked />}
      </Sider>

      <Layout>
        <Header className="kb-console__header">
          {/* 参考站顶栏是「站点 › 当前页」的面包屑，不是一个大标题：同一行里把来处与所在都说完 */}
          <nav className="kb-console__crumb" aria-label="当前位置">
            <BrandMark size={18} />
            <Text className="kb-console__crumb-site">知识库管理平台</Text>
            <span className="kb-console__crumb-sep" aria-hidden="true">
              ›
            </span>
            <Title level={5} className="kb-console__page-title">
              {currentLabel}
            </Title>
          </nav>
          <Dropdown
            menu={{
              items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
              onClick: ({ key }) => {
                if (key === 'logout') {
                  onLogout()
                }
              },
            }}
          >
            {/* 用 Button 而不是裸 div 做触发器：键盘可聚焦、屏幕阅读器能念出「账号菜单」 */}
            <Button type="text" className="kb-console__account" aria-label="账号菜单">
              <Avatar className="kb-console__avatar" size="small" icon={<UserOutlined />} />
              <span className="kb-console__account-text">
                <Text className="kb-console__name">{identity?.display_name}</Text>
                <Text className="kb-console__dept">
                  {identity?.department_name ?? '未分配部门'}
                </Text>
              </span>
            </Button>
          </Dropdown>
        </Header>

        <Content className="kb-console__content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
