/** 占位页：路由与菜单已接好，页面内容等对应任务落地。 */

import { Card, Empty, Typography } from 'antd'

export default function ModulePlaceholder({ title, planned }) {
  return (
    <Card>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {title}
      </Typography.Title>
      <Empty description={`${title}页面将在 tasklist ${planned} 实现`} />
    </Card>
  )
}
