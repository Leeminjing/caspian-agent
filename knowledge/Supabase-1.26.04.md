# Supabase 1.26.04

Source: https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/functions/examples/cloudflare-turnstile.mdx

### Cloudflare Turnstile Validation Edge Function (TypeScript)

Source: https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/functions/examples/cloudflare-turnstile.mdx

Implements server-side validation for Cloudflare Turnstile tokens using a Supabase Edge Function. It verifies the token against Cloudflare's '/siteverify' endpoint and requires a 'CLOUDFLARE_SECRET_KEY' environment variable.

```ts
import { withSupabase } from 'npm:@supabase/server@^1'

console.log('Hello from Cloudflare Trunstile!')

function ips(req: Request) {
  return req.headers.get('x-forwarded-for')?.split(/\s*,\s*/)
}

// `withSupabase` handles CORS and preflight requests for you.
export default {
  fetch: withSupabase({ auth: 'none' }, async (req) => {
    const { token } = await req.json()
    const clientIps = ips(req) || ['']
    const ip = clientIps[0]

    // Validate the token by calling the
    // "/siteverify" API endpoint.
    let formData = new FormData()
    formData.append('secret', Deno.env.get('CLOUDFLARE_SECRET_KEY') ?? '')
    formData.append('response', token)
    formData.append('remoteip', ip)

    const url = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
    const result = await fetch(url, {
      body: formData,
      method: 'POST',
    })

    const outcome = await result.json()
    console.log(outcome)
    if (outcome.success) {
      return Response.json({ success: true })
    }
    return Response.json({ success: false })
  }),
}
```
