/**
 * Canonical HEARTLAND network config.
 * Single source of truth for all cross-domain navigation and footer links.
 *
 * To add a new subdomain: append an entry here, bump @heartland/ui version,
 * and the change propagates across every consuming site on upgrade.
 */
type HeartlandSiteId = 'home' | 'app' | 'scoring' | 'guide' | 'atlas' | 'redcap' | 'synthetic' | 'fhir';
interface HeartlandNetworkEntry {
    id: HeartlandSiteId;
    label: string;
    shortLabel: string;
    url: string;
    tagline: string;
}
declare const HEARTLAND_NETWORK: readonly HeartlandNetworkEntry[];
/** External references that appear across multiple site footers. */
declare const HEARTLAND_EXTERNAL_LINKS: {
    readonly zenodo: "https://doi.org/10.5281/zenodo.19101219";
    readonly osf: "https://doi.org/10.17605/OSF.IO/YUSGH";
    readonly cureus: "https://www.cureus.com/";
    readonly medrxiv: "https://www.medrxiv.org/";
    readonly orcid: "https://orcid.org/0009-0009-1099-5690";
    readonly email: "mailto:vickymuller@heartlandprotocol.org";
    readonly githubOrg: "https://github.com/vickymuller-md";
};
declare function findSite(id: HeartlandSiteId): HeartlandNetworkEntry;

export { HEARTLAND_EXTERNAL_LINKS, HEARTLAND_NETWORK, type HeartlandNetworkEntry, type HeartlandSiteId, findSite };
