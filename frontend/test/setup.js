/**
 * vitest 的 jsdom 环境补丁（tasklist 14.8）。
 *
 * jsdom 只实现 DOM 标准里与布局无关的部分，antd 依赖的几个浏览器 API 它是空的：不补，
 * 渲染 Menu/Dropdown 时会直接抛 `matchMedia is not a function`，断言还没跑就被环境挡住。
 * 这里只补 API 存在性，不做任何业务替身——真正的假件在各用例里显式声明。
 */

import '@testing-library/jest-dom/vitest'

// antd 的响应式工具（Grid、Sider 折叠）用它判断断点
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

// antd 的部分组件用它观察尺寸（jsdom 里没有布局，回调不会真的触发）
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}

    unobserve() {}

    disconnect() {}
  }
}
