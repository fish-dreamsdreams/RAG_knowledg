/**
 * 问答页 / 侧栏 3D 助手：右键关闭与设置，左键或「对话」打开闲聊。
 * WebGL 不可用或加载失败时提示，不回退 2D（按产品确认）。
 */
import { Component, lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { UserOutlined } from '@ant-design/icons'
import { Button, Dropdown, message } from 'antd'
import { streamAssistantChat } from '../../api/assistant'
import AssistantChatPanel from './AssistantChatPanel'
import AssistantSettingsModal from './AssistantSettingsModal'
import './assistant.css'

const STORAGE_KEY = 'kb_assistant_widget'
const DEFAULT_SIZE = { width: 240, height: 400 }
export const DEFAULT_SPEECH = '萌新小小，时刻准备着！'
const SPEAK_CHAR_MS = import.meta.env.MODE === 'test' ? 0 : 90
const SPEAK_PUNCT_MS = import.meta.env.MODE === 'test' ? 0 : 180

function speakWait(ch) {
  return /[，。！？、…,.!?;；：:]/.test(ch) ? SPEAK_PUNCT_MS : SPEAK_CHAR_MS
}

const AssistantScene = lazy(() => import('./AssistantScene'))

function readStore() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) {
      return { open: true, x: null, y: null }
    }
    const parsed = JSON.parse(raw)
    return {
      open: parsed.open !== false,
      x: Number.isFinite(parsed.x) ? parsed.x : null,
      y: Number.isFinite(parsed.y) ? parsed.y : null,
    }
  } catch {
    return { open: true, x: null, y: null }
  }
}

function writeStore(next) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
}

export function hasWebGL() {
  if (typeof document === 'undefined') {
    return false
  }
  try {
    const canvas = document.createElement('canvas')
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}

class SceneErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return this.props.fallback
    }
    return this.props.children
  }
}

function FailHint() {
  return (
    <div className="kb-assistant__fail" role="status">
      3D 助手加载失败，请检查 WebGL 或刷新页面。
    </div>
  )
}

let chatSeq = 0
function nextChatId() {
  chatSeq += 1
  return `a-${chatSeq}`
}

export default function AssistantWidget({ mood = 'idle', speech = DEFAULT_SPEECH, docked = false }) {
  const [{ open, x, y }, setLayout] = useState(readStore)
  const [webgl] = useState(hasWebGL)
  const [chatOpen, setChatOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [turns, setTurns] = useState([])
  const hostRef = useRef(null)
  const bubbleRef = useRef(null)
  const drag = useRef(null)

  const persist = useCallback((patch) => {
    setLayout((prev) => {
      const next = { ...prev, ...patch }
      writeStore(next)
      return next
    })
  }, [])

  const onPointerDown = (event) => {
    if (docked || event.button !== 0) {
      return
    }
    const node = hostRef.current
    if (!node) {
      return
    }
    const rect = node.getBoundingClientRect()
    const parent = node.offsetParent?.getBoundingClientRect()
    drag.current = {
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top,
      parent,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event) => {
    if (!drag.current) {
      return
    }
    const parent = drag.current.parent
    if (!parent) {
      return
    }
    const maxX = Math.max(0, parent.width - DEFAULT_SIZE.width)
    const maxY = Math.max(0, parent.height - DEFAULT_SIZE.height)
    const nextX = Math.min(maxX, Math.max(0, event.clientX - parent.left - drag.current.dx))
    const nextY = Math.min(maxY, Math.max(0, event.clientY - parent.top - drag.current.dy))
    persist({ x: nextX, y: nextY })
  }

  const onPointerUp = (event) => {
    if (drag.current) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    drag.current = null
  }

  useEffect(() => {
    return () => {
      drag.current = null
    }
  }, [])

  const sendChat = async () => {
    const content = draft.trim()
    if (!content || sending) {
      return
    }
    const history = turns.map((item) => ({ role: item.role, content: item.content }))
    const assistantId = nextChatId()
    setDraft('')
    setTurns((prev) => [
      ...prev,
      { id: nextChatId(), role: 'user', content },
      { id: assistantId, role: 'assistant', content: '' },
    ])
    setSending(true)
    let spoken = Promise.resolve()
    const speak = (delta) => {
      spoken = spoken.then(async () => {
        for (const ch of delta) {
          setTurns((prev) =>
            prev.map((item) =>
              item.id === assistantId ? { ...item, content: `${item.content}${ch}` } : item,
            ),
          )
          const wait = speakWait(ch)
          if (wait) {
            await new Promise((resolve) => setTimeout(resolve, wait))
          }
        }
      })
    }
    try {
      await streamAssistantChat({
        content,
        history,
        onDelta: speak,
      })
      await spoken
      setTurns((prev) =>
        prev.map((item) =>
          item.id === assistantId && !item.content.trim()
            ? { ...item, content: '小小走神了，再说一次嘛～' }
            : item,
        ),
      )
    } catch (err) {
      await spoken.catch(() => {})
      message.error(err.message || '小小现在接不到模型')
      setTurns((prev) =>
        prev.map((item) =>
          item.id === assistantId
            ? { ...item, content: item.content.trim() || '呜，小小现在连不上大模型…' }
            : item,
        ),
      )
    } finally {
      setSending(false)
    }
  }

  const lastAssistant = [...turns].reverse().find((item) => item.role === 'assistant')
  const bubbleText = lastAssistant?.content || (sending ? '小小在想…' : speech)
  const liveMood = sending ? (lastAssistant?.content ? 'talk' : 'think') : lastAssistant ? 'talk' : mood

  useEffect(() => {
    const node = bubbleRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [bubbleText])

  if (!open) {
    return (
      <Button
        className={docked ? 'kb-assistant-reopen kb-assistant-reopen--docked' : 'kb-assistant-reopen'}
        type="primary"
        shape="circle"
        aria-label="显示 3D 助手"
        icon={<UserOutlined />}
        onClick={() => persist({ open: true })}
      />
    )
  }

  const placed = !docked && x != null && y != null
  const style = placed
    ? { left: x, top: y, right: 'auto', bottom: 'auto' }
    : undefined

  return (
    <>
      <Dropdown
        trigger={['contextMenu']}
        menu={{
          items: [
            { key: 'chat', label: '对话' },
            { key: 'settings', label: '设置' },
            { type: 'divider' },
            { key: 'close', label: '关闭助手' },
          ],
          onClick: ({ key }) => {
            if (key === 'chat') {
              setChatOpen(true)
            }
            if (key === 'settings') {
              setSettingsOpen(true)
            }
            if (key === 'close') {
              setChatOpen(false)
              persist({ open: false })
            }
          },
        }}
      >
        <div
          ref={hostRef}
          className={docked ? 'kb-assistant kb-assistant--docked' : 'kb-assistant'}
          style={style}
          role="figure"
          aria-label="3D 助手"
          onClick={() => setChatOpen(true)}
          onPointerDown={docked ? undefined : onPointerDown}
          onPointerMove={docked ? undefined : onPointerMove}
          onPointerUp={docked ? undefined : onPointerUp}
          onPointerCancel={docked ? undefined : onPointerUp}
        >
          <div className="kb-assistant__bubble">
            <div
              ref={bubbleRef}
              className="kb-assistant__bubble-text"
              onClick={(event) => event.stopPropagation()}
              onPointerDown={(event) => event.stopPropagation()}
            >
              {bubbleText}
            </div>
          </div>
          <div className="kb-assistant__stage">
            {!webgl ? (
              <FailHint />
            ) : (
              <SceneErrorBoundary fallback={<FailHint />}>
                <Suspense fallback={<div className="kb-assistant__loading">加载模型…</div>}>
                  <AssistantScene mood={liveMood} />
                </Suspense>
              </SceneErrorBoundary>
            )}
          </div>
        </div>
      </Dropdown>
      {chatOpen ? (
        <AssistantChatPanel
          messages={turns}
          draft={draft}
          sending={sending}
          onDraftChange={setDraft}
          onSend={sendChat}
          onClose={() => setChatOpen(false)}
        />
      ) : null}
      <AssistantSettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </>
  )
}
