import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  COUNTY_TOPOLOGY_CONTRACT,
  SOURCE_BOUND_TOPOLOGY_STATUS,
  STATE_TOPOLOGY_CONTRACT,
  validateCurrentConnecticutCountyIds,
  validateTopology,
  type TopologyContract,
} from "@/components/map/topology";

const SYNTHETIC_CONTRACT: TopologyContract = {
  path: "/fixture.topo.json",
  objectName: "counties",
  bytes: 1,
  sha256: createHash("sha256")
    .update("synthetic-topology-contract", "utf8")
    .digest("hex"),
  featureCount: 2,
  idPattern: /^[0-9]{5}$/,
};

function syntheticTopology(ids = ["01991", "01993"]): unknown {
  return {
    type: "Topology",
    arcs: [],
    objects: {
      counties: {
        type: "GeometryCollection",
        geometries: ids.map((id) => ({ type: "Polygon", id, arcs: [] })),
      },
    },
  };
}

describe("local Census 2025 topology contract", () => {
  it("contains only same-origin paths and the preregistered identities", () => {
    for (const contract of [COUNTY_TOPOLOGY_CONTRACT, STATE_TOPOLOGY_CONTRACT]) {
      expect(contract.path.startsWith("/data/topology/")).toBe(true);
      expect(contract.path).not.toMatch(/^https?:/);
      expect(contract.sha256).toMatch(/^[0-9a-f]{64}$/);
    }
    expect(COUNTY_TOPOLOGY_CONTRACT.featureCount).toBe(3_144);
    expect(STATE_TOPOLOGY_CONTRACT.featureCount).toBe(51);
    expect(SOURCE_BOUND_TOPOLOGY_STATUS).toBe("MATERIALIZED_VERSION_BOUND_RELEASE");
  });

  it("validates a synthetic exact-ID analogue", () => {
    expect(validateTopology(syntheticTopology(), SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toHaveProperty("objects.counties");
  });

  it("rejects wrong object names, numeric IDs, extra IDs, and missing IDs", () => {
    const wrongObject = syntheticTopology() as Record<string, unknown>;
    (wrongObject.objects as Record<string, unknown>).wrong =
      (wrongObject.objects as Record<string, unknown>).counties;
    delete (wrongObject.objects as Record<string, unknown>).counties;
    expect(() => validateTopology(wrongObject, SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toThrow(/exactly object counties/);

    const numeric = syntheticTopology() as {
      objects: { counties: { geometries: Array<{ id: string | number }> } };
    };
    numeric.objects.counties.geometries[0].id = 1991;
    expect(() => validateTopology(numeric, SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toThrow(/invalid string ID/);

    expect(() => validateTopology(syntheticTopology(["01991", "01995"]), SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toThrow(/analytical universe/);
    expect(() => validateTopology(syntheticTopology(["01991"]), SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toThrow(/exactly 2 geometries/);
  });

  it("rejects non-polygon display geometry", () => {
    const topology = syntheticTopology() as {
      objects: { counties: { geometries: Array<{ type: string }> } };
    };
    topology.objects.counties.geometries[0].type = "LineString";
    expect(() => validateTopology(topology, SYNTHETIC_CONTRACT, ["01991", "01993"]))
      .toThrow(/Polygon or MultiPolygon/);
  });

  it("requires current Connecticut planning regions and rejects obsolete counties", () => {
    const current = [
      "09110", "09120", "09130", "09140", "09150",
      "09160", "09170", "09180", "09190",
    ];
    expect(() => validateCurrentConnecticutCountyIds(current)).not.toThrow();
    expect(() => validateCurrentConnecticutCountyIds(current.slice(1))).toThrow(/09110 is absent/);
    expect(() => validateCurrentConnecticutCountyIds([...current, "09001"]))
      .toThrow(/obsolete Connecticut county 09001/);
  });
});
