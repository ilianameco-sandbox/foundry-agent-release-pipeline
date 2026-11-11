#!/usr/bin/env bash
# G1 for hosted agents: build the agent's image from its source folder, fail on fixable HIGH/CRITICAL
# vulnerabilities, and write an SBOM (CycloneDX). Nothing is pushed here.
# Run from cicd/:  bash ../.github/scripts/build-scan-image.sh <agent-dir> <tag> <sbom-path>
# Outputs (GITHUB_OUTPUT): local_image, repository
set -euo pipefail

agent_dir="$1"
tag="$2"
sbom="$3"
TRIVY_VERSION="${TRIVY_VERSION:?set TRIVY_VERSION}"

read -r context dockerfile repository < <(python -c "
from pathlib import Path
from agent_cicd.image import build_info
info = build_info(Path('${agent_dir}'), require_source=True)
print(info['build_context'], info['dockerfile'], info['repository'])")

trivy_dir="${RUNNER_TEMP:-/tmp}/trivy-${TRIVY_VERSION}"
if [ ! -x "${trivy_dir}/trivy" ]; then
  mkdir -p "${trivy_dir}"
  curl -sSfL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    | tar -xz -C "${trivy_dir}" trivy
fi

local_image="local/${repository}:${tag}"
docker build --pull -t "${local_image}" -f "${dockerfile}" "${context}"
"${trivy_dir}/trivy" image --no-progress --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "${local_image}"
mkdir -p "$(dirname "${sbom}")"
"${trivy_dir}/trivy" image --no-progress --format cyclonedx --output "${sbom}" "${local_image}"

{
  echo "local_image=${local_image}"
  echo "repository=${repository}"
} >> "${GITHUB_OUTPUT:-/dev/null}"
echo "Built and scanned ${local_image}; SBOM in ${sbom}"
