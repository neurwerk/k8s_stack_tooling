#!/usr/bin/env bash
# Read-only connectivity checks for the server's firewall path.
# No credentials, installations, configuration changes, or image downloads.

set -u

usage() {
  cat <<'HELP'
Usage: bash connection_test.sh [--no-color] [--wan-only]

Run on the target server to test its network path.
HTTPS tests use HEAD, verify TLS, bypass proxies, and follow redirects.
Known hosts are grouped by firewall wildcard; unknown subdomains cannot be
enumerated. These are connectivity checks, not full image pulls or logins.

Environment overrides:
  CONNECT_TIMEOUT=4       MAX_TIME=8
  DNS_SERVER=<host>       OLLAMA_HOST=<host>
  NTP_SERVER=<host>       AD_SERVER=<host>
  DNS_DOMAIN=<domain>     AUTHORITATIVE_DNS_SERVERS="ns1.example.org ns2.example.org"
  INTERNAL_DNS_HOST=<host>
  INTERNAL_HOSTS="auth.example.org chat.example.org"

Site-specific targets are optional and have no defaults. Host lists are
space-separated. Authoritative DNS checks need both a domain and nameservers.

Example:
  NTP_SERVER=dc.example.org AD_SERVER=dc.example.org bash connection_test.sh

Tools: curl; dig for DNS; nc for TCP; python3 for optional NTP.
Exit codes: 0 = no connection failures; 1 = failures; 2 = invocation/tool error.
Yellow REACHED means an HTTP/DNS/NTP reply needs interpretation, not a pass.
SKIP means a tool or optional target is missing. No root access is needed.
HELP
}

parse_arguments() {
  COLOR=auto
  WAN_ONLY=0

  local argument
  for argument in "$@"; do
    case "$argument" in
      --no-color) COLOR=no ;;
      --wan-only) WAN_ONLY=1 ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        printf 'Unknown argument: %s\n' "$argument" >&2
        usage
        exit 2
        ;;
    esac
  done
}

configure() {
  CONNECT_TIMEOUT=${CONNECT_TIMEOUT:-4}
  MAX_TIME=${MAX_TIME:-8}
  DNS_SERVER=${DNS_SERVER:-}
  OLLAMA_HOST=${OLLAMA_HOST:-}
  NTP_SERVER=${NTP_SERVER:-}
  AD_SERVER=${AD_SERVER:-}
  DNS_DOMAIN=${DNS_DOMAIN:-}
  AUTHORITATIVE_DNS_SERVERS=${AUTHORITATIVE_DNS_SERVERS:-}
  INTERNAL_DNS_HOST=${INTERNAL_DNS_HOST:-}
  INTERNAL_HOSTS=${INTERNAL_HOSTS:-}

  local timeout
  for timeout in "$CONNECT_TIMEOUT" "$MAX_TIME"; do
    if [[ ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
      printf 'Timeouts must be positive integers.\n' >&2
      exit 2
    fi
  done

  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required.\n' >&2
    exit 2
  fi

  OS=$(uname -s)
}

initialize_output() {
  GREEN='' RED='' YELLOW='' DIM='' BOLD='' RESET=''
  if [[ -t 1 && "$COLOR" != no && -z "${NO_COLOR:-}" ]]; then
    GREEN=$'\033[32m'
    RED=$'\033[31m'
    YELLOW=$'\033[33m'
    DIM=$'\033[2m'
    BOLD=$'\033[1m'
    RESET=$'\033[0m'
  fi

  PASS_COUNT=0
  FAIL_COUNT=0
  REVIEW_COUNT=0
  SKIP_COUNT=0
  trap 'printf "\nInterrupted; checks are incomplete.\n"; exit 130' INT
}

section() {
  printf '\n%b%s%b\n' "$BOLD" "$1" "$RESET"
}

result() {
  local state=$1 address=$2 detail=${3:-} color=''
  case "$state" in
    OK)
      color=$GREEN
      PASS_COUNT=$((PASS_COUNT + 1))
      ;;
    FAIL)
      color=$RED
      FAIL_COUNT=$((FAIL_COUNT + 1))
      ;;
    REACHED)
      color=$YELLOW
      REVIEW_COUNT=$((REVIEW_COUNT + 1))
      ;;
    SKIP)
      color=$DIM
      SKIP_COUNT=$((SKIP_COUNT + 1))
      ;;
  esac
  printf '  %-57s %b%-7s%b %s\n' "$address" "$color" "$state" "$RESET" "$detail"
}

check_https() {
  local host=$1 path=${2:-/} output exit_code=0 http_code remote_ip
  local line location redirects='' error='' detail

  if [[ -t 1 ]]; then
    printf '  %-57s testing...\r' "$host:443"
  fi
  output=$(curl --disable -4 --noproxy '*' --silent --show-error --head --location \
    --max-redirs 8 --proto '=https' --proto-redir '=https' \
    --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
    --dump-header - --output /dev/null \
    --write-out $'\n%{http_code}\t%{remote_ip}\n' \
    "https://$host$path" 2>&1) || exit_code=$?

  # curl appends the final status and remote IP after all redirect headers.
  IFS=$'\t' read -r http_code remote_ip <<< "${output##*$'\n'}"
  while IFS= read -r line; do
    line=${line%$'\r'}
    case "$line" in
      [Ll][Oo][Cc][Aa][Tt][Ii][Oo][Nn]:*)
        location=${line#*:}
        location=${location# }
        location=${location#$'\t'}
        case "$location" in
          https://*|http://*)
            location=${location#*://}
            location=${location%%/*}
            ;;
          //*)
            location=${location#//}
            location=${location%%/*}
            ;;
          *) continue ;;
        esac
        if [[ -n "$location" && "$location" != "$host" && " $redirects " != *" $location "* ]]; then
          redirects="${redirects:+$redirects }$location"
        fi
        ;;
      curl:*) error="${error:+$error }$line" ;;
    esac
  done <<< "$output"

  detail="HTTP ${http_code:-000}${remote_ip:+; $remote_ip}"
  if [[ -n "$redirects" ]]; then
    detail="$detail; redirects: $redirects"
  fi

  if [[ "$exit_code" != 0 ]]; then
    result FAIL "$host:443" "curl $exit_code; ${error:0:180}${redirects:+; redirects: $redirects}"
    return
  fi

  case "$http_code" in
    2??|3??) result OK "$host:443" "$detail" ;;
    401) result OK "$host:443" "$detail; authentication required (expected for registries)" ;;
    *) result REACHED "$host:443" "$detail; check authorization, URL, or firewall filtering" ;;
  esac
}

check_https_hosts() {
  local host
  for host in "$@"; do
    check_https "$host"
  done
}

check_tcp() {
  local host=$1 port=$2 label=$3 output exit_code=0
  if ! command -v nc >/dev/null 2>&1; then
    result SKIP "$host:$port TCP" 'nc missing'
    return
  fi

  if [[ "$OS" == Darwin ]]; then
    output=$(nc -4 -z -G "$CONNECT_TIMEOUT" -w "$CONNECT_TIMEOUT" "$host" "$port" 2>&1) || exit_code=$?
  else
    output=$(nc -4 -z -w "$CONNECT_TIMEOUT" "$host" "$port" 2>&1) || exit_code=$?
  fi

  if [[ "$exit_code" == 0 ]]; then
    result OK "$host:$port TCP" "$label; TCP connection only"
  else
    result FAIL "$host:$port TCP" "$label; ${output:-connection refused or timed out}"
  fi
}

check_dns() {
  local server=$1 mode=$2 query=$3 record_type=$4 recursion=$5
  local output exit_code=0 status='' transport=+notcp
  if ! command -v dig >/dev/null 2>&1; then
    result SKIP "$server:53 $mode" 'dig missing'
    return
  fi

  if [[ "$mode" == TCP ]]; then
    transport=+tcp
  fi
  output=$(dig "@$server" "$query" "$record_type" "$transport" +ignore "$recursion" \
    "+time=$CONNECT_TIMEOUT" +tries=1 +noall +comments +answer 2>&1) || exit_code=$?
  if [[ "$output" =~ status:\ ([A-Z]+) ]]; then
    status=${BASH_REMATCH[1]}
  fi

  if [[ "$exit_code" == 0 && "$status" == NOERROR && "$output" == *$'\t'"$record_type"$'\t'* ]]; then
    result OK "$server:53 $mode" "$query $record_type answered"
  elif [[ -n "$status" ]]; then
    result REACHED "$server:53 $mode" "DNS $status; verify the answer for $query"
  else
    result FAIL "$server:53 $mode" 'no DNS reply'
  fi
}

check_smtp() {
  local output exit_code=0
  output=$(curl --disable -4 --noproxy '*' --silent --show-error \
    --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
    --url smtps://smtp.tem.scaleway.com:465 --request NOOP --output /dev/null 2>&1) || exit_code=$?
  if [[ "$exit_code" == 0 ]]; then
    result OK smtp.tem.scaleway.com:465 'SMTP TLS / NOOP'
  else
    result FAIL smtp.tem.scaleway.com:465 "curl $exit_code; ${output:0:180}"
  fi
}

check_ollama() {
  local http_code exit_code=0
  if [[ -z "$OLLAMA_HOST" ]]; then
    result SKIP 'Ollama' 'set OLLAMA_HOST to test'
    return
  fi

  http_code=$(curl --disable -4 --noproxy '*' --silent --show-error \
    --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" --output /dev/null \
    --write-out '%{http_code}' "http://$OLLAMA_HOST:11434/api/tags" 2>/dev/null) || exit_code=$?
  if [[ "$exit_code" != 0 ]]; then
    result FAIL "$OLLAMA_HOST:11434" "curl $exit_code; unable to reach Ollama"
  elif [[ "$http_code" == 200 ]]; then
    result OK "$OLLAMA_HOST:11434" 'Ollama /api/tags HTTP 200'
  else
    result REACHED "$OLLAMA_HOST:11434" "HTTP $http_code; verify API type (Ollama vs llama.cpp)"
  fi
}

check_ntp() {
  local output exit_code=0
  if [[ -z "$NTP_SERVER" ]]; then
    result SKIP 'NTP UDP 123' 'set NTP_SERVER to test'
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    result SKIP "$NTP_SERVER:123 UDP" 'python3 missing'
    return
  fi

  output=$(python3 - "$NTP_SERVER" "$CONNECT_TIMEOUT" 2>&1 <<'PYTHON'
import socket
import struct
import sys
import time

try:
    address = socket.getaddrinfo(sys.argv[1], 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    packet = bytearray(48)
    packet[0] = 0x23  # NTP version 4, client mode.
    timestamp = time.time() + 2208988800  # Seconds since the NTP epoch (1900).
    struct.pack_into("!II", packet, 40, int(timestamp), int(timestamp % 1 * 2**32))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(float(sys.argv[2]))
        sock.connect(address)
        sock.send(packet)
        reply = sock.recv(512)

    # A server reply must echo our transmit timestamp as its origin timestamp.
    if len(reply) < 48 or reply[0] & 7 != 4 or reply[24:32] != packet[40:48]:
        raise ValueError("invalid NTP reply")
    stratum = reply[1]
    leap_indicator = reply[0] >> 6
    print(f"NTP reply, stratum={stratum}; system clock not changed")
    sys.exit(0 if 1 <= stratum <= 15 and leap_indicator != 3 else 2)
except Exception as exc:
    print(str(exc))
    sys.exit(1)
PYTHON
  ) || exit_code=$?

  case "$exit_code" in
    0) result OK "$NTP_SERVER:123 UDP" "$output" ;;
    2) result REACHED "$NTP_SERVER:123 UDP" "$output" ;;
    *) result FAIL "$NTP_SERVER:123 UDP" "$output" ;;
  esac
}

check_public_services() {
  section 'github.com / *.github.com'
  check_tcp github.com 22 'Git SSH'
  check_https_hosts github.com api.github.com codeload.github.com

  section '*.githubusercontent.com'
  check_https_hosts pkg-containers.githubusercontent.com release-assets.githubusercontent.com \
    raw.githubusercontent.com objects.githubusercontent.com

  section 'ghcr.io — exact registry hostname'
  check_https ghcr.io /v2/

  section 'docker.io / *.docker.io'
  check_https docker.io
  check_https registry-1.docker.io /v2/
  check_https auth.docker.io '/token?service=registry.docker.io'

  section '*.docker.com — including nested CDN subdomains'
  check_https_hosts hub.docker.com www.docker.com production.cloudfront.docker.com \
    production.cloudflare.docker.com

  section 'Docker alternate storage — exact hostname'
  check_https docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com

  section 'quay.io / *.quay.io'
  check_https quay.io /v2/
  check_https_hosts cdn.quay.io cdn01.quay.io cdn02.quay.io cdn03.quay.io \
    cdn04.quay.io cdn05.quay.io cdn06.quay.io quayio-production-s3.s3.amazonaws.com

  section 'registry.k8s.io / *.registry.k8s.io / *.pkg.dev'
  check_https registry.k8s.io /v2/
  check_https cdn.registry.k8s.io
  check_https europe-west4-docker.pkg.dev /v2/
  # Exercise a manifest URL as well: /v2/ alone may hide backend redirects.
  check_https registry.k8s.io /v2/sig-storage/csi-attacher/manifests/v4.12.0

  section 'Other registries / OpenSearch — exact hostnames'
  check_https cr.agentgateway.dev /v2/
  check_https cr.fluentbit.io /v2/
  check_https artifacts.opensearch.org /releases/plugins/repository-s3/2.18.0/repository-s3-2.18.0.zip

  section '*.api.letsencrypt.org / Route53'
  check_https acme-v02.api.letsencrypt.org /directory
  check_https acme-staging-v02.api.letsencrypt.org /directory
  check_https route53.amazonaws.com

  section 'Application APIs — exact hostnames'
  check_https openrouter.ai /api/v1/models
  check_https api.search.brave.com
  check_https mcp.context7.com /mcp

  section 'Optional Dify / Python package services'
  check_https marketplace.dify.ai
  check_https updates.dify.ai
  check_https pypi.org /simple/
  check_https files.pythonhosted.org

  section 'SMTP — implicit TLS, no login or email sent'
  check_smtp
}

check_authoritative_dns() {
  section 'Authoritative DNS — configured nameservers, UDP and TCP'
  if [[ -z "$DNS_DOMAIN" || -z "$AUTHORITATIVE_DNS_SERVERS" ]]; then
    result SKIP 'Authoritative DNS' 'set DNS_DOMAIN and AUTHORITATIVE_DNS_SERVERS to test'
    return
  fi

  local host
  local -a nameservers
  read -r -a nameservers <<< "$AUTHORITATIVE_DNS_SERVERS"
  for host in "${nameservers[@]}"; do
    check_dns "$host" UDP "$DNS_DOMAIN" SOA +norecurse
    check_dns "$host" TCP "$DNS_DOMAIN" SOA +norecurse
  done
}

check_internal_services() {
  section 'Internal DNS / local model'
  if [[ -n "$DNS_SERVER" ]]; then
    check_dns "$DNS_SERVER" UDP github.com A +recurse
    check_dns "$DNS_SERVER" TCP github.com A +recurse
  else
    result SKIP 'Internal DNS' 'set DNS_SERVER to test'
  fi
  check_ollama

  section 'Internal application hostnames — DNS and HTTPS'
  if [[ -n "$DNS_SERVER" && -n "$INTERNAL_DNS_HOST" ]]; then
    check_dns "$DNS_SERVER" UDP "$INTERNAL_DNS_HOST" A +recurse
  else
    result SKIP 'Internal host DNS' 'set DNS_SERVER and INTERNAL_DNS_HOST to test'
  fi

  local host
  local -a internal_hosts
  if [[ -n "$INTERNAL_HOSTS" ]]; then
    read -r -a internal_hosts <<< "$INTERNAL_HOSTS"
    for host in "${internal_hosts[@]}"; do
      if [[ -n "$DNS_SERVER" ]]; then
        check_dns "$DNS_SERVER" UDP "$host" A +recurse
      else
        result SKIP "$host DNS" 'set DNS_SERVER to test'
      fi
      check_https "$host"
    done
  else
    result SKIP 'Internal applications' 'set INTERNAL_HOSTS to test'
  fi

  section 'Optional internal LDAP / LDAPS / NTP targets'
  if [[ -n "$AD_SERVER" ]]; then
    check_tcp "$AD_SERVER" 389 'LDAP (optional if using LDAPS only)'
    check_tcp "$AD_SERVER" 636 'LDAPS (TCP only; no bind or certificate validation)'
  else
    result SKIP 'LDAP / LDAPS' 'set AD_SERVER to test TCP 389 and 636'
  fi
  check_ntp
}

print_summary() {
  section 'SUMMARY'
  printf '  %b%d OK%b | %b%d FAILED%b | %b%d REACHED / REVIEW%b | %d SKIPPED\n' \
    "$GREEN" "$PASS_COUNT" "$RESET" "$RED" "$FAIL_COUNT" "$RESET" \
    "$YELLOW" "$REVIEW_COUNT" "$RESET" "$SKIP_COUNT"
  printf '  CDN roots often return 403/404: a real authenticated image pull is still needed.\n'
  printf '  Kubernetes registry backends may change; check any additional redirect hosts.\n'
}

main() {
  parse_arguments "$@"
  configure
  initialize_output

  printf '%bCONNECTION TEST%b\n' "$BOLD" "$RESET"
  printf 'Run location: %s | IPv4, direct connections (no proxy)\n' "$(hostname)"
  printf 'OK = connection/TLS or DNS answer. REACHED = reply, but not a successful service check.\n'
  printf 'Known subdomains only; redirect hosts are shown. No complete wildcard enumeration.\n'

  check_public_services
  check_authoritative_dns
  if [[ "$WAN_ONLY" == 0 ]]; then
    check_internal_services
  fi

  print_summary
  [[ "$FAIL_COUNT" == 0 ]]
}

main "$@"
