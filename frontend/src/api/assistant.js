/** 3D 助手闲聊：SSE 流式走系统 Chat 网关，不检索知识库。 */

import { ApiError, UNAUTHORIZED_EVENT } from './request'
import { clearToken, getToken } from '../utils/token'

function parseSseBlock(block, onEvent) {
  for (const line of block.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('data:')) {
      continue
    }
    const raw = trimmed.slice(5).trim()
    if (!raw) {
      continue
    }
    onEvent(JSON.parse(raw))
  }
}

export async function streamAssistantChat({ content, history, onDelta, signal }) {
  const token = getToken()
  const response = await fetch('/api/v1/assistant/chat', {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ content, history }),
    signal,
  })

  if (response.status === 401) {
    clearToken()
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    throw new ApiError('AUTH_INVALID_TOKEN', '登录已过期，请重新登录', 401)
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(
      body?.code || 'SYS_NETWORK',
      body?.message || '小小现在接不到模型',
      response.status,
    )
  }

  if (!response.body) {
    throw new ApiError('SYS_NETWORK', '当前浏览器无法读取流式响应', response.status)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sawError = null

  const onEvent = (payload) => {
    if (payload.error) {
      sawError = new ApiError(payload.code || 'AI_UPSTREAM_ERROR', payload.error, 502)
      return
    }
    if (payload.delta) {
      onDelta(payload.delta)
    }
  }

  while (!sawError) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    parts.forEach((block) => parseSseBlock(block, onEvent))
    if (done) {
      if (buffer.trim()) {
        parseSseBlock(buffer, onEvent)
      }
      break
    }
  }

  if (sawError) {
    throw sawError
  }
}
