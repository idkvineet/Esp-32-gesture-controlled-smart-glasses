#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <SPIFFS.h>

// -----------------------------------------------------------------------------
// WiFi CREDENTIALS 
// -----------------------------------------------------------------------------
const char* DEFAULT_WIFI_SSID = "Still didn't concent";  // 
const char* DEFAULT_WIFI_PASS = "samajnahiaatakya"; // 

// If left empty: ESP starts in AP mode
#define WIFI_FILE "/wifi.json"

// -----------------------------------------------------------------------------
// Camera model: AI Thinker OV3660
// -----------------------------------------------------------------------------
#define CAMERA_MODEL_AI_THINKER
#include "camera_pins.h"

WebServer server(80);

// -----------------------------------------------------------------------------
// Load WiFi cred from SPIFFS
// -----------------------------------------------------------------------------
void loadWiFiCredentials(String &ssid, String &pass) {
  if (!SPIFFS.exists(WIFI_FILE)) {
    ssid = DEFAULT_WIFI_SSID;
    pass = DEFAULT_WIFI_PASS;
    return;
  }
  File f = SPIFFS.open(WIFI_FILE, "r");
  StaticJsonDocument<256> doc;

  if (deserializeJson(doc, f) == DeserializationError::Ok) {
    ssid = String((const char*)doc["ssid"]);
    pass = String((const char*)doc["password"]);
  }
  f.close();
}

// -----------------------------------------------------------------------------
// Save WiFi credentials → reboot
// -----------------------------------------------------------------------------
void saveWiFiCredentials(const char* ssid, const char* pass) {
  StaticJsonDocument<256> doc;
  doc["ssid"] = ssid;
  doc["password"] = pass;

  File f = SPIFFS.open(WIFI_FILE, FILE_WRITE);
  serializeJson(doc, f);
  f.close();
}

// -----------------------------------------------------------------------------
// HIGH-FPS MJPEG STREAM HANDLER
// -----------------------------------------------------------------------------
void handleStream() {
  WiFiClient client = server.client();

  client.print(
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n"
  );

  while (client.connected()) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) continue;

    client.print("--frame\r\n");
    client.print("Content-Type: image/jpeg\r\n");
    client.printf("Content-Length: %d\r\n\r\n", fb->len);
    client.write(fb->buf, fb->len);
    client.print("\r\n");

    esp_camera_fb_return(fb);

    delay(1); // minimal delay for max FPS
  }
}

// -----------------------------------------------------------------------------
// HTTP: /update_wifi → update SSID/password
// -----------------------------------------------------------------------------
void handleUpdateWiFi() {
  if (!server.hasArg("plain")) {
    server.send(400, "application/json", "{\"error\":\"missing JSON\"}");
    return;
  }

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
    server.send(400, "application/json", "{\"error\":\"invalid JSON\"}");
    return;
  }

  const char* ssid = doc["ssid"];
  const char* pass = doc["password"];

  if (!ssid || strlen(ssid) == 0) {
    server.send(400, "application/json", "{\"error\":\"ssid required\"}");
    return;
  }

  saveWiFiCredentials(ssid, pass);
  server.send(200, "application/json", "{\"status\":\"saved, rebooting\"}");
  delay(300);
  ESP.restart();
}

// -----------------------------------------------------------------------------
// /status
// -----------------------------------------------------------------------------
void handleStatus() {
  StaticJsonDocument<128> doc;

  doc["ip"] = WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["mode"] = (WiFi.getMode() == WIFI_AP) ? "AP" : "STA";

  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

// -----------------------------------------------------------------------------
// Camera initialization (HIGH FPS CONFIG)
// -----------------------------------------------------------------------------
bool initCamera() {
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

  // 🚀 High FPS + good quality
  config.frame_size = FRAMESIZE_SVGA;  // 800x600 at ~30 FPS
  config.jpeg_quality = 12;            // visually high quality
  config.fb_count = 2;                 // dual buffer = faster camera pipeline

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {
    Serial.printf("Camera init failed 0x%x\n", err);
    return false;
  }

  return true;
}

// -----------------------------------------------------------------------------
// Setup
// -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(100);

  SPIFFS.begin(true);

  // Load WiFi credentials
  String ssid, pass;
  loadWiFiCredentials(ssid, pass);

  // Try STA
  if (ssid.length() > 0) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), pass.c_str());

    Serial.printf("Connecting to %s", ssid.c_str());
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
      delay(250);
      Serial.print(".");
    }
    Serial.println();
  }

  // If connection failed → AP
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi failed → AP mode");
    WiFi.mode(WIFI_AP);
    WiFi.softAP("ESP32CAM-Setup");
  }

  // Init camera
  if (!initCamera()) {
    Serial.println("Camera failure!");
  } else {
    Serial.println("Camera ready.");
  }

  // HTTP routes
  server.on("/stream", HTTP_GET, handleStream);
  server.on("/update_wifi", HTTP_POST, handleUpdateWiFi);
  server.on("/status", HTTP_GET, handleStatus);

  server.begin();

  // OTA
  ArduinoOTA.setHostname("esp32cam");
  ArduinoOTA.begin();

  Serial.print("Ready. IP: ");
  Serial.println(WiFi.localIP());
}

// -----------------------------------------------------------------------------
// Loop
// -----------------------------------------------------------------------------
void loop() {
  server.handleClient();
  ArduinoOTA.handle();
}