# shadcn/ui 3.5.0

Source: https://github.com/shadcn-ui/ui/blob/main/packages/shadcn/package.json

### Inspect script confirms local `node` invocation

The official inspect script uses `node dist/index.js mcp` directly — proving the server can be invoked without npx by pointing to the built CLI entry point.

```json
"mcp:inspect": "pnpm dlx @modelcontextprotocol/inspector node dist/index.js mcp"
```
