validate:
    ./ci/validate-schemas.sh
    ./ci/validate-oracle.sh

smoke:
    ./ci/smoke.sh

check:
    ./ci/check-candidates.sh

rust-check:
    cargo check --manifest-path prototypes/rust/Cargo.toml

manifest:
    python3 ci/manifest.py --write
