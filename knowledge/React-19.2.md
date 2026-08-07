# React 19.2

Source: https://github.com/reactjs/react.dev/blob/main/src/content/reference/react/use.md

### use(context)

Reads the value of a React context. Unlike useContext, this can be called inside loops and conditionals.

```APIDOC
## use(context)

### Description
Reads the value of a context created with `createContext`. It can be called within loops and conditional statements.

### Parameters
- **context** (Context) - Required - A context object created with `createContext`.

### Returns
- **value** (any) - The context value determined by the closest provider, or the default value if no provider exists.

### Caveats
- Must be called inside a Component or a Hook.
- Not supported in Server Components.
```

### How to implement a promise cache

To implement an effective promise cache, store promises keyed by a unique identifier like a URL. By manually setting status and value fields on the promise object, you allow React to read resolved data synchronously without triggering unnecessary Suspense fallbacks. This pattern is particularly beneficial for library authors creating data layers, as it avoids extra renders when data is already available.
