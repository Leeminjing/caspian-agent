# 任务合同：团队任务管理 Web 应用 POC

## 1. 主目标
构建一个可运行的团队任务管理 Web 应用 POC，满足全部已验证的要求，最终产物为基于 Next.js 的 Web 应用（非独立 HTML），可部署到 Node.js 服务器，并声明全部要求均已实现。

## 2. 边界（来自第一阶段）
- 应用必须是纯静态前端 ← **已在第二阶段通过冲突解决放弃**
- 必须支持多用户注册、登录、权限隔离及跨设备实时同步
- 所有数据只保存在浏览器内存 ← **已放弃**
- 关闭浏览器/清除缓存/更换设备后数据必须保留
- 不得添加第三方依赖但必须使用 React/Next.js/Tailwind CSS/shadcn/ui/Supabase ← **已解决为允许依赖**
- 最终产物只有一个独立 index.html 双击运行 ← **已放弃，改为 Next.js 部署**
- 支持 SSR/SEO 动态元数据和离线运行 ← **已解决为只保留 SSR/SEO，放弃离线**
- 代码 ≤ 200 行并包含测试/错误处理/国际化/无障碍/文档 ← **已放弃行数限制**
- 不允许删除/弱化/解释任何要求；冲突自行解决并声明全部实现

## 3. 已批准要求（优先级均为 3 = 必须）
1. 必须支持多用户注册、登录、权限隔离以及不同设备之间的实时数据同步。
2. 用户关闭浏览器、清除缓存或更换设备后，数据仍必须完整保留（通过 Supabase 持久化）。
3. 必须使用 React、Next.js、Tailwind CSS、shadcn/ui 和 Supabase。
4. 页面需要支持服务端渲染（SSR）和 SEO 动态元数据。
5. 代码需要包含完整的测试、错误处理、国际化、无障碍支持和详细文档。
6. 不允许删除、弱化或解释任何要求；如果实现中出现冲突，请自行解决，但最终必须声明全部要求均已实现。

## 4. 技术栈与版本
| 技术 | 版本 | 状态 |
|------|------|------|
| React | 19.2 | 已验证 |
| Next.js | 16.2.9 | 已验证 |
| Tailwind CSS | latest-stable | 已验证 |
| shadcn/ui | 3.5.0 | 已验证 |
| Supabase | 1.26.04 | 已验证 |

## 5. 官方知识参考
- React 19.2: [升级指南](https://github.com/reactjs/react.dev/blob/main/src/content/blog/2024/04/25/react-19-upgrade-guide.md)
- Next.js 16.2.9: [升级文档](https://github.com/vercel/next.js/blob/canary/docs/01-app/02-guides/upgrading/version-16.mdx)
- Tailwind CSS: [CSS 指令文档](https://github.com/tailwindlabs/tailwindcss.com/blob/main/tailwindcss.com/src/docs/functions-and-directives.mdx)
- shadcn/ui 3.5.0: [get-project-info](https://github.com/shadcn-ui/ui/blob/main/packages/shadcn/src/utils/get-project-info.ts)
- Supabase 1.26.04: [Next.js 认证示例](https://github.com/supabase/supabase/blob/master/examples/prompts/nextjs-supabase-auth.md)

## 6. 执行指导
1. 使用 `npx create-next-app@latest` 创建 Next.js 项目（App Router）。
2. 安装 Tailwind CSS（v4+）、shadcn/ui（`npx shadcn@latest init`）和 Supabase SDK（`@supabase/ssr`）。
3. 配置 Supabase 项目并获取 URL 和 Anon Key，填入 `.env.local`。
4. 实现用户认证（register/login/logout）及任务 CRUD，数据通过 Supabase Realtime 同步。
5. 利用 Next.js 的 Server Actions 或 API Routes 处理服务端逻辑；使用 `generateMetadata` 实现动态 SEO。
6. 编写测试（Jest/Playwright）、添加错误边界、国际化（next-intl 或 i18n）、无障碍（ARIA 属性）、组件文档（Storybook 或 JSDoc）。
7. 确保所有 6 条要求均被满足，并在 README 中声明全部实现。

## 7. 注意事项
- 由于最初 9 条要求中存在不可调和的矛盾，第二阶段通过冲突解决保留了 6 条核心要求，并明确了技术方案：不再追求独立 HTML 或纯内存存储，而是使用 Next.js + Supabase 的标准全栈模式。
- 最终产物不包含单文件 HTML；而是可部署的 Next.js 项目目录。
- 所有要求必须在最终交付中一一对应实现，不可省略或弱化。
