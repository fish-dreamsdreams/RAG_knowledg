/**
 * 沉淀运营（tasklist 14.6，PRD §6.5）。
 *
 * 三个页签对应运营闭环的三段：**候选 → 已发布 FAQ → 缺口**。它们在数据上首尾相接
 * （缺口的高频问题被挖掘成候选 → 审核发布 → 命中 FAQ 后不再进缺口），放一页里能一眼看出
 * 「还有多少没沉淀」。
 *
 * 页面层的约定：
 *
 * 1. **发布与驳回是两种权限**。驳回只要 `faq:review`（审核动作），发布/改答案/切缓存/下线要
 *    `faq:publish`；没有 publish 的人看得到候选、能驳回，但看不到「发布」。
 * 2. **驳回原因必填**，没填字之前提交按钮是禁用的——原因会被长期留存，事后补不回来。
 * 3. **改动后重新拉取，不做乐观更新**：FAQ 的缓存重建在服务端（Redis 里的向量与索引一起换），
 *    本地先改会让人以为改完了。
 * 4. **挖掘是异步的**：投递后列表自动轮询，直到出结果或超时收手（不留永不停止的定时器）。
 * 5. **缺口转建只标记**：点「转建」打开导入抽屉并带上 `from_gap_id`，索引完成才回填 `filled`
 *    （P16），所以这里显示的是「已转建」而不是「已补齐」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ExperimentOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  listCandidates,
  listFaqs,
  publishCandidate,
  rejectCandidate,
  triggerMining,
  updateFaq,
} from '../api/faq'
import { convertGap, listGaps } from '../api/operations'
import ImportDrawer from '../components/ImportDrawer'
import { useAuth } from '../auth/context'
import { LIST_POLL_MS } from '../config/knowledge'
import { formatDateTime } from '../utils/format'

const { Text, Paragraph } = Typography

const CANDIDATE_STATUS = [
  { value: 'pending', label: '待审核' },
  { value: 'published', label: '已发布' },
  { value: 'rejected', label: '已驳回' },
]

const FAQ_STATUS = [
  { value: 'published', label: '已上线' },
  { value: 'offline', label: '已下线' },
]

const GAP_STATUS = [
  { value: 'open', label: '待处理' },
  { value: 'converted', label: '已转建' },
  { value: 'filled', label: '已补齐' },
]

const GAP_STATUS_COLORS = { open: 'orange', converted: 'blue', filled: 'green' }
const CANDIDATE_STATUS_COLORS = { pending: 'orange', published: 'green', rejected: 'default' }

function labelOf(options, value) {
  return options.find((item) => item.value === value)?.label ?? value
}

/** 0~1 的相似度取两位小数；空值不显示成 0（「没算过」和「算出来是 0」不是一回事）。 */
function ratio(value) {
  return typeof value === 'number' ? value.toFixed(2) : '—'
}

/**
 * 分页列表：只负责页码、加载状态与「什么时候重新取」。
 *
 * `fetch` 由调用方用 `useCallback` 固定（筛选项变了它就变），这里只认它和翻页参数——
 * 每次渲染新建一个 fetch 会让下面的 effect 无限循环。
 */
function usePagedList(fetch) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [state, setState] = useState({ rows: [], total: 0, loading: true, error: null })

  const query = useCallback(() => fetch(page, pageSize), [fetch, page, pageSize])

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      const data = await query()
      setState({
        rows: data?.items ?? [],
        total: data?.total ?? 0,
        loading: false,
        error: null,
      })
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
    }
  }, [query])

  useEffect(() => {
    let cancelled = false
    query()
      .then((data) => {
        if (!cancelled) {
          setState({
            rows: data?.items ?? [],
            total: data?.total ?? 0,
            loading: false,
            error: null,
          })
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            loading: false,
            error: error.message || '加载失败',
          }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [query])

  return { ...state, page, pageSize, setPage, setPageSize, load }
}

export default function OperationsPage() {
  const { hasAny } = useAuth()
  const { message } = App.useApp()

  const canReview = hasAny(['faq:review'])
  const canPublish = hasAny(['faq:publish'])
  const canViewGap = hasAny(['gap:view'])
  const canConvertGap = hasAny(['gap:convert'])
  const canImport = hasAny(['kb:import'])
  const [candidateStatus, setCandidateStatus] = useState('pending')
  const [faqDraft, setFaqDraft] = useState('')
  const [faqKeyword, setFaqKeyword] = useState('')
  const [faqStatus, setFaqStatus] = useState(undefined)
  const [gapStatus, setGapStatus] = useState('open')

  const [mining, setMining] = useState(false)
  const [publishTarget, setPublishTarget] = useState(null)
  const [rejectTarget, setRejectTarget] = useState(null)
  const [editTarget, setEditTarget] = useState(null)
  const [importGap, setImportGap] = useState(null)
  const mineTimeoutRef = useRef(null)

  const fetchCandidates = useCallback(
    (page, pageSize) =>
      listCandidates({ page, page_size: pageSize, status: candidateStatus || undefined }),
    [candidateStatus],
  )
  const fetchFaqs = useCallback(
    (page, pageSize) =>
      listFaqs({
        page,
        page_size: pageSize,
        keyword: faqKeyword.trim() || undefined,
        status: faqStatus || undefined,
      }),
    [faqKeyword, faqStatus],
  )
  const fetchGaps = useCallback(
    (page, pageSize) => listGaps({ page, page_size: pageSize, status: gapStatus || undefined }),
    [gapStatus],
  )

  const candidates = usePagedList(fetchCandidates)
  const faqs = usePagedList(fetchFaqs)
  const gaps = usePagedList(fetchGaps)

  const candidatesLoad = candidates.load
  const gapsLoad = gaps.load
  const faqsLoad = faqs.load

  /** 挖掘期间轮询候选：产出只体现在候选列表里，没有别的进度可看。 */
  useEffect(() => {
    if (!mining) {
      return undefined
    }
    const timer = setInterval(candidatesLoad, LIST_POLL_MS)
    return () => {
      clearInterval(timer)
      if (mineTimeoutRef.current) {
        clearTimeout(mineTimeoutRef.current)
      }
    }
  }, [mining, candidatesLoad])

  const onMine = useCallback(async () => {
    try {
      await triggerMining()
      setMining(true)
      message.info('已投递挖掘任务，产出一批候选需要一会儿，列表会自动刷新')
      // 兜底收尾：挖掘一般一分钟内出结果，超时就停掉轮询，不留一个永不停止的定时器
      mineTimeoutRef.current = setTimeout(() => setMining(false), 5 * 60 * 1000)
    } catch (error) {
      message.error(error.message || '触发挖掘失败')
    }
  }, [message])

  const onToggleCache = useCallback(
    async (row, checked) => {
      try {
        await updateFaq(row.faq_id, { cache_enabled: checked })
        message.success(checked ? '已开启命中' : '已关闭命中（条目保留）')
        faqsLoad()
      } catch (error) {
        message.error(error.message || '切换失败')
      }
    },
    [faqsLoad, message],
  )

  const onToggleStatus = useCallback(
    async (row, next) => {
      try {
        await updateFaq(row.faq_id, { status: next })
        message.success(next === 'offline' ? '已下线' : '已上线')
        faqsLoad()
      } catch (error) {
        message.error(error.message || '操作失败')
      }
    },
    [faqsLoad, message],
  )

  const onConvert = useCallback(
    async (row) => {
      try {
        const prefill = await convertGap(row.gap_id)
        setImportGap(prefill)
        gapsLoad()
      } catch (error) {
        message.error(error.message || '转建失败')
      }
    },
    [gapsLoad, message],
  )

  const candidateColumns = useMemo(
    () => [
      { title: '问题', dataIndex: 'question', render: (value) => <Text strong>{value}</Text> },
      {
        title: '同义问法',
        dataIndex: 'similar_questions',
        width: 110,
        render: (rows) =>
          rows?.length ? (
            <Tooltip
              title={
                <div>
                  {rows.map((item) => (
                    <div key={item}>{item}</div>
                  ))}
                </div>
              }
            >
              <Text type="secondary">{rows.length} 条</Text>
            </Tooltip>
          ) : (
            '—'
          ),
      },
      { title: '频次', dataIndex: 'freq', width: 80, sorter: (a, b) => a.freq - b.freq },
      { title: '置信度', dataIndex: 'confidence', width: 90, render: ratio },
      {
        title: '建议答案',
        dataIndex: 'suggested_answer',
        render: (value) =>
          value ? (
            <Paragraph
              ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
              style={{ margin: 0 }}
            >
              {value}
            </Paragraph>
          ) : (
            <Text type="secondary">无（发布时填写）</Text>
          ),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (value) => (
          <Tag color={CANDIDATE_STATUS_COLORS[value]}>{labelOf(CANDIDATE_STATUS, value)}</Tag>
        ),
      },
      { title: '发现时间', dataIndex: 'created_at', width: 130, render: formatDateTime },
      {
        title: '操作',
        width: 160,
        render: (_, row) => (
          <Space size={4}>
            {canPublish && row.status === 'pending' && (
              <Button size="small" type="primary" onClick={() => setPublishTarget(row)}>
                发布
              </Button>
            )}
            {canReview && row.status === 'pending' && (
              <Button size="small" onClick={() => setRejectTarget(row)}>
                驳回
              </Button>
            )}
            {row.status === 'rejected' && row.reject_reason ? (
              <Tooltip title={row.reject_reason}>
                <Text type="secondary">已驳回</Text>
              </Tooltip>
            ) : null}
          </Space>
        ),
      },
    ],
    [canPublish, canReview],
  )

  const faqColumns = useMemo(
    () => [
      { title: '问题', dataIndex: 'question', render: (value) => <Text strong>{value}</Text> },
      {
        title: '答案',
        dataIndex: 'answer',
        render: (value) => (
          <Paragraph
            ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
            style={{ margin: 0 }}
          >
            {value}
          </Paragraph>
        ),
      },
      {
        title: '参与命中',
        dataIndex: 'cache_enabled',
        width: 100,
        render: (value, row) => (
          <Switch
            size="small"
            checked={value}
            disabled={!canPublish}
            aria-label="参与缓存命中"
            onChange={(checked) => onToggleCache(row, checked)}
          />
        ),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 90,
        render: (value) =>
          value === 'published' ? <Tag color="green">已上线</Tag> : <Tag>已下线</Tag>,
      },
      { title: '更新时间', dataIndex: 'updated_at', width: 130, render: formatDateTime },
      {
        title: '操作',
        width: 170,
        render: (_, row) => (
          <Space size={4}>
            {canPublish && (
              <Button size="small" onClick={() => setEditTarget(row)}>
                改答案
              </Button>
            )}
            {canPublish && (
              <Popconfirm
                title={row.status === 'published' ? '下线这条 FAQ？' : '重新上线这条 FAQ？'}
                description={
                  row.status === 'published'
                    ? '下线后不再参与命中，缓存同步更新。'
                    : '上线后立即参与命中，缓存同步重建。'
                }
                onConfirm={() =>
                  onToggleStatus(row, row.status === 'published' ? 'offline' : 'published')
                }
              >
                <Button size="small" danger={row.status === 'published'}>
                  {row.status === 'published' ? '下线' : '上线'}
                </Button>
              </Popconfirm>
            )}
          </Space>
        ),
      },
    ],
    [canPublish, onToggleCache, onToggleStatus],
  )

  const gapColumns = useMemo(
    () => [
      {
        title: '问题',
        dataIndex: 'question_text',
        render: (value) => <Text strong>{value}</Text>,
      },
      {
        title: '提问部门',
        dataIndex: 'department_name',
        width: 130,
        render: (value) => value || <Text type="secondary">未归属</Text>,
      },
      { title: '频次', dataIndex: 'freq', width: 80, sorter: (a, b) => a.freq - b.freq },
      { title: '最高相似度', dataIndex: 'max_similarity', width: 110, render: ratio },
      { title: '最近提问', dataIndex: 'last_asked_at', width: 130, render: formatDateTime },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (value) => (
          <Tag color={GAP_STATUS_COLORS[value]}>{labelOf(GAP_STATUS, value)}</Tag>
        ),
      },
      {
        title: '操作',
        width: 130,
        render: (_, row) =>
          canConvertGap && canImport && row.status === 'open' ? (
            <Popconfirm
              title="为缺口知识导入相关文档？"
              description="知识的海洋将因你不断壮大(づ￣3￣)づ╭❤～"
              onConfirm={() => onConvert(row)}
            >
              <Button size="small" type="primary">
                导入文档
              </Button>
            </Popconfirm>
          ) : (
            <Text type="secondary">{row.status === 'open' ? '—' : '已处理'}</Text>
          ),
      },
    ],
    [canConvertGap, canImport, onConvert],
  )

  const tabs = [
    canReview && {
      key: 'candidates',
      label: `FAQ 候选${candidates.total ? `（${candidates.total}）` : ''}`,
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Select
              aria-label="候选状态"
              value={candidateStatus}
              options={CANDIDATE_STATUS}
              style={{ width: 140 }}
              onChange={(value) => {
                setCandidateStatus(value)
                candidates.setPage(1)
              }}
            />
            <Button icon={<ReloadOutlined />} onClick={candidates.load}>
              刷新
            </Button>
            {canReview && (
              <Button
                type="primary"
                icon={<ExperimentOutlined />}
                loading={mining}
                onClick={onMine}
              >
                {mining ? '挖掘中…' : '触发挖掘'}
              </Button>
            )}            {mining && (
              <Text type="secondary">
                候选列表每 {Math.round(LIST_POLL_MS / 1000)} 秒自动刷新
              </Text>
            )}
          </Space>
          {candidates.error && <Alert type="error" showIcon message={candidates.error} />}
          <Table
            rowKey="candidate_id"
            size="small"
            loading={candidates.loading}
            columns={candidateColumns}
            dataSource={candidates.rows}
            pagination={paginationOf(candidates)}
            locale={{ emptyText: <Empty description="没有候选：先攒一些未命中的提问，再触发挖掘" /> }}
          />
        </Space>
      ),
    },
    {
      key: 'faqs',
      label: '已发布 FAQ',
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Input
              aria-label="按问题搜索"
              value={faqDraft}
              placeholder="按问题搜索"
              style={{ width: 220 }}
              onChange={(event) => setFaqDraft(event.target.value)}
              onPressEnter={() => {
                setFaqKeyword(faqDraft)
                faqs.setPage(1)
              }}
            />
            <Select
              aria-label="FAQ 状态"
              allowClear
              value={faqStatus}
              options={FAQ_STATUS}
              placeholder="全部状态"
              style={{ width: 140 }}
              onChange={(value) => {
                setFaqStatus(value)
                faqs.setPage(1)
              }}
            />
            <Button
              type="primary"
              onClick={() => {
                setFaqKeyword(faqDraft)
                faqs.setPage(1)
              }}
            >
              查询
            </Button>
            <Button icon={<ReloadOutlined />} onClick={faqs.load}>
              刷新
            </Button>
          </Space>
          {faqs.error && <Alert type="error" showIcon message={faqs.error} />}
          <Table
            rowKey="faq_id"
            size="small"
            loading={faqs.loading}
            columns={faqColumns}
            dataSource={faqs.rows}
            pagination={paginationOf(faqs)}
            locale={{ emptyText: <Empty description="还没有已发布的 FAQ" /> }}
          />
        </Space>
      ),
    },
    canViewGap && {
      // `key` 不能省：rc-tabs 会把缺 key 的 items 直接过滤掉，页签会静默消失（不是隐藏，是不渲染）
      key: 'gaps',
      label: `知识缺口${gaps.total ? `（${gaps.total}）` : ''}`,
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Select
              aria-label="缺口状态"
              value={gapStatus}
              options={GAP_STATUS}
              style={{ width: 140 }}
              onChange={(value) => {
                setGapStatus(value)
                gaps.setPage(1)
              }}
            />
            <Button icon={<ReloadOutlined />} onClick={gaps.load}>
              刷新
            </Button>
            <Text type="secondary">按频次降序：同样补一篇文档，高频缺口先补收益最大</Text>
          </Space>
          {gaps.error && <Alert type="error" showIcon message={gaps.error} />}
          <Table
            rowKey="gap_id"
            size="small"
            loading={gaps.loading}
            columns={gapColumns}
            dataSource={gaps.rows}
            pagination={paginationOf(gaps)}
            locale={{ emptyText: <Empty description="暂无缺口" /> }}
          />
        </Space>
      ),
    },
  ].filter(Boolean)

  return (
    <Card title="沉淀运营" styles={{ body: { paddingTop: 8 } }}>
      <Tabs items={tabs} />

      <PublishModal
        target={publishTarget}
        onClose={() => setPublishTarget(null)}
        onDone={() => {
          setPublishTarget(null)
          candidates.load()
          faqs.load()
        }}
      />
      <RejectModal
        key={rejectTarget?.candidate_id ?? 'none'}
        target={rejectTarget}
        onClose={() => setRejectTarget(null)}
        onDone={() => {
          setRejectTarget(null)
          candidates.load()
        }}
      />
      <EditFaqModal
        target={editTarget}
        onClose={() => setEditTarget(null)}
        onDone={() => {
          setEditTarget(null)
          faqs.load()
        }}
      />

      <ImportDrawer
        open={Boolean(importGap)}
        fromGapId={importGap?.gap_id}
        onClose={() => {
          setImportGap(null)
          gaps.load()
        }}
        onImported={gaps.load}
      />
      {importGap && (
        <Text type="secondary">
          正在补建「{importGap.suggested_title}」：上传文档即可，索引完成后缺口自动标记为已补齐
        </Text>
      )}
    </Card>
  )
}

/** 三个表格共用同一份分页配置。 */
function paginationOf(list) {
  return {
    current: list.page,
    pageSize: list.pageSize,
    total: list.total,
    showSizeChanger: true,
    onChange: (page, size) => {
      list.setPage(page)
      list.setPageSize(size)
    },
  }
}

/** 发布：答案必填，默认带上候选的建议答案，审核人改完再发。 */
function PublishModal({ target, onClose, onDone }) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (target) {
      form.setFieldsValue({ answer: target.suggested_answer ?? '' })
    }
  }, [form, target])

  const submit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await publishCandidate(target.candidate_id, values.answer)
      message.success('已发布，缓存同步重建')
      onDone()
    } catch (error) {
      message.error(error.message || '发布失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={Boolean(target)}
      title="发布为 FAQ"
      okText="发布"
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Text strong>{target?.question}</Text>
        {target?.similar_questions?.length ? (
          <Text type="secondary">同义问法：{target.similar_questions.join('、')}</Text>
        ) : null}
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="answer"
            label="标准答案"
            rules={[{ required: true, message: '答案不能为空' }]}
            extra="发布后命中这条 FAQ 的提问直接返回答案：不检索、不调用生成模型（P4）。"
          >
            <Input.TextArea
              rows={6}
              maxLength={4000}
              showCount
              placeholder="写成可以直接发给用户的答案"
            />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  )
}

/** 驳回：原因必填（服务端同样校验），原因会长期留存。
 *
 * 原因用组件自身的 state，靠父级传下来的 `key` 在换候选/关闭时重新挂载清零——比在
 * effect 里 `setReason('')` 直接（也避免「effect 里同步 setState」这类级联渲染）。
 */
function RejectModal({ target, onClose, onDone }) {
  const { message } = App.useApp()
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    setSaving(true)
    try {
      await rejectCandidate(target.candidate_id, reason.trim())
      message.success('已驳回')
      onDone()
    } catch (error) {
      message.error(error.message || '驳回失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={Boolean(target)}
      title="驳回候选"
      okText="驳回"
      okButtonProps={{ disabled: reason.trim().length === 0 }}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Text strong>{target?.question}</Text>
        <Input.TextArea
          rows={4}
          maxLength={500}
          showCount
          value={reason}
          placeholder="写清为什么不采纳（必填）：问法太泛、已有文档覆盖、答案需人工确认……"
          onChange={(event) => setReason(event.target.value)}
        />
      </Space>
    </Modal>
  )
}

/** 改答案 / 切缓存都在这一处（问句本身不允许改，见 schemas/faq.py 的说明）。 */
function EditFaqModal({ target, onClose, onDone }) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (target) {
      form.setFieldsValue({ answer: target.answer, cache_enabled: target.cache_enabled })
    }
  }, [form, target])

  const submit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await updateFaq(target.faq_id, {
        answer: values.answer,
        cache_enabled: values.cache_enabled,
      })
      message.success('已保存，缓存同步重建')
      onDone()
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={Boolean(target)}
      title="编辑 FAQ"
      okText="保存"
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Text strong>{target?.question}</Text>
        <Text type="secondary">问句不支持修改：它是缓存向量的编码源，要别的问法请再发一条。</Text>
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="answer"
            label="标准答案"
            rules={[{ required: true, message: '答案不能为空' }]}
          >
            <Input.TextArea rows={6} maxLength={4000} showCount />
          </Form.Item>
          <Form.Item
            name="cache_enabled"
            label="参与缓存命中"
            valuePropName="checked"
            extra="关掉后条目仍在，只是不再被匹配到（不需要重建缓存）。"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  )
}
