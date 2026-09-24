/**
 * AI 工作台（tasklist 14.4，PRD §6.4 / design.md §3.6）。
 *
 * 左侧会话、右侧对话。会话**由第一句话定义**：没有「新建会话」接口，点「+ 新会话」只是清空
 * 当前会话，真正的 `chat_sessions` 行在第一轮问答时由服务端创建，标题取那个问题。
 *
 * 三个关键取舍：
 *
 * 1. **一轮只发一个 `ask`**。服务端一次只处理一问（收到的第二帧会等前一轮答完才处理），
 *    所以进行中禁用发送键，而不是把问题排队——排队会让用户对着没反应的输入框干等。
 * 2. **`done` 之前连接断掉就是「中断」**，保留用户消息、给重试按钮；重试复用同一个问题，
 *    不复制用户气泡（PRD §6.4-6 的「用户消息保留」）。代价是服务端历史里这次提问会出现
 *    两次——提问先落库，重试就是又问了同一句，这是当时真实发生的事，前端不替它粉饰。
 * 3. **草稿存 localStorage**（按用户区分）。Token 过期只弹登录框，草稿还在输入框里，
 *    重新登录回到本页可以直接接着发（PRD §6.4-7）。
 *
 * 权限缺失与知识不足都不在这里判断：前者只看 `acl_notice` 布尔量，后者是服务端下发的
 * 固定文案（PRD §8.2），前端不做语义推断。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, App, Button, Empty, Input, Modal, Popconfirm, Spin, Typography } from 'antd'
import { CommentOutlined, DeleteOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { deleteSession, listMessages, listSessions } from '../api/chat'
import ChatBubble from '../components/ChatBubble'
import { useAuth } from '../auth/context'
import { useChatSocket } from '../hooks/useChatSocket'
import './chat.css'

const { Text, Title } = Typography

/** 关闭码含义见 TECH_SPEC §4.6：4401 令牌无效/停用，4403 缺 `ai:chat`，4408 空闲超时。 */
const CLOSE_UNAUTHORIZED = 4401
const CLOSE_FORBIDDEN = 4403
const CLOSE_IDLE = 4408

const EMPTY_HINT = '新建或选择会话开始提问'
const EXPIRED_HINT = '登录状态已失效，请重新登录后继续提问。'

let localId = 0
function nextLocalId() {
  localId += 1
  return `local-${localId}`
}

/** 会话列表按「今天 / 昨天 / 更早」分组（PRD §6.4 的侧栏线框）。 */
function groupLabel(isoTime) {
  const day = new Date(isoTime)
  const today = new Date()
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  if (day >= midnight) {
    return '今天'
  }
  const yesterday = new Date(midnight.getTime() - 24 * 60 * 60 * 1000)
  return day >= yesterday ? '昨天' : '更早'
}

function historyMessages(items) {
  return items.map((item) => ({
    id: item.id,
    role: item.role,
    content: item.content,
    citations: item.citations ?? [],
    stages: [],
    aclNotice: false,
    state: 'done',
    error: null,
  }))
}

export default function ChatPage() {
  const { identity, logout } = useAuth()
  const { message, modal } = App.useApp()
  const navigate = useNavigate()

  const [sessions, setSessions] = useState([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [activeSessionId, setActiveSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [expired, setExpired] = useState(false)

  // 事件回调里要读到「此刻」的值，用 ref 而不是 state：WS 回调不参与 React 渲染周期
  const activeSessionRef = useRef(null)
  const inFlightRef = useRef(false)
  const lastQuestionRef = useRef('')

  const draftKey = identity?.user_id ? `kb_chat_draft_${identity.user_id}` : null

  // 草稿来自 localStorage：身份是异步恢复的，key 到位的那一次渲染里直接读回来
  // （渲染期调整 state 是 React 认可的写法，比放进 effect 少一次空输入框的闪烁）
  const [draftKeyLoaded, setDraftKeyLoaded] = useState(null)
  if (draftKey && draftKey !== draftKeyLoaded) {
    setDraftKeyLoaded(draftKey)
    setDraft(localStorage.getItem(draftKey) ?? '')
  }

  const updateDraft = useCallback(
    (value) => {
      setDraft(value)
      if (draftKey) {
        localStorage.setItem(draftKey, value)
      }
    },
    [draftKey],
  )

  const refreshSessions = useCallback(async () => {
    const data = await listSessions({ page: 1, page_size: 50 })
    setSessions(data?.items ?? [])
    setSessionsLoading(false)
  }, [])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = await listSessions({ page: 1, page_size: 50 })
        if (!cancelled) {
          setSessions(data?.items ?? [])
        }
      } catch {
        // 侧栏拉不到历史不该挡住提问：问答本身是另一条链路（WS），失败就只留空列表
      } finally {
        if (!cancelled) {
          setSessionsLoading(false)
        }
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  /** 只改最后一条助手气泡：事件是逐帧来的，永远作用于本轮。 */
  const patchLastAssistant = useCallback((patch) => {
    setMessages((prev) => {
      const index = prev.findLastIndex((item) => item.role === 'assistant')
      if (index < 0) {
        return prev
      }
      const next = [...prev]
      next[index] = { ...next[index], ...patch(next[index]) }
      return next
    })
  }, [])

  const appendStage = useCallback(
    (stage) => patchLastAssistant((item) => ({ stages: [...item.stages, stage] })),
    [patchLastAssistant],
  )

  const appendToken = useCallback(
    (delta) => patchLastAssistant((item) => ({ content: item.content + delta })),
    [patchLastAssistant],
  )

  const appendCitation = useCallback(
    (citation) => {
      const { type: _type, ...rest } = citation
      patchLastAssistant((item) => ({ citations: [...item.citations, rest] }))
    },
    [patchLastAssistant],
  )

  const finishTurn = useCallback(() => {
    inFlightRef.current = false
    setSending(false)
  }, [])

  const onEvent = useCallback(
    (payload) => {
      switch (payload.type) {
        case 'status':
          appendStage(payload.stage)
          break
        case 'token':
          appendToken(payload.delta)
          break
        case 'citation':
          appendCitation(payload)
          break
        case 'acl_notice':
          patchLastAssistant(() => ({ aclNotice: Boolean(payload.has_denied) }))
          break
        case 'done':
          finishTurn()
          // 首轮问答服务端才建会话：认领它，否则刷新后侧栏找不到这次对话
          if (payload.session_id && payload.session_id !== activeSessionRef.current) {
            activeSessionRef.current = payload.session_id
            setActiveSessionId(payload.session_id)
          }
          patchLastAssistant(() => ({ state: 'done' }))
          refreshSessions().catch(() => {})
          break
        case 'error':
          finishTurn()
          patchLastAssistant(() => ({ state: 'error', error: payload.message }))
          if (payload.code === 'AI_SESSION_NOT_FOUND') {
            // 会话被删或不属于当前用户：丢掉它，下一句会开新会话（服务端已经拒绝了这一轮）
            activeSessionRef.current = null
            setActiveSessionId(null)
            refreshSessions().catch(() => {})
          }
          break
        default:
          // pong 与未登记的类型都不进 UI
          break
      }
    },
    [
      appendCitation,
      appendStage,
      appendToken,
      finishTurn,
      patchLastAssistant,
      refreshSessions,
    ],
  )

  const onClose = useCallback(
    (code) => {
      if (code === CLOSE_UNAUTHORIZED) {
        // 弹登录框而不是直接甩到登录页：草稿要留在输入框里（PRD §6.4-7）
        setExpired(true)
      } else if (code === CLOSE_FORBIDDEN) {
        modal.error({ title: '无问答权限', content: '当前账号没有 AI 问答权限，请联系管理员。' })
      } else if (inFlightRef.current && code !== CLOSE_IDLE) {
        patchLastAssistant(() => ({ state: 'interrupted' }))
      }
      finishTurn()
    },
    [finishTurn, modal, patchLastAssistant],
  )

  const { ask } = useChatSocket({ onEvent, onClose })

  /** 发一轮提问；重试与首次提问走同一条路（差别只在要不要新增用户气泡）。 */
  const send = useCallback(
    ({ content, echo = true }) => {
      const question = content.trim()
      if (!question || inFlightRef.current) {
        return
      }
      inFlightRef.current = true
      setSending(true)
      lastQuestionRef.current = question
      updateDraft('')

      const placeholder = {
        id: nextLocalId(),
        role: 'assistant',
        content: '',
        citations: [],
        stages: [],
        aclNotice: false,
        state: 'streaming',
        error: null,
      }
      setMessages((prev) => {
        const base = echo
          ? [
              ...prev,
              {
                id: nextLocalId(),
                role: 'user',
                content: question,
                citations: [],
                stages: [],
                aclNotice: false,
                state: 'done',
                error: null,
              },
            ]
          : // 重试：只换掉那条失败的气泡，用户消息不复制（PRD §6.4-6）
            prev.filter((item) => item.state === 'done' || item.role === 'user')
        return [...base, placeholder]
      })

      ask({ content: question, sessionId: activeSessionRef.current })
    },
    [ask, updateDraft],
  )

  const retry = useCallback(() => {
    send({ content: lastQuestionRef.current, echo: false })
  }, [send])

  const onSend = () => send({ content: draft })

  const newSession = useCallback(() => {
    activeSessionRef.current = null
    setActiveSessionId(null)
    setMessages([])
  }, [])

  const openSession = useCallback(
    async (sessionId) => {
      if (!sessionId) {
        return
      }
      activeSessionRef.current = sessionId
      setActiveSessionId(sessionId)
      setMessages([])
      try {
        const data = await listMessages(sessionId, { page: 1, page_size: 100 })
        setMessages(historyMessages(data?.items ?? []))
      } catch (error) {
        // 会话在别的标签页被删掉了，或本就不属于当前用户（服务端一律回「不存在」）
        message.error(error.message || '会话读取失败')
        newSession()
        refreshSessions().catch(() => {})
      }
    },
    [message, newSession, refreshSessions],
  )

  const removeSession = useCallback(
    async (sessionId) => {
      await deleteSession(sessionId)
      if (sessionId === activeSessionRef.current) {
        newSession()
      }
      await refreshSessions()
    },
    [newSession, refreshSessions],
  )

  const grouped = useMemo(() => {
    const buckets = new Map()
    sessions.forEach((item) => {
      const label = groupLabel(item.updated_at)
      buckets.set(label, [...(buckets.get(label) ?? []), item])
    })
    return ['今天', '昨天', '更早']
      .filter((label) => buckets.has(label))
      .map((label) => ({ label, items: buckets.get(label) }))
  }, [sessions])

  const onExpiredConfirm = () => {
    setExpired(false)
    logout()
    navigate('/login', { replace: true, state: { from: '/chat' } })
  }

  return (
    <div className="kb-chat">
      <aside className="kb-chat__aside">
        <Button className="kb-chat__new" type="primary" icon={<PlusOutlined />} block onClick={newSession}>
          新会话
        </Button>

        <div className="kb-chat__sessions">
          {sessionsLoading ? (
            <div className="kb-chat__loading">
              <Spin size="small" />
            </div>
          ) : null}

          {!sessionsLoading && grouped.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有历史会话" />
          ) : null}

          {grouped.map((group) => (
            <div key={group.label}>
              <Text className="kb-chat__group-label">{group.label}</Text>
              {group.items.map((item) => {
                const active = item.id === activeSessionId
                return (
                  <div
                    key={item.id}
                    className={
                      active ? 'kb-chat__session kb-chat__session--active' : 'kb-chat__session'
                    }
                  >
                    {/* 选中会话用真按钮：可 Tab 聚焦、回车即开，屏幕阅读器也念得出会话标题 */}
                    <button
                      type="button"
                      className="kb-chat__open"
                      onClick={() => openSession(item.id)}
                    >
                      <Text ellipsis style={{ flex: 1, minWidth: 0 }} title={item.title ?? '未命名会话'}>
                        {item.title || '未命名会话'}
                      </Text>
                    </button>
                    <Popconfirm
                      title="删除该会话？"
                      description="会话中的消息会一起删除，审计记录保留。"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => removeSession(item.id).catch(() => {})}
                    >
                      {/* 删除键只在当前会话上出现：一排垃圾桶会让侧栏没法看 */}
                      <Button
                        className="kb-chat__remove"
                        type="text"
                        size="small"
                        aria-label={`删除会话：${item.title || '未命名会话'}`}
                        icon={<DeleteOutlined />}
                      />
                    </Popconfirm>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </aside>

      <section className="kb-chat__main">
        <div className="kb-chat__stream">
          {messages.length === 0 ? (
            <div className="kb-chat__empty">
              <span className="kb-chat__empty-mark" aria-hidden="true">
                <CommentOutlined />
              </span>
              <p className="kb-chat__empty-title">{EMPTY_HINT}</p>
            </div>
          ) : (
            <div className="kb-chat__thread">
              {messages.map((item) => (
                <ChatBubble key={item.id} message={item} onRetry={retry} />
              ))}
            </div>
          )}
        </div>

        <div className="kb-chat__composer">
          <div className="kb-chat__composer-inner">
            {sending ? <span className="kb-chat__hint">正在回答，请等这一轮结束再提问</span> : null}
            <div className="kb-chat__field">
              <Input.TextArea
                variant="borderless"
                value={draft}
                onChange={(event) => updateDraft(event.target.value)}
                placeholder="请输入问题，回车发送，Shift + 回车换行"
                autoSize={{ minRows: 1, maxRows: 4 }}
                disabled={expired}
                onPressEnter={(event) => {
                  if (!event.shiftKey) {
                    event.preventDefault()
                    onSend()
                  }
                }}
              />
              <Button
                type="primary"
                icon={<SendOutlined />}
                loading={sending}
                onClick={onSend}
                disabled={!draft.trim() || expired}
              >
                发送
              </Button>
            </div>
          </div>
        </div>
      </section>

      <Modal
        open={expired}
        title="登录状态已失效"
        closable={false}
        maskClosable={false}
        okText="重新登录"
        cancelText="稍后"
        onOk={onExpiredConfirm}
        onCancel={() => setExpired(false)}
      >
        <Alert type="warning" showIcon message={EXPIRED_HINT} />
        <Title level={5} style={{ marginTop: 12, marginBottom: 0 }}>
          输入内容已保留
        </Title>
        <Text type="secondary">重新登录后回到本页即可继续发送。</Text>
      </Modal>
    </div>
  )
}
