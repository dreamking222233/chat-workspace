# 首页与用户管理页 UI/UX 最终审查

日期：2026-08-28

## 审查结论

通过。首页从单屏介绍页扩展为带装饰与分区的落地页；`/admin/users` 因 `.user-row` 与聊天气泡类名冲突导致的整行右偏已解除，五列表头与数据列 left 对齐差值为 0，长邮箱不再侵入下一列。聊天页用户气泡仍靠右。

## 已覆盖需求

- 首页增加元素、装饰和内容层次，不再只有标题和三列文字。
- `/admin/users` 表头与单元格按列对齐、行内垂直居中；操作列右对齐。
- 登录态 CTA、管理员页脚入口、窄屏顶栏不溢出。
- 聊天页 `.user-row` 布局未被管理表格改动破坏。

## 审查过程

### 第 1 轮（实现对照方案）

- `LandingPage` 抽出独立组件，Hero 装饰、预览窗、能力/流程/工作区/CTA/页脚齐全。
- 管理表格行改名为 `.admin-user-row`，5 列 grid 对齐。
- 初次 Playwright：滚动检查误用 `window.scrollY`（假阴性）；聊天页空会话没有用户气泡，探针确认 `.user-row` 仍为 `flex-end`。

### 第 2 轮（前端分析 + 回归）

阻塞项：

- `.user-cell` 内层 span 未设 `min-width: 0` / `overflow: hidden`，长邮箱会越过列边界。
- 已登录移动端顶栏 `actionsBottom: 66` 对 `navBottom: 64`，纵向溢出 2px。

相关项：

- 已登录 Hero CTA 仍写死「开始使用」。
- 页脚 `760px` 宽度与其它区块不一致。
- 页脚管理入口路径不一致。

处理：

- `.user-cell-text` 改为纵向 flex，`flex: 1; min-width: 0; overflow: hidden; max-width: 100%`，名称和邮箱省略号截断。
- `980px` 隐藏 `.landing-user`，`.landing-nav-actions` `flex-wrap: nowrap`。
- Hero / Banner CTA 按登录态切换；页脚管理入口 `/admin/users`；footer 宽度与 banner/stats 对齐。
- 滚动检测改为 `.landing-shell.scrollTop`。
- 长邮箱用例改为足够长的字符串，避免宽列上假阴性。

二次 Playwright：全部 PASS，`ok: true`，`failed: []`。

## 验证结果

| 项目 | 结果 |
| --- | --- |
| `npx tsc -b` | 通过 |
| `npm run build` | 通过（`index-B73utpxQ.js` / `index-WtUGoH7f.css`） |
| 前端 HTTP `http://localhost:4180/` | `200 OK` |
| 后端健康检查 | `status=ok` |
| Playwright 首页结构 | 导航、Hero 光晕、预览窗、6 卡、3 步、页脚均存在 |
| Playwright 对齐 | 5 列 `delta: 0`，`display: grid` |
| Playwright 长邮箱 | 截断且不重叠下一列 |
| Playwright 已登录 CTA / 页脚 | 「进入工作区」、管理控制台 → `/admin/users` |
| Playwright 390 宽顶栏 | 不溢出，高度 64 |
| 聊天 `.user-row` | `flex` + `flex-end` |

对齐实测（1440 宽桌面）：

| 列 | headLeft | rowLeft | delta |
| --- | --- | --- | --- |
| 用户 | 305 | 305 | 0 |
| 角色 | 801 | 801 | 0 |
| 授权到期 | 905 | 905 | 0 |
| 状态 | 1049 | 1049 | 0 |
| 操作 | 1153 | 1153 | 0 |

首单元格相对表格左缘内边距 19px，符合 `padding: 12px 18px`。

## 残余风险

- 其它管理列表（用量、模型渠道）仍可能存在类似密度问题，本轮未改。
- 管理表格桌面列模板最小约 854px；介于约 760–900px 时可能出现横向滚动，再窄则走卡片化断点。
- Playwright 未建立像素基线，后续视觉回归需另存截图对照。
- 首页滚动发生在 `.landing-shell`，后续脚本不要再用 `window.scrollY`。
