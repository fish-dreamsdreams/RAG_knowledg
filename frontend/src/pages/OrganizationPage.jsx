/**
 * 组织架构（从系统配置拆出的独立模块）。
 *
 * 部门 / 用户 / 角色仍由 OrganizationPanel 按 `org:dept` / `org:user` / `org:role` 收起页签，
 * 本页只负责外壳：有任一组织权限即可进入。
 */

import { Card } from 'antd'
import OrganizationPanel from '../components/OrganizationPanel'

export default function OrganizationPage() {
  return (
    <Card title="组织架构" styles={{ body: { paddingTop: 8 } }}>
      <OrganizationPanel />
    </Card>
  )
}
