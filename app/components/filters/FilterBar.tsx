"use client";

import type { FilterState } from "@/lib/filters";
import { ALL_STATE_ABBRS, STATE_ABBR_TO_NAME } from "@/lib/fips";

interface Props {
  value: FilterState;
  onChange: (patch: Partial<FilterState>) => void;
}

export function FilterBar({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-[var(--color-grid)] bg-[var(--color-panel)] px-4 py-3 text-sm">
      <div className="flex items-center gap-2">
        <label htmlFor="state" className="text-[var(--color-stone)]">
          State
        </label>
        <select
          id="state"
          value={value.state ?? ""}
          onChange={(e) => onChange({ state: e.target.value || null })}
          className="rounded-md border border-[var(--color-grid)] bg-white px-2 py-1.5 text-[var(--color-cool)]"
        >
          <option value="">All 50 + DC</option>
          {ALL_STATE_ABBRS.map((abbr) => (
            <option key={abbr} value={abbr}>
              {STATE_ABBR_TO_NAME[abbr] ?? abbr}
            </option>
          ))}
        </select>
      </div>

      <Toggle
        label="Rural only"
        checked={value.ruralOnly}
        onChange={(v) => onChange({ ruralOnly: v })}
      />
      <Toggle
        label="CAH only"
        checked={value.cahOnly}
        onChange={(v) => onChange({ cahOnly: v })}
      />
      <Toggle
        label="HPSA overlay"
        checked={value.hpsaOnly}
        onChange={(v) => onChange({ hpsaOnly: v })}
      />

      <div className="ml-auto flex items-center gap-2">
        <span className="text-[var(--color-stone)]">Color by</span>
        <div className="flex rounded-md border border-[var(--color-grid)]">
          <MetricButton
            active={value.metric === "density"}
            onClick={() => onChange({ metric: "density" })}
          >
            Density
          </MetricButton>
          <MetricButton
            active={value.metric === "distance"}
            onClick={() => onChange({ metric: "distance" })}
          >
            Distance
          </MetricButton>
        </div>
      </div>
    </div>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-[var(--color-grid-hi)] text-[var(--color-signal-deep)] focus:ring-[var(--color-signal)]"
      />
      <span className="select-none">{label}</span>
    </label>
  );
}

function MetricButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "px-3 py-1.5 text-sm transition " +
        (active
          ? "bg-[var(--color-signal-deep)] text-white"
          : "bg-white text-[var(--color-cool)] hover:bg-[var(--color-panel-hi)]")
      }
    >
      {children}
    </button>
  );
}
