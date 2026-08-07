# shadcn/ui 3.5.0

Source: https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/button.tsx

### Button asChild composition pattern

The Button component shows the canonical shadcn/ui composition semantic: the `asChild` prop uses `Slot.Root` from Radix UI to merge styling onto a child element while preserving the child's own semantic role. This pattern is shared across Button, Badge, SidebarMenuButton, BreadcrumbLink, and other primitives.

```typescript
function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}
```
