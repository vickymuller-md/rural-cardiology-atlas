export function AtlasFooter() {
  return (
    <footer className="border-t border-[var(--color-grid)] bg-[var(--color-terminal)]">
      <div className="mx-auto flex max-w-[1200px] flex-col gap-4 px-6 py-10 text-sm text-[var(--color-stone)]">
        <p className="font-medium text-[var(--color-cool)]">Rural Cardiology Desert Atlas</p>
        <p className="max-w-4xl leading-relaxed">
          Area-level aggregate public data for counties and county equivalents only; no patient health information.
          NPPES records and addresses may be administrative or stale and do not prove
          current practice, employment, availability, licensure, credentialing,
          full-time status, or acceptance of patients. Distances are great-circle,
          not road distance or travel time.
        </p>
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <a className="underline" href="/about">Methodology and limitations</a>
          <a
            className="underline"
            href="https://doi.org/10.5281/zenodo.19101219"
            target="_blank"
            rel="noopener noreferrer"
          >
            HEARTLAND Protocol v3.3
          </a>
          <a
            className="underline"
            href="https://github.com/vickymuller-md/rural-cardiology-atlas"
            target="_blank"
            rel="noopener noreferrer"
          >
            Source code
          </a>
        </div>
        <p>© 2026 Vicky Muller Ferreira, MD · MIT</p>
      </div>
    </footer>
  );
}
