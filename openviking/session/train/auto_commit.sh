#!/usr/bin/env bash

# Opt-in code checkpoint and local note target before services start. Never push.
openviking_capture_train_launch_command() {
  local -a command=(bash "$0")
  local argument option candidate sensitive
  while [[ $# -gt 0 ]]; do
    argument="$1"
    option="${argument%%=*}"
    sensitive=false
    if [[ "${option}" == --?* ]]; then
      for candidate in --api-key --access-token --auth-token --password --secret --token --server-url --benchmark-service-url; do
        # argparse accepts unambiguous long-option prefixes; redact them before quoting.
        if [[ "${candidate}" == "${option}"* ]]; then
          sensitive=true
          break
        fi
      done
    fi
    if [[ "${sensitive}" == "true" ]]; then
      if [[ "${argument}" == *=* ]]; then
        command+=("${option}=***")
        shift
      else
        command+=("${option}" "***")
        shift
        if [[ $# -gt 0 ]]; then shift; fi
      fi
    else
      command+=("${argument}")
      shift
    fi
  done
  printf -v OPENVIKING_TRAIN_LAUNCH_COMMAND '%q ' "${command[@]}"
  OPENVIKING_TRAIN_LAUNCH_COMMAND="${OPENVIKING_TRAIN_LAUNCH_COMMAND% }"
  export OPENVIKING_TRAIN_LAUNCH_COMMAND
}

openviking_train_auto_commit() {
  local repo_root="$1"
  local run_name="$2"
  if [[ "${AUTO_COMMIT:-false}" != "true" ]]; then
    unset OPENVIKING_TRAIN_GIT_NOTES_COMMIT
    unset OPENVIKING_TRAIN_LAUNCH_COMMAND
    return 0
  fi

  local status commit
  status="$(git -C "${repo_root}" status --porcelain)" || return
  if [[ -n "${status}" ]]; then
    local commit_message="train: checkpoint before ${run_name} $(date +%Y%m%d_%H%M%S)"
    printf '[auto-commit] detected pending changes, committing...\n'
    git -C "${repo_root}" add -A || return
    git -C "${repo_root}" commit -m "${commit_message}" || return
  else
    printf '[auto-commit] working tree clean, nothing to commit\n'
  fi

  OPENVIKING_TRAIN_GIT_NOTES_COMMIT="$(git -C "${repo_root}" rev-parse HEAD)" || return
  export OPENVIKING_TRAIN_GIT_NOTES_COMMIT
  commit="$(git -C "${repo_root}" rev-parse --short HEAD)" || return
  printf '[auto-commit] current commit: %s\n' "${commit}"
}
