import type { Metadata } from "next";
import { Geist, Instrument_Serif, Sora } from "next/font/google";
import Link from "next/link";
import { HeartLineMark } from "@/components/landing/Mark";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
  display: "swap",
});

const sora = Sora({
  variable: "--font-editorial",
  weight: ["300", "400", "500", "600", "700"],
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
    description: "Interactive map of cardiology access gaps across US counties.",
    url: "https://atlas.heartlandprotocol.org",
    siteName: "Rural Cardiology Desert Atlas",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "Rural Cardiology Desert Atlas",
    description: "Interactive map of cardiology access gaps across US counties.",
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${sora.variable} ${instrumentSerif.variable}`}
    >
      <body className="min-h-screen bg-[var(--color-terminal)] font-[var(--font-editorial)] text-[var(--color-cool)] antialiased selection:bg-[var(--color-alert)]/40 selection:text-[var(--color-cool)]">
        <Masthead />
        <main>{children}</main>
        <Colophon />
      </body>
    </html>
  );
}

function Masthead() {
  return (
    <header className="border-b border-[var(--color-grid)] bg-[var(--color-terminal)]/85 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-terminal)]/70">
      <div className="mx-auto flex max-w-[1200px] items-center justify-between gap-8 px-6 py-5">
        <Link href="/" className="group flex items-center gap-2.5">
          <HeartLineMark className="h-7 w-7 text-[var(--color-alert)] transition-transform group-hover:scale-105" />
          <span className="font-[var(--font-editorial)] text-[18px] font-semibold tracking-tight text-[var(--color-cool)]">
            Heartland
            <span className="ml-1 font-[var(--font-display)] italic font-normal text-[var(--color-stone)]">
              · Atlas
            </span>
          </span>
        </Link>

        <nav className="hidden items-center gap-8 font-[var(--font-editorial)] text-[14px] text-[var(--color-cool)]/80 md:flex">
          <Link href="/about" className="transition-colors hover:text-[var(--color-alert)]">
            Methodology
          </Link>
          <a
            href="https://heartlandprotocol.org"
            target="_blank"
            rel="noopener noreferrer"
            className="transition-colors hover:text-[var(--color-alert)]"
          >
            The Protocol
          </a>
          <a
            href="https://doi.org/10.5281/zenodo.18566403"
            target="_blank"
            rel="noopener noreferrer"
            className="transition-colors hover:text-[var(--color-alert)]"
          >
            Research
          </a>
        </nav>

        <a
          href="https://github.com/vickymuller-md/rural-cardiology-atlas"
          target="_blank"
          rel="noopener noreferrer"
          className="group inline-flex items-center gap-2 rounded-full bg-[var(--color-cool)] px-5 py-2.5 font-[var(--font-editorial)] text-[13.5px] font-medium text-[var(--color-terminal)] transition-colors hover:bg-[var(--color-alert)] hover:text-[var(--color-cool)]"
        >
          View source
          <span className="transition-transform group-hover:translate-x-0.5">→</span>
        </a>
      </div>
    </header>
  );
}

function Colophon() {
  return (
    <footer className="border-t border-[var(--color-grid)] bg-[var(--color-terminal-deep)] px-6 py-12 no-print">
      <div className="mx-auto flex max-w-[1200px] flex-col gap-4 text-[13px] text-[var(--color-stone)] md:flex-row md:items-start md:justify-between">
        <div className="max-w-xl leading-relaxed">
          Aggregate public data only — no patient health information. County
          provider counts do not reflect part-time, locum, or telehealth
          availability. Full methodology on{" "}
          <Link href="/about" className="underline decoration-[var(--color-grid-hi)] hover:text-[var(--color-alert)]">
            /about
          </Link>
          .
        </div>
        <div className="flex flex-col gap-1 md:items-end">
          <p>Vicky Muller Ferreira, MD · ORCID 0009-0009-1099-5690</p>
          <p className="text-[12px]">
            MIT License · Companion to the{" "}
            <a
              className="underline decoration-[var(--color-grid-hi)] hover:text-[var(--color-alert)]"
              href="https://heartlandprotocol.org"
              target="_blank"
              rel="noopener noreferrer"
            >
              HEARTLAND Protocol
            </a>
          </p>
        </div>
      </div>
    </footer>
  );
}
