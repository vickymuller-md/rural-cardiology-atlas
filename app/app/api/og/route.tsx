import { ImageResponse } from "next/og";
import { loadCounties } from "@/lib/counties";
import { STATE_ABBR_TO_NAME } from "@/lib/fips";

export const runtime = "nodejs";
export const dynamic = "force-static";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const fips = url.searchParams.get("fips");
  const { index } = await loadCounties();
  const c = fips ? index[fips] ?? null : null;

  const title = c
    ? `${c.county} County, ${STATE_ABBR_TO_NAME[c.state] ?? c.state}`
    : "Rural Cardiology Desert Atlas";
  const subtitle = c
    ? `${c.n_cardiologists} cardiologist${c.n_cardiologists === 1 ? "" : "s"} · ${
        c.miles_to_nearest_cardiologist != null
          ? `${c.miles_to_nearest_cardiologist.toFixed(0)} mi to nearest`
          : "—"
      }`
    : "Cardiology access across US counties";
  const tag = c
    ? c.n_cardiologists === 0
      ? "ZERO CARDIOLOGISTS"
      : c.hpsa_primary_care
        ? "HPSA · PRIMARY CARE"
        : c.rural
          ? "RURAL COUNTY"
          : "COUNTY REPORT"
    : "PUBLIC DATA · UPDATED MONTHLY";

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          padding: "72px",
          background: "#faf6ee",
          color: "#0f2544",
          fontFamily: "sans-serif",
        }}
      >
        <div
          style={{
            fontSize: 22,
            letterSpacing: 6,
            textTransform: "uppercase",
            color: c?.n_cardiologists === 0 ? "#d63a20" : "#7a8593",
          }}
        >
          {tag}
        </div>
        <div
          style={{
            marginTop: 16,
            fontSize: 80,
            lineHeight: 1.05,
            fontStyle: "italic",
          }}
        >
          {title}
        </div>
        <div
          style={{
            marginTop: 16,
            fontSize: 36,
            color: "#1e4e41",
          }}
        >
          {subtitle}
        </div>
        <div
          style={{
            marginTop: "auto",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <div style={{ fontSize: 22, color: "#7a8593" }}>
            atlas.heartlandprotocol.org
          </div>
          <div style={{ fontSize: 22, color: "#7a8593" }}>
            Vicky Muller Ferreira, MD
          </div>
        </div>
      </div>
    ),
    { width: 1200, height: 630 }
  );
}
