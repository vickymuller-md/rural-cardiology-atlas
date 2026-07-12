// src/components/HeartLineMark.tsx
import { jsx, jsxs } from "react/jsx-runtime";
function HeartLineMark({ className }) {
  return /* @__PURE__ */ jsxs(
    "svg",
    {
      className,
      viewBox: "0 0 36 32",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: "1.6",
      strokeLinecap: "round",
      strokeLinejoin: "round",
      "aria-hidden": "true",
      children: [
        /* @__PURE__ */ jsx("path", { d: "M18 28 C 6 20 3 12 8 7 C 12 3 16 5 18 8 C 20 5 24 3 28 7 C 33 12 30 20 18 28 Z" }),
        /* @__PURE__ */ jsx("path", { d: "M5 17 H 12 L 14 12 L 16 22 L 18 17 H 24", strokeWidth: "1.4" })
      ]
    }
  );
}

// src/tokens/network.ts
var HEARTLAND_NETWORK = [
  {
    id: "home",
    label: "The Protocol",
    shortLabel: "Protocol",
    url: "https://heartlandprotocol.org",
    tagline: "Eight-module implementation framework for rural heart failure care."
  },
  {
    id: "app",
    label: "Clinical App",
    shortLabel: "App",
    url: "https://app.heartlandprotocol.org",
    tagline: "Interactive decision-support PWA with calculators, titration wizards, and alerts."
  },
  {
    id: "scoring",
    label: "Risk Engine",
    shortLabel: "Scoring",
    url: "https://scoring.heartlandprotocol.org",
    tagline: "Zero-dependency npm package and live risk score calculator."
  },
  {
    id: "guide",
    label: "Pocket Guide",
    shortLabel: "Guide",
    url: "https://guide.heartlandprotocol.org",
    tagline: "Offline-first bedside reference with GDMT tables and red-flag cards."
  },
  {
    id: "atlas",
    label: "Access Atlas",
    shortLabel: "Atlas",
    url: "https://atlas.heartlandprotocol.org",
    tagline: "County-level map of cardiology access gaps across the United States."
  },
  {
    id: "redcap",
    label: "REDCap Template",
    shortLabel: "REDCap",
    url: "https://redcap.heartlandprotocol.org",
    tagline: "Pre-built REDCap instruments for the HEARTLAND registry."
  },
  {
    id: "synthetic",
    label: "Synthetic Cohort",
    shortLabel: "Synthetic",
    url: "https://synthetic.heartlandprotocol.org",
    tagline: "Python generator for synthetic heart failure cohorts with rural variables."
  },
  {
    id: "fhir",
    label: "FHIR IG",
    shortLabel: "FHIR",
    url: "https://fhir.heartlandprotocol.org",
    tagline: "HL7 FHIR Implementation Guide for HEARTLAND interoperability."
  }
];
var HEARTLAND_EXTERNAL_LINKS = {
  zenodo: "https://doi.org/10.5281/zenodo.19101219",
  osf: "https://doi.org/10.17605/OSF.IO/YUSGH",
  cureus: "https://www.cureus.com/",
  medrxiv: "https://www.medrxiv.org/",
  orcid: "https://orcid.org/0009-0009-1099-5690",
  email: "mailto:vickymuller@heartlandprotocol.org",
  githubOrg: "https://github.com/vickymuller-md"
};
function findSite(id) {
  const entry = HEARTLAND_NETWORK.find((s) => s.id === id);
  if (!entry) throw new Error(`Unknown HEARTLAND site id: ${id}`);
  return entry;
}

// src/components/Masthead.tsx
import { jsx as jsx2, jsxs as jsxs2 } from "react/jsx-runtime";
function buildWordmark(currentSite) {
  if (currentSite === "home") return "Heartland";
  const entry = findSite(currentSite);
  return `Heartland \xB7 ${entry.shortLabel}`;
}
function Masthead({
  currentSite,
  navItems,
  cta,
  secondaryCta,
  wordmark,
  version,
  homeHref
}) {
  const href = homeHref ?? findSite(currentSite).url;
  const label = wordmark ?? buildWordmark(currentSite);
  return /* @__PURE__ */ jsx2("header", { className: "sticky top-0 z-40 border-b border-grid bg-terminal/85 backdrop-blur supports-[backdrop-filter]:bg-terminal/70", children: /* @__PURE__ */ jsxs2("div", { className: "mx-auto flex max-w-[1200px] items-center justify-between gap-8 px-6 py-5", children: [
    /* @__PURE__ */ jsxs2("a", { href, className: "group flex items-center gap-2.5", children: [
      /* @__PURE__ */ jsx2(HeartLineMark, { className: "h-7 w-7 text-alert transition-transform group-hover:scale-105" }),
      /* @__PURE__ */ jsx2("span", { className: "font-editorial text-[18px] font-semibold tracking-tight text-cool", children: label }),
      version ? /* @__PURE__ */ jsx2("span", { className: "ml-1 hidden rounded-full border border-grid bg-panel px-2 py-0.5 font-mono text-[10.5px] tracking-tight text-cool/70 sm:inline-flex", children: version }) : null
    ] }),
    navItems && navItems.length > 0 ? /* @__PURE__ */ jsx2(
      "nav",
      {
        "aria-label": "Primary",
        className: "hidden items-center gap-8 font-editorial text-[14px] text-cool/80 md:flex",
        children: navItems.map((item) => /* @__PURE__ */ jsx2(
          "a",
          {
            href: item.href,
            target: item.external ? "_blank" : void 0,
            rel: item.external ? "noopener noreferrer" : void 0,
            className: "transition-colors hover:text-alert",
            children: item.label
          },
          item.href
        ))
      }
    ) : /* @__PURE__ */ jsx2(NetworkSwitcher, { currentSite }),
    /* @__PURE__ */ jsxs2("div", { className: "flex items-center gap-2", children: [
      secondaryCta ? /* @__PURE__ */ jsxs2(
        "a",
        {
          href: secondaryCta.href,
          target: secondaryCta.external ? "_blank" : void 0,
          rel: secondaryCta.external ? "noopener noreferrer" : void 0,
          className: "hidden items-center gap-2 rounded-full border border-grid bg-panel px-4 py-2.5 font-editorial text-[13.5px] font-medium text-cool/85 transition-colors hover:border-cool/40 hover:text-cool sm:inline-flex",
          children: [
            secondaryCta.label,
            secondaryCta.external ? /* @__PURE__ */ jsx2("span", { className: "text-[11px] text-stone", "aria-hidden": true, children: "\u2197" }) : null
          ]
        }
      ) : null,
      cta ? /* @__PURE__ */ jsxs2(
        "a",
        {
          href: cta.href,
          target: cta.external ? "_blank" : void 0,
          rel: cta.external ? "noopener noreferrer" : void 0,
          className: "group inline-flex items-center gap-2 rounded-full bg-cool px-5 py-2.5 font-editorial text-[13.5px] font-medium text-terminal transition-colors hover:bg-alert hover:text-cool",
          children: [
            cta.label,
            /* @__PURE__ */ jsx2("span", { className: "transition-transform group-hover:translate-x-0.5", "aria-hidden": true, children: "\u2192" })
          ]
        }
      ) : null
    ] })
  ] }) });
}
function NetworkSwitcher({ currentSite }) {
  return /* @__PURE__ */ jsxs2("details", { className: "relative hidden md:block", children: [
    /* @__PURE__ */ jsx2("summary", { className: "cursor-pointer list-none font-editorial text-[14px] text-cool/80 transition-colors hover:text-alert", children: "HEARTLAND Network \u25BE" }),
    /* @__PURE__ */ jsx2("ul", { className: "absolute right-0 top-full z-50 mt-3 w-72 rounded-2xl border border-grid bg-panel p-3 shadow-lg", children: HEARTLAND_NETWORK.map((site) => /* @__PURE__ */ jsx2("li", { children: /* @__PURE__ */ jsxs2(
      "a",
      {
        href: site.url,
        className: `block rounded-lg px-3 py-2 font-editorial text-[13.5px] transition-colors hover:bg-panel-hi ${site.id === currentSite ? "bg-panel-hi text-cool" : "text-cool/80"}`,
        "aria-current": site.id === currentSite ? "page" : void 0,
        children: [
          /* @__PURE__ */ jsx2("span", { className: "block font-medium", children: site.label }),
          /* @__PURE__ */ jsx2("span", { className: "mt-0.5 block text-[12px] text-stone", children: site.url.replace("https://", "") })
        ]
      }
    ) }, site.id)) })
  ] });
}

// src/components/Colophon.tsx
import { jsx as jsx3, jsxs as jsxs3 } from "react/jsx-runtime";
var DEFAULT_DESCRIPTION = "Heart failure Evidence-based Access in Rural Treatment, Linking Advanced Network Delivery \u2014 a peer-reviewed implementation framework and its companion open-source toolkit.";
var DEFAULT_LEGAL = "Built by Vicky Muller Ferreira, MD. For licensed clinicians only. Not a medical device. Not for direct patient care. No patient health information is ever stored.";
function Colophon({
  currentSite,
  extraBlocks = [],
  description = DEFAULT_DESCRIPTION,
  legal = DEFAULT_LEGAL,
  version
}) {
  const site = findSite(currentSite);
  const year = (/* @__PURE__ */ new Date()).getFullYear();
  return /* @__PURE__ */ jsx3("footer", { className: "border-t border-grid bg-terminal", children: /* @__PURE__ */ jsxs3("div", { className: "mx-auto max-w-[1200px] px-6 py-16", children: [
    /* @__PURE__ */ jsxs3("div", { className: "grid grid-cols-1 gap-12 md:grid-cols-12", children: [
      /* @__PURE__ */ jsxs3("div", { className: "md:col-span-5", children: [
        /* @__PURE__ */ jsxs3("a", { href: site.url, className: "inline-flex items-center gap-2.5", children: [
          /* @__PURE__ */ jsx3(HeartLineMark, { className: "h-7 w-7 text-alert" }),
          /* @__PURE__ */ jsx3("span", { className: "font-editorial text-[18px] font-semibold tracking-tight text-cool", children: currentSite === "home" ? "Heartland" : `Heartland \xB7 ${site.shortLabel}` })
        ] }),
        /* @__PURE__ */ jsx3("p", { className: "mt-4 max-w-md font-editorial text-[14.5px] leading-relaxed text-cool/70", children: description }),
        /* @__PURE__ */ jsx3("p", { className: "mt-6 font-editorial text-[12.5px] leading-relaxed text-stone", children: legal })
      ] }),
      extraBlocks.map((block) => /* @__PURE__ */ jsx3(FooterBlock, { title: block.title, children: block.links.map((l) => /* @__PURE__ */ jsx3(
        FooterLink,
        {
          href: l.href,
          external: l.external,
          children: l.label
        },
        `${block.title}-${l.href}`
      )) }, block.title)),
      /* @__PURE__ */ jsxs3(FooterBlock, { title: "Research", children: [
        /* @__PURE__ */ jsx3(FooterLink, { href: HEARTLAND_EXTERNAL_LINKS.cureus, external: true, children: "Cureus article" }),
        /* @__PURE__ */ jsx3(FooterLink, { href: HEARTLAND_EXTERNAL_LINKS.zenodo, external: true, children: "Zenodo deposit" }),
        /* @__PURE__ */ jsx3(FooterLink, { href: HEARTLAND_EXTERNAL_LINKS.osf, external: true, children: "OSF deposit" }),
        /* @__PURE__ */ jsx3(FooterLink, { href: HEARTLAND_EXTERNAL_LINKS.orcid, external: true, children: "ORCID profile" })
      ] }),
      /* @__PURE__ */ jsx3(FooterBlock, { title: "Network", children: HEARTLAND_NETWORK.map((entry) => /* @__PURE__ */ jsx3(
        FooterLink,
        {
          href: entry.url,
          external: entry.id !== currentSite,
          current: entry.id === currentSite,
          children: entry.shortLabel
        },
        entry.id
      )) })
    ] }),
    /* @__PURE__ */ jsxs3("div", { className: "mt-14 flex flex-col gap-3 border-t border-grid pt-6 font-editorial text-[12.5px] text-stone md:flex-row md:items-center md:justify-between", children: [
      /* @__PURE__ */ jsxs3("p", { children: [
        "\xA9 ",
        year,
        " Vicky Muller Ferreira, MD \xB7 Released under MIT"
      ] }),
      /* @__PURE__ */ jsxs3("p", { children: [
        currentSite === "home" ? "HEARTLAND Protocol" : `Heartland \xB7 ${site.shortLabel}`,
        version ? ` \xB7 ${version}` : "",
        " \xB7 open source"
      ] })
    ] })
  ] }) });
}
function FooterBlock({
  title,
  children
}) {
  return /* @__PURE__ */ jsxs3("div", { className: "md:col-span-2", children: [
    /* @__PURE__ */ jsx3("p", { className: "mb-4 font-editorial text-[12.5px] uppercase tracking-[0.18em] text-cool", children: title }),
    /* @__PURE__ */ jsx3("ul", { className: "space-y-3 font-editorial text-[14.5px]", children })
  ] });
}
function FooterLink({
  href,
  children,
  external,
  current
}) {
  const externalAttrs = external ? { target: "_blank", rel: "noopener noreferrer" } : {};
  return /* @__PURE__ */ jsx3("li", { children: /* @__PURE__ */ jsxs3(
    "a",
    {
      href,
      ...externalAttrs,
      "aria-current": current ? "page" : void 0,
      className: `inline-flex items-baseline gap-1 transition-colors hover:text-alert ${current ? "text-cool" : "text-cool/80"}`,
      children: [
        children,
        external && /* @__PURE__ */ jsx3("span", { className: "text-[11px] text-stone", "aria-hidden": true, children: "\u2197" })
      ]
    }
  ) });
}

// src/tokens/colors.ts
var HEARTLAND_COLORS = {
  terminal: "#faf6ee",
  terminalDeep: "#f2ecde",
  panel: "#ffffff",
  panelHi: "#efe9dd",
  grid: "#e3dcd0",
  gridHi: "#c5bcac",
  cool: "#0f2544",
  stone: "#7a8593",
  signal: "#2e6f5e",
  signalDeep: "#1e4e41",
  alert: "#ff6b55",
  alertDeep: "#e04a35",
  critical: "#d63a20"
};
export {
  Colophon,
  HEARTLAND_COLORS,
  HEARTLAND_EXTERNAL_LINKS,
  HEARTLAND_NETWORK,
  HeartLineMark,
  Masthead,
  findSite
};
//# sourceMappingURL=index.js.map
