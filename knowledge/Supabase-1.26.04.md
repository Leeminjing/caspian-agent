# Supabase 1.26.04

Source: https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/realtime/getting_started.mdx

### Initialize the Supabase client

Configure the client using your project URL and publishable key.

```ts
import { createClient } from '@supabase/supabase-js'

const supabase = createClient('https://<project>.supabase.co', '<sb_publishable_key>')
```

```dart
import 'package:supabase_flutter/supabase_flutter.dart';

void main() async {
  await Supabase.initialize(
    url: 'https://<project>.supabase.co',
    publishableKey: '<sb_publishable_key>',
  );
  runApp(MyApp());
}

final supabase = Supabase.instance.client;
```

```swift
import Supabase

let supabase = SupabaseClient(
  supabaseURL: URL(string: "https://<project>.supabase.co")!,
  supabaseKey: "<sb_publishable_key>"
)
```

```python
from supabase import create_client, Client

url: str = "https://<project>.supabase.co"
key: str = "<sb_publishable_key>"
supabase: Client = create_client(url, key)
```

```c#
using Supabase;

var supabase = new Client(
  "https://<project>.supabase.co",
  "<sb_publishable_key>"
);
await supabase.InitializeAsync();
```
