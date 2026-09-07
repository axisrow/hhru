#!/usr/bin/env bash
#
# Local release script for `hhru-bot`.
#
# Same token-based release_pypi.sh pattern as the other axisrow projects
# (code-helper, direct-cli, hermes-agent-axisrow, tg_content_factory, ...).
# Version lives in pyproject.toml (`[project] version`), NOT a git tag: the
# tag vX.Y.Z is created by the git step AFTER a confirmed upload.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
PYPROJECT="${ROOT_DIR}/pyproject.toml"

usage() {
  cat <<'EOF'
Usage:
  scripts/release_pypi.sh testpypi
  scripts/release_pypi.sh pypi [--no-git]
  scripts/release_pypi.sh all [--no-git]

Behavior:
  - loads .env from the repository root when present
  - derives the package version from pyproject.toml (`[project] version`),
    the single source of truth
  - rebuilds dist artifacts from scratch
  - runs twine checks before upload
  - uploads to TestPyPI, PyPI, or both
  - after a successful PyPI upload (pypi/all only, NOT testpypi), commits
    the version bump in pyproject.toml, tags vX.Y.Z and pushes the current
    branch with the tag to its upstream. --no-git skips this step. The
    commit is made only after the upload is confirmed, so a failed upload
    never produces a release commit; only pyproject.toml is staged, so
    unrelated working-tree changes are left untouched. Re-runs are safe:
    an already-committed bump is skipped; an existing tag is reused.

Required .env variables:
  TWINE_USERNAME=__token__
  TEST_PYPI_TOKEN=pypi-...   # for testpypi/all
  PYPI_TOKEN=pypi-...        # for pypi/all
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 1
fi

TARGET="$1"
GIT_STEP=1

if [[ $# -eq 2 ]]; then
  if [[ "$2" == "--no-git" ]]; then
    GIT_STEP=0
  else
    usage
    exit 1
  fi
fi

case "${TARGET}" in
  testpypi|pypi|all)
    ;;
  *)
    usage
    exit 1
    ;;
esac

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

TWINE_USERNAME="${TWINE_USERNAME:-__token__}"

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

resolve_version() {
  local version
  version="$(grep -E '^version = ' "${PYPROJECT}" | head -n 1 | sed -E 's/^version = "(.*)"/\1/')"
  if [[ -z "${version}" ]]; then
    echo "Could not read version from ${PYPROJECT}" >&2
    exit 1
  fi
  if [[ ! "${version}" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
    echo "Invalid PEP 440 version read from ${PYPROJECT}: '${version}'" >&2
    exit 1
  fi
  echo "${version}"
}

build_artifacts() {
  require_command python3

  local version
  version="$(resolve_version)"
  echo "package version -> ${version} (from pyproject.toml)"

  echo "Cleaning old build artifacts"
  rm -rf "${ROOT_DIR}/dist" "${ROOT_DIR}/build" "${ROOT_DIR}"/*.egg-info

  echo "Building package"
  (
    cd "${ROOT_DIR}"
    python3 -m build
  )

  echo "Checking artifacts with twine"
  (
    cd "${ROOT_DIR}"
    python3 -m twine check dist/*
  )
}

upload_target() {
  local repository="$1"
  local password_var="$2"

  require_var "${password_var}"

  echo "Uploading to ${repository}"
  (
    cd "${ROOT_DIR}"
    TWINE_USERNAME="${TWINE_USERNAME}" \
    TWINE_PASSWORD="${!password_var}" \
    python3 -m twine upload --non-interactive --skip-existing --repository "${repository}" dist/*
  )
}

# Commit the version bump, tag and push the current branch. Runs only after a
# confirmed PyPI upload (pypi/all), never after testpypi. Only pyproject.toml
# is staged, so unrelated working-tree changes are left alone. Re-runs are
# safe: if the bump is already committed, the commit is skipped; an existing
# vX.Y.Z tag is reused, not re-created.
git_release() {
  local version
  version="$(resolve_version)"
  local tag="v${version}"

  echo "Recording release ${version} in git"

  cd "${ROOT_DIR}"

  if git diff --quiet -- "${PYPROJECT}"; then
    echo "  version bump already committed; nothing to commit"
  else
    git add -- "${PYPROJECT}"
    git commit -m "chore(release): hhru-bot ${version} на PyPI"
  fi

  if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
    echo "  tag ${tag} already exists; reusing"
  else
    git tag "${tag}"
  fi

  local branch upstream remote
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "${branch}" == "HEAD" ]]; then
    echo "  detached HEAD; skipping push" >&2
    return 0
  fi
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [[ -z "${upstream}" ]]; then
    echo "  no upstream tracking for ${branch}; skipping push" >&2
    return 0
  fi
  remote="${upstream%%/*}"
  echo "  pushing ${branch} and ${tag} to ${remote}"
  git push "${remote}" "${branch}" "${tag}"
}

build_artifacts

case "${TARGET}" in
  testpypi)
    upload_target "testpypi" "TEST_PYPI_TOKEN"
    ;;
  pypi)
    upload_target "pypi" "PYPI_TOKEN"
    if [[ "${GIT_STEP}" -eq 1 ]]; then
      git_release
    fi
    ;;
  all)
    upload_target "testpypi" "TEST_PYPI_TOKEN"
    upload_target "pypi" "PYPI_TOKEN"
    if [[ "${GIT_STEP}" -eq 1 ]]; then
      git_release
    fi
    ;;
esac
