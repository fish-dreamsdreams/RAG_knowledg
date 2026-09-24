/**
 * 运营闭环接口：知识缺口、审计与看板（tasklist 13.1 ~ 13.3，供给 14.6 / 14.7）。
 *
 * 看板的每个数字都来自审计（`qa_audit_logs`），所以这里的时间窗口径与审计一致：
 * `range` 只有 `today` / `7d` 两种，按业务时区切天，不用前端拼日期区间。
 */

import request from './request'

export function listGaps(params = {}) {
  return request.get('/knowledge-gaps', { params })
}

/**
 * 转建：只把缺口标成 `converted` 并回预填信息，真正的知识单元要带 `from_gap_id` 走导入接口；
 * 索引完成后后端才把它回填成 `filled`（P16）。
 */
export function convertGap(gapId) {
  return request.post(`/knowledge-gaps/${gapId}/convert`)
}

export function listAuditLogs(params = {}) {
  return request.get('/audit-logs', { params })
}

export function dashboardSummary(range = '7d') {
  return request.get('/dashboard/summary', { params: { range } })
}

export function dashboardTopQuestions(range = '7d', limit = 10) {
  return request.get('/dashboard/top-questions', { params: { range, limit } })
}

export function dashboardTopKnowledge(range = '7d', limit = 10) {
  return request.get('/dashboard/top-knowledge', { params: { range, limit } })
}

export function dashboardTokenTrend(range = '7d') {
  return request.get('/dashboard/token-trend', { params: { range } })
}
