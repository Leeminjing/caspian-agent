# Supabase 1.26.04

Source: https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/getting-started/quickstarts/kotlin.mdx

### Initialize the Supabase client

Create the Supabase client instance with your project URL and key, installing the Postgrest plugin.

```kotlin
import ...

val supabase = createSupabaseClient(
    supabaseUrl = "https://xyzcompany.supabase.co",
    supabaseKey = "your_publishable_key"
  ) {
    install(Postgrest)
}
...
```
