/**
 * 引用卡片（tasklist 14.4 / 17.5，design.md §3.6「引用卡片」）。
 *
 * 卡片只承载**已放行**单元的信息：标题、切片摘要、附图。无权单元的标题与正文不允许出现在
 * 这里（P3）——所以卡片内容完全来自后端下发的 `citation`，前端不做任何"补齐"。
 *
 * 附图地址是后端代理的相对路径，Token 由渲染时附加（见 `utils/asset.js`）：引用会存进
 * `chat_messages.citations`，Token 不能跟着进库。
 */

import { Card, Image, Space, Tag, Tooltip, Typography } from 'antd'
import { FileTextOutlined } from '@ant-design/icons'
import { assetUrl } from '../utils/asset'

const { Paragraph, Text } = Typography

/** 切片 id 是 UUID，全串没人读得下去，前 8 位足够对上审计里的 `citation_ids`。 */
function shortId(chunkId) {
  return String(chunkId ?? '').slice(0, 8)
}

export default function CitationCard({ citation }) {
  const assets = citation.assets ?? []

  return (
    <Card size="small" styles={{ body: { padding: 12 } }} style={{ marginBottom: 8 }}>
      <Space size={6} wrap>
        <Tag color="blue">引用</Tag>
        <FileTextOutlined />
        <Text strong>{citation.title}</Text>
        {citation.chunk_id ? (
          <Tooltip title={`切片 ${citation.chunk_id}`}>
            <Text type="secondary" style={{ fontSize: 'calc(12px * var(--kb-scale))' }}>
              #{shortId(citation.chunk_id)}
            </Text>
          </Tooltip>
        ) : null}
      </Space>

      {citation.snippet ? (
        <Paragraph
          type="secondary"
          ellipsis={{ rows: 3, tooltip: citation.snippet }}
          style={{ margin: '8px 0 0', fontSize: 'calc(12px * var(--kb-scale))' }}
        >
          {citation.snippet}
        </Paragraph>
      ) : null}

      {assets.length > 0 ? (
        <Space size={8} wrap style={{ marginTop: 8 }}>
          {assets.map((asset) => (
            <Image
              key={asset.name}
              src={assetUrl(asset.url)}
              alt={asset.name}
              height={72}
              style={{ objectFit: 'contain', border: '1px solid #f0f0f0', borderRadius: 4 }}
            />
          ))}
        </Space>
      ) : null}
    </Card>
  )
}
