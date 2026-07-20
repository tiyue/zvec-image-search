# Landing page assets

The public GitHub Pages site is served from `docs/`. Runtime CSS and images are
stored locally so the page does not depend on Tailwind CDN, Google Fonts, or
Unsplash during normal browsing.

Rebuild the minified stylesheet from the repository root after changing HTML
utility classes or `tailwind.input.css`:

```powershell
npx.cmd --yes tailwindcss@3.4.17 `
  -c site/landing/tailwind.config.cjs `
  -i site/landing/tailwind.input.css `
  -o docs/assets/app.css `
  --minify
```

Localized image sources:

- `hero-portrait.webp`: `photo-1544005313-94ddf0286df2`
- `showcase-search.webp`: `photo-1488426862026-3ee34a7d66df`
- `showcase-tasks.webp`: `photo-1551288049-bebda4e38f71`
- `showcase-organize.webp`: `photo-1529139574466-a303027c1d8b`
- `showcase-settings.webp`: `photo-1555949963-ff9fe0c870eb`

The originals were retrieved from Unsplash and converted by its image service
to 1600 × 900 WebP at quality 78. Review image licensing before replacing or
redistributing these assets outside this project.
