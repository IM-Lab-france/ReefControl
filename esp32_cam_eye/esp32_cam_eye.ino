#include <Arduino.h>
#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <ArduinoOTA.h>
#include <DNSServer.h>

#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 1
#define ESP_ARDUINO_VERSION_MINOR 0
#define ESP_ARDUINO_VERSION_PATCH 0
#endif

#if ESP_ARDUINO_VERSION_MAJOR >= 2
#define WIFI_EVT_AP_START ARDUINO_EVENT_WIFI_AP_START
#define WIFI_EVT_AP_STOP ARDUINO_EVENT_WIFI_AP_STOP
#define WIFI_EVT_AP_STACONNECTED ARDUINO_EVENT_WIFI_AP_STACONNECTED
#define WIFI_EVT_AP_STADISCONNECTED ARDUINO_EVENT_WIFI_AP_STADISCONNECTED
#define WIFI_EVT_STA_CONNECTED ARDUINO_EVENT_WIFI_STA_CONNECTED
#define WIFI_EVT_STA_DISCONNECTED ARDUINO_EVENT_WIFI_STA_DISCONNECTED
#else
#define WIFI_EVT_AP_START SYSTEM_EVENT_AP_START
#define WIFI_EVT_AP_STOP SYSTEM_EVENT_AP_STOP
#define WIFI_EVT_AP_STACONNECTED SYSTEM_EVENT_AP_STACONNECTED
#define WIFI_EVT_AP_STADISCONNECTED SYSTEM_EVENT_AP_STADISCONNECTED
#define WIFI_EVT_STA_CONNECTED SYSTEM_EVENT_STA_CONNECTED
#define WIFI_EVT_STA_DISCONNECTED SYSTEM_EVENT_STA_DISCONNECTED
#endif

#define CAMERA_MODEL_AI_THINKER

#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27

#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

const uint32_t WIFI_TIMEOUT_MS = 15000;
const char *AP_PASSWORD = "esp32setup";
const IPAddress AP_IP(192, 168, 4, 1);
const IPAddress AP_GATEWAY(192, 168, 4, 1);
const IPAddress AP_NETMASK(255, 255, 255, 0);

Preferences prefs;
WebServer server(80);
DNSServer dnsServer;
const byte DNS_PORT = 53;
bool dnsRunning = false;

struct WifiConfig
{
  String ssid;
  String password;
};

struct ImageSettings
{
  int brightness;
  int contrast;
  int saturation;
  framesize_t framesize;
};

WifiConfig wifiConfig;
ImageSettings imageSettings;

bool staConnected = false;
bool apMode = false;
bool otaEnabled = false;
String apSSID;

struct FrameSizeMap
{
  const char *name;
  framesize_t size;
};

const FrameSizeMap frameSizeMap[] = {
    {"QVGA", FRAMESIZE_QVGA},
    {"VGA", FRAMESIZE_VGA},
    {"SVGA", FRAMESIZE_SVGA},
    {"XGA", FRAMESIZE_XGA},
    {"UXGA", FRAMESIZE_UXGA}};

String escapeJson(const String &value)
{
  String out;
  out.reserve(value.length());
  for (size_t i = 0; i < value.length(); ++i)
  {
    char c = value[i];
    switch (c)
    {
    case '\\':
      out += "\\\\";
      break;
    case '"':
      out += "\\\"";
      break;
    case '\n':
      out += "\\n";
      break;
    case '\r':
      out += "\\r";
      break;
    case '\t':
      out += "\\t";
      break;
    default:
      if (static_cast<uint8_t>(c) < 0x20)
      {
        char buf[7];
        snprintf(buf, sizeof(buf), "\\u%04x", c);
        out += buf;
      }
      else
      {
        out += c;
      }
    }
  }
  return out;
}

String htmlEscape(const String &value)
{
  String out;
  out.reserve(value.length());
  for (size_t i = 0; i < value.length(); ++i)
  {
    char c = value[i];
    if (c == '&')
      out += "&amp;";
    else if (c == '<')
      out += "&lt;";
    else if (c == '>')
      out += "&gt;";
    else if (c == '\"')
      out += "&quot;";
    else if (c == '\'')
      out += "&#39;";
    else
      out += c;
  }
  return out;
}

framesize_t frameSizeFromString(const String &name)
{
  for (const auto &entry : frameSizeMap)
  {
    if (name.equalsIgnoreCase(entry.name))
    {
      return entry.size;
    }
  }
  return FRAMESIZE_SVGA;
}

String frameSizeToString(framesize_t size)
{
  for (const auto &entry : frameSizeMap)
  {
    if (entry.size == size)
    {
      return String(entry.name);
    }
  }
  return String("SVGA");
}

int clampSetting(int value)
{
  if (value < -2)
    return -2;
  if (value > 2)
    return 2;
  return value;
}

const char *httpMethodToString(HTTPMethod method)
{
  switch (method)
  {
  case HTTP_GET:
    return "GET";
  case HTTP_POST:
    return "POST";
  case HTTP_PUT:
    return "PUT";
  case HTTP_PATCH:
    return "PATCH";
  case HTTP_DELETE:
    return "DELETE";
  case HTTP_OPTIONS:
    return "OPTIONS";
  default:
    return "UNKNOWN";
  }
}

void logHttpRequest(const String &path)
{
  WiFiClient client = server.client();
  IPAddress remote = client ? client.remoteIP() : IPAddress();
  Serial.printf("[HTTP] %s %s from %s (mode %s)\n",
                httpMethodToString(server.method()),
                path.c_str(),
                remote.toString().c_str(),
                apMode ? "AP" : "STA");
}

void wifiEventHandler(WiFiEvent_t event, WiFiEventInfo_t info)
{
  switch (event)
  {
  case WIFI_EVT_AP_START:
    Serial.println("[WIFI] SoftAP started");
    break;
  case WIFI_EVT_AP_STOP:
    Serial.println("[WIFI] SoftAP stopped");
    break;
  case WIFI_EVT_AP_STACONNECTED:
    Serial.printf("[WIFI] Station %02X:%02X:%02X:%02X:%02X:%02X joined AP (aid=%u)\n",
                  info.wifi_ap_staconnected.mac[0], info.wifi_ap_staconnected.mac[1],
                  info.wifi_ap_staconnected.mac[2], info.wifi_ap_staconnected.mac[3],
                  info.wifi_ap_staconnected.mac[4], info.wifi_ap_staconnected.mac[5],
                  info.wifi_ap_staconnected.aid);
    break;
  case WIFI_EVT_AP_STADISCONNECTED:
    Serial.printf("[WIFI] Station %02X:%02X:%02X:%02X:%02X:%02X left AP (aid=%u)\n",
                  info.wifi_ap_stadisconnected.mac[0], info.wifi_ap_stadisconnected.mac[1],
                  info.wifi_ap_stadisconnected.mac[2], info.wifi_ap_stadisconnected.mac[3],
                  info.wifi_ap_stadisconnected.mac[4], info.wifi_ap_stadisconnected.mac[5],
                  info.wifi_ap_stadisconnected.aid);
    break;
  case WIFI_EVT_STA_CONNECTED:
    Serial.println("[WIFI] Connected to upstream AP");
    break;
  case WIFI_EVT_STA_DISCONNECTED:
    Serial.printf("[WIFI] Disconnected from upstream AP (reason %d)\n", info.wifi_sta_disconnected.reason);
    break;
  default:
    break;
  }
}

void loadConfigs()
{
  wifiConfig.ssid = prefs.getString("wifi_ssid", "");
  wifiConfig.password = prefs.getString("wifi_password", "");
  imageSettings.brightness = prefs.getShort("img_brightness", 0);
  imageSettings.contrast = prefs.getShort("img_contrast", 0);
  imageSettings.saturation = prefs.getShort("img_saturation", 0);
  String storedFrame = prefs.getString("img_framesize", "SVGA");
  imageSettings.framesize = frameSizeFromString(storedFrame);
}

void saveWifiConfig(const String &ssid, const String &password)
{
  prefs.putString("wifi_ssid", ssid);
  prefs.putString("wifi_password", password);
  wifiConfig.ssid = ssid;
  wifiConfig.password = password;
}

void saveImageSettings()
{
  prefs.putShort("img_brightness", imageSettings.brightness);
  prefs.putShort("img_contrast", imageSettings.contrast);
  prefs.putShort("img_saturation", imageSettings.saturation);
  prefs.putString("img_framesize", frameSizeToString(imageSettings.framesize));
}

void applyImageSettings()
{
  sensor_t *sensor = esp_camera_sensor_get();
  if (!sensor)
  {
    Serial.println("Sensor not ready");
    return;
  }
  sensor->set_brightness(sensor, clampSetting(imageSettings.brightness));
  sensor->set_contrast(sensor, clampSetting(imageSettings.contrast));
  sensor->set_saturation(sensor, clampSetting(imageSettings.saturation));
  sensor->set_framesize(sensor, imageSettings.framesize);
}

void initCamera()
{
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = imageSettings.framesize;
  config.jpeg_quality = 12;
  config.fb_count = 1;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = CAMERA_FB_IN_PSRAM;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK)
  {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return;
  }
  applyImageSettings();
}

void startAccessPoint()
{
  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%02X%02X", mac[4], mac[5]);
  apSSID = "ESP32-CAM-SETUP-" + String(suffix);
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_GATEWAY, AP_NETMASK);
  WiFi.softAP(apSSID.c_str(), AP_PASSWORD);
  apMode = true;
  staConnected = false;
  otaEnabled = false;
  dnsServer.stop();
  dnsRunning = dnsServer.start(DNS_PORT, "*", AP_IP);
  Serial.printf("Started AP %s @ %s\n", apSSID.c_str(), WiFi.softAPIP().toString().c_str());

  if (dnsRunning)
  {
    Serial.println("DNS captif demarre sur port 53");
  }
  else
  {
    Serial.println("DNS captif indisponible");
  }
}

void startWiFi()
{
  if (wifiConfig.ssid.isEmpty())
  {
    Serial.println("No WiFi credentials stored. Starting AP...");
    startAccessPoint();
    return;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(wifiConfig.ssid.c_str(), wifiConfig.password.c_str());
  Serial.printf("Connecting to %s", wifiConfig.ssid.c_str());
  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < WIFI_TIMEOUT_MS)
  {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED)
  {
    staConnected = true;
    apMode = false;
    if (dnsRunning)
    {
      dnsServer.stop();
      dnsRunning = false;
    }
    Serial.printf("Connected. IP: %s\n", WiFi.localIP().toString().c_str());
  }
  else
  {
    Serial.println("Failed to connect, starting AP");
    startAccessPoint();
  }
}

String buildOtaHostname()
{
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  if (mac.length() >= 4)
  {
    return "ESP32-CAM-EYE-" + mac.substring(mac.length() - 4);
  }
  return "ESP32-CAM-EYE";
}

void setupOTA()
{
  if (!staConnected)
  {
    otaEnabled = false;
    return;
  }
  String host = buildOtaHostname();
  ArduinoOTA.setHostname(host.c_str());
  ArduinoOTA.onStart([]()
                     { Serial.println("OTA update start"); });
  ArduinoOTA.onEnd([]()
                   { Serial.println("\nOTA update end"); });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total)
                        { Serial.printf("OTA progress: %u%%\n", (progress * 100) / total); });
  ArduinoOTA.onError([](ota_error_t error)
                     { Serial.printf("OTA error[%u]\n", error); });
  ArduinoOTA.begin();
  otaEnabled = true;
  Serial.printf("OTA ready on host %s\n", host.c_str());
}

String buildSettingsJsonPayload(bool includeWifi)
{
  String json = "{";
  json += "\"brightness\":" + String(imageSettings.brightness) + ',';
  json += "\"contrast\":" + String(imageSettings.contrast) + ',';
  json += "\"saturation\":" + String(imageSettings.saturation) + ',';
  json += "\"framesize\":\"" + frameSizeToString(imageSettings.framesize) + "\"";
  if (includeWifi)
  {
    json += ",\"wifi_ssid\":\"" + escapeJson(wifiConfig.ssid) + "\"";
  }
  json += '}';
  return json;
}

String buildFullConfigJson()
{
  String json = "{";
  json += "\"wifi_ssid\":\"" + escapeJson(wifiConfig.ssid) + "\",";
  json += "\"brightness\":" + String(imageSettings.brightness) + ',';
  json += "\"contrast\":" + String(imageSettings.contrast) + ',';
  json += "\"saturation\":" + String(imageSettings.saturation) + ',';
  json += "\"framesize\":\"" + frameSizeToString(imageSettings.framesize) + "\"";
  json += '}';
  return json;
}

void sendJsonError(int code, const String &detail)
{
  String json = "{\"error\":\"" + escapeJson(detail) + "\"}";
  server.send(code, "application/json", json);
}

bool extractJsonString(const String &payload, const char *key, String &out)
{
  String pattern = String('\"') + key + '\"';
  int keyIndex = payload.indexOf(pattern);
  if (keyIndex < 0)
    return false;
  int colonIndex = payload.indexOf(':', keyIndex + pattern.length());
  if (colonIndex < 0)
    return false;
  int valueStart = colonIndex + 1;
  while (valueStart < payload.length() && isspace(payload[valueStart]))
    valueStart++;
  if (valueStart >= payload.length() || payload[valueStart] != '\"')
    return false;
  int cursor = valueStart + 1;
  String value;
  while (cursor < payload.length())
  {
    char c = payload[cursor];
    if (c == '\\' && cursor + 1 < payload.length())
    {
      value += payload[cursor + 1];
      cursor += 2;
      continue;
    }
    if (c == '\"')
      break;
    value += c;
    cursor++;
  }
  out = value;
  return true;
}

bool extractJsonInt(const String &payload, const char *key, int &out)
{
  String pattern = String('\"') + key + '\"';
  int keyIndex = payload.indexOf(pattern);
  if (keyIndex < 0)
    return false;
  int colonIndex = payload.indexOf(':', keyIndex + pattern.length());
  if (colonIndex < 0)
    return false;
  int valueStart = colonIndex + 1;
  while (valueStart < payload.length() && isspace(payload[valueStart]))
    valueStart++;
  if (valueStart >= payload.length())
    return false;
  int valueEnd = valueStart;
  while (valueEnd < payload.length() && (isdigit(payload[valueEnd]) || payload[valueEnd] == '-'))
  {
    valueEnd++;
  }
  if (valueEnd == valueStart)
    return false;
  String number = payload.substring(valueStart, valueEnd);
  out = number.toInt();
  return true;
}

void handleRoot()
{
  logHttpRequest(server.uri());
  String modeDescription = staConnected ? String("STA (connecte)") : (apMode ? String("AP (configuration)") : String("STA (non connecte)"));
  String ip = staConnected ? WiFi.localIP().toString() : (apMode ? WiFi.softAPIP().toString() : String("0.0.0.0"));
  String html = F("<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ESP32-CAM</title><style>body{font-family:Arial,Helvetica,sans-serif;margin:24px;background:#f7f7f7;color:#111;}a.button{display:inline-block;margin:8px 8px 0 0;padding:10px 16px;background:#0366d6;color:#fff;border-radius:4px;text-decoration:none;}section{background:#fff;padding:16px;border-radius:8px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,0.1);}h1{margin-top:0;}</style></head><body>");
  html += "<h1>ESP32-CAM Eye</h1><section><h3>Etat WiFi</h3><p>Mode: " + modeDescription + "</p><p>IP: " + ip + "</p></section>";
  html += "<section><h3>Navigation</h3><a class='button' href='/wifi'>Config WiFi</a><a class='button' href='/image'>Reglages image</a><a class='button' href='/capture' target='_blank'>Capture JPEG</a></section>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

String buildFrameOptionsHtml()
{
  String options;
  for (const auto &entry : frameSizeMap)
  {
    options += "<option value='";
    options += entry.name;
    options += "'";
    if (entry.size == imageSettings.framesize)
    {
      options += " selected";
    }
    options += ">";
    options += entry.name;
    options += "</option>";
  }
  return options;
}

void handleWifiPage()
{
  logHttpRequest(server.uri());
  String html = F("<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>WiFi Setup</title><style>body{font-family:Arial;margin:24px;background:#eef2f7;}label{display:block;margin-top:12px;}input{width:100%;padding:8px;margin-top:4px;}button{margin-top:16px;padding:10px 16px;background:#0b8457;color:white;border:none;border-radius:4px;}#status{margin-top:12px;font-weight:bold;}</style></head><body><h2>Configurer le WiFi</h2><p>Entrez le SSID et le mot de passe pour le mode station.</p>");
  html += "<label>SSID<input type='text' id='ssid' value='" + htmlEscape(wifiConfig.ssid) + "'></label>";
  html += "<label>Mot de passe<input type='password' id='password' value='" + htmlEscape(wifiConfig.password) + "'></label>";
  html += F("<button onclick=\"saveWifi()\">Enregistrer</button><div id='status'></div><script>function saveWifi(){const body={ssid:document.getElementById('ssid').value,password:document.getElementById('password').value};fetch('/api/wifi',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(()=>{document.getElementById('status').textContent='Sauvegarde reussie, redemarrage...';}).catch(()=>{document.getElementById('status').textContent='Erreur de sauvegarde';});}</script></body></html>");
  server.send(200, "text/html", html);
}

void handleImagePage()
{
  logHttpRequest(server.uri());
  String html = F("<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Reglages image</title><style>body{font-family:Arial;margin:24px;background:#fefefe;}label{display:block;margin:12px 0 4px;}input[type=range]{width:100%;}select{width:100%;padding:8px;}button{margin-top:16px;padding:10px 16px;background:#0366d6;color:#fff;border:none;border-radius:4px;}#status{margin-top:12px;font-weight:bold;}img{margin-top:16px;max-width:100%;height:auto;border:1px solid #ddd;border-radius:4px;}</style></head><body><h2>Parametres camera</h2>");
  html += "<label>Luminosite: <span id='brightnessValue'>" + String(imageSettings.brightness) + "</span></label>";
  html += "<input type='range' id='brightness' min='-2' max='2' value='" + String(imageSettings.brightness) + "' oninput=\"document.getElementById('brightnessValue').textContent=this.value\">";
  html += "<label>Contraste: <span id='contrastValue'>" + String(imageSettings.contrast) + "</span></label>";
  html += "<input type='range' id='contrast' min='-2' max='2' value='" + String(imageSettings.contrast) + "' oninput=\"document.getElementById('contrastValue').textContent=this.value\">";
  html += "<label>Saturation: <span id='saturationValue'>" + String(imageSettings.saturation) + "</span></label>";
  html += "<input type='range' id='saturation' min='-2' max='2' value='" + String(imageSettings.saturation) + "' oninput=\"document.getElementById('saturationValue').textContent=this.value\">";
  html += "<label>Resolution</label><select id='framesize'>" + buildFrameOptionsHtml() + "</select>";
  html += F("<button onclick=\"saveSettings()\">Appliquer</button><button style='background:#0b8457;margin-left:8px;' onclick=\"refreshPreview()\">Capture test</button><div id='status'></div><img id='preview' alt='Previsualisation' src=''/>\n<script>function saveSettings(){const body={brightness:parseInt(document.getElementById('brightness').value),contrast:parseInt(document.getElementById('contrast').value),saturation:parseInt(document.getElementById('saturation').value),framesize:document.getElementById('framesize').value};fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(()=>{document.getElementById('status').textContent='Reglages appliques';}).catch(()=>{document.getElementById('status').textContent='Erreur d\'enregistrement';});}\nfunction refreshPreview(){document.getElementById('preview').src='/capture?ts='+Date.now();}\n</script></body></html>");
  server.send(200, "text/html", html);
}

void handleCapture()
{
  logHttpRequest(server.uri());
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb)
  {
    sendJsonError(500, "Capture echouee");
    return;
  }
  WiFiClient client = server.client();
  if (!client)
  {
    esp_camera_fb_return(fb);
    sendJsonError(500, "Client invalide");
    return;
  }
  client.print("HTTP/1.1 200 OK\r\n");
  client.print("Content-Type: image/jpeg\r\n");
  client.print("Content-Length: ");
  client.print(fb->len);
  client.print("\r\nConnection: close\r\n\r\n");
  client.write(fb->buf, fb->len);
  client.stop();
  esp_camera_fb_return(fb);
}

void handleApiConfigAll()
{
  logHttpRequest(server.uri());
  server.send(200, "application/json", buildFullConfigJson());
}

void handleApiSettingsGet()
{
  logHttpRequest(server.uri());
  server.send(200, "application/json", buildSettingsJsonPayload(true));
}

void handleApiSettingsPost()
{
  logHttpRequest(server.uri());
  String body = server.arg("plain");
  if (body.length() == 0)
  {
    sendJsonError(400, "Payload vide");
    return;
  }
  bool updated = false;
  int newValue;
  if (extractJsonInt(body, "brightness", newValue))
  {
    imageSettings.brightness = clampSetting(newValue);
    updated = true;
  }
  if (extractJsonInt(body, "contrast", newValue))
  {
    imageSettings.contrast = clampSetting(newValue);
    updated = true;
  }
  if (extractJsonInt(body, "saturation", newValue))
  {
    imageSettings.saturation = clampSetting(newValue);
    updated = true;
  }
  String frameValue;
  if (extractJsonString(body, "framesize", frameValue))
  {
    imageSettings.framesize = frameSizeFromString(frameValue);
    updated = true;
  }
  if (!updated)
  {
    sendJsonError(400, "Aucun parametre valide");
    return;
  }
  applyImageSettings();
  saveImageSettings();
  server.send(200, "application/json", buildSettingsJsonPayload(true));
}

void handleApiWifiPost()
{
  logHttpRequest(server.uri());
  String body = server.arg("plain");
  if (body.length() == 0)
  {
    sendJsonError(400, "Payload vide");
    return;
  }
  String ssid;
  String password;
  if (!extractJsonString(body, "ssid", ssid))
  {
    sendJsonError(400, "SSID manquant");
    return;
  }
  if (!extractJsonString(body, "password", password))
  {
    password = "";
  }
  saveWifiConfig(ssid, password);
  server.send(200, "application/json", "{\"status\":\"saved\"}");
  delay(500);
  ESP.restart();
}

void handleNotFound()
{
  logHttpRequest(server.uri());
  if (apMode)
  {
    // En mode AP, redirige tout vers la page principale
    Serial.println(">>> Redirection captive vers /");
    handleRoot();
  }
  else
  {
    sendJsonError(404, "Route inconnue");
  }
}

void setupServer()
{
  server.on("/", HTTP_GET, handleRoot);
  server.on("/wifi", HTTP_GET, handleWifiPage);
  server.on("/image", HTTP_GET, handleImagePage);
  server.on("/capture", HTTP_GET, handleCapture);
  server.on("/api/config/all", HTTP_GET, handleApiConfigAll);
  server.on("/api/settings", HTTP_GET, handleApiSettingsGet);
  server.on("/api/settings", HTTP_POST, handleApiSettingsPost);
  server.on("/api/wifi", HTTP_POST, handleApiWifiPost);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.println("HTTP server demarre");
}

void setup()
{
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  prefs.begin("CONFIG", false);
  loadConfigs();
  initCamera();
  WiFi.onEvent(wifiEventHandler);
  startWiFi();
  if (staConnected)
  {
    setupOTA();
  }
  setupServer();

  Serial.println("========================================");
  Serial.printf("Mode: %s\n", apMode ? "AP" : "STA");
  Serial.printf("IP: %s\n", apMode ? WiFi.softAPIP().toString().c_str() : WiFi.localIP().toString().c_str());
  if (apMode)
  {
    Serial.printf("SSID: %s\n", apSSID.c_str());
    Serial.printf("Password: %s\n", AP_PASSWORD);
    Serial.println("Connecte-toi et va sur http://192.168.4.1");
  }
}
void loop()
{
  server.handleClient();
  if (apMode && dnsRunning)
  {
    dnsServer.processNextRequest();
  }
  if (otaEnabled)
  {
    ArduinoOTA.handle();
  }
}
