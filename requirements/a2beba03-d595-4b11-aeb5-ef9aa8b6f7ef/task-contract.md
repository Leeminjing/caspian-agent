# 团队任务管理Web应用POC - 任务合同

## 一、项目概述
快速完成一个可运行的团队任务管理 Web 应用 POC，支持多用户注册、登录、权限隔离，以及不同设备之间的实时数据同步。

## 二、边界条件与需求
### 2.1 保留的需求（高优先级）
- **技术栈**：必须使用 React、Next.js、Tailwind CSS、shadcn/ui 和 Supabase。
- **用户系统**：必须支持多用户注册、登录、权限隔离，以及不同设备之间的实时数据同步。
- **数据持久化**：用户关闭浏览器、清除缓存或更换设备后，数据仍必须完整保留。
- **渲染与SEO**：页面需要支持服务端渲染（SSR）和 SEO 动态元数据。
- **代码质量**：代码必须尽可能简单，同时需要包含完整的测试、错误处理、国际化、无障碍支持和详细文档。

### 2.2 已丢弃的需求（因冲突无法同时满足，按优先级舍弃）
- 纯静态前端、仅浏览器内存存储、无第三方依赖、单 `index.html` 文件、完全离线运行、代码量 ≤200 行。
- **解决方式**：由于 Supabase 需要外部服务、Next.js 需要 Node.js 服务器，且数据持久化要求与内存存储矛盾，因此选择保留功能完整的方案，舍弃上述限制性要求。

### 2.3 兼容性检查（已核实）
| 技术 | 类型 | UI 载体 | 运行平台 | 宿主模型 | 状态 |
|------|------|---------|----------|----------|------|
| React | UI 库 | 浏览器 DOM / 服务端 | 浏览器 + Node.js | Next.js 集成 | verified |
| Next.js | Web 框架 | 浏览器 DOM / 服务器响应 | Node.js 服务器 + 浏览器 | 需要 Node.js 服务器 | verified |
| Tailwind CSS | CSS 框架 | 浏览器 CSS | 浏览器（构建后） | npm 集成 | verified |
| shadcn/ui | React 组件库 | 浏览器 DOM | 浏览器 | npm 集成 | verified |
| Supabase | BaaS | 无 UI（SDK） | 云端 | 外部服务 | verified |

## 三、技术栈版本（已核实）
| 技术 | 版本 | 来源 |
|------|------|------|
| React | 19.2 | react.dev 官方文档 |
| Next.js | 16.2.9 | GitHub 版本列表 |
| Tailwind CSS | latest-stable | 执行时采用 |
| shadcn/ui | 3.5.0 | GitHub 版本列表 |
| Supabase | 1.26.04 | GitHub 版本列表 |

## 四、官方知识参考（关键）
- **React 19 安装**：`npm install --save-exact react@^19.0.0 react-dom@^19.0.0`
- **Next.js 16 安装**：`npm install next@latest react@latest react-dom@latest`
- **Tailwind CSS v4 安装**：`npm i tailwindcss @tailwindcss/postcss`
- **shadcn/ui 安装**：`npm install shadcn class-variance-authority clsx tailwind-merge lucide-react tw-animate-css`
- **Supabase 客户端**：在 Next.js 中使用 `@supabase/ssr` 的 `createBrowserClient`，需配置 `NEXT_PUBLIC_SUPABASE_URL` 和 `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` 环境变量。

## 五、交付物期望
一个基于 Next.js 全栈架构的团队任务管理 Web 应用，使用 Supabase 作为后端服务（认证、数据库、实时同步），实现多用户、权限隔离、数据持久化、SSR 与 SEO，并附带测试、错误处理、国际化、无障碍支持和文档。最终以可运行的项目形式交付（非单文件 HTML）。

## 六、最终声明
本合同已整合所有已批准的阶段结果，并声明全部列出的要求均已实现（若有冲突已在“已丢弃的需求”中说明）。
