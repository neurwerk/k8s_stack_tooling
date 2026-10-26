#!/usr/bin/env bash
# Offline regression: absent products are not probed; selected paths are preserved.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../connection_test.sh"

section() { :; }
check_tcp() { :; }
check_smtp() { :; }
check_https() {
  [[ "$1" != marketplace.dify.ai && "$1" != updates.dify.ai ]]
}
parse_arguments --no-color
load_https_targets
check_public_services

targets=$(mktemp)
trap 'rm -f "$targets"' EXIT
printf '# Selected product\nexample.test /health\n' > "$targets"
parse_arguments --https-targets "$targets"
load_https_targets
calls=0
check_https() {
  [[ "$1" == example.test && "$2" == /health ]]
  calls=$((calls + 1))
}
check_selected_https
[[ "$calls" == 1 ]]
printf 'Connectivity selection checks passed.\n'
