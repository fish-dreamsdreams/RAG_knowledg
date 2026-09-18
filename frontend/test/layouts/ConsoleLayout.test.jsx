/**
 * 控制台侧栏的权限边界（tasklist 14.8 / PRD §3「无菜单权限不展示」）。
 *
 * 断言分两层：有权限的菜单在、没权限的菜单不在，并且名字**一个字符都不出现在 DOM 里**。
 * 只测「菜单项没渲染」是不够的——把菜单名塞进 disabled 项、tooltip 或标题栏同样算泄露，
 * 所以这里直接查整棵树。
 */

import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { AuthContext } from '../../src/auth/context'
import { NAV_ITEMS } from '../../src/config/nav'
import ConsoleLayout from '../../src/layouts/ConsoleLayout'

const IDENTITY = {
  display_name: '张三',
  department_name: '销售部',
  role_codes: ['employee'],
}

/**
 * rc-menu 挂载后会异步补一次选中/尺寸状态。不等它落定就断言，React 会报 act 警告——警告
 * 多了就没人看测试输出了，所以这里显式收完这一轮再断言。
 */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

/** 按 `permissions` 造一个能过 `hasAny` 的登录态；`*`（system_admin）全放行。 */
async function renderLayout(permissions) {
  const hasAny = (codes) =>
    codes.some((code) => code === '*' || permissions.includes(code))

  render(
    <AuthContext.Provider value={{ identity: IDENTITY, hasAny, logout: () => {} }}>
      <MemoryRouter initialEntries={['/chat']}>
        <ConsoleLayout />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
  await settle()
}

describe('ConsoleLayout 的侧栏菜单', () => {
  it('只渲染有权限的菜单，没权限的连名字都不出现', async () => {
    await renderLayout(['chat'])

    expect(screen.getAllByText('AI问答').length).toBeGreaterThan(0)
    for (const label of ['知识维护', '沉淀运营', '运营看板', '组织架构', '系统配置']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
    expect(document.body.textContent).not.toContain('系统配置')
    expect(document.body.textContent).not.toContain('组织架构')
  })

  it('一个权限都没有时侧栏为空，外壳（账号）仍在', async () => {
    await renderLayout([])

    for (const item of NAV_ITEMS) {
      expect(screen.queryByText(item.label)).not.toBeInTheDocument()
    }
    expect(screen.getByText('张三')).toBeInTheDocument()
  })

  it('自定义角色只拿到页面主按钮码时也能进对应菜单', async () => {
    // requires = 菜单码 ∪ 页面主按钮码（config/nav.js）：只授按钮码不该被挡在门外
    await renderLayout(['gap:view'])

    expect(screen.getByText('沉淀运营')).toBeInTheDocument()
    expect(screen.queryByText('系统配置')).not.toBeInTheDocument()
  })
})
