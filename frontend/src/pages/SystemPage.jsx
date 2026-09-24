/**
 * 系统配置（tasklist 14.7，PRD §6.7）。
 *
 * 组织架构已拆到独立模块 `/org`。本页只保留模型配置（`sys:model`）。
 */

import { Card, Empty } from 'antd'
import ModelConfigPanel from '../components/ModelConfigPanel'
import { useAuth } from '../auth/context'

export default function SystemPage() {
  const { hasAny } = useAuth()

  return (
    <Card title="系统配置" styles={{ body: { paddingTop: 8 } }}>
      {hasAny(['sys:model']) ? (
        <ModelConfigPanel />
      ) : (
        <Empty description="当前账号没有系统配置权限" />
      )}
    </Card>
  )
}
