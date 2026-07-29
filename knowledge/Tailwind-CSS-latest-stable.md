# Tailwind CSS latest-stable

Source: https://github.com/tailwindlabs/tailwindcss.com/blob/main/tailwindcss.com/src/docs/upgrade-guide.mdx

### PostCSS Plugin Configuration for v4

Official upgrade guide for PostCSS plugin setup in Tailwind CSS v4, showing the transition from v3's integrated plugin to the dedicated @tailwindcss/postcss package.

```markdown
In v3, the `tailwindcss` package was a PostCSS plugin, but in v4 the PostCSS plugin lives in a dedicated `@tailwindcss/postcss` package.

Additionally, in v4 imports and vendor prefixing is now handled for you automatically, so you can remove `postcss-import` and `autoprefixer` if they are in your project:

```js
// [!code filename:postcss.config.mjs]
export default {
  plugins: {
    // [!code --:4]
    "postcss-import": {},
    tailwindcss: {},
    autoprefixer: {},
    // [!code ++:2]
    "@tailwindcss/postcss": {},
  },
};
```
```
