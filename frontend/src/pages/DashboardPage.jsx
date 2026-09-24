/**
 * 运营看板（tasklist 14.7，PRD §6.6）。
 *
 * 每个数字都来自审计表（`qa_audit_logs`），所以这里不拼日期区间：只切 `today` / `7d` 两档，
 * 切天由服务端按业务时区做（前端自己算「今天」会跟后端差一个时区）。
 *
 * 页面层的约定：
 *
 * 1. **四个接口一起等**（`Promise.all`）再渲染：各画各的会出现指标卡与图表的先后跳变。
 * 2. **比率是 0~1 的小数**，展示成百分比是前端的事（后端不猜你要几位小数）。
 * 3. **热门知识的标题可能是 null**：单元被删了但历史引用还在榜上，这时显示单元编号——
 *    空标题会让运营以为数据坏了。
 * 4. **没有数据就画空态**，不画一条贴底的零线：零线和「这天没人问」看起来一样。
 *
 * 页面分成两个页签：「概览」是聚合指标与图表（切今天/近 7 天），「审计流水」是能逐条展开的
 * 明细（有自己的日期范围）。两者用不同的时间控件，因为看趋势与查单条是两种节奏。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Segmented,
  Space,
  Statistic,
  Tabs,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import {
  dashboardSummary,
  dashboardTokenTrend,
  dashboardTopKnowledge,
  dashboardTopQuestions,
} from '../api/operations'
import AuditLogPanel from '../components/AuditLogPanel'
import ChartCard from '../components/ChartCard'
import { shortId } from '../utils/format'

const { Text } = Typography

const RANGE_OPTIONS = [
  { value: 'today', label: '今天' },
  { value: '7d', label: '近 7 天' },
]

const TOP_LIMIT = 10

/** 0~1 → `66.7%`。分母为 0 时后端给 0，照实显示，不假装「暂无数据」。 */
function percent(value) {
  return `${((value ?? 0) * 100).toFixed(1)}%`
}

/** 一天一个点的折线不画数据点标记：7 个点还行，30 天就糊成一片了。 */
const SMOOTH_LINE = { type: 'line', smooth: true, symbol: 'none', lineStyle: { width: 2 } }

function truncate(text, size = 18) {
  return text.length > size ? `${text.slice(0, size)}…` : text
}

/**
 * 横向柱统一用浅色渐变（左浅 → 右深，深色落在柱尖那一端）。
 * 实心柱在浅色看板上太重，渐变能把“这根有多长”的落点标出来。
 */
function barGradient(from, to) {
  return {
    type: 'linear',
    x: 0,
    y: 0,
    x2: 1,
    y2: 0,
    colorStops: [
      { offset: 0, color: from },
      { offset: 1, color: to },
    ],
  }
}

/**
 * 柱色按横轴数值分档：最大的一档红，然后橙 → 黄 → 绿 → 青 → 蓝 → 紫。
 * 用「去重后的数值」排名而不是行号，正是为了让数值相同的一批柱子拿到同一个颜色；
 * 档次超过七档就都落回紫色（彩虹就那么长）。
 */
const VALUE_RAMP = [
  ['#ffd6d6', '#e8534f'],
  ['#ffe1cc', '#ef8b3c'],
  ['#ffefc2', '#e8bf3a'],
  ['#d6f2dd', '#4fb977'],
  ['#cdf0ee', '#3fb2ab'],
  ['#d3e3fd', '#5a8ce8'],
  ['#e6d9fb', '#8b6fd6'],
]

/** 传入一批数值，返回「数值 → 渐变」的取色函数（两幅柱状图共用）。 */
function rampByValue(values) {
  const ranked = [...new Set(values)].sort((a, b) => b - a)
  const position = new Map(ranked.map((value, index) => [value, index]))
  return (value) => {
    const stops = VALUE_RAMP[Math.min(position.get(value) ?? 0, VALUE_RAMP.length - 1)]
    return barGradient(stops[0], stops[1])
  }
}

export default function DashboardPage() {
  const items = [
    { key: 'overview', label: '概览', children: <OverviewTab /> },
    { key: 'audit', label: '审计流水', children: <AuditLogPanel /> },
  ]

  return (
    <Card title="运营看板" styles={{ body: { paddingTop: 8 } }}>
      <Tabs items={items} />
    </Card>
  )
}

/** 概览页签：聚合指标 + 四个图。指标与图表一起等、一起画，避免先后跳变。 */
function OverviewTab() {
  const [range, setRange] = useState('7d')
  const [state, setState] = useState({
    loading: true,
    error: null,
    summary: null,
    questions: [],
    knowledge: [],
    trend: null,
  })

  /** 四个接口一起取：各自渲染会让指标卡与图表出现先后跳变。 */
  const fetchAll = useCallback(
    () =>
      Promise.all([
        dashboardSummary(range),
        dashboardTopQuestions(range, TOP_LIMIT),
        dashboardTopKnowledge(range, TOP_LIMIT),
        dashboardTokenTrend(range),
      ]),
    [range],
  )

  const applyResult = useCallback(([summary, questions, knowledge, trend]) => {
    setState({ loading: false, error: null, summary, questions, knowledge, trend })
  }, [])

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      applyResult(await fetchAll())
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '看板加载失败' }))
    }
  }, [applyResult, fetchAll])

  useEffect(() => {
    // 首屏只发请求、在回调里落地：effect 中同步 setState 会多一次级联渲染
    let cancelled = false
    fetchAll()
      .then((data) => {
        if (!cancelled) applyResult(data)
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '看板加载失败' }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [applyResult, fetchAll])

  const trend = state.trend
  const points = useMemo(() => trend?.points ?? [], [trend])

  const questionOption = useMemo(() => {
    // 横向柱状图的 y 轴自下而上，反转一下才是「最高频的在最上面」
    const rows = [...state.questions].reverse()
    // 颜色跟着横轴数值走，与「热门知识」同一套规则
    const colorOf = rampByValue(rows.map((row) => row.hits))
    return {
      grid: { left: 8, right: 24, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: {
        type: 'category',
        data: rows.map((row) => truncate(row.question)),
        axisLabel: { width: 140, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((row) => ({
            value: row.hits,
            itemStyle: { color: colorOf(row.hits) },
          })),
          barMaxWidth: 16,
          itemStyle: { borderRadius: [0, 3, 3, 0] },
        },
      ],
    }
  }, [state.questions])

  const knowledgeOption = useMemo(() => {
    const rows = [...state.knowledge].reverse()
    // 与「高频问题」同一套取色规则
    const colorOf = rampByValue(rows.map((row) => row.hits))
    return {
      grid: { left: 8, right: 24, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: {
        type: 'category',
        data: rows.map((row) => truncate(row.title || `已删除单元 ${shortId(row.unit_id)}`)),
        axisLabel: { width: 140, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((row) => ({
            value: row.hits,
            itemStyle: { color: colorOf(row.hits) },
          })),
          barMaxWidth: 16,
          itemStyle: { borderRadius: [0, 3, 3, 0] },
        },
      ],
    }
  }, [state.knowledge])

  const tokenOption = useMemo(
    () => ({
      grid: { left: 8, right: 8, top: 40, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis' },
      legend: { data: ['输入 Token', '输出 Token', '提问数'], top: 0 },
      xAxis: { type: 'category', data: points.map((point) => point.day) },
      yAxis: [
        { type: 'value', name: 'Token' },
        { type: 'value', name: '提问数', minInterval: 1, splitLine: { show: false } },
      ],
      series: [
        { ...SMOOTH_LINE, name: '输入 Token', data: points.map((point) => point.prompt_tokens) },
        { ...SMOOTH_LINE, name: '输出 Token', data: points.map((point) => point.completion_tokens) },
        {
          ...SMOOTH_LINE,
          name: '提问数',
          yAxisIndex: 1,
          itemStyle: { color: '#faad14' },
          data: points.map((point) => point.pv),
        },
      ],
    }),
    [points],
  )

  const latencyOption = useMemo(
    () => ({
      grid: { left: 8, right: 8, top: 40, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value) => `${Math.round(value)} ms`,
      },
      legend: { data: ['平均', 'P50', 'P90'], top: 0 },
      xAxis: { type: 'category', data: points.map((point) => point.day) },
      yAxis: { type: 'value', name: 'ms' },
      series: [
        { ...SMOOTH_LINE, name: '平均', data: points.map((point) => point.avg_latency_ms) },
        {
          ...SMOOTH_LINE,
          name: 'P50',
          itemStyle: { color: '#52c41a' },
          data: points.map((point) => point.p50_latency_ms),
        },
        {
          ...SMOOTH_LINE,
          name: 'P90',
          itemStyle: { color: '#ff4d4f' },
          data: points.map((point) => point.p90_latency_ms),
        },
      ],
    }),
    [points],
  )

  const summary = state.summary
  const emptyTrend = !state.loading && points.length === 0

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        size="small"
        extra={
          <Space>
            <Segmented
              options={RANGE_OPTIONS}
              value={range}
              onChange={setRange}
              aria-label="时间范围"
            />
            <Button icon={<ReloadOutlined />} onClick={load} loading={state.loading}>
              刷新
            </Button>
          </Space>
        }
      >
        {state.error ? (
          <Alert
            type="error"
            showIcon
            message={state.error}
            action={
              <Button size="small" onClick={load}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <Row gutter={[16, 16]} className="kb-kpi-grid">
              <Col xs={12} md={6}>
                <Statistic title="提问量（PV）" value={summary?.pv ?? 0} loading={state.loading} />
              </Col>
              <Col xs={12} md={6}>
                <Statistic title="提问用户（UV）" value={summary?.uv ?? 0} loading={state.loading} />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="知识单元"
                  value={summary?.knowledge_count ?? 0}
                  loading={state.loading}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="总 Token"
                  value={summary?.total_tokens ?? 0}
                  loading={state.loading}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="FAQ 命中率"
                  value={percent(summary?.faq_hit_rate)}
                  loading={state.loading}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="知识覆盖率"
                  value={percent(summary?.kb_coverage_rate)}
                  loading={state.loading}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="平均响应"
                  suffix="ms"
                  value={Math.round(summary?.avg_latency_ms ?? 0)}
                  loading={state.loading}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="P90 响应"
                  suffix="ms"
                  value={Math.round(summary?.p90_latency_ms ?? 0)}
                  loading={state.loading}
                />
              </Col>
            </Row>
            {summary && (
              <Text type="secondary">
                P50 {Math.round(summary.p50_latency_ms)} ms ／ 统计窗口{' '}
                {summary.range === 'today' ? '今天' : '近 7 天'}
              </Text>
            )}
          </>
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <ChartCard
            title="高频问题"
            option={questionOption}
            loading={state.loading}
            error={state.error}
            empty={!state.loading && state.questions.length === 0}
          />
        </Col>
        <Col xs={24} xl={12}>
          <ChartCard
            title="热门知识"
            option={knowledgeOption}
            loading={state.loading}
            error={state.error}
            empty={!state.loading && state.knowledge.length === 0}
          />
        </Col>
        <Col xs={24} xl={12}>
          <ChartCard
            title="Token 趋势"
            option={tokenOption}
            loading={state.loading}
            error={state.error}
            empty={emptyTrend}
          />
        </Col>
        <Col xs={24} xl={12}>
          <ChartCard
            title="响应时长趋势"
            option={latencyOption}
            loading={state.loading}
            error={state.error}
            empty={emptyTrend}
          />
        </Col>
      </Row>
    </Space>
  )
}
