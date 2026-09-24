/**
 * 编辑抽屉（tasklist 14.5，PRD §6.3「编辑抽屉：标题、分类、启用、解析文本只读预览」）。
 *
 * 两处取舍：
 *
 * 1. **启用开关立即生效**，不等「保存」。后端把启用/停用做成了独立接口（要同步 Milvus
 *    副本），混进 `PUT /knowledge-units/{id}` 会让人以为「改了标题但没点保存」也能改状态。
 * 2. **展示的是解析状态而不是「解析文本」**。切片正文在切片抽屉里看，这里放的是这篇
 *    文档解析到哪一步、失败了为什么——它才是编辑标题时真正需要的信息。
 */

import { useCallback, useEffect, useState } from 'react'
import { Alert, App, Button, Descriptions, Drawer, Form, Input, Space, Switch, Typography } from 'antd'
import { getUnit, setUnitEnabled, updateUnit } from '../api/knowledge'
import ParseStatusTag from './ParseStatusTag'
import { formatDateTime, formatFileSize, shortId } from '../utils/format'
import { FORMAT_LABELS } from '../config/knowledge'

const { Text } = Typography

export default function UnitEditDrawer({ unitId, open, onClose, onSaved }) {
  const { message } = App.useApp()
  const [detail, setDetail] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [loadedUnitId, setLoadedUnitId] = useState(unitId)
  const [form] = Form.useForm()

  // 换单元时先把上一条的详情清掉（渲染期调整 state，effect 里清会先闪一帧旧标题）
  if (unitId !== loadedUnitId) {
    setLoadedUnitId(unitId)
    setDetail(null)
    setError(null)
  }

  const load = useCallback(async () => {
    if (!unitId) {
      return
    }
    try {
      const data = await getUnit(unitId)
      setDetail(data)
      setError(null)
      form.setFieldsValue({ title: data.title, category: data.category ?? '' })
    } catch (loadError) {
      setError(loadError.message || '知识单元读取失败')
    }
  }, [form, unitId])

  useEffect(() => {
    if (!open || !unitId) {
      return undefined
    }
    let cancelled = false
    getUnit(unitId)
      .then((data) => {
        if (!cancelled) {
          setDetail(data)
          setError(null)
          form.setFieldsValue({ title: data.title, category: data.category ?? '' })
        }
      })
      .catch((loadError) => {
        if (!cancelled) {
          setError(loadError.message || '知识单元读取失败')
        }
      })
    return () => {
      cancelled = true
    }
    // 不直接调 `load`：effect 里调一个含 setState 的函数会多渲染一轮（oxlint 也会报），
    // 所以在这里把结果落在 then 里；`load` 留给 409 之后的事件路径
  }, [form, open, unitId])

  const onSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await updateUnit(unitId, {
        title: values.title,
        // 空分类要显式传 null：`UnitUpdate` 只处理出现过的字段，不传就是「保持原值」
        category: values.category?.trim() ? values.category.trim() : null,
        version: detail.version,
      })
      message.success('已保存')
      onSaved?.()
      onClose?.()
    } catch (saveError) {
      if (saveError?.httpStatus === 409) {
        message.error('这篇文档刚被别人改过，已为你刷新最新内容，请重新编辑')
        await load()
      } else if (saveError?.message) {
        message.error(saveError.message)
      }
    } finally {
      setSaving(false)
    }
  }

  const onToggleEnabled = async (checked) => {
    try {
      await setUnitEnabled(unitId, checked)
      setDetail((prev) => (prev ? { ...prev, status: checked ? 'enabled' : 'disabled' } : prev))
      message.success(checked ? '已启用，检索立即可见' : '已停用，不再参与检索')
      onSaved?.()
    } catch (toggleError) {
      message.error(toggleError.message || '状态切换失败')
    }
  }

  return (
    <Drawer
      title="编辑知识单元"
      width={620}
      open={open}
      onClose={onClose}
      footer={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={saving} disabled={!detail} onClick={onSave}>
            保存
          </Button>
        </Space>
      }
    >
      {error && <Alert type="error" showIcon message={error} />}
      {detail && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Form form={form} layout="vertical">
            <Form.Item
              name="title"
              label="标题"
              rules={[{ required: true, message: '请输入标题' }]}
            >
              <Input maxLength={255} placeholder="检索结果与引用卡片上显示的名字" />
            </Form.Item>
            <Form.Item name="category" label="分类">
              <Input maxLength={64} placeholder="选填，例如 财务制度" />
            </Form.Item>
          </Form>

          <Space>
            <Text>启用</Text>
            <Switch
              checked={detail.status === 'enabled'}
              onChange={onToggleEnabled}
              aria-label="启用或停用这篇文档"
            />
            <Text type="secondary">切换立即生效（停用后不再参与检索）</Text>
          </Space>

          <Descriptions title="解析信息" size="small" column={1} bordered>
            <Descriptions.Item label="编号">
              <Text copyable={{ text: detail.unit_id }}>{shortId(detail.unit_id)}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="原件">{detail.source_filename}</Descriptions.Item>
            <Descriptions.Item label="格式 / 大小">
              {FORMAT_LABELS[detail.format] ?? detail.format} · {formatFileSize(detail.file_size)}
            </Descriptions.Item>
            <Descriptions.Item label="解析状态">
              <ParseStatusTag row={detail} />
            </Descriptions.Item>
            <Descriptions.Item label="创建 / 更新">
              {formatDateTime(detail.created_at)} / {formatDateTime(detail.updated_at)}
            </Descriptions.Item>
            <Descriptions.Item label="版本">v{detail.version}</Descriptions.Item>
          </Descriptions>
        </Space>
      )}
    </Drawer>
  )
}
