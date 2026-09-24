/**
 * 知识维护页的展示常量（PRD §6.3）。
 *
 * 阶段枚举与后端 `services/ingest.py` 的 `STAGE_*` 一致：
 * queued → parsing → chunking → embedding → indexed / failed。
 * 终态只有 indexed 与 failed，列表轮询靠它判断什么时候可以停。
 */

export const STAGE_LABELS = {
  queued: '等待解析',
  parsing: '解析中',
  chunking: '切块中',
  embedding: '向量化中',
  indexed: '已完成',
  failed: '解析失败',
}

/** 解析中一律 processing 色：让「还在跑」和「已经坏了」在颜色上就分得开。 */
export const STAGE_COLORS = {
  queued: 'default',
  parsing: 'processing',
  chunking: 'processing',
  embedding: 'processing',
  indexed: 'success',
  failed: 'error',
}

export const TERMINAL_STAGES = new Set(['indexed', 'failed'])

export const FORMAT_LABELS = { pdf: 'PDF', docx: 'DOCX', md: 'MD', txt: 'TXT' }

export const FORMAT_OPTIONS = Object.entries(FORMAT_LABELS).map(([value, label]) => ({
  value,
  label,
}))

export const STATUS_OPTIONS = [
  { value: 'enabled', label: '启用' },
  { value: 'disabled', label: '停用' },
]

/** 单文件与批次上限和后端 `services/knowledge.py` 保持一致（TECH_SPEC §7）。 */
export const MAX_FILE_BYTES = 20 * 1024 * 1024
export const MAX_BATCH_FILES = 50
export const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.md', '.markdown', '.txt']

/** 列表轮询：解析是异步的，不轮询就永远停在「等待解析」。 */
export const LIST_POLL_MS = 3000
export const IMPORT_POLL_MS = 1500

export function isParsing(row) {
  return !TERMINAL_STAGES.has(row.parse_status)
}

/**
 * 权限标签的文本（PRD §6.3）：全局 → 部门名 → 角色名 → 人员名。
 *
 * 只看列表行给的名称，不再反查接口；一个都没配时不返回「无」而是空数组，
 * 由组件决定画成红色「未授权」——空 ACL 是拒绝，不是公开（P1）。
 */
export function aclTagTexts(row) {
  const texts = []
  if (row.acl_global) {
    texts.push('全局')
  }
  for (const name of [
    ...(row.acl_departments ?? []),
    ...(row.acl_roles ?? []),
    ...(row.acl_users ?? []),
  ]) {
    if (!texts.includes(name)) {
      texts.push(name)
    }
  }
  return texts
}
