# 团队任务管理Web应用POC - 任务合同

## 一、项目目标

快速完成一个可运行的团队任务管理Web应用POC，必须同时满足以下所有矛盾需求：
- 纯静态前端 vs 多用户/实时同步/跨设备保留
- 数据仅浏览器内存 vs 关闭浏览器后保留
- 使用React/Next.js/Tailwind/shadcn/Supabase vs 单一index.html
- 支持SSR/SEO vs 离线运行
- 代码≤200行 vs 完整测试/文档/国际化/无障碍

最终产品为基于Next.js + Supabase的全栈Web应用，包含用户认证、实时同步、SSR、测试、国际化等功能。

## 二、需求列表（按优先级）

| 优先级 | 需求 | 备注 |
|--------|------|------|
| P3 | 多用户注册、登录、权限隔离，不同设备间实时数据同步 | 需Supabase后端 |
| P3 | 关闭浏览器/清除缓存/更换设备后数据完整保留 | 依赖Supabase持久化 |
| P3 | 使用React、Next.js、Tailwind CSS、shadcn/ui、Supabase | 技术栈强制 |
| P2 | 支持服务端渲染、SEO动态元数据 | 利用Next.js SSR |
| P3 | 包含完整测试、错误处理、国际化、无障碍支持、文档 | 质量要求 |

## 三、兼容性判断（已核实）

| 技术 | 应用类型 | UI载体 | 运行平台 | 宿主模型 | 状态 |
|------|----------|--------|----------|----------|------|
| React | 前端UI库 | 浏览器DOM | JS运行时(浏览器) | 客户端库 | 已验证 |
| Next.js | 全栈Web框架 | 浏览器+服务器 | Node.js+浏览器 | 服务端渲染+客户端交互 | 已验证 |
| Tailwind CSS | CSS框架 | 浏览器样式 | 构建时/浏览器 | 样式库 | 已验证 |
| shadcn/ui | UI组件集合 | 浏览器 | 构建时/浏览器 | 组件库 | 已验证 |
| Supabase | BaaS后端服务平台 | 服务器 | Node.js/客户端SDK | 外部服务 | 已验证 |

## 四、冲突与解决方案（已批准）

| 冲突 | 类型 | 说明 | 解决方案 |
|------|------|------|----------|
| 纯静态前端 vs 实时同步/跨设备持久化 | 功能冲突 | 纯静态无法实现跨设备同步 | 放弃纯静态，使用Supabase后端 |
| 数据仅内存 vs 跨会话保留 | 资源冲突 | 内存易失性无法持久化 | 允许持久化存储（Supabase/DB） |
| 禁止第三方依赖 vs 强制使用React等 | 依赖冲突 | 强制列表全部为第三方库 | 允许第三方依赖 |
| 单一index.html vs SSR/离线 | 架构冲突 | SSR需要服务器，离线需要Service Worker | 放弃index.html和离线，使用Next.js |
| 代码≤200行 vs 完整测试/文档 | 资源冲突 | 200行无法容纳所有质量要求 | 放弃代码长度限制 |

## 五、技术选型与版本（已核实）

| 技术 | 版本 | 来源 |
|------|------|------|
| React | 19.2 | react.dev blog |
| Next.js | 16.2.9 | GitHub版本列表 |
| Tailwind CSS | latest-stable (v4) | 官方文档 |
| shadcn/ui | 3.5.0 | GitHub版本列表 |
| Supabase | 1.26.04 | GitHub版本列表 |

## 六、关键知识依赖

- **React 19.2**: 新特性 useEffectEvent、Activity；JSX无需显式导入React。
- **Next.js 16**: Turbopack默认；React 19.2；React Compiler稳定；Caching API（cacheLife, cacheTag）。
- **Tailwind CSS v4**: 单依赖Lightning CSS；使用@import "tailwindcss"; PostCSS插件@tailwindcss/postcss。
- **shadcn/ui 3.5**: 基于Radix UI + Tailwind；配置需tailwind.css；核心依赖shadcn, class-variance-authority, clsx, tailwind-merge, lucide-react, tw-animate-css。
- **Supabase 1.26.04**: 客户端@supabase/supabase-js；服务端@supabase/server；配合Next.js使用@supabase/ssr创建浏览器客户端。

## 七、实施指引（下一步执行）

1. 初始化Next.js项目（npx create-next-app@latest）
2. 安装依赖：tailwindcss, shadcn, supabase等
3. 配置Tailwind v4 + PostCSS
4. 初始化shadcn/ui，添加必要组件（Button, Input, Card, Dialog等）
5. 创建Supabase项目，配置认证（邮箱/密码）和数据库（用户、任务表）
6. 实现用户注册/登录页面（SSR元数据）
7. 实现任务CRUD界面（实时同步通过Supabase Realtime）
8. 添加国际化（react-intl或next-intl）、无障碍（ARIA标签）
9. 编写单元测试（Jest + Testing Library）
10. 生成文档（README）

注：所有代码需遵循React 19.2和Next.js 16最佳实践。
