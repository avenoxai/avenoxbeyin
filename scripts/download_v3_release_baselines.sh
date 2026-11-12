#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-https://github.com/avenoxai/avenoxbeyin/releases/download}"
base_url="${base_url%/}"
mkdir -p baseline

for version in 3.0.2 3.1.0; do
  name="beyin-v3-$version.zip"
  for asset in "$name" "$name.sha256"; do
    curl --fail --location --silent --show-error --retry 3 --retry-all-errors --retry-delay 1 --retry-max-time 90 --max-time 30 \
      --output "baseline/$asset.download" "$base_url/v$version/$asset"
    mv "baseline/$asset.download" "baseline/$asset"
  done
  (cd baseline && sha256sum --check "$name.sha256")
done
