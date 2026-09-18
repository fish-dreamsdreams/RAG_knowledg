/** 展示用格式化：日期与文件大小（PRD §6.3 的「更新时间」列与导入进度表）。 */

function pad(value) {
  return String(value).padStart(2, '0')
}

/** `2026-09-15T10:20:30Z` → `09-15 18:20`（本地时区，列表里跨年信息不重要）。 */
export function formatDateTime(value) {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return '—'
  }
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatFileSize(bytes) {
  if (bytes === null || bytes === undefined) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 编号列：uuid 太长，只取前 8 位，完整值放 Tooltip（列表本身不提供业务编号）。 */
export function shortId(value) {
  return value ? String(value).slice(0, 8) : '—'
}
