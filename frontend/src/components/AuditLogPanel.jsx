/**
 * 审计流水（tasklist 14.7 的「审计流水」页签，读 13.2 的 `/audit-logs`）。
 *
 * 这一页的用途只有一个：**排查单轮问答**——用户说「刚才那个问题答错了」，运营要能拿出
 * 那一行的 trace_id、命中路径、放行单元、token 与耗时。所以默认按时间倒序、每行可以展开看全量字段。
 *
 * 三条约定：
 *
 * 1. **不选日期就是近 7 天**，由服务端解释（`resolve_day_range`）：前端自己算「今天」会跟业务
 *    时区差一天，跨零点时两个人看到的不是同一批数据。
 * 2. **不按用户下拉筛选**：审计只存 `user_id`，而按姓名查人要 `org:user`——为了一个筛选框把
 *    组织接口暴露给看板读者不划算。列表里用短 id + Tooltip 全量 id，配合 trace_id 足够定位。
 * 3. **不做前端缓存**：排查场景要的是「此刻的真相」，点了筛选/翻页就重新拉。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { listAuditLogs } from '../api/operations'
import { formatDateTime, shortId } from '../utils/format'
const { Text } = Typography

const STATUS_OPTIONS = [
  { value: 'answered', label: '已回答' },
  { value: 'denied', label: '无权限' },
  { value: 'gap', label: '知识缺口' },
  { value: 'interrupted', label: '中断' },
]

/** 状态标签文案与颜色，表格与筛选共用一份，不会两处对不上。 */
const STATUS_LABELS = Object.fromEntries(STATUS_OPTIONS.map((item) => [item.value, item.label]))
const STATUS_COLORS = {
  answered: 'green',
  denied: 'red',
  gap: 'orange',
  interrupted: 'default',
}

const FAQ_OPTIONS = [
  { value: 'true', label: '已命中' },
  { value: 'false', label: '未命中' },
]

export default function AuditLogPanel() {
  const [range, setRange] = useState(null)
  const [status, setStatus] = useState(undefined)
  const [faqHit, setFaqHit] = useState(undefined)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [state, setState] = useState({ rows: [], total: 0, loading: true, error: null })

  const query = useMemo(() => {
    const params = { page, page_size: pageSize }
    if (range?.[0] && range?.[1]) {
      params.start = range[0].format('YYYY-MM-DD')
      params.end = range[1].format('YYYY-MM-DD')
    }
    if (status) {
      params.answer_status = status
    }
    if (faqHit) {
      params.faq_hit = faqHit === 'true'
    }
    return params
  }, [faqHit, page, pageSize, range, status])

  const fetchLogs = useCallback(() => listAuditLogs(query), [query])

  const applyResult = useCallback((data) => {
    setState({ rows: data?.items ?? [], total: data?.total ?? 0, loading: false, error: null })
  }, [])

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      applyResult(await fetchLogs())
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '审计加载失败' }))
    }
  }, [applyResult, fetchLogs])

  useEffect(() => {
    // 首屏只发请求、在回调里落地：effect 中同步 setState 会多一次级联渲染
    let cancelled = false
    fetchLogs()
      .then((data) => {
        if (!cancelled) applyResult(data)
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '审计加载失败' }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [applyResult, fetchLogs])

  const columns = [
    { title: '时间', dataIndex: 'created_at', width: 130, render: formatDateTime },
    {
      title: '提问人',
      dataIndex: 'user_id',
      width: 110,
      render: (value) =>
        value ? (
          <Tooltip title={value}>
            <Text code>{shortId(value)}</Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '问题',
      dataIndex: 'question',
      ellipsis: true,
      render: (value, row) => (
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          <Text ellipsis={{ tooltip: value }} style={{ maxWidth: '100%' }}>
            {value}
          </Text>
          {row.rewritten && row.rewritten !== value && (
            <Text type="secondary" ellipsis={{ tooltip: row.rewritten }} style={{ fontSize: 'calc(12px * var(--kb-scale))' }}>
              改写：{row.rewritten}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: 'FAQ',
      dataIndex: 'faq_hit',
      width: 82,
      render: (value) =>
        value ? <Tag color="blue">命中</Tag> : <Text type="secondary">—</Text>,
    },
    {
      title: '状态',
      dataIndex: 'answer_status',
      width: 96,
      render: (value) => (
        <Tag color={STATUS_COLORS[value] ?? 'default'}>{STATUS_LABELS[value] ?? value}</Tag>
      ),
    },
    {
      title: '放行 / 拒绝',
      key: 'access',
      width: 110,
      render: (_, row) => (
        <Text>
          {row.allowed_unit_ids?.length ?? 0} / {row.denied_count}
        </Text>
      ),
    },
    {
      title: '引用',
      dataIndex: 'citation_ids',
      width: 70,
      render: (rows) => rows?.length ?? 0,
    },
    {
      title: '最高相似度',
      dataIndex: 'max_similarity',
      width: 100,
      render: (value) => (typeof value === 'number' ? value.toFixed(3) : '—'),
    },
    {
      title: 'Token',
      key: 'tokens',
      width: 100,
      render: (_, row) => (
        <Text type="secondary">
          {row.prompt_tokens} + {row.completion_tokens}
        </Text>
      ),
    },
    {
      title: '耗时',
      dataIndex: 'latency_ms',
      width: 90,
      render: (value) => `${value} ms`,
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <DatePicker.RangePicker
          value={range}
          allowEmpty={[true, true]}
          aria-label="审计日期范围"
          placeholder={['开始时间', '结束时间']}
          onChange={(value) => {
            setRange(value)
            setPage(1)
          }}
        />
        <Select
          allowClear
          aria-label="回答状态"
          value={status}
          options={STATUS_OPTIONS}
          placeholder="全部状态"
          style={{ width: 140 }}
          onChange={(value) => {
            setStatus(value)
            setPage(1)
          }}
        />
        <Select
          allowClear
          aria-label="FAQ 命中"
          value={faqHit}
          options={FAQ_OPTIONS}
          placeholder="FAQ 全部"
          style={{ width: 130 }}
          onChange={(value) => {
            setFaqHit(value)
            setPage(1)
          }}
        />
        <Button icon={<ReloadOutlined />} onClick={load} loading={state.loading}>
          刷新
        </Button>
      </Space>

      {state.error && <Alert type="error" showIcon message={state.error} />}

      <Table
        rowKey="audit_id"
        size="small"
        loading={state.loading}
        columns={columns}
        dataSource={state.rows}
        scroll={{ x: 1160 }}
        expandable={{ expandedRowRender: (row) => <AuditDetail row={row} /> }}
        pagination={{
          current: page,
          pageSize,
          total: state.total,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 条`,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="这段时间没有问答记录"
            />
          ),
        }}
      />
    </Space>
  )
}

/** 展开行：单轮问答的完整现场（排查时不用再去查库）。 */
function AuditDetail({ row }) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Text>
        trace_id：<Text code>{row.trace_id}</Text>
      </Text>
      <Text>问题：{row.question}</Text>
      <Text>改写后：{row.rewritten || '—'}</Text>
      <Text>放行单元：{row.allowed_unit_ids?.length ? row.allowed_unit_ids.join('、') : '无'}</Text>
      <Text>引用切片：{row.citation_ids?.length ? row.citation_ids.join('、') : '无'}</Text>
      <Text type="secondary">
        Token 输入 {row.prompt_tokens} / 输出 {row.completion_tokens}，耗时 {row.latency_ms} ms
      </Text>
    </Space>
  )
}
