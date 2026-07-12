import type { GeometryCollection, Topology } from "topojson-specification";
import { feature } from "topojson-client";
import type { FeatureCollection, Polygon, MultiPolygon } from "geojson";

export interface TopologyContract {
  path: string;
  objectName: "counties" | "states";
  bytes: number;
  sha256: string;
  featureCount: number;
  idPattern: RegExp;
  requireCurrentConnecticut?: boolean;
}

export const COUNTY_TOPOLOGY_CONTRACT: TopologyContract = {
  path: "/data/topology/counties-2025-5m.topo.json",
  objectName: "counties",
  bytes: 1_349_047,
  sha256: "affdae57c1fe33dd5e64ccf1c735f83db830374d5dcff375231a667aa96aa4d5",
  featureCount: 3_144,
  idPattern: /^[0-9]{5}$/,
  requireCurrentConnecticut: true,
};

export const STATE_TOPOLOGY_CONTRACT: TopologyContract = {
  path: "/data/topology/states-2025-5m.topo.json",
  objectName: "states",
  bytes: 465_345,
  sha256: "05a7896dc0cd3be17813dbe7b571e74eafd690a6078a99698aa9a52df01b6bbe",
  featureCount: 51,
  idPattern: /^[0-9]{2}$/,
};

export const SOURCE_BOUND_TOPOLOGY_STATUS = "MATERIALIZED_VERSION_BOUND_RELEASE";

export type CountyTopology = Topology<{ counties: GeometryCollection }>;
export type StateTopology = Topology<{ states: GeometryCollection }>;

function contractError(message: string): never {
  throw new Error(`Atlas topology contract violation: ${message}`);
}

function equalSorted(actual: readonly string[], expected: readonly string[]): boolean {
  if (actual.length !== expected.length) return false;
  const a = [...actual].sort();
  const b = [...expected].sort();
  return a.every((value, index) => value === b[index]);
}

export function validateCurrentConnecticutCountyIds(countyIds: readonly string[]): void {
  for (const planningRegion of [
    "09110",
    "09120",
    "09130",
    "09140",
    "09150",
    "09160",
    "09170",
    "09180",
    "09190",
  ]) {
    if (!countyIds.includes(planningRegion)) {
      contractError(`required Connecticut planning region ${planningRegion} is absent`);
    }
  }
  for (let code = 1; code <= 15; code += 2) {
    const obsolete = `09${String(code).padStart(3, "0")}`;
    if (countyIds.includes(obsolete)) contractError(`obsolete Connecticut county ${obsolete} is present`);
  }
}

export function validateTopology(
  value: unknown,
  contract: TopologyContract,
  expectedIds: readonly string[]
): Topology<Record<string, GeometryCollection>> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    contractError(`${contract.objectName} asset must be a TopoJSON object`);
  }
  const topology = value as Partial<Topology<Record<string, GeometryCollection>>>;
  if (topology.type !== "Topology" || !topology.objects || typeof topology.objects !== "object") {
    contractError(`${contract.objectName} asset must have type Topology and an objects map`);
  }
  const objectNames = Object.keys(topology.objects);
  if (objectNames.length !== 1 || objectNames[0] !== contract.objectName) {
    contractError(`${contract.objectName} asset must expose exactly object ${contract.objectName}`);
  }
  const collection = topology.objects[contract.objectName];
  if (!collection || collection.type !== "GeometryCollection") {
    contractError(`${contract.objectName} object must be a GeometryCollection`);
  }
  if (collection.geometries.length !== contract.featureCount) {
    contractError(
      `${contract.objectName} must contain exactly ${contract.featureCount} geometries`
    );
  }
  const ids = collection.geometries.map((geometry, index) => {
    if (geometry.type !== "Polygon" && geometry.type !== "MultiPolygon") {
      contractError(
        `${contract.objectName} geometry ${index + 1} must be Polygon or MultiPolygon`
      );
    }
    if (typeof geometry.id !== "string" || !contract.idPattern.test(geometry.id)) {
      contractError(`${contract.objectName} geometry ${index + 1} has an invalid string ID`);
    }
    return geometry.id;
  });
  if (!equalSorted(ids, expectedIds)) {
    contractError(`${contract.objectName} IDs do not equal the analytical universe`);
  }
  if (contract.requireCurrentConnecticut) validateCurrentConnecticutCountyIds(ids);
  const validated = topology as Topology<Record<string, GeometryCollection>>;
  const materialized = feature(validated, collection) as FeatureCollection<Polygon | MultiPolygon>;
  if (
    materialized.features.length !== contract.featureCount ||
    materialized.features.some(
      (item) =>
        item.geometry === null ||
        (item.geometry.type !== "Polygon" && item.geometry.type !== "MultiPolygon")
    )
  ) {
    contractError(
      `${contract.objectName} must materialize every feature as a non-null Polygon/MultiPolygon`
    );
  }
  return validated;
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function loadTopology(
  contract: TopologyContract,
  expectedIds: readonly string[]
): Promise<Topology<Record<string, GeometryCollection>>> {
  if (!contract.path.startsWith("/") || contract.path.includes("://")) {
    contractError(`${contract.objectName} path must be same-origin and absolute`);
  }
  const response = await fetch(contract.path, { cache: "force-cache" });
  if (!response.ok || response.redirected) {
    contractError(
      `${contract.objectName} local asset returned HTTP ${response.status} or redirected`
    );
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength !== contract.bytes) {
    contractError(
      `${contract.objectName} byte length ${bytes.byteLength} does not match ${contract.bytes}`
    );
  }
  const digest = await sha256Hex(bytes);
  if (digest !== contract.sha256) {
    contractError(`${contract.objectName} SHA-256 does not match the preregistered pin`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch (error) {
    throw new Error(`Atlas topology contract violation: ${contract.objectName} is invalid JSON`, {
      cause: error,
    });
  }
  return validateTopology(parsed, contract, expectedIds);
}

export async function loadLocalTopologies(expectedCountyIds: readonly string[]): Promise<{
  counties: CountyTopology;
  states: StateTopology;
}> {
  const expectedStateIds = [...new Set(expectedCountyIds.map((fips) => fips.slice(0, 2)))].sort();
  const [counties, states] = await Promise.all([
    loadTopology(COUNTY_TOPOLOGY_CONTRACT, expectedCountyIds),
    loadTopology(STATE_TOPOLOGY_CONTRACT, expectedStateIds),
  ]);

  return { counties: counties as CountyTopology, states: states as StateTopology };
}
