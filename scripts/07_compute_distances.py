"""Reference-distance compatibility surface.

V19 computes provider county counts and all distances in the single offline
build from the same canonical provider/hospital hashes.  A standalone mutation
of public county JSON is intentionally impossible.
"""

from atlas_pipeline.canonical import (
    compute_cah_accounting,
    haversine_miles,
    nearest_provider,
    public_one_decimal,
)
from atlas_pipeline.errors import IntegrityError


def main() -> None:
    raise IntegrityError(
        "standalone distance mutation is retired; run the hash-bound offline build"
    )


if __name__ == "__main__":
    main()
