# Plan：首页与用户管理页 UI/UX 优化

日期：2026-08-28

## 需求

1. 首页 `/` 过简，需要增加元素、装饰和内容层次，丰富视觉与信息。
2. `/admin/users` 列表布局错乱，表头与行内容未居中对齐、被挤到右侧。

## 原因

- 首页 `LandingPage` 仅有导航、标题、一句介绍、一个按钮和三列文字，缺少分区、装饰与产品预览。
- 用户管理表格使用 `.user-row`，与聊天页用户气泡 `.user-row { display: flex; justify-content: flex-end }` 类名冲突，后声明样式覆盖了管理表格的 grid 布局。

## 方案

### 首页

- 保留现有品牌（ChatGPT）、白底黑按钮、绿色徽章语言。
- 增加：背景光晕与网格装饰、双 CTA、能力标签、对话工作区预览、数据亮点、六宫格能力、三步流程、文本/图片能力区、底部行动条和页脚。
- 将 `LandingPage` 抽到 `src/components/LandingPage.tsx`，避免继续堆在 `Root.tsx`。

### 用户管理页

- 管理表格行改名为 `.admin-user-row`，与聊天 `.user-row` 解耦。
- 统一全宽 grid：用户列左对齐并垂直居中，角色/到期/状态与表头同列对齐，操作列右对齐。
- 统计卡、工具栏与表格间距一并整理。

## 涉及文件

- `src/components/LandingPage.tsx`（新建）
- `src/Root.tsx`
- `src/components/AdminUsersPage.tsx`
- `src/styles.css`
- `md/plan-home-admin-ui-20260828.md`
- `md/impl-home-admin-ui-20260828.md`

## 验证

- 首页桌面/移动：装饰、分区、CTA、登录态按钮。
- `/admin/users`：表头与数据列对齐、垂直居中；聊天页用户气泡仍靠右。
- `npm run build`。
