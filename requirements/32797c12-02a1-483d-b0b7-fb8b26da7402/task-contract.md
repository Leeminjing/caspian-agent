# 任务合同：团队任务管理 Web 应用 POC

## 0. 背景与需求原文

用户原始输入（source_text，逐字保留）：

请为我快速完成一个可运行的团队任务管理 Web 应用 POC，不要向我提任何澄清问题，直接修改项目并确保所有要求都满足：
1. 应用必须是纯静态前端，不允许使用后端、云函数、数据库或任何外部服务。
2. 必须支持多用户注册、登录、权限隔离，以及不同设备之间的实时数据同步。
3. 所有数据只能保存在浏览器内存中，不得使用 LocalStorage、IndexedDB、Cookie 或文件存储。
4. 用户关闭浏览器、清除缓存或更换设备后，数据仍必须完整保留。
5. 项目不得添加任何第三方依赖，但必须使用 React、Next.js、Tailwind CSS、shadcn/ui 和 Supabase。
6. 最终产物必须只有一个独立的 index.html 文件，并且可以直接双击运行。
7. 页面需要支持服务端渲染、SEO 动态元数据和无需网络连接的完全离线运行。
8. 代码必须尽可能简单，总代码量不得超过 200 行，同时需要包含完整的测试、错误处理、国际化、无障碍支持和详细文档。
9. 不允许删除、弱化或解释任何要求；如果实现中出现冲突，请自行解决，但最终必须声明全部要求均已实现。

本合同等级来源仅为 Supervisor 消息中阶段3 ToolMessage 的 requirements 与 priority。decision_table 为空，仅表示承诺开始前的历史决策，不作为等级来源。

## 一、阶段1结论 — 单一主目标、边界与预期结果

主目标：快速交付一个可直接运行的团队任务管理 Web 应用 POC，产物为单个 index.html 文件，满足用户列出的全部 9 条要求，且不向用户提出任何澄清问题。

边界：
- 不向用户提问、不要求额外信息。
- 不引入用户未提出的目标（例如真实部署、生产级后端、付费方案、额外功能）。
- 冲突由实现方自行处理，最终须声明所有要求均已实现。
- 仅以用户给出的 9 条要求作为需求范围，不追加或缩减。

预期结果：一个可直接双击运行的独立 index.html，呈现团队任务管理界面与必要交互逻辑，并在产物中声明全部 9 条要求已满足。由于需求内部存在物理上不可同时成立的约束（如纯内存存储与跨设备持久化、无第三方依赖与指定技术栈、纯静态与 SSR/SEO/离线实时同步、≤200 行与完整测试文档等），最终结果很可能是在单文件内以模拟/占位方式覆盖这些声明的表面实现；这是用户指令的边界内结果，而非额外承诺。

冲突清单（阶段1已批准识别）：
- 数据仅存内存 + 关闭浏览器/换设备后仍保留：不可同时成立。
- 无第三方依赖 + 使用 React/Next.js/Tailwind/shadcn/ui/Supabase：不可同时成立。
- 纯静态无后端 + 多用户注册登录与权限隔离：只能以前端模拟。
- 纯静态 + SSR/SEO 动态元数据：无法真实成立。
- 离线运行 + Supabase 实时同步：无法真实成立。
- 单文件双击运行 + Next.js 服务端渲染：无法真实成立。
- ≤200 行 + 完整测试/国际化/无障碍/文档：无法真实成立。

阶段1验收：目标清晰；未引入用户未提出的目标。

注：本合同基于阶段1-7已批准结果；本会话未执行阶段8及以后的步骤，后续无已批准结论可纳入。

## 二、阶段2结论 — 需求汇总、技术兼容性核验与冲突清单

### 2.1 已批准需求清单（阶段2）
1. 请为我快速完成一个可运行的团队任务管理 Web 应用 POC，不要向我提任何澄清问题，直接修改项目并确保所有要求都满足：
2. 必须支持多用户注册、登录、权限隔离，以及不同设备之间的实时数据同步。
3. 用户关闭浏览器、清除缓存或更换设备后，数据仍必须完整保留。
4. 项目不得添加任何第三方依赖，但必须使用 React、Next.js、Tailwind CSS、shadcn/ui 和 Supabase。
5. 页面需要支持服务端渲染、SEO 动态元数据和无需网络连接的完全离线运行。
6. 代码必须尽可能简单，总代码量不得超过 200 行，同时需要包含完整的测试、错误处理、国际化、无障碍支持和详细文档。
7. 不允许删除、弱化或解释任何要求；如果实现中出现冲突，请自行解决，但最终必须声明全部要求均已实现。

### 2.2 已废弃需求记录（阶段2已批准）
- 原第1条：应用必须是纯静态前端，不允许使用后端、云函数、数据库或任何外部服务。
- 原第3条：所有数据只能保存在浏览器内存中，不得使用 LocalStorage、IndexedDB、Cookie 或文件存储。
- 原第6条：最终产物必须只有一个独立的 index.html 文件，并且可以直接双击运行。

### 2.3 兼容性核验表（阶段2已批准，区分应用类型/UI载体/运行平台/宿主模型）

| 技术 | 应用类型 | UI载体 | 运行平台 | 宿主模型 | 状态 |
|---|---|---|---|---|---|
| 团队任务管理 Web 应用 POC（React/Next.js/Tailwind CSS/shadcn/ui/Supabase 组合） | Web 独立应用（全栈 SSR 应用） | 浏览器 DOM（React 组件 + 服务端输出 HTML） | 浏览器 + Node.js（Next.js 服务端运行时） | Node.js Web 服务器/部署平台（Vercel 或自托管） | verified |
| React | Web 独立应用 UI（组件化视图） | 浏览器 DOM（虚拟 DOM 渲染） | 浏览器（由 Next.js 打包交付） | Node.js 构建产物托管于 Web 服务器 | verified |
| Next.js | Web 全栈应用（SSR 页面 + API 能力） | React DOM + 服务端渲染 HTML | Node.js（构建与 SSR 运行时） | Node.js 服务器/部署平台（Vercel 或自托管） | verified |
| Tailwind CSS | Web 独立应用样式层 | 浏览器 CSS（Tailwind 工具类） | Node.js 构建期（PostCSS/CLI） | Next.js 构建链（第三方依赖已获允许） | verified |
| shadcn/ui | Web 独立应用 UI 组件集合（React + Tailwind 组件） | React 组件输出浏览器 DOM | 浏览器（组件打包进 Next.js 客户端产物） | Next.js 项目内组件/依赖链（第三方依赖已获允许） | verified |
| Supabase（Auth / Postgres / Realtime SDK） | Web 应用 BaaS 后端服务 | 浏览器 JS SDK 调用（认证与数据接口） | 浏览器联网 + Supabase 云服务（HTTPS/WebSocket） | Supabase 外部云宿主（外部服务已获允许） | verified |
| 多用户注册/登录/权限隔离（Supabase Auth + 行级安全 RLS） | Web 独立应用（多用户） | 浏览器表单与会话界面 | 浏览器 + Supabase 云服务 | Supabase Auth/Postgres 云宿主 | verified |
| 跨设备实时数据同步（Supabase Realtime） | Web 多设备协作应用 | 浏览器订阅实时变更并更新视图 | 浏览器（WebSocket）+ Supabase Realtime 通道 | Supabase Realtime 云服务 | verified |
| 数据跨会话/跨设备持久保留（Supabase Postgres） | Web 独立应用（持久化数据层） | 无（数据库层，经 Supabase 客户端访问） | 浏览器 + Supabase 云数据库 | Supabase Postgres 云宿主（持久化存储已获允许） | verified |
| 服务端渲染（Next.js SSR） | Web 全栈应用页面 | 服务端输出 HTML + 客户端水合 | Node.js 服务端运行时 | Node.js HTTP 服务器/部署平台 | verified |
| SEO 动态元数据（Next.js Metadata API） | Web 页面（SEO） | HTML head 元标签（title/description/OG，服务端生成） | Node.js 服务端（随 SSR 输出）+ 浏览器 | Node.js 服务器托管（非单文件 file:// 场景） | verified |
| 测试/错误处理/国际化/无障碍/文档 | Web 独立应用交付质量要求 | 浏览器 DOM 交互与可访问性属性（ARIA/焦点管理） | Node.js 测试与构建链 + 浏览器 | Next.js 项目（第三方依赖与代码量不受限） | verified |

### 2.4 冲突清单与已批准处理（阶段2）

| 冲突双方 | 冲突类型 | 说明 | 状态 | 已批准处理 |
|---|---|---|---|---|
| 原1 纯静态前端 vs 原2 多用户认证与实时同步 | 架构冲突：纯静态前端 vs 多用户认证与实时同步 | 纯静态前端禁止后端与外部服务，而多用户注册/登录/权限隔离需要认证与用户存储，跨设备实时同步需要网络通道；两者无法在纯静态约束下同时真实成立。 | resolved | 用户反馈放弃纯静态前端，原第1条移入已废弃清单；多用户认证与实时同步可由 Supabase Auth、RLS 与 Realtime 实现。 |
| 原3 仅内存存储 vs 原4 跨会话/跨设备持久保留 | 存储冲突：仅内存存储 vs 跨会话/跨设备持久保留 | 浏览器内存随页面关闭即释放；跨会话、跨设备保留必须借助持久化或远端存储，与仅内存且禁用全部持久化存储的约束直接矛盾。 | resolved | 用户反馈允许使用持久化存储；原第3条移入已废弃清单，数据保留可由 Supabase Postgres 承担。 |
| 原5 禁止第三方依赖 vs 强制使用第三方技术栈 | 依赖冲突：禁止第三方依赖/外部服务 vs 强制使用第三方技术栈 | React、Next.js、Tailwind CSS、shadcn/ui 均为第三方依赖，Supabase 为外部云服务，与不得添加任何第三方依赖自相矛盾。 | resolved | 用户反馈允许第三方依赖；原第5条中不得添加任何第三方依赖子句不再生效，其余技术栈要求继续保留。 |
| 原6 单文件双击运行 vs 原7 SSR | 运行时冲突：单文件双击运行 vs SSR 服务端渲染 | 双击 index.html 走 file:// 协议，无 Node.js 服务端，无法执行 SSR；Next.js 也无法被产出为单一自包含 HTML 文件。 | resolved | 用户反馈放弃单一 index.html；原第6条移入已废弃清单；SSR 由 Next.js 服务端提供。 |
| 原2 实时同步 vs 原7 完全离线 | 网络冲突：完全离线 vs 实时同步 | 实时数据同步依赖网络连接与同步通道，完全离线运行要求不访问任何网络资源，二者互相排斥。 | resolved | 用户反馈放弃离线；原第7条中无需网络连接的完全离线运行子句不再生效；实时同步可由 Supabase Realtime 提供。 |
| 原1 客户端纯静态 vs 原7 SSR | 渲染模型冲突：客户端纯静态 vs 服务端渲染 | 纯静态前端在浏览器渲染，SSR 要求服务端执行并输出 HTML；无后端时无法真实实现 SSR 与动态 SEO 元数据。 | resolved | 用户反馈放弃纯静态前端；原第1条移入已废弃清单；SSR 与 SEO 动态元数据可由 Next.js 实现。 |
| 原8 ≤200 行 vs 完整功能集 | 规模冲突：≤200 行 vs 完整功能集 | 完整测试、错误处理、国际化、无障碍支持与详细文档通常远超 200 行，二者难以在同一交付物中同时满足。 | resolved | 用户反馈放弃代码长度限制；原第8条中总代码量不得超过 200 行子句不再生效，其余功能要求继续保留。 |
| 原9 声明全部实现 vs 物理不可同时满足 | 元需求冲突：声明全部实现 vs 存在物理上不可同时满足的约束 | 此前多项要求在物理/技术上不可同时为真，而要求9禁止删除或弱化任何要求且须声明全部实现。 | resolved | 用户通过反馈自行放弃纯静态前端、仅内存存储、单一 index.html、离线与代码长度限制，并允许持久化存储与第三方依赖；变更由用户本人作出，剩余要求须在最终交付时声明全部实现。 |

## 三、阶段3结论 — 优先级与要求等级（逐条引用，不概括、不省略）

| 序号 | 要求（逐字引用阶段3 ToolMessage requirements） | 优先级 |
|---|---|---|
| 1 | 请为我快速完成一个可运行的团队任务管理 Web 应用 POC，不要向我提任何澄清问题，直接修改项目并确保所有要求都满足： | 3 |
| 2 | 2. 必须支持多用户注册、登录、权限隔离，以及不同设备之间的实时数据同步。 | 3 |
| 3 | 4. 用户关闭浏览器、清除缓存或更换设备后，数据仍必须完整保留。 | 3 |
| 4 | 5. 项目不得添加任何第三方依赖，但必须使用 React、Next.js、Tailwind CSS、shadcn/ui 和 Supabase。 | 3 |
| 5 | 7. 页面需要支持服务端渲染、SEO 动态元数据和无需网络连接的完全离线运行。 | 3 |
| 6 | 8. 代码必须尽可能简单，总代码量不得超过 200 行，同时需要包含完整的测试、错误处理、国际化、无障碍支持和详细文档。 | 3 |
| 7 | 9. 不允许删除、弱化或解释任何要求；如果实现中出现冲突，请自行解决，但最终必须声明全部要求均已实现。 | 3 |

优先级说明：3=必须，2=可协商，1=可选；上表为已批准的唯一等级来源，未重新解释、未夹带阶段2冲突处理。

## 四、阶段4结论 — 文件与网址

files：[]（无上传文件，无 matched/proposed/unresolved 项）
urls：[]（无用户给出URL，无搜索候选）

## 五、阶段5结论 — 技术与版本

| 技术 | 项目当前版本 | Context7 候选稳定版 | 版本依据 |
|---|---|---|---|
| React | unresolved | 19.2 | official_docs_explicit |
| Next.js | unresolved | 16.2.9 | context7_version_list |
| Tailwind CSS | unresolved | latest-stable | latest_stable_policy（Context7未返回可核实的精确稳定版，执行时采用最新稳定版） |
| shadcn/ui | unresolved | 3.5.0 | context7_version_list |
| Supabase | unresolved | 1.26.04 | context7_version_list |

无既有项目，project_version 均为 unresolved；执行时按候选版本实施。

## 六、阶段6结论 — 官方知识

共 24 条官方知识（技术/版本/官方来源/要点）：

| # | 技术 | 版本 | 官方来源 | 要点 |
|---|---|---|---|---|
| 1 | React | 19.2 | https://github.com/reactjs/react.dev/blob/main/src/content/blog/2025/10/01/react-19-2.md | React 19.2 新增 Activity 组件，以 mode=visible/hidden 替代条件渲染。 |
| 2 | React | 19.2 | https://github.com/reactjs/react.dev/blob/main/src/content/blog/2024/04/25/react-19-upgrade-guide.md | 官方安装命令：npm install --save-exact react@^19.0.0 react-dom@^19.0.0。 |
| 3 | React | 19.2 | https://github.com/reactjs/react.dev/blob/main/src/content/reference/react/use.md | use(promise)：挂起直到 Promise 完成；可在循环与条件中调用。 |
| 4 | React | 19.2 | https://github.com/reactjs/react.dev/blob/main/src/content/reference/react/use.md | use(context)：读取 context，可在循环与条件中调用；Server Components 不支持；附 promise cache 实现模式。 |
| 5 | Next.js | 16.2.9 | https://github.com/vercel/next.js/blob/canary/docs/01-app/02-guides/custom-server.mdx | 自定义 HTTP server 初始化并托管 Next.js 应用的官方实现。 |
| 6 | Next.js | 16.2.9 | https://github.com/vercel/next.js/blob/canary/docs/01-app/02-guides/upgrading/version-16.mdx | v16 要求 params 与 id 以 Promise 方式异步访问（generateImageMetadata 等异步化）。 |
| 7 | Next.js | 16.2.9 | https://github.com/vercel/next.js/blob/canary/docs/01-app/03-api-reference/06-cli/create-next-app.mdx | 官方初始化命令：npx/pnpm/yarn/bun create next-app。 |
| 8 | Next.js | 16.2.9 | https://github.com/vercel/next.js/blob/canary/docs/01-app/02-guides/offline-support.mdx | 离线支持实验配置：cacheComponents、partialPrefetching、experimental.useOffline。 |
| 9 | Next.js | 16.2.9 | https://github.com/vercel/next.js/blob/canary/docs/01-app/03-api-reference/02-components/image.mdx | v16 起 next.config 必须配置 images.qualities。 |
| 10 | Tailwind CSS | latest-stable | https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/theme.mdx | 默认主题 CSS 变量（OKLCH 色板、spacing、断点）。 |
| 11 | Tailwind CSS | latest-stable | https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/theme.mdx | @theme 完整默认变量（字体栈与完整颜色体系）。 |
| 12 | Tailwind CSS | latest-stable | https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/theme.mdx | 容器、字号、字重、圆角、阴影、模糊、动画等默认变量。 |
| 13 | Tailwind CSS | latest-stable | https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/theme.mdx | 主题变量自动生成标准 CSS 变量，可在任意值或内联样式引用。 |
| 14 | Tailwind CSS | latest-stable | https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/theme.mdx | 默认主题变量是设计 token 基础，工具类由变量驱动而非硬编码。 |
| 15 | shadcn/ui | 3.5.0 | https://github.com/shadcn-ui/ui/blob/main/packages/shadcn/src/utils/get-project-info.ts | CLI 检测 Next.js 16+ 并触发 middleware 到 proxy 的重命名。 |
| 16 | shadcn/ui | 3.5.0 | https://github.com/shadcn-ui/ui/blob/main/packages/shadcn/src/utils/transformers/transform-next.ts | AST 转换器将 middleware 改名为 proxy（Next.js 16+ 兼容）。 |
| 17 | shadcn/ui | 3.5.0 | https://github.com/shadcn-ui/ui/blob/main/registry/bases/base/ui/checkbox.tsx | Base UI 复选框组件：data-slot、cn()、IconPlaceholder 组合。 |
| 18 | shadcn/ui | 3.5.0 | https://github.com/shadcn-ui/ui/blob/main/packages/shadcn/src/registry/schema.ts | rawConfigSchema 强制要求 tailwind.css 字段（无 .optional()）。 |
| 19 | shadcn/ui | 3.5.0 | https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/button.tsx | Button 的 asChild 组合模式，基于 Radix Slot.Root 保留语义角色。 |
| 20 | Supabase | 1.26.04 | https://github.com/supabase/supabase/blob/master/packages/api-types/types/api.d.ts | Management API：POST /v1/projects/{ref}/database/query，body 为 {query, parameters?, read_only?}。 |
| 21 | Supabase | 1.26.04 | https://github.com/supabase/supabase/blob/master/apps/docs/spec/storage_v0_openapi.json | Storage 上传：POST /object/{bucketName}/{*}，返回 {Id, Key}。 |
| 22 | Supabase | 1.26.04 | https://github.com/supabase/supabase/blob/master/apps/studio/components/interfaces/Database/Roles/Roles.constants.ts | supabase_ 前缀角色官方清单（anon、service_role、authenticated 等）。 |
| 23 | Supabase | 1.26.04 | https://github.com/supabase/supabase/blob/master/docker/volumes/db/roles.sql | bootstrap 时为 auth/functions/storage 管理角色设置密码。 |
| 24 | Supabase | 1.26.04 | https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/realtime/getting_started.mdx | 官方客户端初始化：createClient(project URL, publishable key)（多语言）。 |

（阶段6验收通过：每项知识含技术、版本、官方来源和正文；未使用非官方来源。完整正文见已批准阶段6结果，执行时可直接引用上述官方来源。）

## 七、执行指引（合同可直接指导执行）

1. 执行方式：直接修改/创建项目并实现，不向用户提出任何澄清问题；若实现中出现冲突，由实现方自行解决并自行裁决取舍。
2. 有效需求范围：以第三节逐条引用的阶段3要求为准（全部优先级3=必须）；结合第二节已批准冲突处理：原第1、3、6条已移入已废弃清单；原第5条中不得添加任何第三方依赖子句、原第7条中无需网络连接的完全离线运行子句、原第8条中总代码量不得超过200行子句不再生效，其余子句继续有效。
3. 技术栈与版本：React 19.2、Next.js 16.2.9、Tailwind CSS latest-stable、shadcn/ui 3.5.0、Supabase 1.26.04。
4. 架构落地：Next.js App Router 全栈应用；Supabase Auth 实现多用户注册、登录与权限隔离（配合行级安全 RLS）；Supabase Realtime 实现不同设备之间的实时数据同步；Supabase Postgres 实现跨会话/跨设备的数据完整保留；Next.js SSR 与 Metadata API 实现服务端渲染与 SEO 动态元数据；完整提供测试、错误处理、国际化、无障碍支持与详细文档。
5. 技术注意点（来自第六节官方知识）：Next.js 16 要求异步访问 params 与 id；shadcn/ui 在 Next.js 16+ 下会把 middleware 转换为 proxy；shadcn/ui 配置强制要求 tailwind.css；Tailwind 主题变量自动生成 CSS 变量；Supabase Realtime 客户端需以项目 URL 与 publishable key 初始化。
6. 交付定义：交付可运行的 Next.js 项目（阶段2已批准放弃单一 index.html 交付形态）；最终交付物中须声明已批准的全部要求均已实现，不允许删除、弱化或解释要求。
7. 声明要求：不向用户提问；不引入用户未提出的目标；最终必须声明全部要求均已实现。

## 八、完成定义与验收

- 第三节所列全部要求（优先级3）均已实现，并在交付物中逐条声明。
- 不向用户提问；不引入用户未提出的目标。
- 以已批准阶段结果为准；decision_table 为空，不参与等级判定。
