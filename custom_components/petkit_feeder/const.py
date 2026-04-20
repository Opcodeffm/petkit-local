"""Constants for Petkit Feeder Local integration."""

DOMAIN = "petkit_feeder"

# Local HTTP server
LOCAL_SERVER_PORT = 80

# Config
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"

# Defaults
DEFAULT_SCAN_INTERVAL = 15  # seconds — feeder heartbeats every ~10s

# Platforms
PLATFORMS = ["sensor", "binary_sensor", "button", "switch"]
