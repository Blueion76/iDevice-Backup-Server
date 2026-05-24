#!/bin/bash

# Clean up stale PID files to allow clean restarts
rm -f /run/dbus/pid /run/avahi-daemon/pid /run/avahi-daemon//pid

# Start dbus (required by avahi)
mkdir -p /var/run/dbus
dbus-daemon --system --fork

# Start Avahi for mDNS discovery (needed for wifi sync)
avahi-daemon -D

# Start usbmuxd in background
usbmuxd -U usbmux &

# iLinuxNetworkBackup-style OpenSSL compatibility profile for pairing/backup
mkdir -p /backups
if [ ! -f /backups/openssl-weak.conf ]; then
cat <<'EOF' > /backups/openssl-weak.conf
.include /etc/ssl/openssl.cnf
[openssl_init]
alg_section = evp_properties
[evp_properties]
rh-allow-sha1-signatures = yes
EOF
fi
export OPENSSL_WEAK_CONF=/backups/openssl-weak.conf

# Start the FastAPI backend
exec uvicorn main:app --host 0.0.0.0 --port 8987
