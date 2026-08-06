# shadcn/ui 3.5.0

Source: https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/switch.tsx

### Switch component composition (new-york-v4 style)

The official shadcn/ui Switch built on Radix UI SwitchPrimitive.Root and SwitchPrimitive.Thumb, with size variants (`sm` and `default`) and data-slot attributes.

Context7 证据中该组件源码以 `"use client"` 开头，导入 `React`、`SwitchPrimitive` 与 `cn`，并导出 `Switch` 组件；`Root` 使用 `data-slot="switch"`、`data-size={size}` 以及多个 `data-[state=...]`/`data-[size=...]` Tailwind 类，`Thumb` 使用 `data-slot="switch-thumb"`。
