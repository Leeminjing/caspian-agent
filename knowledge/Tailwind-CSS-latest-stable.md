# Tailwind CSS latest-stable

Source: https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/detecting-classes-in-source-files.mdx

### Source file scanning - verification of class detection

Source: https://github.com/tailwindlabs/tailwindcss.com/blob/main/src/docs/detecting-classes-in-source-files.mdx

Tailwind scans files as plain text. To verify classes are properly detected, always use complete class names. Use @source inline() to safelist classes that might otherwise be missed.

```css
/* CSS */
@import "tailwindcss";
@source inline("underline");

/* Generated CSS */
.underline {
  text-decoration-line: underline;
}
```
