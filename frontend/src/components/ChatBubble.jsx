/**
 * 问答气泡（tasklist 14.4，PRD §6.4 / design.md §3.6）。
 *
 * 一个气泡要覆盖四种形态，它们都由服务端事件决定，前端不猜：
 *
 * | 形态 | 触发 | 表现 |
 * |------|------|------|
 * | 进行中 | 已发 `ask`、还没收到 `done` | 鉴权前「检索与鉴权中…」，之后「正在生成…」 |
 * | 完成 | `done` | Markdown 正文 + 溯源卡片（**只有结束时才出卡片**） |
 * | 中断 | 连接在 `done` 之前关闭 | 「生成中断，请重试」+ 重试 |
 * | 失败 | `error` 帧 | 服务端文案 + 重试 |
 *
 * 两条安全约束（P3 / PRD §8.1）：
 * - 权限缺失提示只有一个布尔量可依据，提示条**不可点击、不含标题或文件名**；
 * - 知识不足的文案由服务端作为答案下发（PRD §8.2），前端不自己编提示，也不去猜
 *   "这一轮算不算没有命中"——FAQ 命中同样不产生 `generate` 阶段，靠阶段推断必然误判。
 */

import { Alert, Button, Space, Spin, Tag, Typography } from 'antd'
import { CheckCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import CitationCard from './CitationCard'

const { Text } = Typography

/** `status` 的取值是**已完成**的节点名（design.md §3.5），文案用完成时。 */
const STAGE_LABELS = {
  faq_hit: '已查 FAQ',
  rewrite: '已改写',
  retrieve: '已召回',
  rerank: '已重排',
  cutoff: '已截断',
  acl: '已鉴权',
  generate: '已生成',
}

/** 权限缺失提示条：文案取自 PRD §8.1，逐字不改，也不带任何可点元素。 */
const ACL_NOTICE = {
  message: '部分参考资料因权限受限无法展示',
  description: '检测到相关制度/资料，但您当前所属部门或角色无权查阅该内容。',
}

const INTERRUPTED_TEXT = '生成中断，请重试'

function waitingLabel(stages) {
  if (Array.isArray(stages) && (stages.includes('acl') || stages.includes('generate'))) {
    return '正在生成…'
  }
  return '检索与鉴权中…'
}

function Markdown({ content }) {
  return (
    <div className="kb-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // 外链新开页：控制台里的跳转不该把整个工作台顶掉
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

function Stages({ stages }) {
  if (!stages?.length) {
    return null
  }
  return (
    <Space size={4} wrap style={{ marginBottom: 8 }}>
      {stages.map((stage) => (
        <Tag key={stage} icon={<CheckCircleOutlined />} color="default">
          {STAGE_LABELS[stage] ?? stage}
        </Tag>
      ))}
    </Space>
  )
}

export default function ChatBubble({ message, onRetry }) {
  const isUser = message.role === 'user'
  const streaming = message.state === 'streaming'
  const failed = message.state === 'interrupted' || message.state === 'error'

  const retryButton = (
    <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
      重试
    </Button>
  )

  return (
    <div className={isUser ? 'kb-bubble kb-bubble--user' : 'kb-bubble kb-bubble--answer'}>
      <div className="kb-bubble__column">
        {/* 回答那一块浅蓝底由 chat.css 给：与用户那条实心蓝一眼分得开 */}
        <div className="kb-bubble__body">
          {isUser ? (
            <span>{message.content}</span>
          ) : (
            <>
              {streaming ? <Stages stages={message.stages} /> : null}

              {streaming && !message.content ? (
                <Space size={8}>
                  <Spin size="small" />
                  <Text type="secondary">{waitingLabel(message.stages)}</Text>
                </Space>
              ) : (
                <Markdown content={message.content} />
              )}

              {streaming && message.content ? (
                <span className="kb-caret" aria-hidden="true" />
              ) : null}

              {failed ? (
                <Text type={message.state === 'error' ? 'danger' : 'secondary'}>
                  {message.state === 'error' ? message.error : INTERRUPTED_TEXT}
                </Text>
              ) : null}
            </>
          )}
        </div>

        {!isUser && message.aclNotice ? (
          <Alert
            type="warning"
            showIcon
            message={ACL_NOTICE.message}
            description={ACL_NOTICE.description}
          />
        ) : null}

        {/* 溯源卡片只在流式结束后出现（PRD §6.4-3）：中途的引用是半成品，会误导判读 */}
        {!isUser && message.state === 'done' && message.citations?.length
          ? message.citations.map((citation) => (
              <CitationCard key={`${citation.unit_id}-${citation.chunk_id}`} citation={citation} />
            ))
          : null}

        {!isUser && failed ? retryButton : null}
      </div>
    </div>
  )
}
