/**
 * 知识单元接口（tasklist 14.5，TECH_SPEC §4.2）。
 *
 * 导入是异步的：`importUnits` 只完成「落盘 + 建行 + 投队列」，进度靠 `getImportStatus`
 * 轮询（或列表行自带的 stage/progress），别指望它在响应里带回解析结果。
 */

import request from './request'

export function listUnits(params = {}) {
  return request.get('/knowledge-units', { params })
}

export function getUnit(unitId) {
  return request.get(`/knowledge-units/${unitId}`)
}

/**
 * 批量导入。`category` 只作为导入时的默认分类下发；文件本身不带分类概念。
 * 单次上限 50 个文件，超出由调用方分批。
 */
export function importUnits(files, { category, fromGapId } = {}) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  if (category) {
    form.append('category', category)
  }
  if (fromGapId) {
    form.append('from_gap_id', fromGapId)
  }
  return request.post('/knowledge-units/import', form)
}

export function getImportStatus(unitId) {
  return request.get(`/knowledge-units/${unitId}/import-status`)
}

export function updateUnit(unitId, payload) {
  return request.put(`/knowledge-units/${unitId}`, payload)
}

export function setUnitEnabled(unitId, enabled) {
  return request.put(`/knowledge-units/${unitId}/enabled`, { enabled })
}

export function deleteUnit(unitId) {
  return request.delete(`/knowledge-units/${unitId}`)
}

export function listChunks(unitId, params = {}) {
  return request.get(`/knowledge-units/${unitId}/chunks`, { params })
}

export function getChunk(unitId, chunkId) {
  return request.get(`/knowledge-units/${unitId}/chunks/${chunkId}`)
}

export function updateChunk(unitId, chunkId, payload) {
  return request.put(`/knowledge-units/${unitId}/chunks/${chunkId}`, payload, {
    timeout: 120000,
  })
}

export function retryUnit(unitId) {
  return request.post(`/knowledge-units/${unitId}/retry`)
}

export function getUnitAcl(unitId) {
  return request.get(`/knowledge-units/${unitId}/acl`)
}

export function saveUnitAcl(unitId, payload) {
  return request.put(`/knowledge-units/${unitId}/acl`, payload)
}

/** 权限弹窗的候选项；人员按 `keyword` 服务端过滤（`kb:acl` 即可，不需要 `org:*`）。 */
export function listAclOptions(params = {}) {
  return request.get('/knowledge-units/acl-options', { params })
}
