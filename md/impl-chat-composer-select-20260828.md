# 聊天页输入框、权限提示与模型选择实施记录

日期：2026-08-28

## 本轮范围

只改 `/chat` 底部输入区视觉与交互：

1. 输入文字与输入框垂直居中。
2. 未授权提示更明显。
3. 模型选择改为自定义下拉，Windows / macOS 样式一致。
4. 未授权时点击不展示模型列表。

## 文件变更

- `src/components/SelectMenu.tsx`：新建自定义下拉（触发器 + 面板 + 键盘 Escape / 点击外部关闭）。
- `src/components/SelectMenu.test.tsx`：打开选中、锁定不渲染 option。
- `src/App.tsx`：输入框自动高度改为 40px；未授权警告条；原生 `select` 换成 `SelectMenu`。
- `src/App.test.tsx`：模型选择改为点击 option；增加未授权不泄露模型名用例。
- `src/styles.css`：composer 垂直居中、对称 padding、警告条、`.select-menu*`。
- `md/plan-chat-composer-select-20260828.md`

## 已实现行为

- textarea `min-height: 40px`，上下 padding 均为 9px，`line-height: 22px`，与加号/发送按钮同高；`.composer` `align-items: center`。
- 未授权：`role="alert"` 红底描边条，13px / 字重 600，带屏蔽图标。
- 模型选择：自定义列表，含「自动模型」标题行和带勾选的选项；不再使用原生 `select`。
- `locked` 时不打开 listbox、不渲染 option；点击只弹出权限 toast。

## 验证

- `npx tsc -b`：通过。
- `npm test -- src/components/SelectMenu.test.tsx src/App.test.tsx`：23 passed。
- Playwright：textarea/加号/发送相对输入框中心偏差 0；已授权打开 17 个 option；未授权横幅强调且点击不出现 listbox。
