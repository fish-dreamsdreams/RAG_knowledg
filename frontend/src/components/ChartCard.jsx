/**
 * ECharts 容器（tasklist 14.7）。
 *
 * 只注册真正用到的图表与组件（`echarts/core` + 按需引入），而不是 `import * as echarts from
 * 'echarts'`：后者会把整包地图、地理与所有图表类型打进产物，一个看板页面不值得让首屏多背
 * 几百 KB。
 *
 * 三件事必须自己管，否则图表会「看着在但不对」：
 *
 * 1. **尺寸变化要 `resize()`**：侧栏收起/窗口缩放都不会触发重绘，用 ResizeObserver 盯着容器。
 * 2. **option 变了要整体替换**（`setOption(option, true)`）：否则上一次的 series 会留在图上，
 *    切换时间范围后会出现两条折线。
 * 3. **卸载要 `dispose()`**：ECharts 实例挂在 DOM 上，不销毁就会随页面切换一直堆积。
 */

import { useEffect, useRef } from 'react'
import { Alert, Card, Empty, Skeleton } from 'antd'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

export default function ChartCard({
  title,
  option,
  height = 280,
  loading = false,
  error = null,
  empty = false,
  extra = null,
}) {
  const boxRef = useRef(null)
  const chartRef = useRef(null)

  useEffect(() => {
    if (loading || error || empty || !option || !boxRef.current) {
      return undefined
    }
    const chart = echarts.init(boxRef.current)
    chartRef.current = chart
    // 整体替换而不是合并：否则切时间范围后旧的 series 会留在图上
    chart.setOption(option, true)
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(boxRef.current)
    return () => {
      observer.disconnect()
      chart.dispose()
      chartRef.current = null
    }
    // option 变化时重建实例：重建比「合并 + 手动清理 series」更不容易出错，看板的 option
    // 又是用户点一下才变的，这点开销可以忽略
  }, [option, loading, error, empty])

  return (
    <Card title={title} extra={extra} size="small">
      {error ? (
        <Alert type="error" showIcon message={error} />
      ) : loading ? (
        <Skeleton active paragraph={{ rows: 5 }} title={false} />
      ) : empty ? (
        <Empty description="所选时间范围内没有数据" style={{ padding: '48px 0' }} />
      ) : (
        <div ref={boxRef} style={{ width: '100%', height }} />
      )}
    </Card>
  )
}
