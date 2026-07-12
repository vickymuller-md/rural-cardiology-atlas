# @heartland/ui

Shared header, footer, logo, and design tokens for the HEARTLAND Protocol network of sites.

Every consuming site — `heartlandprotocol.org` and its seven subdomains — imports from this package so headers, footers, palette, typography, and cross-links stay in lockstep.

## Install

This package is not published to npm. Consume it directly from GitHub:

```bash
npm install github:vickymuller-md/heartland-ui#v0.1.0
```

Pin to a tag or commit SHA to avoid surprise updates.

## Usage

### Next.js 16 / React 19

In your root layout (`app/layout.tsx`):

```tsx
import { Masthead, Colophon } from '@heartland/ui';
import '@heartland/ui/css/theme.css';

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Masthead currentSite="app" cta={{ label: 'Sign in', href: '/login' }} />
        <main>{children}</main>
        <Colophon currentSite="app" version="v1.0.2" />
      </body>
    </html>
  );
}
```

### Tailwind v4

Import the theme once in your `globals.css` (alongside `@import "tailwindcss"`):

```css
@import 'tailwindcss';
@import '@heartland/ui/css/theme.css';
```

This exposes the HEARTLAND palette as `bg-terminal`, `text-cool`, `border-grid`, etc.

## The 8 sites

```ts
import { HEARTLAND_NETWORK, findSite } from '@heartland/ui';
// or, if you only need the data (no React):
import { HEARTLAND_NETWORK } from '@heartland/ui/tokens/network';
```

| id | URL | Purpose |
|-|-|-|
| `home` | https://heartlandprotocol.org | Umbrella domain / protocol overview |
| `app` | https://app.heartlandprotocol.org | Clinical decision support PWA |
| `scoring` | https://scoring.heartlandprotocol.org | npm package + live calculator |
| `guide` | https://guide.heartlandprotocol.org | Offline pocket reference |
| `atlas` | https://atlas.heartlandprotocol.org | US cardiology access atlas |
| `redcap` | https://redcap.heartlandprotocol.org | REDCap instruments |
| `synthetic` | https://synthetic.heartlandprotocol.org | Synthetic HF cohort generator |
| `fhir` | https://fhir.heartlandprotocol.org | FHIR Implementation Guide |

## Masthead props

| Prop | Type | Notes |
|-|-|-|
| `currentSite` | `HeartlandSiteId` | **Required.** Identifies the site for wordmark + active state. |
| `navItems` | `MastheadNavItem[]` | Optional center nav. Omit to get the default network switcher. |
| `cta` | `MastheadCta` | Optional primary action on the right. |
| `wordmark` | `ReactNode` | Override the wordmark text. Defaults to `Heartland · <shortLabel>`. |
| `homeHref` | `string` | Override the logo-click target. |

## Colophon props

| Prop | Type | Notes |
|-|-|-|
| `currentSite` | `HeartlandSiteId` | **Required.** |
| `extraBlocks` | `ColophonBlock[]` | Site-specific blocks (Package, Platform) shown before Research + Network. |
| `description` | `ReactNode` | Override the brand paragraph. |
| `legal` | `ReactNode` | Override the clinical/legal microcopy. |
| `version` | `string` | Appears in the copyright bar (e.g. `v1.0.2`). |

## Adding a new subdomain

1. Append an entry to `src/tokens/network.ts`.
2. Bump `package.json#version` (semver minor).
3. `git tag v0.X.0 && git push --tags`.
4. Consuming sites upgrade with `npm install github:vickymuller-md/heartland-ui#v0.X.0`.

## Build

```bash
npm install
npm run build      # emits dist/ via tsup
npm run typecheck
```

## License

MIT © Vicky Muller Ferreira, MD
