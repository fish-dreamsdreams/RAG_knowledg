/** 3D 助手旁的迷你对话面板。 */
import { Button, Input } from 'antd'
import { SendOutlined } from '@ant-design/icons'

export default function AssistantChatPanel({
  messages,
  draft,
  sending,
  onDraftChange,
  onSend,
  onClose,
}) {
  return (
    <div className="kb-assistant-chat" role="dialog" aria-label="与小小对话">
      <div className="kb-assistant-chat__head">
        <span>和小小聊聊</span>
        <button type="button" className="kb-assistant-chat__x" aria-label="收起对话" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="kb-assistant-chat__log">
        {messages.length === 0 ? (
          <p className="kb-assistant-chat__empty">点头上的小小，或在下面输入一句话。</p>
        ) : (
          messages.map((item) => (
            <div
              key={item.id}
              className={
                item.role === 'user' ? 'kb-assistant-chat__row kb-assistant-chat__row--user' : 'kb-assistant-chat__row'
              }
            >
              {item.content}
            </div>
          ))
        )}
      </div>
      <div className="kb-assistant-chat__composer">
        <Input
          value={draft}
          maxLength={500}
          disabled={sending}
          placeholder="跟小小说点什么…"
          onChange={(event) => onDraftChange(event.target.value)}
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
          disabled={!draft.trim()}
          aria-label="发送"
          onClick={onSend}
        />
      </div>
    </div>
  )
}
