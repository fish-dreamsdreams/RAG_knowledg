/**
 * 模型配置面板（tasklist 14.7，PRD §6.7）。
 *
 * **密钥的三种语义决定界面**（见 schemas/system.py）：
 *
 * - 输入框留空 = 不改动当前密钥；
 * - 输入框填了新值 = 覆盖；
 * - 「清除密钥」= 显式传空串，回到环境变量兜底。
 *
 * 所以「清除」必须是一个独立动作，不能和「留空」合并——否则管理员想清除却只是没改。
 * 读接口只回掩码：明文一旦能回显，写接口的权限就等于把网关密钥送出去了。
 *
 * Embedding / Rerank 走本地权重，这里只在只读区展示模型名（重排模型可改，因为换权重不需要
 * 重启服务）；能改的是 Chat 网关与三个阈值。
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Space,
  Tag,
  Typography,
} from 'antd'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { getModelConfig, updateModelConfig } from '../api/system'

const { Text } = Typography

export default function ModelConfigPanel() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [view, setView] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [newKey, setNewKey] = useState('')

  /** 落地一次读取结果：effect 与「重新读取」共用，避免两处各写一遍。 */
  const applyView = useCallback(
    (data) => {
      setView(data)
      form.setFieldsValue(data)
      setNewKey('')
    },
    [form],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      applyView(await getModelConfig())
    } catch (err) {
      setError(err.message || '读取模型配置失败')
    } finally {
      setLoading(false)
    }
  }, [applyView])

  useEffect(() => {
    // 首屏在 effect 里只发请求、在回调里落地：effect 中同步 setState 会级联一次渲染
    let cancelled = false
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
  }, [applyView])

  const save = async () => {
    const values = await form.validateFields()
    const payload = { ...values }
    // 空串在写接口里是「清除」，所以只在真的填了字时才带上 api_key
    if (newKey.trim()) {
      payload.api_key = newKey.trim()
    }
    setSaving(true)
    try {
      const data = await updateModelConfig(payload)
      setView(data)
      form.setFieldsValue(data)
      setNewKey('')
      message.success('已保存，新配置对后续问答立即生效')
    } catch (err) {
      message.error(err.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const clearKey = async () => {
    setSaving(true)
    try {
      const data = await updateModelConfig({ api_key: '' })
      setView(data)
      setNewKey('')
      message.success('已清除，网关密钥回到环境变量兜底')
    } catch (err) {
      message.error(err.message || '清除失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      title="模型网关与阈值"
      size="small"
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            重新读取
          </Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>
            保存
          </Button>
        </Space>
      }
    >
      {error ? (
        <Alert type="error" showIcon message={error} />
      ) : (
        <Form form={form} layout="vertical" disabled={loading} requiredMark={false}>
          <Row gutter={16}>
            <Col xs={24} lg={12}>
              <Form.Item name="base_url" label="大模型接口地址（LLM_Base URL）">
                <Input maxLength={512} placeholder="https://api.deepseek.com/v1" />
              </Form.Item>
            </Col>
            <Col xs={24} lg={12}>
              <Form.Item name="chat_model" label="模型名称">
                <Input maxLength={128} placeholder="deepseek-chat" />
              </Form.Item>
            </Col>
            <Col xs={24} lg={6}>
              <Form.Item name="temperature" label="温度">
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} lg={6}>
              <Form.Item name="max_tokens" label="最大输出 Token">
                <InputNumber min={1} max={32768} step={128} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} lg={12}>
              <Form.Item name="rerank_model" label="重排模型（本地权重路径）">
                <Input maxLength={128} />
              </Form.Item>
            </Col>
          </Row>

          <Alert
            type={view?.api_key_configured ? 'success' : 'warning'}
            showIcon
            style={{ marginBottom: 16 }}
            message={
              <Space>
                <span>API密钥：</span>
                {view?.api_key_configured ? (
                  <Tag color="green">已配置 {view.api_key_masked}</Tag>
                ) : (
                  <Tag color="orange">未配置，走环境变量</Tag>
                )}
              </Space>
            }
            description={
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Text type="secondary">
                  密钥只以掩码返回、永不回显明文；留空保存表示不改动当前密钥。
                </Text>
                <Space.Compact style={{ width: '100%', maxWidth: 520 }}>
                  <Input.Password
                    value={newKey}
                    maxLength={512}
                    autoComplete="new-password"
                    placeholder="填入新密钥以覆盖当前值"
                    onChange={(event) => setNewKey(event.target.value)}
                  />
                  <Popconfirm
                    title="清除API密钥？"
                    description="清除后回到环境变量兜底；若环境变量也没有，问答会直接失败。"
                    onConfirm={clearKey}
                  >
                    <Button danger disabled={!view?.api_key_configured}>
                      清除密钥
                    </Button>
                  </Popconfirm>
                </Space.Compact>
              </Space>
            }
          />

          <Row gutter={16}>
            <Col xs={24} lg={8}>
              <Form.Item name="faq_sim_threshold" label="FAQ 命中阈值">
                <InputNumber min={0} max={1} step={0.01} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} lg={8}>
              <Form.Item name="gap_sim_threshold" label="缺口判定阈值">
                <InputNumber min={0} max={1} step={0.01} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} lg={8}>
              <Form.Item name="faq_cluster_min_freq" label="候选最小频次">
                <InputNumber min={1} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Descriptions size="small" column={1} title="不改动项">
            <Descriptions.Item label="Embedding / Rerank">
              本地权重（BGE-M3 / bge-reranker-v2-m3），控制台不提供配置项；换权重请改环境变量并重启。
            </Descriptions.Item>
          </Descriptions>
        </Form>
      )}
    </Card>
  )
}
