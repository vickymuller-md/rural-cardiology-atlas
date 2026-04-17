import type { Metadata } from "next";
import { Geist, Instrument_Serif, Sora } from "next/font/google";
import { Masthead, Colophon } from "@heartland/ui";
import "@heartland/ui/css/theme.css";
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
        <Masthead
          currentSite="atlas"
          navItems={[{ label: "Methodology", href: "/about" }]}
          cta={{
            label: "View source",
            href: "https://github.com/vickymuller-md/rural-cardiology-atlas",
            external: true,
          }}
        />
        <main>{children}</main>
        <Colophon
          currentSite="atlas"
          description="Interactive US map visualizing county-level cardiology access gaps. Aggregate public data only — no patient health information. County provider counts do not reflect part-time, locum, or telehealth availability."
        />
      </body>
    </html>
  );
}
