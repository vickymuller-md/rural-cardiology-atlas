import * as react_jsx_runtime from 'react/jsx-runtime';
import { ReactNode } from 'react';
import { HeartlandSiteId } from './tokens/network.js';
export { HEARTLAND_EXTERNAL_LINKS, HEARTLAND_NETWORK, HeartlandNetworkEntry, findSite } from './tokens/network.js';

/**
 * HeartLineMark — the HEARTLAND wordmark icon.
 * Soft hand-drawn heart silhouette with a subtle ECG trace.
 * Zero deps, safe in any React renderer (Next, Astro islands, Remix).
 */
declare function HeartLineMark({ className }: {
    className?: string;
}): react_jsx_runtime.JSX.Element;

interface MastheadCta {
    /** Display text, e.g. "Request access". */
    label: string;
    /** URL -- external or site-internal path. */
    href: string;
    /** Adds target="_blank" rel handling when true. */
    external?: boolean;
}
interface MastheadNavItem {
    label: string;
    href: string;
    external?: boolean;
}
interface MastheadProps {
    /** Which site in the HEARTLAND network is rendering this masthead. */
    currentSite: HeartlandSiteId;
    /**
     * Optional secondary nav (above the primary CTA).
     * If omitted, a compact "network switcher" dropdown is rendered instead.
     */
    navItems?: MastheadNavItem[];
    /** Optional primary CTA on the right. Omit on marketing sites with nothing to click. */
    cta?: MastheadCta;
    /** Optional outlined secondary CTA shown to the left of the primary CTA (e.g. GitHub). */
    secondaryCta?: MastheadCta;
    /**
     * Override the wordmark. Defaults to the site's label from the network config
     * (e.g. "Heartland" for home, "Heartland · Clinical App" for app).
     */
    wordmark?: ReactNode;
    /** Optional small version chip shown after the wordmark (e.g. "v1.0.2"). */
    version?: string;
    /** Homepage URL for the logo click target. Defaults to the current site's root. */
    homeHref?: string;
}
/**
 * Masthead — soft, generous nav bar shared by every HEARTLAND site.
 * Sticky, warm-terminal background, editorial font across every node.
 * Mirrors the canonical layout used on synthetic.heartlandprotocol.org.
 */
declare function Masthead({ currentSite, navItems, cta, secondaryCta, wordmark, version, homeHref, }: MastheadProps): react_jsx_runtime.JSX.Element;

interface ColophonLink {
    label: string;
    href: string;
    external?: boolean;
}
interface ColophonBlock {
    title: string;
    links: ColophonLink[];
}
interface ColophonProps {
    currentSite: HeartlandSiteId;
    /**
     * Optional extra footer blocks shown before the shared "Research" + "Network"
     * blocks (e.g. per-site platform/package links).
     */
    extraBlocks?: ColophonBlock[];
    /** Optional description text shown under the wordmark. Defaults to a generic tagline. */
    description?: ReactNode;
    /** Override the legal microcopy. Keep brief; default carries the HIPAA/PHI guardrail. */
    legal?: ReactNode;
    /** Version label shown at the bottom right. E.g. "v3.2", "v1.0.2". */
    version?: string;
}
/**
 * Colophon — uncluttered footer shared across every HEARTLAND site.
 * Editorial font across every surface, matching the canonical layout used
 * on synthetic.heartlandprotocol.org.
 */
declare function Colophon({ currentSite, extraBlocks, description, legal, version, }: ColophonProps): react_jsx_runtime.JSX.Element;

/**
 * HEARTLAND color palette as JS constants.
 * Prefer the CSS tokens in `@heartland/ui/css/theme.css` for styling;
 * use these constants only when a hex value is needed in TS (e.g. SVG fill).
 */
declare const HEARTLAND_COLORS: {
    readonly terminal: "#faf6ee";
    readonly terminalDeep: "#f2ecde";
    readonly panel: "#ffffff";
    readonly panelHi: "#efe9dd";
    readonly grid: "#e3dcd0";
    readonly gridHi: "#c5bcac";
    readonly cool: "#0f2544";
    readonly stone: "#7a8593";
    readonly signal: "#2e6f5e";
    readonly signalDeep: "#1e4e41";
    readonly alert: "#ff6b55";
    readonly alertDeep: "#e04a35";
    readonly critical: "#d63a20";
};
type HeartlandColorToken = keyof typeof HEARTLAND_COLORS;

export { Colophon, type ColophonBlock, type ColophonLink, type ColophonProps, HEARTLAND_COLORS, HeartLineMark, type HeartlandColorToken, HeartlandSiteId, Masthead, type MastheadCta, type MastheadNavItem, type MastheadProps };
