#!/usr/bin/env bash
# Build, semantic-version-tag and (optionally) push the mailroom-reloaded images.
#
#   scripts/docker_release.sh              # build + tag locally (mailroom-reloaded:<v>)
#   scripts/docker_release.sh --no-build   # skip the build; only (re)tag the existing :latest images
#   scripts/docker_release.sh --push       # also push to Docker Hub; needs DOCKERHUB_OWNER=<user-or-org>
#
# Which images are release artifacts (the ones to push):
#   mailroom-reloaded  (app)      -> :<version> and :latest
# Local-only (never tagged with a version, never pushed):
#   mailroom-reloaded-dev         -> :latest only
#
# The version is read from pyproject.toml (the single source of truth).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
if [[ -z "$VERSION" ]]; then
  echo "docker_release: no 'version = \"...\"' in pyproject.toml" >&2
  exit 1
fi

build=1
push=0
for arg in "$@"; do
  case "$arg" in
    --no-build) build=0 ;;
    --push) push=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "docker_release: unknown flag $arg" >&2; exit 2 ;;
  esac
done

# Release image name -> build command (context is the repo root).
RELEASE_IMAGES=(mailroom-reloaded)
build_cmd() {
  case "$1" in
    mailroom-reloaded) echo "docker build -f deploy/Dockerfile --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS=" ;;
    *) echo "docker_release: no build command for $1" >&2; exit 1 ;;
  esac
}

echo "docker_release: version ${VERSION}"
for name in "${RELEASE_IMAGES[@]}"; do
  if [[ "$build" == 1 ]]; then
    echo "docker_release: building ${name}:${VERSION}"
    # shellcheck disable=SC2046
    $(build_cmd "$name") -t "${name}:${VERSION}" -t "${name}:latest" .
  else
    docker image tag "${name}:latest" "${name}:${VERSION}"
  fi
  echo "docker_release: tagged ${name}:${VERSION} + ${name}:latest"
done

if [[ "$push" == 1 ]]; then
  : "${DOCKERHUB_OWNER:?set DOCKERHUB_OWNER=<dockerhub-user-or-org> to push}"
  for name in "${RELEASE_IMAGES[@]}"; do
    remote="${DOCKERHUB_OWNER}/${name}"
    docker tag "${name}:${VERSION}" "${remote}:${VERSION}"
    docker tag "${name}:${VERSION}" "${remote}:latest"
    docker push "${remote}:${VERSION}"
    docker push "${remote}:latest"
    echo "docker_release: pushed ${remote}:${VERSION} + ${remote}:latest"
  done
fi

echo "docker_release: done (version ${VERSION})"
