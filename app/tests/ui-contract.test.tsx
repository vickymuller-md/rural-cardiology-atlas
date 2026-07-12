import { readFile } from "node:fs/promises";
import path from "node:path";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CountyPanel } from "@/components/map/CountyPanel";
import { StatsBar } from "@/components/stats/StatsBar";
import {
  DISTANCE_DEFINITION,
  HPSA_LIMITATION,
  HPSA_SNAPSHOT_LABEL,
  PLACES_LABEL,
  PROVIDER_DEFINITION,
} from "@/lib/methodology";
import { makeCounty, makeSummary } from "./fixtures";

describe("controlled public labels", () => {
  it("renders authoritative summary percentages directly and uses a facility denominator", () => {
    render(<StatsBar summary={makeSummary()} />);
    expect(screen.getByText("Areas with zero qualifying providers")).toBeInTheDocument();
    expect(screen.getByText("of 3 (66.7%)")).toBeInTheDocument();
    expect(screen.getByText("66.7% among 3 evaluable in-scope CAHs")).toBeInTheDocument();
    expect(
      screen.getByText("residents of areas with zero qualifying providers")
    ).toBeInTheDocument();
  });

  it("renders a null CHD prevalence as unavailable with the correct semantic label", () => {
    render(<CountyPanel county={makeCounty()} onClose={() => undefined} />);
    const label = screen.getByText("Adult CHD prevalence (age-adjusted %)");
    expect(within(label.parentElement as HTMLElement).getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/not road distance or travel time/i)).toBeInTheDocument();
  });

  it("renders the actual Connecticut planning-region FIPS as an area without appending County", () => {
    render(
      <CountyPanel
        county={makeCounty({
          fips: "09110",
          state: "CT",
          county: "Capitol Planning Region",
        })}
        onClose={() => undefined}
      />
    );
    expect(screen.getByText("FIPS 09110")).toBeInTheDocument();
    expect(screen.getByText("Capitol Planning Region")).toBeInTheDocument();
    expect(screen.queryByText(/Capitol Planning Region County/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close area details" })).toBeInTheDocument();
  });

  it("contains no county-only label on the key UI and OG surfaces", async () => {
    const relativeFiles = [
      "app/layout.tsx",
      "app/page.tsx",
      "app/not-found.tsx",
      "app/county/[fips]/page.tsx",
      "components/stats/StatsBar.tsx",
      "components/map/CountyPanel.tsx",
      "components/map/Legend.tsx",
      "components/map/CountyMap.tsx",
      "lib/og-copy.ts",
    ];
    const forbidden = [
      /county-level/i,
      /county report/i,
      /counties with zero/i,
      /rural counties/i,
      /per county/i,
      /county not found/i,
      /click any county/i,
    ];
    const hits: string[] = [];
    for (const relative of relativeFiles) {
      const source = await readFile(path.join(process.cwd(), relative), "utf8");
      for (const pattern of forbidden) if (pattern.test(source)) hits.push(`${relative}: ${pattern}`);
    }
    expect(hits).toEqual([]);
  });

  it("freezes provider, distance, and PLACES claim language", () => {
    expect(PROVIDER_DEFINITION).toContain("active NPI record");
    expect(PROVIDER_DEFINITION).toContain("primary adult-cardiology taxonomy");
    expect(DISTANCE_DEFINITION).toContain("not road distance or travel time");
    expect(PLACES_LABEL).toContain("age-adjusted prevalence of coronary heart disease");
    expect(PLACES_LABEL).toContain("2023 (%)");
    expect(HPSA_SNAPSHOT_LABEL).toContain("daily HRSA");
    expect(HPSA_SNAPSHOT_LABEL).toContain("July 10, 2026");
    expect(HPSA_SNAPSHOT_LABEL).not.toMatch(/Q3|quarter/i);
    expect(HPSA_LIMITATION).toContain("not a cardiology shortage designation");
  });

  it("identifies the HRSA daily snapshot and forbids a Q3 detailed-file claim", async () => {
    const publicSources = await Promise.all(
      ["app/about/page.tsx", "lib/methodology.ts"].map((relative) =>
        readFile(path.join(process.cwd(), relative), "utf8")
      )
    );
    expect(publicSources.join("\n")).toContain("HPSA_SNAPSHOT_LABEL");
    expect(HPSA_SNAPSHOT_LABEL).toBe(
      "Official daily HRSA primary-care HPSA warehouse snapshot captured July 10, 2026"
    );
    expect(publicSources.join("\n")).not.toMatch(/Q3 FY2026|quarterly HRSA|Q3 detailed/i);
  });
});
