/**
 * 问答会话与历史接口（tasklist 14.4，TECH_SPEC §4.5）。
 *
 * 会话没有「新建」接口：服务端在第一轮问答时创建（`chat_sessions.title` 取首个问题），
 * 所以前端「+ 新会话」只是清空当前会话，不调接口。
 */

import request from './request'

export function listSessions(params = {}) {
  return request.get('/chat/sessions', { params })
}

export function listMessages(sessionId, params = {}) {
  return request.get(`/chat/sessions/${sessionId}/messages`, { params })
}

export function deleteSession(sessionId) {
  return request.delete(`/chat/sessions/${sessionId}`)
}
