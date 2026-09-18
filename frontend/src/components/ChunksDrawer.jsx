/**
 * 切片抽屉：只展示子块（检索单位）。有 `kb:edit` 时可改子块正文；
 * 保存才会重嵌，取消或关抽屉不改库。
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Pagination,
  Space,
  Spin,
  Tooltip,
  Typography,
} from 'antd'
import { getChunk, listChunks, updateChunk } from '../api/knowledge'
import { useAuth } from '../auth/context'
import { isParsing } from '../config/knowledge'

const { Text } = Typography

const PAGE_SIZE = 20

function chunkLabel(chunk) {
  if (chunk.parent_ordinal == null) {
    return `#${chunk.ordinal + 1}`
  }
  return `${chunk.parent_ordinal + 1}.${chunk.ordinal + 1}`
}

/** 新接口给 `chunks`；若后端仍是父子分组，把子块摊平，避免「子块 4」却显示空状态。 */
function normalizeChunkList(data) {
  if (!data) {
    return { chunks: [], childTotal: 0 }
  }
  const nested = (data.parents ?? []).flatMap((parent) =>
    (parent.children ?? []).map((child) => ({
      ...child,
      parent_ordinal: parent.ordinal,
    })),
  )
  const chunks = (data.chunks ?? []).length ? data.chunks : nested
  return {
    chunks,
    childTotal: data.child_total ?? chunks.length,
  }
}

export default function ChunksDrawer({ unit, open, onClose }) {
  const { message } = App.useApp()
  const { hasAny } = useAuth()
  const canEdit = hasAny(['kb:edit'])
  const parsing = Boolean(unit && isParsing(unit))

  const unitId = unit?.unit_id ?? null
  const [page, setPage] = useState(1)
  const [loadedUnitId, setLoadedUnitId] = useState(unitId)
  const [state, setState] = useState({
    chunks: [],
    childTotal: 0,
    loading: false,
    error: null,
  })
  const [editing, setEditing] = useState(null)
  const [loadingEdit, setLoadingEdit] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()

  if (unitId !== loadedUnitId) {
    setLoadedUnitId(unitId)
    setPage(1)
    setEditing(null)
    setState({ chunks: [], childTotal: 0, loading: true, error: null })
  }

  const fetchChunks = useCallback(
    () => (unitId ? listChunks(unitId, { page, page_size: PAGE_SIZE }) : Promise.resolve(null)),
    [page, unitId],
  )

  useEffect(() => {
    if (!open || !unitId) {
      return undefined
    }
    let cancelled = false
    fetchChunks()
      .then((data) => {
        if (!cancelled && data) {
          const next = normalizeChunkList(data)
          setState({
            chunks: next.chunks,
            childTotal: next.childTotal,
            loading: false,
            error: null,
          })
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '切片读取失败' }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [fetchChunks, open, unitId])

  const openEditor = async (chunk) => {
    if (!unitId || parsing) {
      return
    }
    setLoadingEdit(true)
    try {
      const detail = await getChunk(unitId, chunk.chunk_id)
      form.setFieldsValue({ content: detail.content })
      setEditing({ ...detail, parent_ordinal: chunk.parent_ordinal })
    } catch (error) {
      message.error(error.message || '读取切片失败')
    } finally {
      setLoadingEdit(false)
    }
  }

  const closeEditor = () => {
    setEditing(null)
    form.resetFields()
  }

  const onSave = async ({ content }) => {
    if (!unitId || !editing) {
      return
    }
    setSaving(true)
    try {
      await updateChunk(unitId, editing.chunk_id, { content })
      message.success('切片已更新，检索将使用新正文')
      closeEditor()
      const data = await fetchChunks()
      if (data) {
        const next = normalizeChunkList(data)
        setState({
          chunks: next.chunks,
          childTotal: next.childTotal,
          loading: false,
          error: null,
        })
      }
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Drawer
      title={unit ? `切片：${unit.title}` : '切片'}
      width={720}
      open={open}
      onClose={onClose}
      extra={
        <Space size={8}>
          {state.loading && <Spin size="small" />}
          <Text type="secondary">子块 {state.childTotal}</Text>
        </Space>
      }
    >
      {canEdit && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="只编辑子块。保存后会重新向量化；取消不改库。重新解析文档会覆盖手工修改。"
        />
      )}
      {state.error && <Alert type="error" showIcon message={state.error} style={{ marginBottom: 12 }} />}
      {state.chunks.length === 0 ? (
        state.loading ? (
          <Space style={{ width: '100%', justifyContent: 'center', padding: 24 }}>
            <Spin />
          </Space>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有切片：文档可能还在解析，或者解析失败了"
          />
        )
      ) : (
        state.chunks.map((chunk) => (
          <div key={chunk.chunk_id} style={{ marginBottom: 16 }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Text strong>
                子块 {chunkLabel(chunk)} · {chunk.char_count} 字
              </Text>
              {canEdit && (
                <Tooltip title={parsing ? '解析完成后才能改切片' : undefined}>
                  <Button
                    type="link"
                    size="small"
                    disabled={parsing || loadingEdit}
                    onClick={() => openEditor(chunk)}
                  >
                    编辑
                  </Button>
                </Tooltip>
              )}
            </Space>
            <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{chunk.excerpt}</div>
          </div>
        ))
      )}
      {state.childTotal > PAGE_SIZE && (
        <div style={{ marginTop: 12, textAlign: 'right' }}>
          <Pagination
            current={page}
            pageSize={PAGE_SIZE}
            total={state.childTotal}
            showSizeChanger={false}
            size="small"
            onChange={setPage}
          />
        </div>
      )}

      <Modal
        title={editing ? `编辑子块 ${chunkLabel(editing)}` : '编辑子块'}
        open={Boolean(editing)}
        onCancel={closeEditor}
        footer={null}
        destroyOnHidden
        width={640}
      >
        <Form form={form} layout="vertical" onFinish={onSave}>
          <Form.Item
            name="content"
            rules={[{ required: true, whitespace: true, message: '切片正文不能为空' }]}
          >
            <Input.TextArea autoSize={{ minRows: 10, maxRows: 24 }} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button onClick={closeEditor}>取消</Button>
              <Button type="primary" htmlType="submit" loading={saving}>
                保存
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </Drawer>
  )
}
