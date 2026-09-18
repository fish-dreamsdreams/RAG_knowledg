/**
 * 带鉴权的资源地址（tasklist 17.5）。
 *
 * 引用里的图片地址是**后端代理的相对路径且刻意不带 token**（TECH_SPEC §8.1 的取舍：
 * 引用会存进 `chat_messages.citations`、留在浏览器历史与日志里，Token 不该跟着进库）。
 * 所以拼 token 是渲染时的责任：`<img>` 带不了 `Authorization` 头，只能用查询参数。
 */

import { getToken } from './token'

export function assetUrl(path) {
  if (!path) {
    return ''
  }
  const token = getToken()
  if (!token) {
    return path
  }
  return `${path}${path.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(token)}`
}
