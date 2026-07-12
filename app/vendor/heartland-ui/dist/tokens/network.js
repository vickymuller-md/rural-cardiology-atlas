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
export {
  HEARTLAND_EXTERNAL_LINKS,
  HEARTLAND_NETWORK,
  findSite
};
//# sourceMappingURL=network.js.map
