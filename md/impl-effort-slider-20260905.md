# 思考等级滑块实施记录

- 功能名称：effort-slider
- 日期：20260905
- 任务级别：中型
- 当前状态：实施完成

## 1. 做了什么

把 `/chat` 组合区的思考等级从列表菜单改成可拖拽滑块卡片，视觉对齐 ChatGPT 官方思考滑块：白底圆角卡片、左侧闪电、居中等级名、紫蓝渐变轨道、白色圆钮、星点粒子。

档位契约不变：关闭 / `low` / `medium` / `high` / `xhigh`。发送和重新生成仍只在非关闭时携带 `reasoning_effort`。

## 2. 关键改动

- 新增 `src/components/EffortSlider.tsx`：拖拽连续跟手、松手吸附、键盘左右/Home/End、点击外部或 Escape 关闭。
- 卡片标题为「关闭思考」或「思考 + 彩色档位名」；触发器仍显示「思考」或「思考 · 低/中/高/超高」。
- `src/styles.css` 增加渐变填充、粒子漂浮、打开缩放、拖拽时拇指放大。
- 窄屏卡片改为相对视口居中，避免溢出；420px 以下只隐藏「图片」按钮，保留思考图标。
- `src/App.test.tsx` 改为通过 slider 键盘选档；新增组件测试覆盖拖拽吸附、禁用、外部关闭。

## 3. 验证

- `npx tsc -b` 通过
- `npx vitest run src/components/EffortSlider.test.tsx src/App.test.tsx`：29 通过
- Chrome 实机：拖到低/中/高/超高，标题、按钮文案与 `aria-valuetext` 一致；390px 宽度下卡片完整可见
