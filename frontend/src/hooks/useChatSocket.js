/**
 * 问答 WebSocket 传输层（tasklist 14.4，TECH_SPEC §4.6）。
 *
 * 只做连接、心跳与帧的收发：节点顺序、状态机、气泡渲染都在页面里。握手用查询参数
 * `?access_token=`——浏览器的 WebSocket 构造器不能自定义请求头（与图片代理同一套口径）。
 *
 * 三个必须解释的点：
 * 1. **`ask` 时没连上就先排队**。空闲 10 分钟服务端会关连接（`4408`），下次提问不该先报错
 *    再让用户重按；排队后 `open` 时补发，用户只感觉到一次正常的等待。
 * 2. **主动断开要把 `onclose` 摘掉**。否则组件卸载时会回调一次「连接关闭」，页面把它当成
 *    「生成中断」，用户切走页面再回来会看到一条假的失败气泡。
 * 3. **只投递事件，不解释事件**。关闭码的含义（`4401` 弹登录、`4403` 无权限）由页面决定，
 *    传输层多一个判断就会多一处和 PRD 文案不一致的地方。
 */

import { useCallback, useEffect, useRef } from 'react'
import { getToken } from '../utils/token'

/** 心跳间隔：服务端空闲超时 10 分钟，30s 一次足够把连接保活，也不至于刷日志。 */
const PING_INTERVAL_MS = 30000

function socketUrl() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const token = encodeURIComponent(getToken())
  return `${scheme}://${window.location.host}/api/v1/ws/chat?access_token=${token}`
}

export function useChatSocket({ onEvent, onClose }) {
  const socketRef = useRef(null)
  const pingRef = useRef(null)
  const pendingRef = useRef(null)
  const handlersRef = useRef({ onEvent, onClose })

  useEffect(() => {
    handlersRef.current = { onEvent, onClose }
  }, [onEvent, onClose])

  const stopPing = () => {
    if (pingRef.current) {
      clearInterval(pingRef.current)
      pingRef.current = null
    }
  }

  const connect = useCallback(() => {
    if (socketRef.current) {
      return socketRef.current
    }
    const socket = new WebSocket(socketUrl())
    socketRef.current = socket

    socket.onopen = () => {
      stopPing()
      pingRef.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }))
        }
      }, PING_INTERVAL_MS)

      const queued = pendingRef.current
      pendingRef.current = null
      if (queued) {
        socket.send(queued)
      }
    }

    socket.onmessage = (event) => {
      let payload
      try {
        payload = JSON.parse(event.data)
      } catch {
        // 服务端只发 JSON；解不开说明协议对不上，丢掉这一帧比整页崩掉好
        return
      }
      handlersRef.current.onEvent?.(payload)
    }

    socket.onclose = (event) => {
      stopPing()
      if (socketRef.current === socket) {
        socketRef.current = null
      }
      handlersRef.current.onClose?.(event.code)
    }

    return socket
  }, [])

  const disconnect = useCallback(() => {
    stopPing()
    const socket = socketRef.current
    socketRef.current = null
    if (socket) {
      // 主动关闭不是「中断」：摘掉回调，避免卸载时回调一次 onClose
      socket.onclose = null
      socket.close()
    }
  }, [])

  /** 发一轮提问。未连接时排队并在连上后补发。 */
  const ask = useCallback(
    ({ content, sessionId }) => {
      const frame = JSON.stringify({
        type: 'ask',
        session_id: sessionId ?? null,
        content,
      })
      const socket = socketRef.current
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(frame)
        return
      }
      pendingRef.current = frame
      if (!socket) {
        connect()
      }
    },
    [connect],
  )

  useEffect(() => disconnect, [disconnect])

  return { ask, connect, disconnect }
}
