/**
 * 3D 助手设置：暴露 LLM 网关配置。
 * 有 sys:model 才能改；密钥语义与系统配置页一致（不传=不改，空串=清除）。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import { getModelConfig, updateModelConfig } from '../../api/system'
import { useAuth } from '../../auth/context'

const { Text } = Typography

export default function AssistantSettingsModal({ open, onClose }) {
  const { hasAny } = useAuth()
  const canEdit = hasAny(['sys:model'])
  const [form] = Form.useForm()
  const [view, setView] = useState(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [newKey, setNewKey] = useState('')

  const applyView = useCallback(
    (data) => {
      setView(data)
      form.setFieldsValue(data)
      setNewKey('')
    },
    [form],
  )

  useEffect(() => {
    if (!open || !canEdit) {
      return undefined
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    getModelConfig()
      .then((data) => {
        if (!cancelled) applyView(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || '读取模型配置失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, canEdit, applyView])

  const save = async () => {
    const values = await form.validateFields()
    const payload = {
      base_url: values.base_url,
      chat_model: values.chat_model,
      temperature: values.temperature,
      max_tokens: values.max_tokens,
    }
    if (newKey.trim()) {
      payload.api_key = newKey.trim()
    }
    setSaving(true)
    try {
      applyView(await updateModelConfig(payload))
      message.success('已保存，新配置对后续对话立即生效')
      onClose()
    } catch (err) {
      message.error(err.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const clearKey = async () => {
    setSaving(true)
    try {
      applyView(await updateModelConfig({ api_key: '' }))
      message.success('已清除，网关密钥回到环境变量兜底')
    } catch (err) {
      message.error(err.message || '清除失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="助手设置"
      open={open}
      onCancel={onClose}
      destroyOnHidden
      footer={
        canEdit
          ? [
              <Button key="cancel" onClick={onClose}>
                取消
              </Button>,
              <Button key="save" type="primary" loading={saving} onClick={save}>
                保存
              </Button>,
            ]
          : [
              <Button key="ok" type="primary" onClick={onClose}>
                知道了
              </Button>,
            ]
      }
    >
      {!canEdit ? (
        <Alert
          type="info"
          showIcon
          message="当前账号没有模型配置权限"
          description="对话仍会使用系统已配置的大模型。需要改 LLM 地址、模型名或密钥时，请联系管理员。"
        />
      ) : error ? (
        <Alert type="error" showIcon message={error} />
      ) : (
        <Form form={form} layout="vertical" disabled={loading} requiredMark={false}>
          <Form.Item name="base_url" label="大模型接口地址（Base URL）">
            <Input maxLength={512} placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          <Form.Item name="chat_model" label="模型名称">
            <Input maxLength={128} placeholder="deepseek-chat" />
          </Form.Item>
          <Form.Item name="temperature" label="温度">
            <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="max_tokens" label="最大输出 Token">
            <InputNumber min={1} max={32768} step={128} style={{ width: '100%' }} />
          </Form.Item>
          <Alert
            type={view?.api_key_configured ? 'success' : 'warning'}
            showIcon
            message={
              <Space>
                <span>API 密钥</span>
                {view?.api_key_configured ? (
                  <Tag color="green">已配置 {view.api_key_masked}</Tag>
                ) : (
                  <Tag color="orange">未配置，走环境变量</Tag>
                )}
              </Space>
            }
            description={
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Text type="secondary">密钥只以掩码返回；留空保存表示不改动。</Text>
                <Space.Compact style={{ width: '100%' }}>
                  <Input.Password
                    value={newKey}
                    maxLength={512}
                    autoComplete="new-password"
                    placeholder="填入新密钥以覆盖"
                    onChange={(event) => setNewKey(event.target.value)}
                  />
                  <Popconfirm title="清除 API 密钥？" onConfirm={clearKey}>
                    <Button danger disabled={!view?.api_key_configured}>
                      清除
                    </Button>
                  </Popconfirm>
                </Space.Compact>
              </Space>
            }
          />
        </Form>
      )}
    </Modal>
  )
}
