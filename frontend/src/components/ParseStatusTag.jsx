/**
 * 解析状态标签（PRD §6.3 的状态列与导入抽屉的进度表）。
 *
 * 「进度」只在非终态时有意义：已完成/失败再挂一个百分比会让人以为还没跑完，
 * 所以终态只显示文字；失败的原始原因（可能是服务端报错）放 Tooltip，不铺在主行里。
 */

import { Tag, Tooltip } from 'antd'
import { STAGE_COLORS, STAGE_LABELS } from '../config/knowledge'

export default function ParseStatusTag({ row, showProgress = true }) {
  const stage = row.parse_status
  const label = STAGE_LABELS[stage] ?? stage
  const percent = showProgress && stage !== 'indexed' && stage !== 'failed' ? row.progress : null
  const tag = (
    <Tag color={STAGE_COLORS[stage] ?? 'default'} style={{ marginInlineEnd: 0 }}>
      {percent ? `${label} ${percent}%` : label}
    </Tag>
  )

  if (!row.parse_error) {
    return tag
  }
  return <Tooltip title={row.parse_error}>{tag}</Tooltip>
}
