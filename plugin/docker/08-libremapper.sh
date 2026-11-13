#!/usr/bin/with-contenv sh

mkdir -p /config /data/libremapper/runtime /data/libremapper/plans /oxidized
chown -R librenms:librenms /data/libremapper

if [ "${LIBREMAPPER_DISABLE_SERVICES:-false}" = "true" ]; then
    echo "LibreNMS background services disabled for the isolated synthetic test."
    rm -rf /etc/services.d/cron /etc/services.d/dispatcher /etc/services.d/snmpd /etc/services.d/snmptrapd
fi
