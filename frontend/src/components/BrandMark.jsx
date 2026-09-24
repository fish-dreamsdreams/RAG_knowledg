/**
 * 软件图标（登录页、侧栏、与 favicon 同一张图）。
 */

import './brand-mark.css'

const ICON_SRC = '/app-icon.webp'

export default function BrandMark({ size = 26 }) {
  return (
    <img
      className="kb-brand"
      src={ICON_SRC}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      draggable="false"
    />
  )
}
