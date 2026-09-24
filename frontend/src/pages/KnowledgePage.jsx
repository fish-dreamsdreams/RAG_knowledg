/**
 * 知识维护与导入中心（tasklist 14.5，PRD §6.3）。
 *
 * 页面层的几条约定：
 *
 * 1. **权限标签来自列表行**（后端批量拼好的 `acl_*` 名称），不再按行反查 `/acl`——那样一页
 *    20 行就是 20 个接口，而且只有持 `kb:acl` 的人才调得动。
 * 2. **点「查询」才打接口**。筛选项改动不立刻请求：边输边查会把无意义请求铺满网络。
 * 3. **有非终态行才轮询**。导入是异步的，列表要在解析过程中自己走动；全部落终态就停，
 *    否则页面开着就是一个永不收工的定时器。
 * 4. **按钮按权限码逐个收起**，不是整页只读：持 `kb:view` 而没有 `kb:delete` 的人应该能看
 *    列表、看切片，但看不到删除入口。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined, UploadOutlined } from '@ant-design/icons'
import { deleteUnit, listUnits, setUnitEnabled } from '../api/knowledge'
import AclModal from '../components/AclModal'
import ChunksDrawer from '../components/ChunksDrawer'
import ImportDrawer from '../components/ImportDrawer'
import ParseStatusTag from '../components/ParseStatusTag'
import PermissionTags from '../components/PermissionTags'
import UnitEditDrawer from '../components/UnitEditDrawer'
import { useAuth } from '../auth/context'
import {
  FORMAT_LABELS,
  FORMAT_OPTIONS,
  LIST_POLL_MS,
  STATUS_OPTIONS,
  isParsing,
} from '../config/knowledge'
import { formatDateTime, formatFileSize, shortId } from '../utils/format'

const { Text } = Typography

const EMPTY_FILTERS = { keyword: '', category: undefined, format: undefined, status: undefined }

export default function KnowledgePage() {
  const { hasAny } = useAuth()
  const { message } = App.useApp()

  const [draft, setDraft] = useState(EMPTY_FILTERS)
  const [applied, setApplied] = useState(EMPTY_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [state, setState] = useState({ rows: [], total: 0, loading: true, error: null })

  const [importOpen, setImportOpen] = useState(false)
  const [importFiles, setImportFiles] = useState(null)
  const fileInputRef = useRef(null)
  const [editingUnitId, setEditingUnitId] = useState(null)
  const [aclUnit, setAclUnit] = useState(null)
  const [chunksUnit, setChunksUnit] = useState(null)

  const canImport = hasAny(['kb:import'])
  const canEdit = hasAny(['kb:edit'])
  const canDelete = hasAny(['kb:delete'])
  const canConfigAcl = hasAny(['kb:acl'])

  /** 纯请求，不改 state：effect 里同步 setState 会触发额外一轮渲染，lint 也不放过。 */
  const fetchPage = useCallback(
    () =>
      listUnits({
        page,
        page_size: pageSize,
        keyword: applied.keyword?.trim() || undefined,
        category: applied.category || undefined,
        format: applied.format || undefined,
        status: applied.status || undefined,
      }),
    [applied, page, pageSize],
  )

  const applyResult = (data) => ({
    rows: data?.items ?? [],
    total: data?.total ?? 0,
    loading: false,
    error: null,
  })

  /** 事件入口（刷新/查询/重试）：立刻进 loading，让人看得出点到了。 */
  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      setState(applyResult(await fetchPage()))
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '列表加载失败' }))
    }
  }, [fetchPage])

  useEffect(() => {
    let cancelled = false
    fetchPage()
      .then((data) => {
        if (!cancelled) {
          setState(applyResult(data))
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            loading: false,
            error: error.message || '列表加载失败',
          }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [fetchPage])

  const parsing = state.rows.some(isParsing)

  useEffect(() => {
    if (!parsing) {
      return undefined
    }
    const timer = setInterval(() => {
      load()
    }, LIST_POLL_MS)
    return () => clearInterval(timer)
  }, [load, parsing])

  /** 分类筛选的选项来自当前页已加载的数据：分类本身没有独立字典表。 */
  const categories = useMemo(() => {
    const names = new Set(state.rows.map((row) => row.category).filter(Boolean))
    return [...names].map((name) => ({ value: name, label: name }))
  }, [state.rows])

  const onToggleEnabled = useCallback(
    async (row, checked) => {
      // 乐观更新：开关是立即生效的，等接口回来再动会让人以为没点上
      setState((prev) => ({
        ...prev,
        rows: prev.rows.map((item) =>
          item.unit_id === row.unit_id
            ? { ...item, status: checked ? 'enabled' : 'disabled' }
            : item,
        ),
      }))
      try {
        await setUnitEnabled(row.unit_id, checked)
        message.success(checked ? '已启用' : '已停用，不再参与检索')
      } catch (error) {
        message.error(error.message || '状态切换失败')
        load()
      }
    },
    [load, message],
  )

  const onDelete = useCallback(
    async (row) => {
      try {
        await deleteUnit(row.unit_id)
        message.success('已删除')
        // 删掉当前页最后一条时往前退一页，否则会停在空页上
        if (state.rows.length === 1 && page > 1) {
          setPage((value) => value - 1)
        } else {
          load()
        }
      } catch (error) {
        message.error(error.message || '删除失败')
      }
    },
    [load, message, page, state.rows.length],
  )

  const columns = [
    {
      title: '编号',
      dataIndex: 'unit_id',
      width: 110,
      render: (unitId) => (
        <Tooltip title={unitId}>
          <Text code>{shortId(unitId)}</Text>
        </Tooltip>
      ),
    },
    {
      title: '标题',
      dataIndex: 'title',
      ellipsis: true,
      render: (title, row) => (
        // 两行都显式 ellipsis：长标题在窄列里被硬截断会看不出被截了（`title` 属性留全名）
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          <Text strong ellipsis={{ tooltip: title }} style={{ maxWidth: '100%' }}>
            {title}
          </Text>
          <Text type="secondary" ellipsis style={{ fontSize: 'calc(12px * var(--kb-scale))', maxWidth: '100%' }}>
            {formatFileSize(row.file_size)}
          </Text>
        </Space>
      ),
    },
    {
      title: '格式',
      dataIndex: 'format',
      width: 78,
      render: (format) => <Tag>{FORMAT_LABELS[format] ?? format}</Tag>,
    },
    {
      title: '分类',
      dataIndex: 'category',
      width: 110,
      render: (category) => category || <Text type="secondary">—</Text>,
    },
    {
      title: '权限标签',
      key: 'acl',
      width: 220,
      render: (_, row) => <PermissionTags row={row} />,
    },
    {
      title: '解析',
      key: 'parse',
      width: 125,
      render: (_, row) => <ParseStatusTag row={row} />,
    },
    { title: '更新时间', dataIndex: 'updated_at', width: 135, render: formatDateTime },
    {
      title: '状态',
      key: 'status',
      width: 105,
      render: (_, row) =>
        canEdit ? (
          <Popconfirm
            title={row.status === 'enabled' ? '停用这篇文档？' : '启用这篇文档？'}
            description={
              row.status === 'enabled'
                ? '停用后不再参与检索，已发布的 FAQ 不受影响。'
                : '启用后立即参与检索（前提是已配好权限）。'
            }
            okText="确定"
            cancelText="取消"
            onConfirm={() => onToggleEnabled(row, row.status !== 'enabled')}
          >
            <Button size="small">{row.status === 'enabled' ? '启用' : '停用'}</Button>
          </Popconfirm>
        ) : (
          <Tag color={row.status === 'enabled' ? 'green' : 'default'}>
            {row.status === 'enabled' ? '启用' : '停用'}
          </Tag>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 235,
      /* 列宽合计超过卡片宽度，表格本来就要横向滚；不吸住的话
         「删除」永远被切在可视区外，得先滚到底才能点 */
      fixed: 'right',
      render: (_, row) => (
        <Space size={0}>
          {canEdit && (
            <Button type="link" size="small" onClick={() => setEditingUnitId(row.unit_id)}>
              编辑
            </Button>
          )}
          {canConfigAcl && (
            <Button type="link" size="small" onClick={() => setAclUnit(row)}>
              权限
            </Button>
          )}
          <Button type="link" size="small" onClick={() => setChunksUnit(row)}>
            切片
          </Button>
          {canDelete && (
            <Popconfirm
              title={`删除「${row.title}」？`}
              description="删除后不可恢复，向量与切片将一并清除。"
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => onDelete(row)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="知识维护与导入中心"
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>
            刷新
          </Button>
          {canImport && (
            <>
              <Button
                type="primary"
                icon={<UploadOutlined />}
                onClick={() => {
                  setImportFiles(null)
                  setImportOpen(true)
                }}
              >
                批量导入
              </Button>
              {/* 单文件快捷入口：选完直接进抽屉，与「批量导入」共用同一套进度与重试。
                  按钮必须用 ref 去点隐藏的 input：`<label>` 里包一个真实 `<button>` 时，
                  浏览器不会再把点击转给 label 关联的表单控件，文件选择框永远不会弹出来。 */}
              <input
                ref={fileInputRef}
                type="file"
                style={{ display: 'none' }}
                aria-label="上传文档"
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? [])
                  event.target.value = ''
                  if (files.length > 0) {
                    setImportFiles(files)
                    setImportOpen(true)
                  }
                }}
              />
              <Button onClick={() => fileInputRef.current?.click()}>上传文档</Button>
            </>
          )}
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space wrap>
          <Input
            allowClear
            style={{ width: 200 }}
            placeholder="标题或原文件名"
            aria-label="按标题或原文件名筛选"
            value={draft.keyword}
            onChange={(event) => setDraft((prev) => ({ ...prev, keyword: event.target.value }))}
            onPressEnter={() => {
              setPage(1)
              setApplied(draft)
            }}
          />
          <Select
            allowClear
            style={{ width: 160 }}
            placeholder="分类"
            aria-label="按分类筛选"
            options={categories}
            value={draft.category}
            onChange={(value) => setDraft((prev) => ({ ...prev, category: value }))}
          />
          <Select
            allowClear
            style={{ width: 130 }}
            placeholder="格式"
            aria-label="按格式筛选"
            options={FORMAT_OPTIONS}
            value={draft.format}
            onChange={(value) => setDraft((prev) => ({ ...prev, format: value }))}
          />
          <Select
            allowClear
            style={{ width: 130 }}
            placeholder="状态"
            aria-label="按启用状态筛选"
            options={STATUS_OPTIONS}
            value={draft.status}
            onChange={(value) => setDraft((prev) => ({ ...prev, status: value }))}
          />
          <Button
            type="primary"
            onClick={() => {
              setPage(1)
              setApplied(draft)
            }}
          >
            查询
          </Button>
          <Button
            onClick={() => {
              setDraft(EMPTY_FILTERS)
              setApplied(EMPTY_FILTERS)
              setPage(1)
            }}
          >
            重置
          </Button>
        </Space>

        {state.error && (
          <Alert
            type="error"
            showIcon
            message="知识列表加载失败"
            description={state.error}
            action={
              <Button size="small" onClick={load}>
                重试
              </Button>
            }
          />
        )}

        <Table
          rowKey="unit_id"
          size="small"
          loading={state.loading}
          columns={columns}
          dataSource={state.rows}
          scroll={{ x: 1300 }}
          pagination={{
            current: page,
            pageSize,
            total: state.total,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 篇`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage)
              setPageSize(nextSize)
            },
          }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="还没有知识，请先导入文档"
              />
            ),
          }}
        />
      </Space>

      <ImportDrawer
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={load}
        initialFiles={importFiles}
      />
      <UnitEditDrawer
        unitId={editingUnitId}
        open={Boolean(editingUnitId)}
        onClose={() => setEditingUnitId(null)}
        onSaved={load}
      />
      <ChunksDrawer
        unit={chunksUnit}
        open={Boolean(chunksUnit)}
        onClose={() => setChunksUnit(null)}
      />
      <AclModal unit={aclUnit} open={Boolean(aclUnit)} onClose={() => setAclUnit(null)} onSaved={load} />
    </Card>
  )
}
