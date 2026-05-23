#!/bin/sh
# Narrow root helper used by the captive portal after it drops privileges.
# The provisioner runs this via sudo for the few operations on wlan0 that
# require root: tearing down the setup AP, joining the operator's Wi-Fi,
# and (on failure) restarting the AP so the operator can retry.
#
# Usage:
#   ranch-wifi-connect SSID [TIMEOUT]   read password from stdin and join
#   ranch-wifi-connect --restart-ap     bring the BaseStation_Setup AP back up
#
# wlan0 cannot be in AP mode and station mode simultaneously — `nmcli
# device wifi connect` requires station mode to scan for the target SSID,
# so we tear down the AP before issuing the connect.

set -eu

AP_CON="BaseStation_Setup"

if [ "${1:-}" = "--restart-ap" ]; then
    exec nmcli connection up "$AP_CON"
fi

SSID="${1:-}"
TIMEOUT="${2:-45}"

if [ -z "$SSID" ]; then
    echo "Usage: $0 SSID [TIMEOUT]" >&2
    echo "       $0 --restart-ap" >&2
    exit 2
fi

PASSWORD=$(cat)

nmcli connection down "$AP_CON" 2>/dev/null || true

# Brief pause for nl80211 to finish the AP -> station transition before
# nmcli tries to scan.
sleep 1

# `nmcli device wifi connect` does NOT accept `--` as an end-of-options
# sentinel — it would be consumed as the SSID positional arg and the real
# SSID then becomes "invalid extra argument". An SSID that literally
# starts with a `-` is theoretically ambiguous, but every legitimate
# operator-typed SSID is safe; we accept that very narrow gap rather than
# breaking the common case.
if [ -n "$PASSWORD" ]; then
    exec nmcli --wait "$TIMEOUT" device wifi connect "$SSID" password "$PASSWORD"
else
    exec nmcli --wait "$TIMEOUT" device wifi connect "$SSID"
fi
