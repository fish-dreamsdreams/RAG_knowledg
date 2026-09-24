/**
 * 组织架构面板（独立模块 `/org`，供给 PRD §6.7）。
 *
 * 部门 / 用户 / 角色三个子页签，各自一套权限码（`org:dept` / `org:user` / `org:role`）：
 * 只持其中一个的人进来也只看到那一块，**而且看不到的部分一个请求都不发**——否则页面上会
 * 平白多出一排 403。
 *
 * 两处容易踩的地方：
 *
 * 1. **建用户时部门是必填**，编用户时部门可以不动（`UserUpdate` 是 exclude_unset 语义）。
 *    所以编辑表单里部门留空 = 不改，而不是「清空部门」。
 * 2. **角色代码建完不能改**（`RoleUpdate` 里没有 `code`）：代码被权限判定与前端落点逻辑引用，
 *    改它等于换一个角色。编辑时把它显示成只读。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  createDepartment,
  createRole,
  createUser,
  deleteDepartment,
  deleteRole,
  deleteUser,
  listDepartments,
  listPermissions,
  listRoles,
  listUsers,
  updateDepartment,
  updateRole,
  updateUser,
} from '../api/org'
import { useAuth } from '../auth/context'
import { PASSWORD_MIN_LENGTH } from '../config/account'

const { Text } = Typography

/** 扁平化部门树：下拉选项要「客服部 / 售后组」这样的路径，父级选择也要能列出整棵树。 */
function flattenDepartments(nodes, parentPath = '') {
  return (nodes ?? []).flatMap((node) => {
    const path = parentPath ? `${parentPath} / ${node.name}` : node.name
    return [
      { id: node.id, name: node.name, path, parent_id: node.parent_id, is_active: node.is_active },
      ...flattenDepartments(node.children, path),
    ]
  })
}

export default function OrganizationPanel() {
  const { hasAny } = useAuth()

  const canManageDept = hasAny(['org:dept'])
  const canManageUser = hasAny(['org:user'])
  const canManageRole = hasAny(['org:role'])

  const items = [
    canManageDept && {
      key: 'departments',
      label: '部门',
      children: <DepartmentsTab />,
    },
    canManageUser && {
      key: 'users',
      label: '用户',
      children: <UsersTab />,
    },
    canManageRole && {
      key: 'roles',
      label: '角色',
      children: <RolesTab />,
    },
  ].filter(Boolean)

  if (items.length === 0) {
    return <Empty description="当前账号没有组织架构权限" />
  }

  return <Tabs items={items} />
}

function DepartmentsTab() {
  const { message } = App.useApp()
  const [state, setState] = useState({ rows: [], loading: true, error: null })
  const [editing, setEditing] = useState(null)
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      setState({ rows: flattenDepartments(await listDepartments()), loading: false, error: null })
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
    }
  }, [])

  useEffect(() => {
    // 首屏只发请求、在回调里落地：effect 中同步 setState 会多一次级联渲染
    let cancelled = false
    listDepartments()
      .then((tree) => {
        if (!cancelled) {
          setState({ rows: flattenDepartments(tree), loading: false, error: null })
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const remove = async (row) => {
    try {
      await deleteDepartment(row.id)
      message.success('部门已删除')
      load()
    } catch (error) {
      message.error(error.message || '删除失败')
    }
  }

  const columns = [
    { title: '部门', dataIndex: 'path', render: (value) => <Text strong>{value}</Text> },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: (value) => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: '操作',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            onClick={() => {
              setEditing(row)
              setOpen(true)
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除这个部门？"
            description="部门下还有用户时会失败，请先把人员调到别的部门。"
            onConfirm={() => remove(row)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null)
            setOpen(true)
          }}
        >
          新建部门
        </Button>
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      </Space>
      {state.error && <Alert type="error" showIcon message={state.error} />}
      <Table
        rowKey="id"
        size="small"
        loading={state.loading}
        columns={columns}
        dataSource={state.rows}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有部门" /> }}
      />
      <DepartmentModal
        key={editing?.id ?? 'new'}
        open={open}
        target={editing}
        options={state.rows}
        onClose={() => setOpen(false)}
        onDone={() => {
          setOpen(false)
          load()
        }}
      />
    </Space>
  )
}

function DepartmentModal({ open, target, options, onClose, onDone }) {
  const { message } = App.useApp()
  const formRef = useRef(null)
  const [saving, setSaving] = useState(false)

  const submit = async (values) => {
    setSaving(true)
    try {
      if (target) {
        await updateDepartment(target.id, values)
      } else {
        await createDepartment(values)
      }
      message.success(target ? '已保存' : '已创建')
      onDone()
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title={target ? '编辑部门' : '新建部门'}
      okText="保存"
      confirmLoading={saving}
      onOk={() => formRef.current?.submit()}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form
        ref={formRef}
        layout="vertical"
        requiredMark={false}
        onFinish={submit}
        initialValues={{
          name: target?.name ?? '',
          parent_id: target?.parent_id ?? undefined,
          is_active: target?.is_active ?? true,
        }}
      >
        <Form.Item name="name" label="部门名称" rules={[{ required: true, message: '名称不能为空' }]}>
          <Input maxLength={100} />
        </Form.Item>
        <Form.Item name="parent_id" label="上级部门" extra="留空表示顶级部门。">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="顶级部门"
            options={options
              .filter((item) => item.id !== target?.id)
              .map((item) => ({ value: item.id, label: item.path }))}
          />
        </Form.Item>
        <Form.Item name="is_active" label="启用" valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function UsersTab() {
  const { message } = App.useApp()
  const [departments, setDepartments] = useState([])
  const [roles, setRoles] = useState([])
  const [draft, setDraft] = useState('')
  const [keyword, setKeyword] = useState('')
  const [departmentId, setDepartmentId] = useState(undefined)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [state, setState] = useState({ rows: [], total: 0, loading: true, error: null })
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)

  const fetchUsers = useCallback(
    () =>
      listUsers({
        page,
        page_size: pageSize,
        keyword: keyword.trim() || undefined,
        department_id: departmentId || undefined,
      }),
    [departmentId, keyword, page, pageSize],
  )

  const applyUsers = useCallback((data) => {
    setState({ rows: data?.items ?? [], total: data?.total ?? 0, loading: false, error: null })
  }, [])

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      applyUsers(await fetchUsers())
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
    }
  }, [applyUsers, fetchUsers])

  useEffect(() => {
    let cancelled = false
    fetchUsers()
      .then((data) => {
        if (!cancelled) applyUsers(data)
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [applyUsers, fetchUsers])

  // 下拉选项只取一次：人员列表翻页不该重复拉部门和角色
  useEffect(() => {
    let cancelled = false
    Promise.all([listDepartments(), listRoles()])
      .then(([deptTree, roleRows]) => {
        if (!cancelled) {
          setDepartments(flattenDepartments(deptTree))
          setRoles(roleRows ?? [])
        }
      })
      .catch((error) => {
        if (!cancelled) {
          message.error(error.message || '部门或角色选项加载失败，新建用户前请先刷新')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const remove = async (row) => {
    try {
      await deleteUser(row.id)
      message.success('用户已删除')
      load()
    } catch (error) {
      message.error(error.message || '删除失败')
    }
  }

  const columns = [
    { title: '账号', dataIndex: 'username', width: 140 },
    { title: '姓名', dataIndex: 'display_name', width: 140 },
    {
      title: '部门',
      dataIndex: 'department_name',
      width: 160,
      render: (value) => value || <Text type="secondary">未归属</Text>,
    },
    {
      title: '角色',
      dataIndex: 'roles',
      render: (rows) =>
        rows?.length ? (
          <Space size={4} wrap>
            {rows.map((role) => (
              <Tag key={role.id}>{role.name}</Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">未分配</Text>
        ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 90,
      render: (value) => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: '操作',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            onClick={() => {
              setEditing(row)
              setOpen(true)
            }}
          >
            编辑
          </Button>
          <Popconfirm title="删除这个用户？" description="历史审计与问答记录仍会保留。" onConfirm={() => remove(row)}>
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Input
          aria-label="按账号或姓名搜索"
          value={draft}
          placeholder="账号或姓名"
          style={{ width: 200 }}
          onChange={(event) => setDraft(event.target.value)}
          onPressEnter={() => {
            setKeyword(draft)
            setPage(1)
          }}
        />
        <Select
          aria-label="按部门筛选"
          allowClear
          showSearch
          optionFilterProp="label"
          value={departmentId}
          placeholder="全部部门"
          style={{ width: 200 }}
          options={departments.map((item) => ({ value: item.id, label: item.path }))}
          onChange={(value) => {
            setDepartmentId(value)
            setPage(1)
          }}
        />
        <Button
          onClick={() => {
            setKeyword(draft)
            setPage(1)
          }}
        >
          查询
        </Button>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null)
            setOpen(true)
          }}
        >
          新建用户
        </Button>
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      </Space>
      {state.error && <Alert type="error" showIcon message={state.error} />}
      <Table
        rowKey="id"
        size="small"
        loading={state.loading}
        columns={columns}
        dataSource={state.rows}
        pagination={{
          current: page,
          pageSize,
          total: state.total,
          showSizeChanger: true,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
        locale={{ emptyText: <Empty description="没有符合条件的用户" /> }}
      />
      <UserModal
        key={editing?.id ?? 'new'}
        open={open}
        target={editing}
        departments={departments}
        roles={roles}
        onClose={() => setOpen(false)}
        onDone={() => {
          setOpen(false)
          load()
        }}
      />
    </Space>
  )
}

function UserModal({ open, target, departments, roles, onClose, onDone }) {
  const { message } = App.useApp()
  const formRef = useRef(null)
  const [saving, setSaving] = useState(false)

  const submit = async (values) => {
    setSaving(true)
    try {
      if (target) {
        const payload = {
          display_name: values.display_name,
          role_ids: values.role_ids ?? [],
          is_active: values.is_active,
        }
        // 部门留空 = 不改（exclude_unset 语义），不能顺手传 null
        if (values.department_id) {
          payload.department_id = values.department_id
        }
        if (values.password) {
          payload.password = values.password
        }
        await updateUser(target.id, payload)
      } else {
        await createUser({
          username: values.username,
          display_name: values.display_name,
          password: values.password,
          department_id: values.department_id,
          role_ids: values.role_ids ?? [],
          is_active: values.is_active,
        })
      }
      message.success(target ? '已保存' : '已创建')
      onDone()
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title={target ? '编辑用户' : '新建用户'}
      okText="保存"
      confirmLoading={saving}
      onOk={() => formRef.current?.submit()}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form
        ref={formRef}
        layout="vertical"
        requiredMark={false}
        onFinish={submit}
        initialValues={{
          username: target?.username ?? '',
          display_name: target?.display_name ?? '',
          password: '',
          department_id: target?.department_id ?? undefined,
          role_ids: target?.role_ids ?? [],
          is_active: target?.is_active ?? true,
        }}
      >
        <Form.Item
          name="username"
          label="账号"
          rules={target ? [] : [{ required: true, message: '账号不能为空' }]}
          extra={target ? '账号创建后不可修改。' : '字母、数字、下划线、点或连字符。'}
        >
          <Input maxLength={64} disabled={Boolean(target)} />
        </Form.Item>
        <Form.Item
          name="display_name"
          label="姓名"
          rules={[{ required: true, message: '姓名不能为空' }]}
        >
          <Input maxLength={64} />
        </Form.Item>
        <Form.Item
          name="password"
          label={target ? '重置密码' : '初始密码'}
          rules={
            target
              ? [
                  {
                    validator: (_, value) =>
                      !value || value.length >= PASSWORD_MIN_LENGTH
                        ? Promise.resolve()
                        : Promise.reject(new Error(`至少 ${PASSWORD_MIN_LENGTH} 位`)),
                  },
                ]
              : [
                  { required: true, message: '初始密码不能为空' },
                  { min: PASSWORD_MIN_LENGTH, message: `至少 ${PASSWORD_MIN_LENGTH} 位` },
                ]
          }
          extra={target ? '留空表示不改密码。' : null}
        >
          <Input.Password maxLength={128} autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="department_id"
          label="部门"
          rules={target ? [] : [{ required: true, message: '请选择部门' }]}
          extra={target ? '留空表示不改动所属部门。' : null}
        >
          <Select
            allowClear={Boolean(target)}
            showSearch
            optionFilterProp="label"
            placeholder="选择部门"
            options={departments.map((item) => ({ value: item.id, label: item.path }))}
          />
        </Form.Item>
        <Form.Item name="role_ids" label="角色">
          <Select
            mode="multiple"
            allowClear
            placeholder="可多选"
            options={roles.map((role) => ({ value: role.id, label: `${role.name}（${role.code}）` }))}
          />
        </Form.Item>
        <Form.Item name="is_active" label="启用" valuePropName="checked" extra="停用后无法登录。">
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function RolesTab() {
  const { message } = App.useApp()
  const [state, setState] = useState({ rows: [], loading: true, error: null })
  const [permissions, setPermissions] = useState([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)

  const applyRoles = useCallback((rows) => {
    setState({ rows: rows ?? [], loading: false, error: null })
  }, [])

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      applyRoles(await listRoles())
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
    }
  }, [applyRoles])

  useEffect(() => {
    let cancelled = false
    listRoles()
      .then((rows) => {
        if (!cancelled) applyRoles(rows)
      })
      .catch((error) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: error.message || '加载失败' }))
        }
      })
    listPermissions()
      .then((rows) => {
        if (!cancelled) setPermissions(rows ?? [])
      })
      .catch((error) => {
        if (!cancelled) {
          message.error(error.message || '权限码加载失败，新建角色前请先刷新')
        }
      })
    return () => {
      cancelled = true
    }
  }, [applyRoles])

  /** 权限码按 `type` 分组：一屏几百个码不分组的勾选框没法用。 */
  const groups = useMemo(() => {
    const byType = new Map()
    for (const permission of permissions) {
      const key = permission.type || '其他'
      if (!byType.has(key)) {
        byType.set(key, [])
      }
      byType.get(key).push(permission)
    }
    return [...byType.entries()].map(([type, rows]) => ({
      type,
      rows: [...rows].sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0)),
    }))
  }, [permissions])

  const remove = async (row) => {
    try {
      await deleteRole(row.id)
      message.success('角色已删除')
      load()
    } catch (error) {
      message.error(error.message || '删除失败')
    }
  }

  const columns = [
    { title: '角色代码', dataIndex: 'code', width: 150, render: (value) => <Text code>{value}</Text> },
    { title: '名称', dataIndex: 'name', width: 150 },
    {
      title: '说明',
      dataIndex: 'description',
      render: (value) => value || <Text type="secondary">—</Text>,
    },
    {
      title: '权限数',
      dataIndex: 'permission_ids',
      width: 100,
      render: (rows) => rows?.length ?? 0,
    },
    {
      title: '操作',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            onClick={() => {
              setEditing(row)
              setOpen(true)
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除这个角色？"
            description="还在使用该角色的用户会失去对应权限。"
            onConfirm={() => remove(row)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null)
            setOpen(true)
          }}
        >
          新建角色
        </Button>
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
        <Tooltip title="角色权限改动即时生效：用户不需要重新登录，前端每次都用服务端下发的权限码渲染。">
          <Text type="secondary">权限改动即时生效</Text>
        </Tooltip>
      </Space>
      {state.error && <Alert type="error" showIcon message={state.error} />}
      <Table
        rowKey="id"
        size="small"
        loading={state.loading}
        columns={columns}
        dataSource={state.rows}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有角色" /> }}
      />
      <RoleModal
        key={editing?.id ?? 'new'}
        open={open}
        target={editing}
        groups={groups}
        onClose={() => setOpen(false)}
        onDone={() => {
          setOpen(false)
          load()
        }}
      />
    </Space>
  )
}

function RoleModal({ open, target, groups, onClose, onDone }) {
  const { message } = App.useApp()
  const formRef = useRef(null)
  const [saving, setSaving] = useState(false)

  const submit = async (values) => {
    setSaving(true)
    try {
      if (target) {
        await updateRole(target.id, {
          name: values.name,
          description: values.description || null,
          permission_ids: values.permission_ids ?? [],
        })
      } else {
        await createRole({
          code: values.code,
          name: values.name,
          description: values.description || null,
          permission_ids: values.permission_ids ?? [],
        })
      }
      message.success(target ? '已保存' : '已创建')
      onDone()
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title={target ? '编辑角色' : '新建角色'}
      okText="保存"
      width={720}
      confirmLoading={saving}
      onOk={() => formRef.current?.submit()}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form
        ref={formRef}
        layout="vertical"
        requiredMark={false}
        onFinish={submit}
        initialValues={{
          code: target?.code ?? '',
          name: target?.name ?? '',
          description: target?.description ?? '',
          permission_ids: target?.permission_ids ?? [],
        }}
      >
        <Form.Item
          name="code"
          label="角色代码"
          rules={target ? [] : [{ required: true, message: '代码不能为空' }]}
          extra={target ? '代码被权限判定与登录落点引用，创建后不可修改。' : '例如 kb_admin，创建后不可修改。'}
        >
          <Input maxLength={64} disabled={Boolean(target)} />
        </Form.Item>
        <Form.Item name="name" label="名称" rules={[{ required: true, message: '名称不能为空' }]}>
          <Input maxLength={64} />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input maxLength={255} />
        </Form.Item>
        <Form.Item
          name="permission_ids"
          label="权限码"
          extra={groups.length === 0 ? '权限码字典为空，请刷新后再试。' : undefined}
        >
          <Checkbox.Group style={{ width: '100%' }}>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {groups.map((group) => (
                <Card key={group.type} size="small" title={group.type}>
                  <Space wrap size={[12, 8]}>
                    {group.rows.map((permission) => (
                      <Checkbox key={permission.id} value={permission.id}>
                        {permission.name}
                        <Text type="secondary">（{permission.code}）</Text>
                      </Checkbox>
                    ))}
                  </Space>
                </Card>
              ))}
            </Space>
          </Checkbox.Group>
        </Form.Item>
      </Form>
    </Modal>
  )
}
