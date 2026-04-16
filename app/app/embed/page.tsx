import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { Atlas } from "@/components/Atlas";
import { loadCounties } from "@/lib/counties";

export const revalidate = 3600;

export default async function EmbedPage() {
  const { list, index } = await loadCounties();
  return (
    <NuqsAdapter>
      <div className="flex flex-col gap-3 py-4">
        <Suspense fallback={<div className="h-[600px] rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)]" />}>
          <Atlas list={list} index={index} />
        </Suspense>
        <p className="text-center text-[11px] text-[var(--color-stone)]">
          Rural Cardiology Desert Atlas · Vicky Muller Ferreira, MD ·{" "}
          <a
            className="underline"
            href="https://atlas.heartlandprotocol.org"
            target="_blank"
            rel="noopener noreferrer"
          >
            atlas.heartlandprotocol.org
          </a>
        </p>
      </div>
    </NuqsAdapter>
  );
}
