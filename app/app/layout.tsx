import type { Metadata } from "next";
import { Masthead } from "@heartland/ui";
import "@heartland/ui/css/theme.css";
import { AtlasFooter } from "@/components/layout/AtlasFooter";
import { socialImageMetadata } from "@/lib/og-copy";
import "./globals.css";

const globalSocialImages = socialImageMetadata(null, "Rural Cardiology Desert Atlas");

export const metadata: Metadata = {
  metadataBase: new URL("https://atlas.heartlandprotocol.org"),
  title: {
    default: "Rural Cardiology Desert Atlas",
    template: "%s | Rural Cardiology Desert Atlas",
  },
  description:
    "Area-level map across U.S. counties and county equivalents using qualifying NPPES-listed providers, great-circle distance, Critical Access Hospitals, and public contextual measures.",
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
    description: "Area-level map of qualifying NPPES-listed provider access metrics across U.S. counties and county equivalents.",
    url: "https://atlas.heartlandprotocol.org",
    siteName: "Rural Cardiology Desert Atlas",
    type: "website",
    images: globalSocialImages.openGraph,
  },
  twitter: {
    card: "summary_large_image",
    title: "Rural Cardiology Desert Atlas",
    description: "Area-level map of qualifying NPPES-listed provider access metrics across U.S. counties and county equivalents.",
    images: globalSocialImages.twitter,
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen flex flex-col bg-terminal font-editorial text-cool antialiased selection:bg-alert/40 selection:text-cool">
        <Masthead
          currentSite="atlas"
          navItems={[{ label: "Methodology", href: "/about" }]}
          secondaryCta={{
            label: "GitHub",
            href: "https://github.com/vickymuller-md/rural-cardiology-atlas",
            external: true,
          }}
          cta={{
            label: "Read the protocol",
            href: "https://doi.org/10.5281/zenodo.19101219",
            external: true,
          }}
        />
        <main className="flex-1">{children}</main>
        <AtlasFooter />
      </body>
    </html>
  );
}
