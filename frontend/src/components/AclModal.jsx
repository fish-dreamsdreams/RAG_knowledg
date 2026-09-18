/**
 * 四维权限弹窗（tasklist 14.5，PRD §6.3「权限弹窗（P0）」）。
 *
 * 判定式是「四维 OR」：全局 OR 部门命中 OR 角色交集 OR 人员在列。因此这里 **不** 做互斥，
 * 全局打开时下面三类仍然可配，只是变成补充条件——文案如实说明，别让用户以为勾了全局
 * 其余就作废了。
 *
 * 部门用 `treeCheckStrictly`：勾父部门**不**级联子部门（P2），部门树不参与权限继承，
 * 想放行薪酬组就得把薪酬组自己勾上。
 *
 * 人员用远程搜索：候选上限 200 条，本地看不出「还有谁」，所以搜索走后端关键词过滤，
 * 同时把初值里的人员名（`view.labels.users`）并进选项，否则已选的人只会显示成一串 UUID。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  App,
  Checkbox,
  Modal,
  Select,
  Space,
  Spin,
  TreeSelect,
  Typography,
} from 'antd'
import { getUnitAcl, listAclOptions, saveUnitAcl } from '../api/knowledge'

const { Text } = Typography

const USER_SEARCH_DEBOUNCE_MS = 300

/** 扁平部门列表 → TreeSelect 的 treeData；停用的部门仍列出，但不允许新勾选。 */
function buildDepartmentTree(departments) {
  const nodes = new Map(
    departments.map((department) => [
      department.id,
      {
        value: department.id,
        title: department.is_active ? department.name : `${department.name}（已停用）`,
        disabled: !department.is_active,
        children: [],
        parentId: department.parent_id,
      },
    ]),
  )
  const roots = []
  for (const node of nodes.values()) {
    const parent = node.parentId ? nodes.get(node.parentId) : null
    if (parent) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

function roleOptions(roles) {
  return roles.map((role) => ({ value: role.id, label: role.name }))
}

export default function AclModal({ unit, open, onClose, onSaved }) {
  const { message } = App.useApp()
  const unitId = unit?.unit_id ?? null
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [options, setOptions] = useState({ departments: [], roles: [], users: [] })
  const [searching, setSearching] = useState(false)
  const [view, setView] = useState(null)
  const [form, setForm] = useState({ aclGlobal: false, departments: [], roles: [], users: [] })
  // 已选人员的展示名：初值来自 `view.labels.users`，搜索过的候选会追加进来
  const [userLabels, setUserLabels] = useState(new Map())
  const [loadedUnitId, setLoadedUnitId] = useState(unitId)
  const searchTimer = useRef(null)

  // 换单元时先清掉上一条的勾选（渲染期调整 state，避免闪一帧别篇文档的权限）
  if (unitId !== loadedUnitId) {
    setLoadedUnitId(unitId)
    setView(null)
    setError(null)
    setLoading(true)
    setForm({ aclGlobal: false, departments: [], roles: [], users: [] })
    setUserLabels(new Map())
  }

  const applyAcl = useCallback((acl, all) => {
    setOptions(all ?? { departments: [], roles: [], users: [] })
    setView(acl)
    setForm({
      aclGlobal: acl.acl_global,
      departments: acl.departments ?? [],
      roles: acl.roles ?? [],
      users: acl.users ?? [],
    })
    setUserLabels(new Map((acl.labels?.users ?? []).map((item) => [item.id, item.name])))
  }, [])

  /** 事件路径（保存遇 409 后重载）。effect 不直接调它，见下面的注释。 */
  const load = useCallback(async () => {
    if (!unitId) {
      return
    }
    setLoading(true)
    setError(null)
    try {
      const [acl, all] = await Promise.all([getUnitAcl(unitId), listAclOptions()])
      applyAcl(acl, all)
    } catch (loadError) {
      setError(loadError.message || '权限读取失败')
    } finally {
      setLoading(false)
    }
  }, [applyAcl, unitId])

  useEffect(() => {
    if (!open || !unitId) {
      return undefined
    }
    let cancelled = false
    Promise.all([getUnitAcl(unitId), listAclOptions()])
      .then(([acl, all]) => {
        if (cancelled) {
          return
        }
        setOptions(all ?? { departments: [], roles: [], users: [] })
        setView(acl)
        setForm({
          aclGlobal: acl.acl_global,
          departments: acl.departments ?? [],
          roles: acl.roles ?? [],
          users: acl.users ?? [],
        })
        setUserLabels(new Map((acl.labels?.users ?? []).map((item) => [item.id, item.name])))
        setError(null)
      })
      .catch((loadError) => {
        if (!cancelled) {
          setError(loadError.message || '权限读取失败')
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })
    // 这里不调 `load`：effect 里调一个含同步 setState 的函数会多渲染一轮（oxlint 也会报）
    return () => {
      cancelled = true
    }
  }, [open, unitId])

  const treeData = useMemo(
    () => buildDepartmentTree(options.departments ?? []),
    [options.departments],
  )

  const mergedUserOptions = useMemo(() => {
    const known = new Map()
    for (const user of options.users ?? []) {
      known.set(user.id, {
        value: user.id,
        label: user.display_name,
        title: user.department_name
          ? `${user.display_name}（${user.username} · ${user.department_name}）`
          : `${user.display_name}（${user.username}）`,
      })
    }
    // 初值里可能有当前候选页之外的人
    for (const [id, name] of userLabels) {
      if (!known.has(id)) {
        known.set(id, { value: id, label: name, title: name })
      }
    }
    return [...known.values()]
  }, [options.users, userLabels])

  const onSearchUsers = useCallback((keyword) => {
    // 防抖用手动定时器：`onSearch` 不接收清理函数，返回 clearTimeout 也没人会调
    if (searchTimer.current) {
      clearTimeout(searchTimer.current)
    }
    const trimmed = keyword.trim()
    searchTimer.current = setTimeout(async () => {
      setSearching(true)
      try {
        const data = await listAclOptions(trimmed ? { keyword: trimmed } : {})
        setOptions((prev) => ({ ...prev, users: data?.users ?? [] }))
        setUserLabels((prev) => {
          const next = new Map(prev)
          for (const user of data?.users ?? []) {
            next.set(user.id, user.display_name)
          }
          return next
        })
      } catch {
        // 搜索失败不该弹错打断选择：保留手上已有的候选，用户再敲一次即可
      } finally {
        setSearching(false)
      }
    }, USER_SEARCH_DEBOUNCE_MS)
  }, [])

  useEffect(
    () => () => {
      if (searchTimer.current) {
        clearTimeout(searchTimer.current)
      }
    },
    [],
  )

  const preview = useMemo(() => {
    const label = (ids, texts) => (ids.length === 0 ? '—' : texts.join('、'))
    const departmentNames = (form.departments ?? [])
      .map((id) => options.departments?.find((item) => item.id === id)?.name ?? String(id).slice(0, 8))
      .filter(Boolean)
    const roleNames = (form.roles ?? [])
      .map((id) => options.roles?.find((item) => item.id === id)?.name ?? String(id).slice(0, 8))
      .filter(Boolean)
    const userNames = (form.users ?? []).map((id) => userLabels.get(id) ?? String(id).slice(0, 8))
    const parts = [
      `全局${form.aclGlobal ? '开启' : '关闭'}`,
      `部门=${label(form.departments, departmentNames)}`,
      `角色=${label(form.roles, roleNames)}`,
      `人员=${label(form.users, userNames)}`,
    ]
    if (!form.aclGlobal && form.departments.length + form.roles.length + form.users.length === 0) {
      parts.push('（四维全空 = 谁都不能读）')
    }
    return parts.join('；')
  }, [form, options.departments, options.roles, userLabels])

  const onSave = async () => {
    setSaving(true)
    try {
      await saveUnitAcl(unitId, {
        acl_global: form.aclGlobal,
        departments: form.departments,
        roles: form.roles,
        users: form.users,
        version: view.version,
      })
      message.success('权限已生效')
      onSaved?.()
      onClose?.()
    } catch (saveError) {
      if (saveError?.httpStatus === 409) {
        message.error('权限刚被其他人改过，已重新载入最新配置')
        await load()
      } else {
        message.error(saveError.message || '保存失败')
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={unit ? `配置数据权限：${unit.title}` : '配置数据权限'}
      open={open}
      onCancel={onClose}
      onOk={onSave}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      okButtonProps={{ disabled: loading || Boolean(error) }}
      width={640}
    >
      {error && <Alert type="error" showIcon message={error} />}
      {loading ? (
        <Space style={{ width: '100%', justifyContent: 'center', padding: 24 }}>
          <Spin />
        </Space>
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Checkbox
            checked={form.aclGlobal}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, aclGlobal: event.target.checked }))
            }
          >
            全局公开（打开后全员可读）
          </Checkbox>
          {form.aclGlobal && (
            <Text type="secondary">已全局公开，下列配置仅作补充（权限判定是四维 OR）。</Text>
          )}

          <div>
            <Text>部门（勾父级不会放行子部门成员）</Text>
            <TreeSelect
              treeData={treeData}
              value={form.departments}
              onChange={(value) =>
                // `treeCheckStrictly` 下回传的是 {value,label,halfChecked} 对象，取 value
                setForm((prev) => ({
                  ...prev,
                  departments: (value ?? []).map((item) =>
                    typeof item === 'string' ? item : item.value,
                  ),
                }))
              }
              treeCheckable
              treeCheckStrictly
              showCheckedStrategy={TreeSelect.SHOW_ALL}
              treeDefaultExpandAll
              allowClear
              placeholder="不选择表示该维度不授权"
              style={{ width: '100%', marginTop: 4 }}
            />
          </div>

          <div>
            <Text>角色</Text>
            <Select
              mode="multiple"
              options={roleOptions(options.roles ?? [])}
              value={form.roles}
              onChange={(value) => setForm((prev) => ({ ...prev, roles: value }))}
              allowClear
              placeholder="不选择表示该维度不授权"
              style={{ width: '100%', marginTop: 4 }}
            />
          </div>

          <div>
            <Text>人员（按姓名或账号搜索）</Text>
            <Select
              mode="multiple"
              showSearch
              filterOption={false}
              onSearch={onSearchUsers}
              options={mergedUserOptions}
              value={form.users}
              onChange={(value) => setForm((prev) => ({ ...prev, users: value }))}
              notFoundContent={searching ? <Spin size="small" /> : null}
              allowClear
              placeholder="不选择表示该维度不授权"
              style={{ width: '100%', marginTop: 4 }}
            />
          </div>

          <Text type="secondary">当前规则预览：{preview}</Text>
        </Space>
      )}
    </Modal>
  )
}
