/**
 * 系统配置接口：模型网关与阈值（tasklist 13.4，供给 14.7）。
 *
 * **密钥永不回显**：读接口只给 `api_key_configured` 与掩码；写接口的 `api_key` 有三种语义——
 * 不传 = 不动、传非空串 = 覆盖、传空串 = 清除（回到 env 兜底）。所以「清除」与「不改」在
 * 前端是两个不同动作，不能都做成「留空即不改」。
 */

import request from './request'

export function getModelConfig() {
  return request.get('/system/model-config')
}

export function updateModelConfig(payload) {
  return request.put('/system/model-config', payload)
}
