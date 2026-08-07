# 任务合同：用 React 做一个显示当前时间的页面

## 一、目标与边界（阶段1结论）
- 主目标：使用 React 创建一个显示当前时间的页面
- 边界：
  1. 不引入用户未提出的功能（实时刷新、日期展示、样式定制、交互逻辑等）
  2. 技术栈限定为 React；不指定构建工具、路由或服务端渲染
  3. 页面显示初始渲染时的当前时间，不默认自动更新
- 预期结果：
  1. 一个可运行的 React 应用，页面渲染出当前时间
  2. 时间以可读格式展示，如本地时间 HH:MM:SS 或完整日期时间字符串
  3. 在浏览器环境中作为前端页面正常显示

## 二、需求、兼容性与冲突（阶段2结论）
需求：
- R1：用 React 做一个显示当前时间的页面
- R2：页面显示可读格式的当前时间，且为初始渲染时的当前时间
- R3：应用作为前端 Web 页面在浏览器中运行

兼容性检查（全部 verified，无 conflict）：

| 技术 | 应用类型 | UI载体 | 运行平台 | 宿主模型 |
|---|---|---|---|---|
| React | 独立 Web 前端页面 | 浏览器 DOM（HTML 挂载节点） | 现代浏览器客户端（CSR） | react-dom |
| JavaScript（ECMAScript Date API） | 独立 Web 前端页面 | 浏览器 DOM（文本内容） | 浏览器 JS 运行时 | ECMAScript 标准内置库 |

冲突：无。

## 三、优先级（阶段3结论）
- R1：3（必须）
- R2：3（必须）
- R3：3（必须）

## 四、文件与网址（阶段4结论）
- 文件：无（files: []）
- URL：无（urls: []）

## 五、版本（阶段5结论）
| 技术 | 版本 | 依据 |
|---|---|---|
| React | 19.2 | 官方文档明确（reactjs/react.dev） |
| JavaScript（ECMAScript Date API） | latest-stable | 执行时采用最新稳定版；无精确稳定版本信息 |

## 六、官方知识要点（阶段6结论）
React 19.2：
- `<Activity mode="visible|hidden">`：将应用拆分为可控制、可优先级的活动，支持 visible/hidden 模式（替代条件渲染）。
- Partial Pre-rendering：可预渲染静态部分并保存 postponed 状态，之后用 resume/resumeAndPrerender 恢复渲染。
- `use()`：Promise 必须缓存，防止每次渲染重建；Promise 应直接传给 use，不要自行读取 status/value。

ECMAScript Date API：
- 构造 Date 必须用 `new Date(...)`；`Date()` 无 new 时行为不同。
- `Date.now()` 返回调用时刻的 UTC 时间值。
- `toISOString()` 返回 ISO 8601 格式 `YYYY-MM-DDTHH:mm:ss.sssZ`（UTC）。
- `Date.parse()` 无法识别的字符串返回 NaN。

## 七、执行指引（可直接指导执行）
1. 交付物：可运行的 React（19.x）前端 Web 页面，使用 react-dom 挂载到浏览器 DOM，CSR 渲染。
2. 页面组件在渲染时创建 `new Date()`，以可读格式展示当前时间（如 `date.toLocaleString()` 或自定义 HH:MM:SS 格式化）。
3. 不添加实时刷新（不使用 setInterval）、日期展示、样式定制或交互逻辑；时间为初始渲染时刻的快照。
4. 验收：浏览器打开后页面显示可读的当前时间，且为初次渲染时刻的时间。
