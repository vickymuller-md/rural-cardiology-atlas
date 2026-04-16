import type { Metadata } from "next";
import { Geist, Instrument_Serif } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
  display: "swap",
});

const instrumentSerif = Instrument_Serif({
  variable: "--font-display",
  weight: "400",
  style: ["normal", "italic"],
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL("https://atlas.heartlandprotocol.org"),
  title: {
    default: "Rural Cardiology Desert Atlas",
    template: "%s | Rural Cardiology Desert Atlas",
  },
  description:
    "Interactive map of cardiology access gaps across US counties. Public data, freely available, maintained alongside the HEARTLAND Protocol.",
  keywords: [
    "rural health",
    "cardiology",
    "heart failure",
    "health disparities",
    "critical access hospitals",
    "HEARTLAND Protocol",
    "Vicky Muller Ferreira",
  ],
  authors: [{ name: "Vicky Muller Ferreira, MD" }],
  openGraph: {
    title: "Rural Cardiology Desert Atlas",
    description:
      "Interactive map of cardiology access gaps across US counties.",
    url: "https://atlas.heartlandprotocol.org",
    siteName: "Rural Cardiology Desert Atlas",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "Rural Cardiology Desert Atlas",
    description:
      "Interactive map of cardiology access gaps across US counties.",
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${instrumentSerif.variable} min-h-screen`}>
        <SiteHeader />
        <main className="mx-auto w-full max-w-[1400px] px-4 pb-24">{children}</main>
        <SiteFooter />
      </body>
    </html>
  );
}

function SiteHeader() {
  return (
    <header className="border-b border-[var(--color-grid)] bg-[var(--color-terminal)]/90 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1400px] items-center justify-between gap-6 px-4 py-4">
        <Link href="/" className="group flex items-center gap-3">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-full bg-[var(--color-alert)]"
          />
          <span className="flex flex-col leading-tight">
            <span className="font-[var(--font-display)] text-xl italic">
              Rural Cardiology Desert
            </span>
            <span className="text-xs uppercase tracking-[0.22em] text-[var(--color-stone)]">
              Atlas · US Counties
            </span>
          </span>
        </Link>
        <nav className="flex items-center gap-6 text-sm">
          <Link
            href="/about"
            className="text-[var(--color-cool)] transition hover:text-[var(--color-signal-deep)]"
          >
            About
          </Link>
          <a
            href="https://heartlandprotocol.org"
            className="text-[var(--color-cool)] transition hover:text-[var(--color-signal-deep)]"
            target="_blank"
            rel="noopener noreferrer"
          >
            HEARTLAND Protocol ↗
          </a>
          <a
            href="https://github.com/vickymuller-md/rural-cardiology-atlas"
            className="text-[var(--color-cool)] transition hover:text-[var(--color-signal-deep)]"
            target="_blank"
            rel="noopener noreferrer"
          >
            Source ↗
          </a>
        </nav>
      </div>
      <div className="atlas-divider h-px w-full" />
    </header>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-[var(--color-grid)] bg-[var(--color-terminal-deep)] px-4 py-10 text-xs text-[var(--color-stone)] no-print">
      <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <p className="max-w-2xl">
          Aggregate public data only — no patient information. County provider counts do not
          reflect part-time, locum, or telehealth availability.
        </p>
        <p>
          Vicky Muller Ferreira, MD · ORCID 0009-0009-1099-5690 · MIT License
        </p>
      </div>
    </footer>
  );
}
