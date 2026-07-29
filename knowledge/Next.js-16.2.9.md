# Next.js 16.2.9

Source: https://github.com/vercel/next.js/blob/canary/docs/01-app/03-api-reference/02-components/image.mdx

### Configure Default Image Qualities in Next.js

Specify the default allowed image quality values in `next.config.js`. This field is required starting with Next.js 16.

```js
module.exports = {
  images: {
    qualities: [75],
  },
}
```
