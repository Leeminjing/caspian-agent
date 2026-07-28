# Supabase 1.26.04

Source: https://github.com/supabase/supabase/blob/master/examples/prompts/nextjs-supabase-auth.md

### Correct Browser Client Implementation

Example of how to correctly implement the Supabase browser client using `@supabase/ssr`.

```typescript
import { createBrowserClient } from '@supabase/ssr'

export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!
  )
}
```
