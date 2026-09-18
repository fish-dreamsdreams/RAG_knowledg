import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发服务器：/api 代理到本地 FastAPI（127.0.0.1:8000）
// ws: true 供问答 WebSocket 通道 /api/v1/ws/chat 使用（TECH_SPEC §4.1、§11）
//
// `test` 段是 vitest 的配置（tasklist 14.8）：跑在 jsdom 里，只用 `test/setup.js` 里那几
// 个 jsdom 缺失的浏览器 API，不引 MSW、不 mock 请求层——本层要证的是「拿到什么身份、
// 渲染出什么」，把网络也假掉就变成在测测试替身。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./test/setup.js'],
    include: ['test/**/*.test.{js,jsx}'],
    css: false,
  },
})
