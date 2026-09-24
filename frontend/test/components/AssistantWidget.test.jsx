import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { streamAssistantChat } from '../../src/api/assistant'
import { AuthContext } from '../../src/auth/context'
import AssistantWidget from '../../src/components/assistant/AssistantWidget'

vi.mock('../../src/api/assistant', () => ({
  streamAssistantChat: vi.fn(),
}))

vi.mock('../../src/api/system', () => ({
  getModelConfig: vi.fn(async () => ({
    base_url: 'https://api.example.com/v1',
    chat_model: 'demo-chat',
    temperature: 0.2,
    max_tokens: 2048,
    api_key_configured: false,
    api_key_masked: null,
  })),
  updateModelConfig: vi.fn(),
}))

function renderWidget() {
  return render(
    <AuthContext.Provider
      value={{
        identity: { permissions: ['sys:model'] },
        hasAny: (codes) => codes.includes('sys:model'),
        logout: () => {},
      }}
    >
      <AssistantWidget docked />
    </AuthContext.Provider>,
  )
}

describe('AssistantWidget', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it('jsdom 没有 WebGL 时提示失败而不是空白', () => {
    renderWidget()
    expect(screen.getByRole('status')).toHaveTextContent('3D 助手加载失败')
  })

  it('头上冒泡框默认文案', () => {
    renderWidget()
    expect(screen.getByText('萌新小小，时刻准备着！')).toBeInTheDocument()
  })

  it('右键关闭后记住，并出现重新打开按钮', () => {
    renderWidget()
    fireEvent.contextMenu(screen.getByRole('figure', { name: '3D 助手' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '关闭助手' }))
    expect(screen.getByRole('button', { name: '显示 3D 助手' })).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('kb_assistant_widget')).open).toBe(false)
  })

  it('关闭后再打开会回到窗口', () => {
    localStorage.setItem('kb_assistant_widget', JSON.stringify({ open: false, x: null, y: null }))
    renderWidget()
    fireEvent.click(screen.getByRole('button', { name: '显示 3D 助手' }))
    expect(screen.getByRole('figure', { name: '3D 助手' })).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('kb_assistant_widget')).open).toBe(true)
  })

  it('右键设置打开 LLM 配置', async () => {
    renderWidget()
    fireEvent.contextMenu(screen.getByRole('figure', { name: '3D 助手' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '设置' }))
    expect(await screen.findByText('大模型接口地址（Base URL）')).toBeInTheDocument()
    expect(screen.getByText('模型名称')).toBeInTheDocument()
    expect(screen.getByText('API 密钥')).toBeInTheDocument()
  })

  it('右键对话打开输入框', () => {
    renderWidget()
    fireEvent.contextMenu(screen.getByRole('figure', { name: '3D 助手' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '对话' }))
    expect(screen.getByRole('dialog', { name: '与小小对话' })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('跟小小说点什么…')).toBeInTheDocument()
  })

  it('流式增量写入冒泡框', async () => {
    streamAssistantChat.mockImplementation(async ({ onDelta }) => {
      onDelta('小小')
      onDelta('来啦')
    })
    renderWidget()
    fireEvent.contextMenu(screen.getByRole('figure', { name: '3D 助手' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '对话' }))
    fireEvent.change(screen.getByPlaceholderText('跟小小说点什么…'), { target: { value: '你好' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('小小来啦', { selector: '.kb-assistant__bubble-text' })).toBeInTheDocument()
  })
})
