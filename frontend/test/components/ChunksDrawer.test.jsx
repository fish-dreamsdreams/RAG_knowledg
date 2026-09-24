/**
 * 切片抽屉：只展示子块；无编辑权不出现保存入口。
 */

import { render, screen } from '@testing-library/react'
import { App } from 'antd'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AuthContext } from '../../src/auth/context'
import ChunksDrawer from '../../src/components/ChunksDrawer'

vi.mock('../../src/api/knowledge', () => ({
  listChunks: vi.fn(),
  getChunk: vi.fn(),
  updateChunk: vi.fn(),
}))

import { listChunks } from '../../src/api/knowledge'

const UNIT = {
  unit_id: '11111111-1111-1111-1111-111111111111',
  title: '差旅制度',
  parse_status: 'indexed',
}

function renderDrawer({ canEdit = false } = {}) {
  listChunks.mockResolvedValue({
    unit_id: UNIT.unit_id,
    child_total: 2,
    chunks: [
      {
        chunk_id: '22222222-2222-2222-2222-222222222222',
        level: 'child',
        ordinal: 0,
        parent_ordinal: 0,
        char_count: 4,
        excerpt: '子块甲摘要',
      },
      {
        chunk_id: '33333333-3333-3333-3333-333333333333',
        level: 'child',
        ordinal: 1,
        parent_ordinal: 0,
        char_count: 4,
        excerpt: '子块乙摘要',
      },
    ],
  })

  return render(
    <App>
      <AuthContext.Provider value={{ identity: { display_name: '测' }, hasAny: () => canEdit }}>
        <ChunksDrawer unit={UNIT} open onClose={() => {}} />
      </AuthContext.Provider>
    </App>,
  )
}

describe('切片抽屉', () => {
  beforeEach(() => {
    listChunks.mockReset()
  })

  it('只展示子块摘要，不出现父块折叠标题', async () => {
    renderDrawer()

    expect(await screen.findByText('子块甲摘要')).toBeInTheDocument()
    expect(screen.getByText('子块乙摘要')).toBeInTheDocument()
    expect(screen.queryByText(/父块/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
  })

  it('有 kb:edit 时每个子块可以编辑', async () => {
    renderDrawer({ canEdit: true })

    expect(await screen.findAllByRole('button', { name: '编辑' })).toHaveLength(2)
  })

  it('旧的父子分组响应也能摊成子块列表，不会空状态', async () => {
    listChunks.mockResolvedValue({
      unit_id: UNIT.unit_id,
      child_total: 2,
      parents: [
        {
          chunk_id: '44444444-4444-4444-4444-444444444444',
          level: 'parent',
          ordinal: 0,
          char_count: 8,
          excerpt: '父块不应出现',
          children: [
            {
              chunk_id: '22222222-2222-2222-2222-222222222222',
              level: 'child',
              ordinal: 0,
              char_count: 4,
              excerpt: '摊平后的子块',
            },
          ],
        },
      ],
    })

    render(
      <App>
        <AuthContext.Provider value={{ identity: { display_name: '测' }, hasAny: () => false }}>
          <ChunksDrawer unit={UNIT} open onClose={() => {}} />
        </AuthContext.Provider>
      </App>,
    )

    expect(await screen.findByText('摊平后的子块')).toBeInTheDocument()
    expect(screen.queryByText('父块不应出现')).not.toBeInTheDocument()
    expect(screen.queryByText(/还没有切片/)).not.toBeInTheDocument()
  })
})
