# NIW Integration — Rural Cardiology Desert Atlas

Internal tracker for the atlas as evidence in the EB-2 NIW petition of
Vicky Muller Ferreira, MD (LOPES LAW PLLC).

## Role in the petition

- **Prong 2 (substantial merit / national importance)** — demonstrates the
  petitioner has moved beyond a single publication into public data
  infrastructure quantifying the access gap the HEARTLAND Protocol addresses.
- **Prong 3 (well-positioned)** — a freely available, regularly updated,
  publicly hosted tool adopted by researchers, policy makers, hospital
  administrators, and journalists.

## Surface for evidence

| Evidence | Where it lives | Cadence |
|-|-|-|
| Live URL | https://atlas.heartlandprotocol.org | Continuous |
| GitHub repo | https://github.com/vickymuller-md/rural-cardiology-atlas | Continuous |
| Zenodo DOI | Minted on first GitHub release | Once, then versioned |
| Unique visitors | Vercel Analytics | Monthly |
| County deep-links opened | Vercel Analytics (page events) | Monthly |
| Iframe embeds | Access-log referer parsing | Monthly |
| Media/academic citations | Manual log below | Ongoing |

## Cross-links to include in Professional Plan V14

`docs atualizados/Professional_Plan_V14.md` should gain one paragraph near
the HEARTLAND App section with:

- Atlas URL + Zenodo DOI (when minted)
- Monthly update cadence
- Specific mention that it provides "the first publicly available,
  county-level, machine-readable dataset combining NPPES cardiology
  supply with CAH presence, HPSA status, and heart disease mortality"

This change is deferred — V14 is the authoritative source and is edited
only when Vicky approves. Draft the paragraph in PR description, not in
V14 directly.

## Integration checklist

- [ ] First release tagged `v0.1.0` → Zenodo DOI minted
- [ ] Custom domain `atlas.heartlandprotocol.org` live (CNAME to Vercel)
- [ ] Add atlas to `heartland-app/README.md` Related Work section
- [ ] Add atlas to the HEARTLAND Protocol Cureus manuscript supplementary
      materials (if peer-review permits an addendum)
- [ ] First monthly traction snapshot recorded in the NIW dossier

## Monthly traction log

| Month | Unique visitors | County reports opened | Embeds detected | Notable citations |
|-|-|-|-|-|
| 2026-04 | _baseline pending launch_ | | | |
