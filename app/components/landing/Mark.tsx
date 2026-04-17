/**
 * HeartLineMark — small hand-drawn heart with a subtle ECG line.
 * Matches the heartland-app wordmark treatment.
 */
export function HeartLineMark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 36 32"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 28 C 6 20 3 12 8 7 C 12 3 16 5 18 8 C 20 5 24 3 28 7 C 33 12 30 20 18 28 Z" />
      <path d="M5 17 H 12 L 14 12 L 16 22 L 18 17 H 24" strokeWidth="1.4" />
    </svg>
  );
}

/**
 * AtlasHeroMark — soft-line vignette anchoring the atlas hero.
 * Suggests a US silhouette with a faint county grid and a small heart
 * at a far-from-care location. Echoes the heartland-app drawing style.
 */
export function AtlasHeroMark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 520 360"
      fill="none"
      aria-hidden="true"
    >
      <ellipse
        cx="260"
        cy="200"
        rx="250"
        ry="140"
        fill="currentColor"
        opacity="0.55"
        className="text-[var(--color-panel-hi)]"
      />

      {/* Horizon lines evoking the heartland */}
      <path
        d="M 20 260 Q 130 220 260 250 T 500 240"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        className="text-[var(--color-cool)]/35"
      />
      <path
        d="M 20 295 Q 150 265 280 285 T 500 278"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        className="text-[var(--color-cool)]/20"
      />

      {/* Stylized US silhouette (CONUS-ish outline) */}
      <path
        d="M 70 145
           C 85 115, 150 100, 200 110
           C 250 118, 300 102, 360 118
           C 420 130, 460 150, 470 200
           C 466 230, 440 250, 400 246
           C 360 240, 320 250, 280 250
           C 240 248, 200 256, 160 248
           C 120 240, 80 218, 70 180 Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="text-[var(--color-cool)]/75"
      />

      {/* Faint county dots, sparse on the rural interior */}
      <g className="fill-[var(--color-cool)]/30">
        <circle cx="120" cy="180" r="1.6" />
        <circle cx="150" cy="200" r="1.6" />
        <circle cx="190" cy="175" r="1.6" />
        <circle cx="230" cy="195" r="1.6" />
        <circle cx="270" cy="180" r="1.6" />
        <circle cx="310" cy="200" r="1.6" />
        <circle cx="350" cy="175" r="1.6" />
        <circle cx="390" cy="195" r="1.6" />
        <circle cx="430" cy="180" r="1.6" />
      </g>

      {/* "Desert" cluster — a group with no provider, centered */}
      <g>
        <circle cx="240" cy="170" r="18" fill="currentColor" className="text-[var(--color-alert)]/15" />
        <circle cx="240" cy="170" r="10" fill="currentColor" className="text-[var(--color-alert)]/30" />
      </g>

      {/* The little heart mark floating above the desert cluster */}
      <g transform="translate(222 110)" className="text-[var(--color-alert)]">
        <path
          d="M18 28 C 6 20 3 12 8 7 C 12 3 16 5 18 8 C 20 5 24 3 28 7 C 33 12 30 20 18 28 Z"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
        <path
          d="M5 17 H 12 L 14 12 L 16 22 L 18 17 H 24"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
      </g>

      {/* Tether from heart to desert cluster */}
      <path
        d="M 240 142 L 240 154"
        stroke="currentColor"
        strokeWidth="1"
        strokeDasharray="2 3"
        className="text-[var(--color-cool)]/40"
      />
    </svg>
  );
}
