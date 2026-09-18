/**
 * 控制台的主题令牌（视觉统一）。
 *
 * 为什么令牌挂在控制台子树、而不是根节点：登录页有一套自己的浅色设计，不能被这里动到。
 * ConfigProvider 是嵌套合并的，所以这些令牌只作用于控制台，且能覆盖挂在 portal 里的
 * 弹窗与抽屉——这正是不用纯 CSS 写死颜色的原因（CSS 选择器够不着 portal，令牌够得着）。
 *
 * 配色照参考站那套中性黑白：主色近黑（选中项、主按钮、链接、Tab 下划线都是它），分层只靠
 * 灰阶与发丝线，彩色留给状态胶囊（且描边不填充，见 console.css）。登录页不动，所以
 * 控制台变黑白、登录页仍是蓝——两处不共享令牌，互不牵连。
 *
 * 字号：`FONT_SCALE = 1.1` 即紧凑基准的 1.1 倍（正文约 15.4 / 表格约 14.3 / 辅助约 13.2）。
 * antd 令牌只收数字、读不了 CSS 变量，所以 `console.css` 里的 `--kb-scale` 必须与这里同步。
 */

/** 字号倍率。改这里，控制台整体字号跟着变（CSS 侧同步改 --kb-scale）。 */
export const FONT_SCALE = 1.1

/** 控件高度：紧凑基准下与 14px 正文配平（按钮、输入、下拉都是这个高） */
const CONTROL_HEIGHT = 32

export const CONSOLE_THEME = {
  token: {
    borderRadius: 8,
    borderRadiusLG: 12,
    boxShadowTertiary: '0 1px 2px rgba(15, 23, 42, 0.04)',
    colorBgLayout: '#f7f8fa',
    colorBorder: '#e6e6ea',
    colorBorderSecondary: '#f0f0f2',
    colorPrimary: '#1f1f1f',
    colorText: '#1f1f1f',
    colorTextSecondary: '#6b7280',
    colorTextTertiary: '#9ca3af',
    colorTextPlaceholder: '#6b7280',
    controlHeight: CONTROL_HEIGHT,
    controlHeightLG: CONTROL_HEIGHT + 8,
    controlHeightSM: 24,
    fontSize: 14 * FONT_SCALE,
  },
  components: {
    Button: {
      defaultActiveBorderColor: '#8b909a',
      defaultBorderColor: '#c4c7cf',
      defaultHoverBorderColor: '#a8adb8',
      dangerShadow: 'none',
      primaryShadow: 'none',
    },
    Card: {
      headerFontSize: 14.5 * FONT_SCALE,
      headerHeight: 44,
      paddingLG: 16,
    },
    Descriptions: {
      itemPaddingBottom: 12,
      labelColor: '#9ca3af',
    },
    Layout: {
      bodyBg: '#f7f8fa',
      headerBg: '#ffffff',
      headerHeight: 56,
      headerPadding: '0 20px',
      siderBg: '#ffffff',
      triggerBg: '#ffffff',
      triggerColor: '#6b7280',
      triggerHeight: 44,
    },
    Menu: {
      itemBorderRadius: 8,
      itemColor: '#6b7280',
      itemHeight: 38,
      itemHoverBg: '#f4f4f6',
      itemHoverColor: '#1f1f1f',
      itemMarginBlock: 2,
      itemMarginInline: 8,
      itemSelectedBg: '#1f1f1f',
      itemSelectedColor: '#ffffff',
    },
    Table: {
      cellPaddingBlock: 11,
      cellPaddingInline: 16,
      headerBg: '#fafafa',
      headerColor: '#6b7280',
      headerSplitColor: '#f0f0f2',
      rowHoverBg: '#fafafa',
    },
    Tabs: {
      inkBarColor: '#1f1f1f',
      itemColor: '#6b7280',
      itemSelectedColor: '#1f1f1f',
      titleFontSize: 14 * FONT_SCALE,
    },
  },
}
