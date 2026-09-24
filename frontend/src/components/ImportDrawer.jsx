/**
 * 导入抽屉（tasklist 14.5，PRD §6.3「导入抽屉」）。
 *
 * 四条规则值得写在代码前面：
 *
 * 1. **关闭抽屉不取消任务**。解析在 Celery 里跑，关掉只是不看了；重新打开时列表会把
 *    当前状态带回来，所以这里不持有任务句柄，也不阻止关闭。
 * 2. **只轮询非终态的行**，全部跑完就停掉定时器——否则一个开着不动的抽屉会一直打接口。
 * 3. **本地先挡一道**。格式与 20MB 上限在客户端先判，不合规的文件根本不投递，
 *    直接把原因写在行里；让用户等一轮网络再看「不支持」是浪费。
 * 4. **投递去重靠 ref 而不是 state**。拖拽会连续触发，两次 `startUpload` 读同一份旧
 *    state 就会把同一批文件投两遍；用 `inFlight` 记录在途的 key，重复的那次直接跳过。
 * 5. **`fromGapId` 只在「缺口转建」这一条路径上传**。带上它之后，导入完成的单元会被后端
 *    回填到对应的 `knowledge_gaps`（PRD §8.3）；普通的导入不传，行为完全不变。
 * 6. **选目录/拖拽的“跳过”不能只有一闪而过的 toast**。一个目录里往往大半是图片、附件，
 *    全摆进表格是噪声（所以只记一行汇总）；但“读到了几个、为什么跳过”必须留在界面上——
 *    否则用户看到空表格，分不清“没读到文件”、“全被滤掉了”还是“投递失败了”。
 * 7. **选目录不用 `<input webkitdirectory>`**。Chromium 在 Windows 上会把这个输入框的目录选择
 *    当成文件对话框开，带上一份文件类型过滤，目录里的文件整个列不出来（界面上就是
 *    「没有与搜索条件匹配的项」，实测 `sample-contracts` 里 7 个 pdf/docx 全部不可见）。
 *    改用 File System Access API（`showDirectoryPicker`）：它是纯目录选择、不做类型过滤，
 *    拿到句柄后自己递归枚举；没有这个 API 的浏览器（Firefox/Safari）再退回 webkitdirectory。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Input,
  Progress,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { DeleteOutlined, RedoOutlined } from '@ant-design/icons'
import { getImportStatus, importUnits, retryUnit } from '../api/knowledge'
import {
  ACCEPTED_EXTENSIONS,
  IMPORT_POLL_MS,
  MAX_BATCH_FILES,
  MAX_FILE_BYTES,
  STAGE_COLORS,
  STAGE_LABELS,
  TERMINAL_STAGES,
} from '../config/knowledge'
import { formatFileSize } from '../utils/format'

const { Text } = Typography

let seq = 0

function nextKey() {
  seq += 1
  return `import-${seq}`
}

function suffixOf(name) {
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot).toLowerCase()
}

/** 本地校验：返回原因字符串表示这个文件不能传（`rejected` 标记为真，不再重投）。 */
function localRejectReason(file) {
  if (!ACCEPTED_EXTENSIONS.includes(suffixOf(file.name))) {
    return `不支持的文件类型，只接受 ${ACCEPTED_EXTENSIONS.join(' / ')}`
  }
  if (file.size > MAX_FILE_BYTES) {
    return `超过单文件 ${formatFileSize(MAX_FILE_BYTES)} 上限`
  }
  if (file.size === 0) {
    return '文件是空的'
  }
  return null
}

function entryOf(file) {
  const reason = localRejectReason(file)
  return {
    key: nextKey(),
    file,
    name: file.name,
    size: file.size,
    unitId: null,
    parse_status: reason ? 'failed' : null,
    progress: 0,
    error: reason,
    rejected: Boolean(reason),
  }
}

/** 一次选目录/拖拽的结果汇总：读到几个、加入几个、每个原因跳过了多少个。 */
function PickSummary({ note, onClose }) {
  const { total, accepted, rejected, source } = note
  const skipped = rejected.length

  const groups = []
  for (const row of rejected) {
    const group = groups.find((item) => item.reason === row.error)
    if (group) {
      group.count += 1
      if (group.samples.length < 3) {
        group.samples.push(row.name)
      }
    } else {
      groups.push({ reason: row.error, count: 1, samples: [row.name] })
    }
  }

  const nothingRead = total === 0
  const message = nothingRead
    ? '这次没有读到文件'
    : accepted === 0
      ? `读到的 ${total} 个文件都不支持导入`
      : `读到 ${total} 个文件：加入 ${accepted} 个${skipped ? `，跳过 ${skipped} 个` : ''}`

  return (
    <Alert
      closable
      showIcon
      type={accepted === 0 ? 'warning' : 'info'}
      message={message}
      onClose={onClose}
      description={
        <Space direction="vertical" size={2}>
          {nothingRead ? (
            <>
              <Text type="secondary">入口：{source}</Text>
              <Text type="secondary">
                可能是目录本身为空，也可能是系统文件对话框把内容过滤掉了（Windows 会按应用记住
                文件类型过滤，目录选择框看着是空的，点确定也什么都拿不到）。
              </Text>
              <Text type="secondary">
                可靠的两种做法：把文件夹直接拖进上面的虚线框；或点「选择文件」，在对话框里 Ctrl+A 全选后打开。
              </Text>
            </>
          ) : (
            groups.map((group) => (
              <Text key={group.reason} type="secondary">
                跳过 {group.count} 个：{group.samples.join('、')}
                {group.count > group.samples.length ? ' 等' : ''}（{group.reason}）
              </Text>
            ))
          )}
        </Space>
      }
    />
  )
}

/** 递归枚举目录句柄下的全部文件（File System Access API，拿到的就是 File）。 */
async function filesUnderDirectory(directory) {
  const files = []
  // `values()` 是标准用法，旧一点的实现只有 `entries()`，两者都可能遇上
  const iterator =
    typeof directory.values === 'function' ? directory.values() : directory.entries()
  for await (const entry of iterator) {
    const handle = Array.isArray(entry) ? entry[1] : entry
    if (handle.kind === 'file') {
      files.push(await handle.getFile())
    } else if (handle.kind === 'directory') {
      files.push(...(await filesUnderDirectory(handle)))
    }
  }
  return files
}

/** 递归展开拖进来的目录项；浏览器不给 entry API 时退化成扁平文件列表。 */
function walkEntry(entry, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file(
        (file) => {
          out.push(file)
          resolve()
        },
        () => resolve(),
      )
      return
    }
    const reader = entry.createReader()
    const readBatch = () => {
      reader.readEntries(async (batch) => {
        if (batch.length === 0) {
          resolve()
          return
        }
        for (const child of batch) {
          await walkEntry(child, out)
        }
        // readEntries 一次只回一批，必须读到空数组才算读完
        readBatch()
      }, resolve)
    }
    readBatch()
  })
}

async function collectDropped(dataTransfer) {
  const entries = [...(dataTransfer.items ?? [])]
    .filter((item) => item.kind === 'file')
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean)
  if (entries.length === 0) {
    return [...(dataTransfer.files ?? [])]
  }
  const files = []
  for (const entry of entries) {
    await walkEntry(entry, files)
  }
  return files
}

export default function ImportDrawer({ open, onClose, onImported, initialFiles, fromGapId }) {
  const { message } = App.useApp()
  const [category, setCategory] = useState('')
  const [queue, setQueue] = useState([])
  const [dragging, setDragging] = useState(false)
  const [lastPick, setLastPick] = useState(null)
  const inFlight = useRef(new Set())
  const handledRef = useRef(null)
  const fileInputRef = useRef(null)
  const folderInputRef = useRef(null)

  const patchEntries = useCallback((keys, changes) => {
    setQueue((rows) =>
      rows.map((row) => (keys.includes(row.key) ? { ...row, ...changes(row) } : row)),
    )
  }, [])

  /** 投递一批已入队的文件；在途的行不会被重复投。 */
  const startUpload = useCallback(
    async (entries, presetCategory) => {
      const pending = entries.filter(
        (row) => row.file && !row.unitId && !row.rejected && !inFlight.current.has(row.key),
      )
      if (pending.length === 0) {
        return
      }
      for (const row of pending) {
        inFlight.current.add(row.key)
      }

      for (let offset = 0; offset < pending.length; offset += MAX_BATCH_FILES) {
        const batch = pending.slice(offset, offset + MAX_BATCH_FILES)
        try {
          const result = await importUnits(
            batch.map((row) => row.file),
            { category: presetCategory?.trim() || undefined, fromGapId },
          )
          // 服务端按上传顺序回传；先按文件名认领，重名时按顺序兜底
          const remaining = [...(result?.items ?? [])]
          patchEntries(
            batch.map((row) => row.key),
            (row) => {
              const index = remaining.findIndex((item) => item.source_filename === row.name)
              const picked = remaining.splice(index >= 0 ? index : 0, 1)[0]
              return {
                unitId: picked?.unit_id ?? null,
                parse_status: picked ? 'queued' : 'failed',
                progress: 0,
                error: picked ? null : '服务端没有返回这个文件的受理结果',
              }
            },
          )
        } catch (error) {
          patchEntries(batch.map((row) => row.key), () => ({
            parse_status: 'failed',
            error: error.message || '导入失败',
          }))
          message.error(error.message || '导入失败')
        } finally {
          for (const row of batch) {
            inFlight.current.delete(row.key)
          }
        }
      }
      onImported?.()
    },
    [fromGapId, message, onImported, patchEntries],
  )

  const addFiles = useCallback(
    (files, { silent = false, source = '选择文件' } = {}) => {
      const entries = files.map(entryOf)
      const valid = entries.filter((entry) => !entry.rejected)
      const rejected = entries.filter((entry) => entry.rejected)
      if (valid.length > 0) {
        setQueue((rows) => [...rows, ...valid])
      }
      if (rejected.length > 0 && !silent) {
        // 手动挑的文件逐个列出原因：用户就是奔着这个文件来的
        setQueue((rows) => [...rows, ...rejected])
      }
      // 一次读多个文件（选目录/拖拽）或筛掉过东西时，把结果留在界面上；
      // 单个文件顺利入队就不必再报一句。带上 source：出问题时能分清是哪条入口惹的祸
      if (silent || rejected.length > 0) {
        setLastPick({ total: entries.length, accepted: valid.length, rejected, source })
      }
      // 开始解析（分类已在上面选好）；本地校验没过的不进这一批
      startUpload(valid, category)
    },
    [category, startUpload],
  )

  const onDrop = useCallback(
    async (event) => {
      event.preventDefault()
      setDragging(false)
      const files = await collectDropped(event.dataTransfer)
      if (files.length > 0) {
        addFiles(files, { silent: true, source: '拖拽' })
      }
    },
    [addFiles],
  )

  /** 选目录：有 File System Access API 就走它，没有才退回 webkitdirectory（见文件头第 7 条）。 */
  const onPickFolder = useCallback(async () => {
    if (typeof window.showDirectoryPicker !== 'function') {
      folderInputRef.current?.click()
      return
    }
    try {
      const directory = await window.showDirectoryPicker({ id: 'kb-import', mode: 'read' })
      addFiles(await filesUnderDirectory(directory), {
        silent: true,
        source: '选择文件夹（File System Access）',
      })
    } catch (error) {
      if (error?.name !== 'AbortError') {
        message.error(error?.message || '读取文件夹失败')
      }
    }
  }, [addFiles, message])

  // 页面顶部「上传文档」带进来的文件
  useEffect(() => {    if (!open || !initialFiles || initialFiles === handledRef.current) {
      return
    }
    handledRef.current = initialFiles
    addFiles(initialFiles)
  }, [addFiles, initialFiles, open])

  const polling = queue.some((row) => row.unitId && !TERMINAL_STAGES.has(row.parse_status))

  useEffect(() => {
    if (!open || !polling) {
      return undefined
    }
    let cancelled = false
    const timer = setInterval(async () => {
      const targets = queue.filter(
        (row) => row.unitId && !TERMINAL_STAGES.has(row.parse_status),
      )
      const results = await Promise.all(
        targets.map((row) =>
          getImportStatus(row.unitId)
            .then((status) => ({ key: row.key, status }))
            .catch(() => null),
        ),
      )
      if (cancelled) {
        return
      }
      const patches = new Map(
        results
          .filter(Boolean)
          .map(({ key, status }) => [
            key,
            {
              parse_status: status.parse_status,
              progress: status.progress ?? 0,
              error: status.error ?? null,
            },
          ]),
      )
      if (patches.size > 0) {
        setQueue((rows) =>
          rows.map((row) => (patches.has(row.key) ? { ...row, ...patches.get(row.key) } : row)),
        )
      }
    }, IMPORT_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [open, polling, queue])

  const onRetry = useCallback(
    async (row) => {
      try {
        await retryUnit(row.unitId)
        patchEntries([row.key], () => ({ parse_status: 'queued', progress: 0, error: null }))
        message.success('已重新投递解析')
      } catch (error) {
        message.error(error.message || '重试失败')
      }
    },
    [message, patchEntries],
  )

  const columns = [
    { title: '文件名', dataIndex: 'name', ellipsis: true },
    { title: '大小', dataIndex: 'size', width: 96, render: (size) => formatFileSize(size) },
    {
      title: '阶段',
      dataIndex: 'parse_status',
      width: 120,
      render: (status) => (
        <Tag color={status ? (STAGE_COLORS[status] ?? 'default') : 'default'}>
          {status ? (STAGE_LABELS[status] ?? status) : '等待投递'}
        </Tag>
      ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 140,
      render: (progress, row) => (
        <Progress
          percent={row.parse_status === 'indexed' ? 100 : (progress ?? 0)}
          size="small"
          status={row.parse_status === 'failed' ? 'exception' : 'normal'}
        />
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, row) => (
        <Space size={0}>
          {row.parse_status === 'failed' && row.unitId && (
            <Button
              type="link"
              size="small"
              icon={<RedoOutlined />}
              onClick={() => onRetry(row)}
            >
              重试
            </Button>
          )}
          <Button
            type="link"
            size="small"
            icon={<DeleteOutlined />}
            aria-label={`从导入列表移除 ${row.name}`}
            onClick={() => setQueue((rows) => rows.filter((item) => item.key !== row.key))}
          >
            移除
          </Button>
        </Space>
      ),
    },
  ]

  const failed = queue.filter((row) => row.error).length
  const pending = queue.filter((row) => row.file && !row.unitId && !row.rejected).length

  return (
    <Drawer
      title="导入文档"
      width={760}
      open={open}
      onClose={onClose}
      footer={
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text type="secondary">关闭抽屉不会取消解析，任务在后台继续。</Text>
          <Button onClick={onClose}>关闭（后台继续）</Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Input
          addonBefore="分类"
          value={category}
          maxLength={64}
          placeholder="选填，例如 财务制度（导入时会写进这批文档）"
          onChange={(event) => setCategory(event.target.value)}
        />

        <div
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          style={{
            border: `1px dashed ${dragging ? '#1f1f1f' : '#d9d9d9'}`,
            background: dragging ? '#f0f7ff' : undefined,
            borderRadius: 8,
            padding: 16,
            textAlign: 'center',
          }}
        >
          <div style={{ marginBottom: 8 }}>拖拽文件或文件夹到此处（文件夹推荐直接拖进来）</div>
          <Space>
            {/* 两个隐藏 input 用 ref 触发：`<label>` 里包一个真实 `<button>` 时，浏览器不会把
                点击转给 label 关联的表单控件，按钮点了没反应。 */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_EXTENSIONS.join(',')}
              style={{ display: 'none' }}
              aria-label="选择要导入的文件"
              onChange={(event) => {
                addFiles(Array.from(event.target.files ?? []))
                event.target.value = ''
              }}
            />
            <Button onClick={() => fileInputRef.current?.click()}>选择文件</Button>
            {/* webkitdirectory 只当警备：支持 File System Access API 的浏览器走 showDirectoryPicker */}
            <input
              ref={folderInputRef}
              type="file"
              multiple
              webkitdirectory=""
              directory=""
              style={{ display: 'none' }}
              aria-label="选择要导入的文件夹"
              onChange={(event) => {
                addFiles(Array.from(event.target.files ?? []), {
                  silent: true,
                  source: '选择文件夹（webkitdirectory）',
                })
                event.target.value = ''
              }}
            />
            <Button onClick={onPickFolder}>选择文件夹</Button>
          </Space>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary">
              支持 PDF / MD / DOCX / TXT，单文件 ≤ {formatFileSize(MAX_FILE_BYTES)}，超过{' '}
              {MAX_BATCH_FILES} 个会自动分批投递
            </Text>
          </div>
        </div>

        {lastPick && (
          <PickSummary note={lastPick} onClose={() => setLastPick(null)} />
        )}

        {pending > 0 && (
          <Button type="primary" onClick={() => startUpload(queue, category)}>
            重新投递 {pending} 个未受理文件
          </Button>
        )}

        {failed > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`${failed} 个文件没成功`}
            description="原因见下表：类型/大小问题要换文件；解析失败可以点「重试」。"
          />
        )}

        <Table
          rowKey="key"
          size="small"
          columns={columns}
          dataSource={queue}
          pagination={false}
          locale={{
            emptyText: (
              <Empty description="还没有选择文件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          }}
        />
      </Space>
    </Drawer>
  )
}
