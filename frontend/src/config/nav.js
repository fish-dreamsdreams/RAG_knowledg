/**
 * 控制台导航与路由访问规则（design.md §3.6 的路由表）。
 *
 * `requires` = 菜单码 ∪ 页面主按钮码：种子里 `type=menu` 的权限码（chat/knowledge/…）决定
 * 菜单可见性，design.md §3.6 登记的是页面主按钮码；两者任一命中即可进入，自定义角色只被
 * 授予按钮码时也不会挡在门外。`*`（system_admin）全部放行。
 */

import {
  ApartmentOutlined,
  BarChartOutlined,
  BookOutlined,
  CommentOutlined,
  DatabaseOutlined,
  SettingOutlined,
} from '@ant-design/icons'

export const NAV_ITEMS = [
  { key: '/chat', label: 'AI问答', icon: CommentOutlined, requires: ['chat', 'ai:chat'] },
  {
    key: '/knowledge',
    label: '知识维护',
    icon: DatabaseOutlined,
    requires: ['knowledge', 'kb:view'],
  },
  {
    key: '/operations',
    label: '沉淀运营',
    icon: BookOutlined,
    requires: ['operations', 'faq:review', 'gap:view'],
  },
  {
    key: '/dashboard',
    label: '运营看板',
    icon: BarChartOutlined,
    requires: ['dashboard', 'dash:view'],
  },
  {
    key: '/org',
    label: '组织架构',
    icon: ApartmentOutlined,
    requires: ['org:dept', 'org:user', 'org:role'],
  },
  {
    key: '/system',
    label: '系统配置',
    icon: SettingOutlined,
    requires: ['system', 'sys:model'],
  },
]

export const NAV_ITEM_BY_PATH = Object.fromEntries(NAV_ITEMS.map((item) => [item.key, item]))

/** 角色 → 登录后默认落点（PRD §4）；未登记的角色回退到第一个有权限的模块。 */
const LANDING_BY_ROLE = {
  system_admin: '/dashboard',
  kb_admin: '/knowledge',
  employee: '/chat',
}

/** 第一个可访问的模块路径；一个权限都没有时返回 null（由页面显示通用空态）。 */
export function firstAllowedPath(hasAny) {
  const item = NAV_ITEMS.find((candidate) => hasAny(candidate.requires))
  return item ? item.key : null
}

export function landingPath(identity, hasAny) {
  for (const code of identity?.role_codes ?? []) {
    const path = LANDING_BY_ROLE[code]
    const item = path ? NAV_ITEM_BY_PATH[path] : undefined
    if (item && hasAny(item.requires)) {
      return path
    }
  }
  return firstAllowedPath(hasAny)
}
