/*
  Reef Controller Firmware v2.0
  Board : Arduino Mega 2560 + RAMPS
  Baud  : 115200
  Protocol overview
    - Every command is a single ASCII line terminated by CR/LF
    - Firmware always replies with either "OK" or "ERR|CODE|MESSAGE"
    - HELLO?/STATUS? expose version info to allow a robust handshake
*/

#if defined(ARDUINO_ARCH_ESP32)
#include <ESP32Servo.h>
#else
#include <Servo.h>
#endif
#include <math.h>
#include <OneWire.h>
#include <DallasTemperature.h>

static const char *FW_VERSION = "2.0.0";
static const uint32_t BAUDRATE = 115200;
static const unsigned long MIN_STEP_DELAY_US = 400;
static const unsigned long STEP_PULSE_US = 4;
static const float R_SERIE = 4700.0f;
static const float BETA = 3950.0f;
static const float R0 = 100000.0f;
static const float T0K = 298.15f;

// --------------- PID controller ----------
typedef float (*SensorReader)();

typedef struct PIDCtrl
{
  float target;
  float Kp;
  float Ki;
  float Kd;
  float integ;
  float prevE;
  unsigned long last_ms;
  int out_pin;
  SensorReader sensor_func;
  float minC;
  float maxC;
  bool fault;
} PIDCtrl;

static PIDCtrl pid_water;
static PIDCtrl pid_reserve;

// ---------------- Pinout -----------------
#define X_STEP 54
#define X_DIR 55
#define X_EN 38
#define Y_STEP 60
#define Y_DIR 61
#define Y_EN 56
#define Z_STEP 46
#define Z_DIR 48
#define Z_EN 62
#define E_STEP 26
#define E_DIR 28
#define E_EN 24

#define HEAT_WATER_PIN 8
#define FAN_PIN 9
#define HEAT_RES_PIN 10
#define SERVO_PIN 11

#define WATER_DS_PIN 3 // X_MIN endstop (digital) pour DS18B20 eau
#define AIR_DS_PIN 2   // X_MAX endstop (digital) pour DS18B20 air
#define YMIN_DS_PIN 14 // Y_MIN endstop (digital) pour DS18B20 #3
#define YMAX_DS_PIN 15 // Y_MAX endstop (digital) pour DS18B20 #4
// pH sur AUX-2 / A9 (entrée analogique libre). Les thermistances analogiques ne sont plus utilisées (toutes les mesures utiles viennent des DS18B20).
#define PH_PIN A9

#define LVL_LOW_PIN 18  // Z_MIN
#define LVL_HIGH_PIN 19 // Z_MAX
#define LVL_ALERT_PIN 4 // libre (AUX-2) pour alerte niveau

static OneWire oneWireWater(WATER_DS_PIN);
static DallasTemperature ds18Water(&oneWireWater);
static OneWire oneWireAir(AIR_DS_PIN);
static DallasTemperature ds18Air(&oneWireAir);
static OneWire oneWireYMin(YMIN_DS_PIN);
static DallasTemperature ds18YMin(&oneWireYMin);
static OneWire oneWireYMax(YMAX_DS_PIN);
static DallasTemperature ds18YMax(&oneWireYMax);

static const uint8_t DS_COUNT = 4;
static DallasTemperature *ds_list[DS_COUNT] = {&ds18Water, &ds18Air, &ds18YMin, &ds18YMax};
static float cached_ds[DS_COUNT] = {NAN, NAN, NAN, NAN};
static bool ds18_pending = false;
static unsigned long ds18_ready_ms = 0;
static unsigned long ds18_next_request_ms = 0;

// --------------- Helpers -----------------
static inline void send_ok() { Serial.println("OK"); }
static inline void send_err(const char *code, const char *message)
{
  Serial.print("ERR|");
  Serial.print(code);
  Serial.print("|");
  Serial.println(message);
}

static float read_ph_voltage()
{
  int raw = analogRead(PH_PIN);
  return (raw / 1023.0f) * 5.0f;
}

static float read_water_tempC()
{
  return cached_ds[0];
}

static float read_air_tempC()
{
  return cached_ds[1];
}

static float read_ymin_tempC()
{
  return cached_ds[2];
}

static float read_ymax_tempC()
{
  return cached_ds[3];
}

static bool level_low() { return digitalRead(LVL_LOW_PIN) == LOW; }
static bool level_high() { return digitalRead(LVL_HIGH_PIN) == LOW; }
static bool level_alert() { return digitalRead(LVL_ALERT_PIN) == LOW; }

static void pid_controller_init(PIDCtrl *p, int out_pin, SensorReader sensor_func, float maxC)
{
  if (!p)
    return;
  p->target = 0.0f;
  p->Kp = 12.0f;
  p->Ki = 0.4f;
  p->Kd = 60.0f;
  p->integ = 0.0f;
  p->prevE = 0.0f;
  p->last_ms = 0;
  p->out_pin = out_pin;
  p->sensor_func = sensor_func;
  p->minC = -5.0f;
  p->maxC = maxC;
  p->fault = false;
}

static void pid_controller_reset(PIDCtrl *p)
{
  if (!p)
    return;
  p->integ = 0;
  p->prevE = 0;
  p->last_ms = 0;
  p->fault = false;
}

static int pid_controller_compute(PIDCtrl *p)
{
  if (!p)
    return 0;
  float t = p->sensor_func ? p->sensor_func() : NAN;
  if (isnan(t) || t < p->minC - 1 || t > p->maxC + 5)
  {
    p->fault = true;
    return 0;
  }

  if (p->target <= 0)
  {
    pid_controller_reset(p);
    return 0;
  }

  unsigned long now = millis();
  float dt = (p->last_ms == 0) ? 0.1f : (now - p->last_ms) / 1000.0f;
  p->last_ms = now;

  float err = p->target - t;
  p->integ += err * dt;
  float deriv = dt > 0 ? (err - p->prevE) / dt : 0;
  p->prevE = err;

  float out = p->Kp * err + p->Ki * p->integ + p->Kd * deriv;
  if (out < 0)
    out = 0;
  if (out > 255)
    out = 255;
  return (int)out;
}

static void heaters_service()
{
  int pwm = pid_controller_compute(&pid_water);
  analogWrite(HEAT_WATER_PIN, pwm > 0 ? 255 : 0);
  pwm = pid_controller_compute(&pid_reserve);
  analogWrite(HEAT_RES_PIN, pwm > 0 ? 255 : 0);
}

static void ds18_service()
{
  unsigned long now = millis();
  if (!ds18_pending && now >= ds18_next_request_ms)
  {
    for (uint8_t i = 0; i < DS_COUNT; ++i)
    {
      ds_list[i]->requestTemperatures();
    }
    ds18_ready_ms = now + 800; // 12-bit conversion ~750ms
    ds18_pending = true;
  }
  else if (ds18_pending && (long)(now - ds18_ready_ms) >= 0)
  {
    for (uint8_t i = 0; i < DS_COUNT; ++i)
    {
      float t = ds_list[i]->getTempCByIndex(0);
      cached_ds[i] = (t > -55.0f && t < 125.0f) ? t : NAN;
    }
    ds18_pending = false;
    ds18_next_request_ms = now + 500; // brief pause before next cycle
  }
}

// --------------- Servo -------------------
static Servo feeder;
static void move_servo(int angle)
{
  feeder.write(constrain(angle, 0, 180));
}

// --------------- Motors ------------------
static bool motors_enabled = false;
static bool abort_steps = false;

static const uint8_t STEP_PIN[4] = {X_STEP, Y_STEP, Z_STEP, E_STEP};
static const uint8_t DIR_PIN[4] = {X_DIR, Y_DIR, Z_DIR, E_DIR};
static const uint8_t EN_PIN[4] = {X_EN, Y_EN, Z_EN, E_EN};

struct AxisState
{
  bool active = false;
  bool dir = true;
  long remaining = 0;
  uint32_t period2_us = 600;
  uint32_t next_step_us = 0;
  bool pending_low = false;
  uint32_t low_deadline_us = 0;
};

static AxisState axis[4];

static void set_motors(bool on)
{
  motors_enabled = on;
  for (int i = 0; i < 4; ++i)
  {
    digitalWrite(EN_PIN[i], on ? LOW : HIGH);
  }
}

static inline uint32_t micros_now() { return micros(); }

static void stepper_service()
{
  uint32_t now = micros_now();
  for (int i = 0; i < 4; ++i)
  {
    AxisState &a = axis[i];
    if (!a.active)
      continue;
    if (a.pending_low && (int32_t)(now - a.low_deadline_us) >= 0)
    {
      digitalWrite(STEP_PIN[i], LOW);
      a.pending_low = false;
    }
    if ((int32_t)(now - a.next_step_us) >= 0)
    {
      if (abort_steps)
      {
        a.active = false;
        continue;
      }
      digitalWrite(STEP_PIN[i], HIGH);
      a.pending_low = true;
      a.low_deadline_us = now + STEP_PULSE_US;
      if (--a.remaining <= 0)
      {
        a.active = false;
      }
      else
      {
        a.next_step_us += a.period2_us;
      }
    }
  }
}

static int axis_from_char(char c)
{
  switch (c)
  {
  case 'X':
    return 0;
  case 'Y':
    return 1;
  case 'Z':
    return 2;
  case 'E':
    return 3;
  }
  return -1;
}

static void deactivate_axes()
{
  for (int i = 0; i < 4; ++i)
  {
    axis[i].active = false;
    digitalWrite(STEP_PIN[i], LOW);
  }
}

// --------------- Fan / AutoCool ----------
static float autocool_thresh = 28.0f;
static int fan_manual = -1; // -1 = auto

static void fan_service()
{
  if (fan_manual >= 0)
  {
    analogWrite(FAN_PIN, constrain(fan_manual, 0, 255));
    return;
  }
  float ta = read_air_tempC();
  if (isnan(ta) || ta <= autocool_thresh)
  {
    analogWrite(FAN_PIN, 0);
    return;
  }
  float delta = ta - autocool_thresh;
  int pwm = (int)(min(delta / 5.0f, 1.0f) * 255.0f);
  analogWrite(FAN_PIN, pwm);
}

// --------------- Status helpers ----------
static void send_status()
{
  Serial.print("STATUS;FW=");
  Serial.print(FW_VERSION);
  Serial.print(";MTR=");
  Serial.print(motors_enabled ? 1 : 0);
  Serial.print(";MTRX=");
  Serial.print(axis[0].active ? 1 : 0);
  Serial.print(";MTRY=");
  Serial.print(axis[1].active ? 1 : 0);
  Serial.print(";MTRZ=");
  Serial.print(axis[2].active ? 1 : 0);
  Serial.print(";MTRE=");
  Serial.print(axis[3].active ? 1 : 0);
  Serial.print(";FAN_MODE=");
  Serial.print(fan_manual >= 0 ? "MAN" : "AUTO");
  Serial.print(";FAN_VAL=");
  Serial.print(fan_manual);
  Serial.print(";AUTO_THRESH=");
  Serial.print(autocool_thresh, 1);
  Serial.print(";PIDW_TGT=");
  Serial.print(pid_water.target, 1);
  Serial.print(";PIDR_TGT=");
  Serial.print(pid_reserve.target, 1);
  Serial.print(";LEVEL_LOW=");
  Serial.print(level_low());
  Serial.print(";LEVEL_HIGH=");
  Serial.print(level_high());
  Serial.print(";LEVEL_ALERT=");
  Serial.print(level_alert());
  Serial.print(";TEMPW=");
  Serial.print(read_water_tempC(), 1);
  Serial.print(";TEMPA=");
  Serial.print(read_air_tempC(), 1);
  Serial.print(";TEMPYMIN=");
  Serial.print(read_ymin_tempC(), 1);
  Serial.print(";TEMPYMAX=");
  Serial.print(read_ymax_tempC(), 1);
  Serial.print(";PH_V=");
  Serial.print(read_ph_voltage(), 3);
  Serial.print(";PH_RAW=");
  Serial.print(analogRead(PH_PIN));
  Serial.print(";SERVO=");
  Serial.print(feeder.read());
  Serial.println();
}

static void send_temps()
{
  Serial.print("T_WATER:");
  Serial.print(read_water_tempC(), 1);
  Serial.print("|T_AIR:");
  Serial.print(read_air_tempC(), 1);
  Serial.print("|T_YMIN:");
  Serial.print(read_ymin_tempC(), 1);
  Serial.print("|T_YMAX:");
  Serial.print(read_ymax_tempC(), 1);
  Serial.print("|PH_V:");
  Serial.print(read_ph_voltage(), 3);
  Serial.print("|PH_RAW:");
  Serial.print(analogRead(PH_PIN));
  Serial.println();
}

static void send_levels()
{
  Serial.print("LEVEL LOW=");
  Serial.print(level_low());
  Serial.print(" HIGH=");
  Serial.print(level_high());
  Serial.print(" ALERT=");
  Serial.print(level_alert());
  Serial.println();
}

// --------------- Command handling --------
static char inbuf[96];
static uint8_t inpos = 0;

static void process_command(String s)
{
  s.trim();
  s.toUpperCase();
  if (s.length() == 0)
    return;

  if (s == "HELLO?")
  {
    Serial.print("HELLO OK;FW=");
    Serial.print(FW_VERSION);
    Serial.println(";BOARD=MEGA");
    return;
  }
  if (s == "STATUS?")
  {
    send_status();
    return;
  }
  if (s == "TEMP?")
  {
    send_temps();
    return;
  }
  if (s == "PH?")
  {
    send_temps();
    return;
  }
  if (s == "LEVEL?")
  {
    send_levels();
    return;
  }

  if (s == "MTR ON")
  {
    set_motors(true);
    abort_steps = false;
    send_ok();
    return;
  }
  if (s == "MTR OFF")
  {
    abort_steps = true;
    set_motors(false);
    deactivate_axes();
    send_ok();
    return;
  }

  if (s.startsWith("FAN "))
  {
    int val = s.substring(4).toInt();
    fan_manual = val < 0 ? -1 : constrain(val, 0, 255);
    send_ok();
    return;
  }

  if (s.startsWith("AUTOCOOL "))
  {
    float val = s.substring(9).toFloat();
    autocool_thresh = constrain(val, 10.0f, 40.0f);
    fan_manual = -1;
    send_ok();
    return;
  }

  if (s.startsWith("HEATW "))
  {
    float v = s.substring(6).toFloat();
    pid_water.target = constrain(v, 0, 40);
    pid_controller_reset(&pid_water);
    send_ok();
    return;
  }

  if (s.startsWith("HEATR "))
  {
    float v = s.substring(6).toFloat();
    pid_reserve.target = constrain(v, 0, 50);
    pid_controller_reset(&pid_reserve);
    send_ok();
    return;
  }

  if (s.startsWith("PIDW "))
  {
    int p = s.indexOf('P');
    int i = s.indexOf('I');
    int d = s.indexOf('D');
    if (p >= 0 && i > p && d > i)
    {
      pid_water.Kp = s.substring(p + 1, i).toFloat();
      pid_water.Ki = s.substring(i + 1, d).toFloat();
      pid_water.Kd = s.substring(d + 1).toFloat();
      pid_controller_reset(&pid_water);
      send_ok();
    }
    else
      send_err("PIDW", "Format PIDW invalide");
    return;
  }

  if (s.startsWith("PIDR "))
  {
    int p = s.indexOf('P');
    int i = s.indexOf('I');
    int d = s.indexOf('D');
    if (p >= 0 && i > p && d > i)
    {
      pid_reserve.Kp = s.substring(p + 1, i).toFloat();
      pid_reserve.Ki = s.substring(i + 1, d).toFloat();
      pid_reserve.Kd = s.substring(d + 1).toFloat();
      pid_controller_reset(&pid_reserve);
      send_ok();
    }
    else
      send_err("PIDR", "Format PIDR invalide");
    return;
  }

  if (s.startsWith("SERVO "))
  {
    move_servo(s.substring(6).toInt());
    send_ok();
    return;
  }

  if (s.startsWith("PUMP "))
  {
    if (level_low())
    {
      send_err("PUMP_LEVEL", "Niveau bas, pompe bloquée");
      return;
    }
    char axc = s.charAt(5);
    int ax = axis_from_char(axc);
    if (ax < 0)
    {
      send_err("PUMP_AXIS", "Axe inconnu");
      return;
    }
    int sp = s.indexOf(' ', 6);
    int sp2 = s.indexOf(' ', sp + 1);
    if (sp < 0 || sp2 < 0)
    {
      send_err("PUMP_ARGS", "Arguments manquants");
      return;
    }
    long steps = s.substring(sp + 1, sp2).toInt();
    unsigned long us = max((unsigned long)s.substring(sp2 + 1).toInt(), MIN_STEP_DELAY_US);
    if (steps == 0)
    {
      send_ok();
      return;
    }
    AxisState &a = axis[ax];
    if (a.active)
    {
      send_err("PUMP_BUSY", "Axe occupé");
      return;
    }
    a.dir = steps > 0;
    a.remaining = labs(steps);
    a.period2_us = 2 * us;
    a.next_step_us = micros_now() + 100;
    digitalWrite(DIR_PIN[ax], a.dir ? HIGH : LOW);
    if (!motors_enabled)
      set_motors(true);
    a.active = true;
    abort_steps = false;
    send_ok();
    return;
  }

  if (s.startsWith("SERVOFEED"))
  {
    // simple feed sequence
    for (int i = 0; i < 2; ++i)
    {
      move_servo(120);
      delay(500);
      move_servo(10);
      delay(500);
    }
    send_ok();
    return;
  }

  send_err("UNKNOWN_CMD", s.c_str());
}

// --------------- Setup/loop --------------
void setup()
{
  Serial.begin(BAUDRATE);
  for (uint8_t i = 0; i < DS_COUNT; ++i)
  {
    ds_list[i]->begin();
    ds_list[i]->setWaitForConversion(false); // non-bloquant
    ds_list[i]->setResolution(12);
  }

  pinMode(X_STEP, OUTPUT);
  pinMode(X_DIR, OUTPUT);
  pinMode(X_EN, OUTPUT);
  pinMode(Y_STEP, OUTPUT);
  pinMode(Y_DIR, OUTPUT);
  pinMode(Y_EN, OUTPUT);
  pinMode(Z_STEP, OUTPUT);
  pinMode(Z_DIR, OUTPUT);
  pinMode(Z_EN, OUTPUT);
  pinMode(E_STEP, OUTPUT);
  pinMode(E_DIR, OUTPUT);
  pinMode(E_EN, OUTPUT);
  for (int i = 0; i < 4; ++i)
  {
    digitalWrite(STEP_PIN[i], LOW);
  }
  set_motors(false);

  pinMode(LVL_LOW_PIN, INPUT_PULLUP);
  pinMode(LVL_HIGH_PIN, INPUT_PULLUP);
  pinMode(LVL_ALERT_PIN, INPUT_PULLUP);

  pinMode(HEAT_WATER_PIN, OUTPUT);
  pinMode(HEAT_RES_PIN, OUTPUT);
  pinMode(FAN_PIN, OUTPUT);

  pid_controller_init(&pid_water, HEAT_WATER_PIN, read_water_tempC, 40.0f);
  pid_controller_init(&pid_reserve, HEAT_RES_PIN, NULL, 60.0f); // reserve desactivee faute de capteur

  feeder.attach(SERVO_PIN);
  move_servo(10);

  Serial.print("BOOTING FW ");
  Serial.println(FW_VERSION);
}

void loop()
{
  ds18_service();
  stepper_service();
  heaters_service();
  fan_service();

  while (Serial.available())
  {
    char c = Serial.read();
    if (c == '\r' || c == '\n')
    {
      if (inpos > 0)
      {
        inbuf[inpos] = '\0';
        process_command(String(inbuf));
        inpos = 0;
      }
    }
    else if (inpos < sizeof(inbuf) - 1)
    {
      inbuf[inpos++] = c;
    }
  }
}
