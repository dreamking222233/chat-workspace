# 首页与用户管理页 UI/UX 优化实施记录

日期：2026-08-28

## 本轮范围

仅优化两个前端页面的视觉、信息层次和布局，不改业务接口：

1. 首页 `/`：原先只有导航、标题、一句介绍、一个按钮和三列文字，补充装饰与分区。
2. `/admin/users`：用户列表表头与单元格错位、内容被挤到右侧，整理为列对齐的表格布局。

## 文件变更

- `src/components/LandingPage.tsx`：新建。从 `Root.tsx` 抽出落地页，增加 sticky 导航、光晕/网格/浮动球装饰、双 CTA、能力标签、工作区预览窗、数据亮点、六宫格能力、三步流程、文本/图片分区、底部行动条和页脚。
- `src/Root.tsx`：默认路由改为渲染 `LandingPage`。
- `src/components/AdminUsersPage.tsx`：表格行类名从 `.user-row` 改为 `.admin-user-row`，补 `role="table/row/columnheader/cell"`，用户列文本包进 `.user-cell-text`。
- `src/styles.css`：落地页整套样式；管理表格改为 5 列 grid，并与聊天页 `.user-row` 解耦；补长邮箱截断、窄屏导航不溢出、页脚宽度对齐。
- `md/plan-home-admin-ui-20260828.md`：方案。
- `md/impl-home-admin-ui-20260828.md`、`md/review-home-admin-ui-20260828.md`：实施与审查记录。

## 已实现行为

### 首页

- 保留 ChatGPT 品牌、白底、黑按钮和绿色徽章语言。
- Hero 增加光晕、网格、左右浮动球；主按钮按登录态切换「开始使用」/「进入工作区」，次按钮「了解功能」滚到能力区（尊重 `prefers-reduced-motion`）。
- 能力标签：流式回复、项目归档、文本/图片、OpenAI 协议。
- 工作区预览窗模拟侧栏、对话气泡和输入条，两侧浮动卡片说明流式输出和图文一体。
- 三列亮点：双模态、项目制、可管理。
- 六宫格：自然对话、项目管理、多模型渠道、图片生成、授权管控、使用可追溯。
- 三步：进入工作区 → 组织项目 → 连接模型。
- 文本/图片对照区、底部 CTA（未登录「免费注册」，已登录「进入工作区」）、页脚。
- 管理员页脚入口指向 `/admin/users`。
- `980px` 隐藏导航锚点与用户名，操作区不换行，避免 sticky 顶栏纵向溢出；`760px` 分区宽度与 banner/footer 对齐。

### 用户管理页

- 根因：聊天气泡 `.user-row { display: flex; justify-content: flex-end }` 覆盖了管理表格同名 class，整行被推到右侧。
- 处理：管理行改名为 `.admin-user-row`，聊天 `.user-row` 继续右对齐用户气泡。
- 5 列 grid：`minmax(0, 1.8fr) 88px 128px 88px 210px`，`align-items: center`、`justify-items: start`。用户/角色/到期/状态与表头左对齐；操作列右对齐。
- 用户列内部 `.user-cell-text` 使用 `flex: 1; min-width: 0; overflow: hidden`，长邮箱省略号截断，不侵入下一列。
- 「居中对齐」指表头与单元格列对齐、行内垂直居中，不是把每个单元格文字水平居中。

## 验证

- `npx tsc -b`：通过。
- `npm run build`：通过，产物 `dist/assets/index-B73utpxQ.js`、`dist/assets/index-WtUGoH7f.css`。
- 前端 `http://localhost:4180/`：`200 OK`。
- 后端 `http://localhost:8000/health`：`{"status":"ok","service":"Chat Workspace API"}`。
- Playwright（系统 Chrome `/Applications/Google Chrome.app/...`，脚本 `/tmp/ui-verify-home-admin.mjs`）：全部通过，报告 `/tmp/ui-verify-20260828/report.json`。

关键检查：

| 检查 | 结果 |
| --- | --- |
| 首页导航 / Hero / 预览窗 / 6 张能力卡 / 3 步流程 / 页脚 | 存在 |
| 未登录 Hero CTA | 「开始使用」 |
| 「了解功能」滚动 | `.landing-shell.scrollTop` 0 → 933，能力区 `top ≈ 80` |
| 登录 CTA | 进入 `/login` |
| 管理员登录 | 进入管理端 |
| 表格 `display` | `grid`，`480.812px 88px 128px 88px 210px` |
| 5 列表头/单元格 left | 全部 `delta: 0` |
| 首列未被挤出表格 | `firstPad: 19` |
| 长邮箱 | `overflowX: true`，`overlapsNext: false` |
| 详情弹窗 | 可打开 |
| 聊天 `.user-row` | `display: flex; justify-content: flex-end` |
| 已登录 Hero CTA | 「进入工作区」 |
| 页脚管理控制台 | 指向 `/admin/users` |
| 已登录 390 宽顶栏 | `overflowX/Y: false`，高度 64 |

## 收尾修复

前端分析指出的阻塞项已在源码中处理，并二次验证：

- 用户列内部 span 未截断：改为 `.user-cell-text` 纵向 flex + `max-width: 100%` 省略号。验证脚本使用足够长的邮箱字符串，避免短串在宽列上假阴性。
- 已登录窄屏顶栏溢出 2px：`980px` 隐藏 `.landing-user`，操作区 `flex-wrap: nowrap`。
- 已登录 Hero 仍显示「开始使用」：按 `user` 切换文案。
- 页脚 `760px` 宽度与 stats/banner 不一致：统一 `calc(100% - 32px)`。
- 页脚管理入口路径不一致：统一 `/admin/users`。
- 滚动检测误用 `window.scrollY`：首页滚动容器是 `.landing-shell`（`body { overflow: hidden }`），改为读 `scrollTop`。

## 已知限制

- 本次只改首页和 `/admin/users` 视觉，其它管理页未做同样对齐。
- Playwright 使用本机 Chrome，未做像素级视觉回归基线。
- 管理表格桌面最小列宽约 854px，窄于该宽度时走 `760px` 卡片化断点，中间三列隐藏。
