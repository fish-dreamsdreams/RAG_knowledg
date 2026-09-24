/**
 * FAQ 审核与维护接口（tasklist 11.2 / 11.3，供给 14.6）。
 *
 * 权限分工决定按钮：看候选/列表与驳回要 `faq:review`，发布、改答案、切缓存、下线要
 * `faq:publish`。发布候选时必须带 `answer`（省略会回退到候选的 `suggested_answer`，
 * 但页面总是让审核人确认过再发）。
 */

import request from './request'

export function listCandidates(params = {}) {
  return request.get('/faq/candidates', { params })
}

export function publishCandidate(candidateId, answer) {
  return request.post(`/faq/candidates/${candidateId}/publish`, { answer })
}

/** 驳回原因必填：候选会被永久标记，没有原因就无法复盘为什么没采纳。 */
export function rejectCandidate(candidateId, reason) {
  return request.post(`/faq/candidates/${candidateId}/reject`, { reason })
}

export function listFaqs(params = {}) {
  return request.get('/faqs', { params })
}

/** 改答案、切 `cache_enabled` 或上下线；三种改动都会同步重建缓存。 */
export function updateFaq(faqId, payload) {
  return request.put(`/faqs/${faqId}`, payload)
}

/** 手动触发挖掘；投递即返回，产出要看候选列表（页面轮询即可）。 */
export function triggerMining() {
  return request.post('/faq/mine')
}
