#!/usr/bin/with-contenv bashio
set -e
bashio::log.info 'Starting AC500 Hybrid Modbus TCP + encrypted BLE/MQTT bridge'
if bashio::config.true 'debug'; then
  export DEBUG=true
fi
if ! bashio::config.has_value 'mqtt_host'; then
  export BLUETTI_MQTT_HOST="$(bashio::services mqtt host)"
fi
if ! bashio::config.has_value 'mqtt_port'; then
  export BLUETTI_MQTT_PORT="$(bashio::services mqtt port)"
fi
if ! bashio::config.has_value 'mqtt_username'; then
  export BLUETTI_MQTT_USER="$(bashio::services mqtt username)"
fi
if ! bashio::config.has_value 'mqtt_password'; then
  export BLUETTI_MQTT_PASSWORD="$(bashio::services mqtt password)"
fi
exec python3 -u /app/bridge.py
