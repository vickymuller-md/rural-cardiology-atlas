"use client";

import { useState } from "react";

export function EmbedSnippet() {
  const [copied, setCopied] = useState(false);
  const snippet = `<iframe src="https://atlas.heartlandprotocol.org/embed" width="100%" height="600" frameborder="0" loading="lazy" title="Rural Cardiology Desert Atlas"></iframe>`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* no-op */
    }
  };

  return (
    <div className="rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] p-4">
      <p className="text-xs uppercase tracking-[0.18em] text-[var(--color-stone)]">
        Embed this atlas
      </p>
      <pre className="mt-2 overflow-x-auto rounded border border-[var(--color-grid)] bg-[var(--color-terminal-deep)] p-2 text-xs">
        <code>{snippet}</code>
      </pre>
      <button
        type="button"
        onClick={copy}
        className="mt-2 rounded-md bg-[var(--color-cool)] px-3 py-1.5 text-xs text-white transition hover:bg-[var(--color-signal-deep)]"
      >
        {copied ? "Copied!" : "Copy snippet"}
      </button>
    </div>
  );
}
