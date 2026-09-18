/**
 * 登录页（tasklist 14.2 的视觉重做）。
 *
 * 守的是「这一页坏掉时最该先发现的事」：品牌与表单都在、后端状态能手动重测、点演示账号会把
 * 账号与口令一起填好、空表单拦得住、后端给的失败原因原样显示、已登录不再停在这里、无权模块
 * 的 `from` 不会被采用。
 *
 * 不断言样式：jsdom 里 CSS 本来就不生效，钉 class 名只会把实现锁死。品牌面的知识图是装饰，
 * 用 `aria-hidden` 屏蔽，所以这里也查不到它的文字——这正是期望。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchHealth } from '../../src/api/auth'
import { ApiError } from '../../src/api/request'
import { AuthContext } from '../../src/auth/context'
import LoginPage from '../../src/pages/LoginPage'

vi.mock('../../src/api/auth', () => ({ fetchHealth: vi.fn() }))

const IDENTITY = {
  display_name: '张三',
  department_name: '销售部',
  role_codes: ['employee'],
}

function renderLogin({ identity = null, login = vi.fn(), hasAny = () => false, from } = {}) {
  render(
    <AuthContext.Provider value={{ identity, hasAny, login }}>
      <MemoryRouter initialEntries={[{ pathname: '/login', state: from ? { from } : undefined }]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/chat" element={<div>落点：AI 工作台</div>} />
          <Route path="/knowledge" element={<div>落点：知识维护</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
  return { login }
}

beforeEach(() => {
  fetchHealth.mockReset()
  fetchHealth.mockResolvedValue({ status: 'ok' })
})

describe('登录页', () => {
  it('品牌面与登录表单都在，演示账号三行齐', async () => {
    renderLogin()

    expect(screen.getByText('知识库管理平台')).toBeInTheDocument()
    expect(screen.getByText('AI比TA更懂你')).toBeInTheDocument()
    expect(screen.getByText('企业内部知识检索与智能问答')).toBeInTheDocument()
    expect(screen.queryByText('薪酬管理制度')).not.toBeInTheDocument()
    expect(screen.queryByText('差旅费用标准')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /演示检索/ })).not.toBeInTheDocument()

    expect(screen.getByLabelText('账号')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登 录' })).toBeInTheDocument()

    for (const username of ['admin', 'kbadm', 'finance01']) {
      expect(screen.getByRole('button', { name: new RegExp(username) })).toBeInTheDocument()
    }

    // 后端探测默认成功：状态胶囊落到「已连接」
    expect(await screen.findByText('后端：已连接')).toBeInTheDocument()
  })

  it('后端连不上时如实说，并且点一下能重新检测', async () => {
    fetchHealth.mockRejectedValueOnce(new Error('boom'))
    renderLogin()

    expect(await screen.findByText('后端：未连接')).toBeInTheDocument()

    fetchHealth.mockResolvedValueOnce({ status: 'ok' })
    fireEvent.click(screen.getByRole('button', { name: /后端：/ }))

    expect(await screen.findByText('后端：已连接')).toBeInTheDocument()
    expect(fetchHealth).toHaveBeenCalledTimes(2)
  })

  it('点演示账号会把账号与口令一起填好，登录带的就是这一对', async () => {
    const { login } = renderLogin()

    fireEvent.click(screen.getByRole('button', { name: /admin/ }))

    expect(screen.getByLabelText('账号')).toHaveValue('admin')
    expect(screen.getByLabelText('密码')).toHaveValue('Demo@123456')

    fireEvent.click(screen.getByRole('button', { name: '登 录' }))

    await waitFor(() =>
      expect(login).toHaveBeenCalledWith({ username: 'admin', password: 'Demo@123456' }),
    )
  })

  it('空表单提交被拦下，不打后端', async () => {
    const { login } = renderLogin()

    fireEvent.click(screen.getByRole('button', { name: '登 录' }))

    expect(await screen.findByText('请输入账号')).toBeInTheDocument()
    expect(screen.getByText('请输入密码')).toBeInTheDocument()
    expect(login).not.toHaveBeenCalled()
  })

  it('登录失败显示后端给的原因，不自己编一句', async () => {
    const login = vi.fn().mockRejectedValue(new ApiError('AUTH_INVALID', '账号或密码错误', 401))
    renderLogin({ login })

    fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: '登 录' }))

    expect(await screen.findByText('账号或密码错误')).toBeInTheDocument()
  })

  it('已登录不再停在登录页，按角色落点走', async () => {
    renderLogin({ identity: IDENTITY, hasAny: (codes) => codes.includes('chat') })

    expect(await screen.findByText('落点：AI 工作台')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '登 录' })).not.toBeInTheDocument()
  })

  it('带过来的 from 无权访问时就别用，回落到本角色的落点', async () => {
    renderLogin({
      identity: IDENTITY,
      hasAny: (codes) => codes.includes('chat'),
      from: '/knowledge',
    })

    expect(await screen.findByText('落点：AI 工作台')).toBeInTheDocument()
    expect(screen.queryByText('落点：知识维护')).not.toBeInTheDocument()
  })

  it('from 有权时才用 from', async () => {
    renderLogin({
      identity: IDENTITY,
      hasAny: (codes) => codes.includes('chat') || codes.includes('knowledge'),
      from: '/knowledge',
    })

    expect(await screen.findByText('落点：知识维护')).toBeInTheDocument()
  })
})
