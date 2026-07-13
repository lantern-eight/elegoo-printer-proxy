#!/usr/bin/env bash
# Create persistent secondary LAN IPs on macOS for the printer proxies.
#
# Each proxy container binds its ports to a dedicated host IP (one IP per
# printer — see README "One IP per printer"). This script gives the Mac
# those extra IPs as network *services* — the CLI equivalent of
# System Settings → Network → adding a service on the same port — which
# survive reboots, unlike a plain `ifconfig alias`.
#
# The IPs are read from docker-compose.yml port bindings, so the compose
# file stays the single source of truth. Re-running is safe: existing
# services are updated in place.
#
# Usage:
#   sudo ./scripts/setup_macos_ips.sh          # auto-detect LAN interface
#   sudo ./scripts/setup_macos_ips.sh en7      # explicit interface device
#
# Remove a service later with:
#   sudo networksetup -removenetworkservice "Printer Proxy <ip>"
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ${EUID} -ne 0 ]]; then

  echo 'Run with sudo — networksetup needs root to modify services.' >&2
  exit 1

fi

compose_file='docker-compose.yml'

if [[ ! -f ${compose_file} ]]; then

  echo "${compose_file} not found — copy docker-compose.example.yml first." >&2
  exit 1

fi

# 1. Collect unique host IPs from compose port bindings ("IP:host:container").
proxy_ips=$(grep -Eo '"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+:[0-9]+"' "${compose_file}" \
  | cut -d '"' -f 2 | cut -d ':' -f 1 | sort -u)

if [[ -z ${proxy_ips} ]]; then

  echo "No IP-scoped port bindings found in ${compose_file}." >&2
  exit 1

fi

# 2. Which interface? Default: the device holding the default route.
device=${1:-$(route -n get default | awk '/interface:/ {print $2}')}

if [[ -z ${device} ]]; then

  echo 'Could not detect the default-route interface; pass one explicitly.' >&2
  exit 1

fi

# 3. Find the existing network service on that device to duplicate.
#    (`listnetworkserviceorder` prints "(N) Name" followed by a line with
#    the device; exclude services this script created on earlier runs.)
base_service=$(networksetup -listnetworkserviceorder \
  | grep -B 1 "Device: ${device})" \
  | grep -E '^\([0-9*]+\)' \
  | sed -E 's/^\([0-9*]+\) //' \
  | grep -v '^Printer Proxy ' \
  | head -n 1)

if [[ -z ${base_service} ]]; then

  echo "No network service found for device ${device}." >&2
  exit 1

fi

# 4. Reuse the primary address's subnet mask and the default gateway.
primary_ip=$(ipconfig getifaddr "${device}" || true)
subnet_mask=$(ipconfig getoption "${device}" subnet_mask 2>/dev/null || true)

if [[ -z ${subnet_mask} ]]; then

  # Static primary IP (no DHCP): convert ifconfig's hex mask to dotted quad.
  hex_mask=$(ifconfig "${device}" | awk '/inet .*netmask/ {print $4; exit}')
  subnet_mask=$(printf '%d.%d.%d.%d' \
    $(((hex_mask >> 24) & 255)) $(((hex_mask >> 16) & 255)) \
    $(((hex_mask >> 8) & 255)) $((hex_mask & 255)))

fi

router=$(route -n get default | awk '/gateway:/ {print $2}')

echo "Interface:    ${device} (service: ${base_service})"
echo "Primary IP:   ${primary_ip:-unknown}"
echo "Subnet mask:  ${subnet_mask}"
echo "Router:       ${router}"
echo

for proxy_ip in ${proxy_ips}; do

  if [[ ${proxy_ip} == "${primary_ip}" ]]; then
    echo "· ${proxy_ip} is the host's primary address — skipping"
    continue

  fi

  service_name="Printer Proxy ${proxy_ip}"
  if networksetup -listallnetworkservices | grep -qx "${service_name}"; then
    echo "· ${service_name} already exists — re-applying address"

  else
    networksetup -duplicatenetworkservice "${base_service}" "${service_name}"
    echo "· created service: ${service_name}"

  fi
  networksetup -setmanual "${service_name}" "${proxy_ip}" "${subnet_mask}" "${router}"
  echo "  → ${proxy_ip} / ${subnet_mask} via ${router}"

done

echo
echo 'Done. The IPs are active now and persist across reboots.'
echo 'Verify from another machine on the LAN:  ping <proxy-ip>'

# 5. Pre-flight check: binding a specific IP to a port below 1024 requires
#    Docker Desktop's privileged helper (com.docker.vmnetd) — macOS only
#    exempts wildcard (0.0.0.0) binds from the root requirement. The helper
#    is installed by a Docker Desktop setting, not by this script: it is
#    Docker-private machinery that Docker Desktop's updater manages.
needs_vmnetd='false'
while read -r host_port; do
  if ((host_port < 1024)); then
    needs_vmnetd='true'
    break
  fi
done < <(grep -Eo '"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+:[0-9]+"' "${compose_file}" \
  | cut -d '"' -f 2 | cut -d ':' -f 2)

if [[ ${needs_vmnetd} == 'true' ]] \
    && [[ ! -f /Library/PrivilegedHelperTools/com.docker.vmnetd ]]; then
  echo
  echo 'WARNING: docker-compose.yml binds a privileged port (<1024) to a'
  echo "specific IP, but Docker Desktop's privileged helper is not installed."
  echo '`docker compose up` will fail with "ports are not available …'
  echo 'com.docker.vmnetd.sock: no such file or directory".'
  echo
  echo 'One-time fix: Docker Desktop → Settings → Advanced →'
  echo '  enable "Allow privileged port mapping", then Apply & restart.'
  echo 'See README section "macOS: enable privileged port mapping".'
  exit 2
fi
