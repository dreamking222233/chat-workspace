# Plan：聊天页输入框、权限提示与模型选择

日期：2026-08-28

## 需求

1. `/chat` 底部输入框文字与输入框上下居中。
2. 「当前账户尚未开通使用权限，请联系管理员。」提示不够明显。
3. 右侧模型选择改为自定义组件，避免 Windows 原生 `select` 无样式。
4. 用户未授权时，点击模型选择不展示模型列表。

## 方案

- 输入框：全局 `box-sizing: border-box` 下，自动增高把 textarea 写成 24px，叠加不对称 padding 后文字偏上。改为与按钮同高（40px）、上下 padding 对称，`.composer` 垂直居中。
- 未授权提示：独立警告条（图标 + 红底/描边 + `role="alert"`），不再用 11px 灰色说明文字。
- 模型选择：新建 `SelectMenu`，触发器 + 绝对定位列表，样式与思考菜单一致，Windows / macOS 相同。
- 未授权：触发器仍可点，但不打开列表、不渲染 option；点击只强化权限提示。

## 涉及文件

- `src/components/SelectMenu.tsx`、`src/components/SelectMenu.test.tsx`
- `src/App.tsx`、`src/App.test.tsx`
- `src/styles.css`
- `md/plan-chat-composer-select-20260828.md`
- `md/impl-chat-composer-select-20260828.md`
- `md/review-chat-composer-select-20260828.md`
