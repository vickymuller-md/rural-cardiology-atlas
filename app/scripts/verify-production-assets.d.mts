export interface ProductionAssetVerification {
  sourceTreeId: string;
  releaseManifestSha256: string;
  countyBytes: Uint8Array;
  summaryBytes: Uint8Array;
  universeBytes: Uint8Array;
  universeIds: readonly string[];
  summary: unknown;
}

export const REQUIRED_RELEASE_FILES: readonly string[];
export const FORBIDDEN_SERVED_TEXT_PATTERNS: ReadonlyArray<{
  readonly label: string;
  readonly pattern: RegExp;
}>;
export function sha256Hex(bytes: Uint8Array): string;
export function resolveReleaseDataDirectory(candidate?: string): string;

export function verifyProductionAssets(options?: {
  dataDirectory?: string;
  expectedReleaseManifestSha256?: string;
  contract?: unknown;
}): Promise<ProductionAssetVerification>;
