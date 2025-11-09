#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <Preferences.h>

#include "config.h"
#include "webpage.h"

namespace {
constexpr uint16_t DNS_PORT = 53;
constexpr const char *PREF_NAMESPACE = "feeder";
constexpr const char *KEY_WIFI_SSID = "wifi_ssid";
constexpr const char *KEY_WIFI_PASS = "wifi_pass";
constexpr const char *KEY_MQTT_HOST = "mqtt_host";
constexpr const char *KEY_MQTT_PORT = "mqtt_port";
constexpr const char *KEY_MQTT_BASE = "mqtt_base";
constexpr const char *KEY_MQTT_USER = "mqtt_user";
constexpr const char *KEY_MQTT_PASS = "mqtt_pass";
constexpr const char *KEY_SERVO_OPEN = "servo_open";
constexpr const char *KEY_SERVO_DELAY = "servo_delay";
constexpr const char *KEY_SERVO_SPEED = "servo_speed";
constexpr const char *KEY_SERVO_CLOSE = "servo_close";

String sanitizeBaseTopic(const String &base) {
  String topic = base;
  topic.trim();
  while (topic.endsWith("/")) {
    topic.remove(topic.length() - 1);
  }
  if (topic.length() == 0) {
    topic = DEFAULT_MQTT_BASE;
  }
  return topic;
}

String deviceId() {
  uint64_t mac = ESP.getEfuseMac();
  char buffer[13];
  snprintf(buffer, sizeof(buffer), "%012llX", mac);
  return String(buffer);
}

String jsonEscape(const String &input) {
  String escaped;
  escaped.reserve(input.length() + 4);
  for (size_t i = 0; i < input.length(); ++i) {
    char c = input.charAt(i);
    switch (c) {
      case '"':
        escaped += "\\\"";
        break;
      case '\\':
        escaped += "\\\\";
        break;
      case '\b':
        escaped += "\\b";
        break;
      case '\f':
        escaped += "\\f";
        break;
      case '\n':
        escaped += "\\n";
        break;
      case '\r':
        escaped += "\\r";
        break;
      case '\t':
        escaped += "\\t";
        break;
      default:
        if (static_cast<uint8_t>(c) < 0x20) {
          char buf[7];
          snprintf(buf, sizeof(buf), "\\u%04X", static_cast<uint8_t>(c));
          escaped += buf;
        } else {
          escaped += c;
        }
        break;
    }
  }
  return escaped;
}

int clampServoAngleDeg(int angle) {
  return constrain(angle, SERVO_MIN_ANGLE_DEG, SERVO_MAX_ANGLE_DEG);
}

int servoDegToRaw(int angle) {
  int clamped = clampServoAngleDeg(angle);
  long span = static_cast<long>(SERVO_MAX_ANGLE_DEG) - static_cast<long>(SERVO_MIN_ANGLE_DEG);
  long shifted = static_cast<long>(clamped) - static_cast<long>(SERVO_MIN_ANGLE_DEG);
  long raw = (shifted * 180L) / span;
  if (raw < 0) {
    raw = 0;
  } else if (raw > 180) {
    raw = 180;
  }
  return static_cast<int>(raw);
}

int servoRawToDeg(int raw) {
  raw = constrain(raw, 0, 180);
  long span = static_cast<long>(SERVO_MAX_ANGLE_DEG) - static_cast<long>(SERVO_MIN_ANGLE_DEG);
  long scaled = (static_cast<long>(raw) * span) / 180L;
  long result = scaled + static_cast<long>(SERVO_MIN_ANGLE_DEG);
  return static_cast<int>(result);
}
}  // namespace

Preferences preferences;
WebServer server(80);
DNSServer dnsServer;
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
Servo feederServo;

String wifiSsid = DEFAULT_WIFI_SSID;
String wifiPass = DEFAULT_WIFI_PASS;
String mqttHost = DEFAULT_MQTT_HOST;
uint16_t mqttPort = DEFAULT_MQTT_PORT;
String mqttBaseTopic = sanitizeBaseTopic(DEFAULT_MQTT_BASE);
String mqttUser = DEFAULT_MQTT_USER;
String mqttPass = DEFAULT_MQTT_PASS;
int servoOpenAngleDeg = SERVO_OPEN_ANGLE_DEG;
int servoCloseAngleDeg = SERVO_CLOSE_ANGLE_DEG;
int servoOpenDelayMs = SERVO_OPEN_DELAY_MS;
int servoSpeedPercent = SERVO_SPEED_PERCENT;
int currentServoAngleDeg = SERVO_CLOSE_ANGLE_DEG;

String availabilityTopic;
String stateTopic;
String commandTopic;

String apSSID;
bool apMode = false;
bool portalActive = false;
bool dnsServerActive = false;

unsigned long lastWiFiAttempt = 0;
unsigned long wifiDisconnectedSince = 0;
unsigned long lastMqttAttempt = 0;

unsigned long lastFeedCompletionMillis = 0;

bool buttonIsPressed = false;
bool longPressHandled = false;
unsigned long buttonDownAt = 0;

bool feedPending = false;
String pendingFeedSource;
bool feedingInProgress = false;

// Forward declarations
void startAPMode(bool forced);
void stopAPMode();
void connectToWiFi(bool force = false);
void ensureMqttConnected();
void publishAvailability(bool online);
void publishState(const char *status);
bool doFeed(const char *source);
void handleButton();
void handleNetwork();
void handleMQTT();
void loadConfig();
void setupWebServer();
void handleSaveWifi();
void handleSaveMqtt();
void handleSaveServo();
void handleFeedRequest();
void handleRestart();
void handleStatus();
void updateMqttTopics();
void updateMqttClientConfig();
void onMqttMessage(char *topic, byte *payload, unsigned int length);
void moveServoSmooth(int startAngle, int endAngle, int speedPercent);

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.printf("\n[Feeder] Booting device %s\n", deviceId().c_str());

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  preferences.begin(PREF_NAMESPACE, false);
  loadConfig();
  updateMqttTopics();
  updateMqttClientConfig();

  feederServo.attach(SERVO_PIN);
  feederServo.write(servoDegToRaw(servoCloseAngleDeg));
  currentServoAngleDeg = servoCloseAngleDeg;

  mqttClient.setCallback(onMqttMessage);
  mqttClient.setKeepAlive(20);
  mqttClient.setSocketTimeout(5);

  connectToWiFi(true);
  setupWebServer();
}

void loop() {
  handleButton();
  handleNetwork();
  handleMQTT();

  if (portalActive && dnsServerActive) {
    dnsServer.processNextRequest();
  }
  server.handleClient();

  if (feedPending && !feedingInProgress) {
    String source = pendingFeedSource;
    feedPending = false;
    if (source.length() == 0) {
      source = "web";
    }
    doFeed(source.c_str());
  }
}

void loadConfig() {
  wifiSsid = preferences.getString(KEY_WIFI_SSID, DEFAULT_WIFI_SSID);
  wifiPass = preferences.getString(KEY_WIFI_PASS, DEFAULT_WIFI_PASS);
  mqttHost = preferences.getString(KEY_MQTT_HOST, DEFAULT_MQTT_HOST);
  mqttPort = preferences.getUShort(KEY_MQTT_PORT, DEFAULT_MQTT_PORT);
  mqttBaseTopic = sanitizeBaseTopic(preferences.getString(KEY_MQTT_BASE, DEFAULT_MQTT_BASE));
  mqttUser = preferences.getString(KEY_MQTT_USER, DEFAULT_MQTT_USER);
  mqttPass = preferences.getString(KEY_MQTT_PASS, DEFAULT_MQTT_PASS);
  servoOpenAngleDeg = constrain(static_cast<int>(preferences.getInt(KEY_SERVO_OPEN, SERVO_OPEN_ANGLE_DEG)),
                                SERVO_MIN_ANGLE_DEG, SERVO_MAX_ANGLE_DEG);
  servoCloseAngleDeg = constrain(static_cast<int>(preferences.getInt(KEY_SERVO_CLOSE, SERVO_CLOSE_ANGLE_DEG)),
                                 SERVO_MIN_ANGLE_DEG, SERVO_MAX_ANGLE_DEG);
  servoOpenDelayMs = preferences.getUInt(KEY_SERVO_DELAY, SERVO_OPEN_DELAY_MS);
  servoSpeedPercent = constrain(preferences.getUChar(KEY_SERVO_SPEED, SERVO_SPEED_PERCENT), 1, 100);

  Serial.println("[Config] Loaded configuration:");
  Serial.printf("         Wi-Fi SSID: %s\n", wifiSsid.c_str());
  Serial.printf("         MQTT host : %s:%u\n", mqttHost.c_str(), mqttPort);
  Serial.printf("         Base topic: %s\n", mqttBaseTopic.c_str());
  if (mqttUser.length() > 0) {
    Serial.printf("         MQTT user : %s\n", mqttUser.c_str());
  } else {
    Serial.println("         MQTT user : <none>");
  }
  Serial.printf("         Servo open: %d deg\n", servoOpenAngleDeg);
  Serial.printf("         Servo close: %d deg\n", servoCloseAngleDeg);
  Serial.printf("         Servo wait: %d ms\n", servoOpenDelayMs);
  Serial.printf("         Servo speed: %d%%\n", servoSpeedPercent);
}

void updateMqttTopics() {
  mqttBaseTopic = sanitizeBaseTopic(mqttBaseTopic);
  availabilityTopic = mqttBaseTopic + "/availability";
  stateTopic = mqttBaseTopic + "/state";
  commandTopic = mqttBaseTopic + "/command";
}

void updateMqttClientConfig() {
  mqttClient.setServer(mqttHost.c_str(), mqttPort);
  lastMqttAttempt = 0;
  mqttClient.disconnect();
}

void connectToWiFi(bool force) {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  if (wifiSsid.isEmpty() || wifiPass.isEmpty()) {
    Serial.println("[Wi-Fi] Missing Wi-Fi credentials, starting AP mode");
    startAPMode(true);
    return;
  }

  unsigned long now = millis();
  if (!force && now - lastWiFiAttempt < WIFI_RETRY_INTERVAL_MS) {
    return;
  }

  lastWiFiAttempt = now;

  Serial.printf("[Wi-Fi] Connecting to SSID \"%s\" ...\n", wifiSsid.c_str());
  WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());

  unsigned long startMillis = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startMillis < WIFI_CONNECT_TIMEOUT_MS) {
    delay(200);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[Wi-Fi] Connected, IP address: %s\n", WiFi.localIP().toString().c_str());
    wifiDisconnectedSince = 0;
    if (apMode) {
      stopAPMode();
    }
    publishAvailability(true);
  } else {
    Serial.println("[Wi-Fi] Connection timeout, starting AP mode");
    startAPMode(false);
  }
}

void startAPMode(bool forced) {
  if (apMode && portalActive) {
    return;
  }

  if (forced) {
    publishAvailability(false);
  }

  wifi_mode_t currentMode = WiFi.getMode();
  if (currentMode == WIFI_MODE_STA || currentMode == WIFI_MODE_APSTA) {
    WiFi.disconnect(true);
  }

  String suffix = deviceId();
  suffix = suffix.substring(suffix.length() - 4);
  suffix.toUpperCase();
  apSSID = String(AP_SSID_PREFIX) + suffix;

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apSSID.c_str());
  delay(100);
  IPAddress apIP = WiFi.softAPIP();
  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServerActive = dnsServer.start(DNS_PORT, "*", apIP);

  if (!dnsServerActive) {
    Serial.println("[AP] Failed to start DNS server");
  }

  portalActive = true;
  apMode = true;

  Serial.printf("[AP] Started portal SSID: %s IP: %s\n", apSSID.c_str(), apIP.toString().c_str());
}

void stopAPMode() {
  if (!apMode) {
    return;
  }
  Serial.println("[AP] Stopping portal");
  if (dnsServerActive) {
    dnsServer.stop();
    dnsServerActive = false;
  }
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  portalActive = false;
  apMode = false;
}

void ensureMqttConnected() {
  if (mqttHost.isEmpty()) {
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (mqttClient.connected()) {
    return;
  }

  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RETRY_INTERVAL_MS) {
    return;
  }
  lastMqttAttempt = now;

  String clientId = "Feeder-" + deviceId();
  Serial.printf("[MQTT] Connecting as %s to %s:%u\n", clientId.c_str(), mqttHost.c_str(), mqttPort);

  const char *userPtr = mqttUser.length() ? mqttUser.c_str() : nullptr;
  const char *passPtr = mqttPass.length() ? mqttPass.c_str() : nullptr;

  bool connected = mqttClient.connect(
      clientId.c_str(),
      userPtr,
      passPtr,
      availabilityTopic.c_str(),
      1,
      true,
      "offline");

  if (connected) {
    Serial.println("[MQTT] Connected");
    mqttClient.publish(availabilityTopic.c_str(), "online", true);
    mqttClient.subscribe(commandTopic.c_str());
    publishState("idle");
  } else {
    Serial.printf("[MQTT] Connection failed, rc=%d\n", mqttClient.state());
  }
}

void publishAvailability(bool online) {
  if (!mqttClient.connected()) {
    return;
  }
  mqttClient.publish(availabilityTopic.c_str(), online ? "online" : "offline", true);
}

void publishState(const char *status) {
  if (!mqttClient.connected()) {
    return;
  }

  String payload = "{";
  payload += "\"status\":\"";
  payload += status;
  payload += "\",\"lastFeedMs\":";
  payload += lastFeedCompletionMillis;
  payload += ",\"servoOpenAngle\":";
  payload += servoOpenAngleDeg;
  payload += ",\"servoCloseAngle\":";
  payload += servoCloseAngleDeg;
  payload += ",\"servoOpenDelayMs\":";
  payload += servoOpenDelayMs;
  payload += ",\"servoSpeedPercent\":";
  payload += servoSpeedPercent;
  payload += "}";

  mqttClient.publish(stateTopic.c_str(), payload.c_str(), true);
}

void moveServoSmooth(int startAngleDeg, int endAngleDeg, int speedPercent) {
  speedPercent = constrain(speedPercent, 1, 100);
  int startClamped = clampServoAngleDeg(startAngleDeg);
  int endClamped = clampServoAngleDeg(endAngleDeg);
int startRaw = servoDegToRaw(startClamped);
int endRaw = servoDegToRaw(endClamped);

  feederServo.write(startRaw);

  if (startRaw == endRaw || speedPercent >= 100) {
    feederServo.write(endRaw);
    return;
  }

  int direction = (endRaw > startRaw) ? 1 : -1;
  int steps = abs(endRaw - startRaw);
  int delayPerStep = (100 - speedPercent) / 2;
  if (delayPerStep < 1 && steps > 0) {
    delayPerStep = 1;
  }

  int angle = startRaw;
  for (int i = 0; i < steps; ++i) {
    angle += direction;
    feederServo.write(angle);
    delay(delayPerStep);
  }
  feederServo.write(endRaw);
}

bool doFeed(const char *source) {
  if (feedingInProgress) {
    Serial.printf("[Feed] Ignored (%s) because a feed is already running\n", source);
    return false;
  }
  feedingInProgress = true;
  Serial.printf("[Feed] Triggered by %s\n", source);
  publishState("feeding");

  int targetOpen = clampServoAngleDeg(servoOpenAngleDeg);
  moveServoSmooth(currentServoAngleDeg, targetOpen, servoSpeedPercent);
  currentServoAngleDeg = targetOpen;
  if (servoOpenDelayMs > 0) {
    delay(servoOpenDelayMs);
  }
  moveServoSmooth(currentServoAngleDeg, servoCloseAngleDeg, servoSpeedPercent);
  currentServoAngleDeg = servoCloseAngleDeg;

  lastFeedCompletionMillis = millis();
  publishState("idle");
  feedingInProgress = false;
  return true;
}

void handleButton() {
  bool pressed = digitalRead(BUTTON_PIN) == LOW;
  unsigned long now = millis();

  if (pressed && !buttonIsPressed) {
    buttonIsPressed = true;
    buttonDownAt = now;
    longPressHandled = false;
  } else if (pressed && buttonIsPressed && !longPressHandled &&
             (now - buttonDownAt) >= BUTTON_LONG_PRESS_MS) {
    longPressHandled = true;
    Serial.println("[Button] Long press detected, entering AP mode");
    startAPMode(true);
  } else if (!pressed && buttonIsPressed) {
    unsigned long duration = now - buttonDownAt;
    buttonIsPressed = false;
    if (duration >= BUTTON_DEBOUNCE_MS && !longPressHandled) {
      doFeed("button");
    }
  }
}

void handleNetwork() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiDisconnectedSince = 0;
    if (apMode) {
      stopAPMode();
    }
    return;
  }

  if (mqttClient.connected()) {
    mqttClient.disconnect();
  }

  unsigned long now = millis();
  if (wifiDisconnectedSince == 0) {
    wifiDisconnectedSince = now;
  }

  if (!apMode && (now - wifiDisconnectedSince) > (WIFI_CONNECT_TIMEOUT_MS * 2)) {
    startAPMode(false);
  }

  if (!apMode) {
    connectToWiFi();
  }
}

void handleMQTT() {
  ensureMqttConnected();
  if (mqttClient.connected()) {
    mqttClient.loop();
  }
}

void setupWebServer() {
  server.on("/", HTTP_GET, []() {
    server.send_P(200, "text/html", INDEX_HTML);
  });

  server.on("/feed", HTTP_POST, handleFeedRequest);
  server.on("/saveWifi", HTTP_POST, handleSaveWifi);
  server.on("/saveMqtt", HTTP_POST, handleSaveMqtt);
  server.on("/saveServo", HTTP_POST, handleSaveServo);
  server.on("/restart", HTTP_POST, handleRestart);
  server.on("/status", HTTP_GET, handleStatus);

  server.onNotFound([]() {
    if (portalActive) {
      server.sendHeader("Location", "/", true);
      server.send(302, "text/plain", "");
    } else {
      server.send(404, "text/plain", "Not found");
    }
  });

  server.begin();
  Serial.println("[HTTP] Web server started on port 80");
}

void handleFeedRequest() {
  server.send(200, "text/plain", "Nourrissage lance.");
  pendingFeedSource = "web";
  feedPending = true;
}

void handleSaveWifi() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  ssid.trim();
  pass.trim();

  preferences.putString(KEY_WIFI_SSID, ssid);
  preferences.putString(KEY_WIFI_PASS, pass);

  wifiSsid = ssid;
  wifiPass = pass;

  server.send(200, "text/plain",
              "Configuration Wi-Fi sauvegardee. Nouvelle connexion en cours.");

  stopAPMode();
  connectToWiFi(true);
}

void handleSaveMqtt() {
  String host = server.arg("host");
  String portStr = server.arg("port");
  String base = server.arg("base");
  String user = server.arg("user");
  String passwd = server.arg("pwd");

  host.trim();
  portStr.trim();
  base.trim();
  user.trim();
  passwd.trim();

  uint16_t port = mqttPort;
  if (portStr.length() > 0) {
    port = static_cast<uint16_t>(portStr.toInt());
    if (port == 0) {
      port = DEFAULT_MQTT_PORT;
    }
  }

  preferences.putString(KEY_MQTT_HOST, host);
  preferences.putUShort(KEY_MQTT_PORT, port);
  preferences.putString(KEY_MQTT_BASE, base);
  preferences.putString(KEY_MQTT_USER, user);
  preferences.putString(KEY_MQTT_PASS, passwd);

  mqttHost = host;
  mqttPort = port;
  mqttBaseTopic = sanitizeBaseTopic(base);
  mqttUser = user;
  mqttPass = passwd;
  updateMqttTopics();
  updateMqttClientConfig();

  server.send(200, "text/plain", "Configuration MQTT sauvegardee.");
}

void handleSaveServo() {
  String openStr = server.arg("openAngle");
  String closeStr = server.arg("closeAngle");
  String delayStr = server.arg("openDelay");
  String speedStr = server.arg("speed");
  String changedField = server.arg("changedField");
  changedField.trim();

  int newOpen = servoOpenAngleDeg;
  int newClose = servoCloseAngleDeg;
  int newDelay = servoOpenDelayMs;
  int newSpeed = servoSpeedPercent;

  bool hasOpen = openStr.length() > 0;
  bool hasClose = closeStr.length() > 0;
  bool hasDelay = delayStr.length() > 0;
  bool hasSpeed = speedStr.length() > 0;

  if (hasOpen) {
    newOpen = clampServoAngleDeg(openStr.toInt());
  }
  if (hasClose) {
    newClose = clampServoAngleDeg(closeStr.toInt());
  }
  if (hasDelay) {
    int parsedDelay = delayStr.toInt();
    if (parsedDelay < 0) {
      parsedDelay = 0;
    }
    newDelay = parsedDelay;
  }
  if (hasSpeed) {
    newSpeed = constrain(speedStr.toInt(), 1, 100);
  }

  servoOpenAngleDeg = newOpen;
  servoCloseAngleDeg = newClose;
  servoOpenDelayMs = newDelay;
  servoSpeedPercent = newSpeed;

  preferences.putInt(KEY_SERVO_OPEN, servoOpenAngleDeg);
  preferences.putInt(KEY_SERVO_CLOSE, servoCloseAngleDeg);
  preferences.putUInt(KEY_SERVO_DELAY, static_cast<uint32_t>(servoOpenDelayMs));
  preferences.putUChar(KEY_SERVO_SPEED, static_cast<uint8_t>(servoSpeedPercent));

  bool changedOpen = changedField.equalsIgnoreCase("openAngle");
  bool changedClose = changedField.equalsIgnoreCase("closeAngle");

  if (changedOpen && !changedClose) {
    moveServoSmooth(currentServoAngleDeg, servoOpenAngleDeg, servoSpeedPercent);
    currentServoAngleDeg = servoOpenAngleDeg;
  } else if (changedClose && !changedOpen) {
    moveServoSmooth(currentServoAngleDeg, servoCloseAngleDeg, servoSpeedPercent);
    currentServoAngleDeg = servoCloseAngleDeg;
  } else if (hasOpen && !hasClose) {
    moveServoSmooth(currentServoAngleDeg, servoOpenAngleDeg, servoSpeedPercent);
    currentServoAngleDeg = servoOpenAngleDeg;
  } else if (hasClose && !hasOpen) {
    moveServoSmooth(currentServoAngleDeg, servoCloseAngleDeg, servoSpeedPercent);
    currentServoAngleDeg = servoCloseAngleDeg;
  } else {
    moveServoSmooth(currentServoAngleDeg, servoCloseAngleDeg, servoSpeedPercent);
    currentServoAngleDeg = servoCloseAngleDeg;
  }

  server.send(200, "text/plain", "Parametres servo sauvegardes.");
}

void handleRestart() {
  server.send(200, "text/plain", "Redemarrage...");
  delay(200);
  ESP.restart();
}

void handleStatus() {
  bool wifiUp = (WiFi.status() == WL_CONNECTED);
  String apName = apMode ? apSSID : String("");
  String payload = "{";
  payload += "\"wifiConnected\":";
  payload += wifiUp ? "true" : "false";
  payload += ",\"ip\":\"";
  payload += wifiUp ? WiFi.localIP().toString() : String("");
  payload += "\",\"apMode\":";
  payload += apMode ? "true" : "false";
  payload += ",\"apSsid\":\"";
  payload += jsonEscape(apName);
  payload += "\",\"lastFeedMs\":";
  payload += lastFeedCompletionMillis;
  payload += ",\"wifiSsid\":\"";
  payload += jsonEscape(wifiSsid);
  payload += "\",\"wifiPass\":\"";
  payload += jsonEscape(wifiPass);
  payload += "\",\"mqttHost\":\"";
  payload += jsonEscape(mqttHost);
  payload += "\",\"mqttPort\":";
  payload += mqttPort;
  payload += ",\"mqttBase\":\"";
  payload += jsonEscape(mqttBaseTopic);
  payload += "\",\"mqttUser\":\"";
  payload += jsonEscape(mqttUser);
  payload += "\",\"mqttPass\":\"";
  payload += jsonEscape(mqttPass);
  payload += "\",\"servoOpenAngle\":";
  payload += servoOpenAngleDeg;
  payload += ",\"servoCloseAngle\":";
  payload += servoCloseAngleDeg;
  payload += ",\"servoOpenDelayMs\":";
  payload += servoOpenDelayMs;
  payload += ",\"servoSpeedPercent\":";
  payload += servoSpeedPercent;
  payload += ",\"servoMinAngle\":";
  payload += SERVO_MIN_ANGLE_DEG;
  payload += ",\"servoMaxAngle\":";
  payload += SERVO_MAX_ANGLE_DEG;
  payload += "}";
  server.send(200, "application/json", payload);
}

void onMqttMessage(char *topic, byte *payload, unsigned int length) {
  String incomingTopic(topic);
  if (incomingTopic != commandTopic) {
    return;
  }

  String message;
  message.reserve(length);
  for (unsigned int i = 0; i < length; ++i) {
    message += static_cast<char>(payload[i]);
  }
  message.trim();
  message.toUpperCase();

  Serial.printf("[MQTT] Command received: %s\n", message.c_str());
  if (message == "FEED") {
    bool success = doFeed("mqtt");
    if (!success) {
      Serial.println("[MQTT] Feed ignored (cooldown)");
    }
  }
}
