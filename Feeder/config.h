#pragma once

// Hardware configuration
#define SERVO_PIN 4
#define BUTTON_PIN 9
#define SERVO_OPEN_ANGLE_DEG 90
#define SERVO_CLOSE_ANGLE_DEG 0
#define SERVO_OPEN_DELAY_MS 600
#define SERVO_SPEED_PERCENT 100
#define SERVO_MIN_ANGLE_DEG -720
#define SERVO_MAX_ANGLE_DEG 720

// Default connectivity configuration
#define DEFAULT_MQTT_HOST "192.168.1.140"
#define DEFAULT_MQTT_PORT 1883
#define DEFAULT_MQTT_BASE "aquarium/feeder"
#define DEFAULT_MQTT_USER "skull"
#define DEFAULT_MQTT_PASS "XUgrute8"

#define AP_SSID_PREFIX "FishFeeder-"
#define DEFAULT_WIFI_SSID ""
#define DEFAULT_WIFI_PASS ""

// Behaviour tuning
#define BUTTON_DEBOUNCE_MS 50
#define BUTTON_LONG_PRESS_MS 3000
#define WIFI_CONNECT_TIMEOUT_MS 10000
#define WIFI_RETRY_INTERVAL_MS 15000
#define MQTT_RETRY_INTERVAL_MS 5000
