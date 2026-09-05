# 思考等级滑块实施方案

- 功能名称：effort-slider
- 日期：20260905
- 任务级别：中型（聊天组合区交互、自定义组件、动效与测试）
- 当前状态：实施完成

## 1. 用户原始需求

把 `/chat` 的思考等级选择改成可拖拽滑块，带动态效果，视觉与交互对齐 ChatGPT 官方思考滑块（白卡片、闪电图标、彩色等级名、紫蓝渐变轨道、白色圆钮、星点粒子）。

## 2. 技术方案

1. 新增 `EffortSlider` 组件，保留现有五档契约：关闭 / `low` / `medium` / `high` / `xhigh`。
2. 触发器仍是组合区「思考」按钮；展开后为浮动卡片，不再使用 `menuitemradio` 列表。
3. 轨道可点击、可拖拽；拖动时拇指连续跟随，松手吸附到最近档位；键盘左右/Home/End 调档。
4. 轨道按档位展开紫蓝渐变，粒子密度与标题颜色随等级变化；打开卡片带缩放淡入。
5. 点击外部或 Escape 关闭；图片模式禁用；打开模型选择时关闭滑块。
6. 发送/重新生成仍只在非关闭时携带 `reasoning_effort`。

## 3. 涉及文件

- `src/components/EffortSlider.tsx`
- `src/components/EffortSlider.test.tsx`
- `src/App.tsx`
- `src/App.test.tsx`
- `src/styles.css`
- `md/impl-effort-slider-20260905.md`
- `md/review-effort-slider-20260905.md`

## 4. 实施步骤

- [x] 实现滑块组件（拖拽、吸附、键盘、粒子）
- [x] 替换组合区旧菜单并更新样式
- [x] 更新前端测试
- [x] 浏览器验证拖拽与档位请求
