/**
 * 权限缺失提示条的两条底线（tasklist 14.8 / P3 / PRD §8.1）。
 *
 * 服务端只下发一个布尔量 `has_denied`，前端拿不到、也不该去猜被拦下的是哪份文档。用例故意
 * 把标题和单元 id 一起塞进消息对象（模拟「上游某天顺手多带了一个字段」），断言它们一个字都
 * 没被渲染——只断言「文案正确」是拦不住这类回归的。
 */

import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import ChatBubble from '../../src/components/ChatBubble'

const DENIED_TITLE = '薪酬管理制度'
const DENIED_UNIT = '0f2b1c44-6f1e-4a0b-9d3f-1c9d2a7e5b11'
const DENIED_ANSWER = '现行客服退款流程中没有关于该问题的规定，因此无法据此给出具体办法。'

/** 一次「拒答 + 权限缺失提示」的真实形态：无引用、只有布尔量提示。 */
function deniedMessage(extra = {}) {
  return {
    role: 'assistant',
    state: 'done',
    content: DENIED_ANSWER,
    citations: [],
    aclNotice: true,
    ...extra,
  }
}

function notice() {
  return screen.getByRole('alert')
}

describe('ChatBubble 的权限缺失提示条', () => {
  it('只按固定文案渲染，被拦文档的标题与 id 一个字都不出现', () => {
    render(
      <ChatBubble
        message={deniedMessage({
          // 这些字段本不该存在：真有人把它们接进渲染，这条用例就得红
          denied: [{ unit_id: DENIED_UNIT, title: DENIED_TITLE }],
          denied_titles: [DENIED_TITLE],
        })}
      />,
    )

    // 先查泄露：这两条一旦红了，失败信息直接指出漏了哪个标题/ id
    expect(document.body.textContent).not.toContain(DENIED_TITLE)
    expect(document.body.textContent).not.toContain(DENIED_UNIT)
    expect(screen.getByText('部分参考资料因权限受限无法展示')).toBeInTheDocument()
    expect(
      screen.getByText('检测到相关制度/资料，但您当前所属部门或角色无权查阅该内容。'),
    ).toBeInTheDocument()
  })

  it('提示条里没有可点元素（能点就等于多给一条探测路径）', () => {
    render(<ChatBubble message={deniedMessage()} />)

    expect(within(notice()).queryByRole('link')).not.toBeInTheDocument()
    expect(within(notice()).queryByRole('button')).not.toBeInTheDocument()
  })

  it('没有 acl_notice 时不出现提示条', () => {
    render(<ChatBubble message={deniedMessage({ aclNotice: false })} />)

    expect(screen.queryByText('部分参考资料因权限受限无法展示')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('用户自己的提问不触发提示条（提示只属于助手回答）', () => {
    render(<ChatBubble message={deniedMessage({ role: 'user', content: '薪酬标准是多少？' })} />)

    expect(screen.queryByText('部分参考资料因权限受限无法展示')).not.toBeInTheDocument()
  })
})
