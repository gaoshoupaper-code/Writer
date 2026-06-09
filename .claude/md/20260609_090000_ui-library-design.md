---
type: design
status: confirmed
created: 2026-06-09 09:00
require: 20260608_150000_ui-library-upgrade.md
related: []
---

# 前端 UI 库引入 — 技术设计方案

## 架构方向（待确认）

**推荐路径：shadcn/ui + Tailwind CSS**

核心逻辑：
- shadcn/ui 主题系统原生基于 CSS 变量 → 直接映射现有 `--teal`/`--coral`/`--ink` 体系
- 组件源码复制到项目（非 npm 依赖） → 完全可控，可魔改
- 基于 Radix UI 原语 → 键盘导航、焦点管理、ARIA 现成
- 需引入 Tailwind CSS → 可与现有 4000 行 CSS 无冲突共存

## 已确认决策

### D1. UI 库选型 → shadcn/ui + Tailwind CSS
- shadcn/ui 组件源码复制到项目，非 npm 依赖
- 基于 Radix UI 原语（键盘导航、焦点管理、ARIA）
- 内置全部所需组件：Toast、Tabs、Sheet、Dropdown、Collapsible、Skeleton、Tooltip、Dialog、Select 等

### D2. Tailwind CSS 引入策略 → 全局引入
- 用户选择：`@tailwind base` + `@tailwind utilities` 全局引入
- ⚠️ 风险标记：Tailwind base 层（preflight）会注入 CSS reset，可能影响现有组件
- **缓解方案**：在 `tailwind.config.ts` 中设置 `corePlugins: { preflight: false }` 禁用 reset，保留其他 base 功能
- 这样既满足"全局引入"的意图，又消除对现有 4000 行 CSS 的副作用

### D3. 主题 Token 映射 → 映射 shadcn Token 到现有变量
- 在 `:root` 中定义 shadcn 的语义 Token（`--primary`, `--background`, `--foreground`...）指向现有 CSS 变量
- 例如：`--primary: var(--teal)`, `--foreground: var(--ink)`, `--destructive: var(--danger)`
- 一次映射，所有 shadcn 组件自动继承暖色调体系

### D4. 组件文件组织 → `components/ui/`
- shadcn/ui 组件放入 `frontend/components/ui/`（button.tsx, toast.tsx, tabs.tsx...）
- 现有组件保持在 `frontend/components/workspace/`
- 新旧隔离，渐进替换期间互不干扰

### D5. 动画方案 → 纯 CSS transition/animation
- 利用 shadcn/ui 内置的 `data-[state=open]` 等 CSS 过渡
- shimmer 加载用 `@keyframes` 实现
- 不引入 framer-motion，零额外依赖
- 后续如需复杂动画编排，可按需引入

### D6. Tailwind preflight 补丁 → 禁用
- `tailwind.config.ts` 中设置 `corePlugins: { preflight: false }`
- 禁用 Tailwind CSS reset，保护现有 4000 行 CSS 不受影响
- `@tailwind base` 仍用于 CSS 变量声明等非 reset 功能

### D7. Toast 组件 → Sonner
- shadcn/ui 官方推荐的独立 Toast 库
- API：`toast("消息")` / `toast.error("出错了")` / `toast.success("完成")`
- 零配置、自带动画、Promise 状态支持、~3KB
- 无需 Provider 包裹，无需手动管理状态

## Phase 拆解（WBS）

### Phase 0：基础设施搭建
> 验证：构建通过，现有 UI 零变化

| Task | 内容 | 涉及文件 |
|---|---|---|
| 0.1 | 安装 Tailwind CSS（`tailwindcss`, `@tailwindcss/postcss`） | `package.json` |
| 0.2 | 创建 `postcss.config.mjs` | 新建 |
| 0.3 | 创建 `tailwind.config.ts`，配置 `preflight: false` + 内容路径 | 新建 |
| 0.4 | `globals.css` 顶部添加 `@import "tailwindcss"` + shadcn 主题 Token 映射 | `globals.css` |
| 0.5 | 初始化 shadcn/ui（`npx shadcn@latest init`） | `components.json` |
| 0.6 | **验证**：`npm run build` 通过，浏览器打开无视觉变化 | |

### Phase 1：Toast 通知（零冲突验证）
> 验证：错误/成功提示以 Toast 形式展示，底部 error-copy 移除

| Task | 内容 | 涉及文件 |
|---|---|---|
| 1.1 | 添加 Sonner 组件 + shadcn/ui sonner wrapper | `components/ui/` 新建 |
| 1.2 | 创建 Toast 工具函数（`toast.success()`, `toast.error()`） | `lib/toast.ts` 新建 |
| 1.3 | `page.tsx` 中替换所有 `setError()` 为 `toast.error()` | `page.tsx` |
| 1.4 | 移除底部的 `<p className="error-copy">` 渲染 | `page.tsx` |
| 1.5 | **验证**：触发错误场景 → Toast 弹出，页面底部无 error-copy | |

### Phase 2：TracePanel 升级
> 验证：TracePanel 功能不变，视觉和交互质量显著提升

| Task | 内容 | 涉及文件 |
|---|---|---|
| 2.1 | 添加 shadcn 组件：Tabs, DropdownMenu, Sheet, Collapsible, Badge, ScrollArea, Tooltip, Skeleton | `components/ui/` 新建 |
| 2.2 | 下拉选择器 → shadcn DropdownMenu | TracePanel 相关组件 |
| 2.3 | 检查系统 Tab 栏 → shadcn Tabs | `TracePanel.tsx` |
| 2.4 | 抽屉面板 → shadcn Sheet（带滑入/滑出动画） | `TraceChainDrawer.tsx` |
| 2.5 | 节点详情 → shadcn Collapsible | 相关组件 |
| 2.6 | 状态徽章 → shadcn Badge | 各 trace 子组件 |
| 2.7 | 滚动区域 → shadcn ScrollArea | 各列表区域 |
| 2.8 | 悬停提示 → shadcn Tooltip | 需要额外信息的元素 |
| 2.9 | 加载态 → Skeleton shimmer | `TracePanel.tsx` |
| 2.10 | **验证**：TracePanel 全功能正常，动画流畅，暗色模式无漏光 | |

### Phase 3：ChatPanel 升级
> 验证：聊天功能不变，消息体验和输入交互升级

| Task | 内容 | 涉及文件 |
|---|---|---|
| 3.1 | 添加 shadcn 组件：Textarea, Button, Skeleton | `components/ui/` 新建 |
| 3.2 | 消息气泡升级（精致圆角、阴影层次、出现动画） | `ChatPanel.tsx` |
| 3.3 | 工具状态指示器 → shimmer Skeleton + 状态图标 | `ChatPanel.tsx` / `ToolTree.tsx` |
| 3.4 | 输入框 → shadcn Textarea + 字符计数 + 快捷键提示 | `ChatPanel.tsx` |
| 3.5 | 发送/停止按钮 → shadcn Button + 状态反馈 | `ChatPanel.tsx` |
| 3.6 | **验证**：聊天全流程正常，动画流畅 | |

### Phase 4：整体布局升级
> 验证：布局功能不变，质感和专业度提升

| Task | 内容 | 涉及文件 |
|---|---|---|
| 4.1 | 添加 shadcn 组件：Select, Separator | `components/ui/` 新建 |
| 4.2 | Sidebar 导航项升级（选中态动画、hover 过渡） | `Sidebar.tsx` |
| 4.3 | TopBar 工作区切换器 → shadcn Select | `TopBar.tsx` |
| 4.4 | 面板分割 → shadcn Separator + 柔和分割线 | `AppShell.tsx` / `Sidebar.tsx` |
| 4.5 | 面板切换过渡动画（CSS transition） | `page.tsx` / `AppShell.tsx` |
| 4.6 | **验证**：整体布局功能正常，暗色模式可用 | |

## 设计已冻结

所有技术决策已确认，WBS 已落地。可进入实施阶段。
