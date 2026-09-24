/**
 * 权限标签（PRD §6.3）：全局 / 部门名 / 角色名 / 人员名，超过 3 个折成 `+N`。
 *
 * 一个都没配时渲染红色「未授权」而不是留空：空 ACL 是**拒绝**，不是公开（P1），
 * 留空会让人以为这行还没轮到配权限。名称直接取列表行带的 `acl_*`，不再按行反查接口。
 */

import { Tag, Tooltip } from 'antd'
import { aclTagTexts } from '../config/knowledge'

const VISIBLE = 3

export default function PermissionTags({ row }) {
  const texts = aclTagTexts(row)
  if (texts.length === 0) {
    return <Tag color="red" style={{ marginInlineEnd: 0 }}>未授权</Tag>
  }

  const visible = texts.slice(0, VISIBLE)
  const rest = texts.length - visible.length

  return (
    <span>
      {visible.map((text) => (
        <Tag key={text} color={text === '全局' ? 'green' : 'blue'} style={{ marginInlineEnd: 4 }}>
          {text}
        </Tag>
      ))}
      {rest > 0 && (
        <Tooltip title={texts.join('、')}>
          {/* 用 span 而不是 Tag：Tooltip 需要一个能挂事件的宿主，Tag 在这里只是文字容器 */}
          <Tag style={{ marginInlineEnd: 0 }}>+{rest}</Tag>
        </Tooltip>
      )}
    </span>
  )
}
