// --- RAMPS 1.4 Pins ---
#define X_STEP 54
#define X_DIR  55
#define X_EN   38

#define Y_STEP 60
#define Y_DIR  61
#define Y_EN   56

#define Z_STEP 46
#define Z_DIR  48
#define Z_EN   62

#define E_STEP 26
#define E_DIR  28
#define E_EN   24

#define BED_PIN 8        // Mosfet Bed
#define TH_0 A13         // Thermistance hotend T0
#define TH_1 A14         // Thermistance bed T1

// --- Thermistance (Type 100k / B3950) ---
const float R_SERIE = 4700.0;
const float T0_K = 298.15;   // 25°C
const float BETA = 3950.0;
const float R0 = 100000.0;

// --- Prototypes ---
void moveMotor(int stepPin, int dirPin, int enPin, long steps, int speed);
float readTemp(int analogPin);
void processCommand(String cmd);

void setup() {
  Serial.begin(115200);

  pinMode(X_STEP,OUTPUT); pinMode(X_DIR,OUTPUT); pinMode(X_EN,OUTPUT);
  pinMode(Y_STEP,OUTPUT); pinMode(Y_DIR,OUTPUT); pinMode(Y_EN,OUTPUT);
  pinMode(Z_STEP,OUTPUT); pinMode(Z_DIR,OUTPUT); pinMode(Z_EN,OUTPUT);
  pinMode(E_STEP,OUTPUT); pinMode(E_DIR,OUTPUT); pinMode(E_EN,OUTPUT);
  
  digitalWrite(X_EN,HIGH);
  digitalWrite(Y_EN,HIGH);
  digitalWrite(Z_EN,HIGH);
  digitalWrite(E_EN,HIGH);

  pinMode(BED_PIN,OUTPUT);
  digitalWrite(BED_PIN,LOW); 

  Serial.println("Simple RAMPS Controller Ready. Type HELP");
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    processCommand(cmd);
  }
}

void processCommand(String cmd) {
  if (cmd.startsWith("MX") || cmd.startsWith("MY") || cmd.startsWith("MZ") || cmd.startsWith("ME")) {
    char axis = cmd.charAt(1);
    long steps = cmd.substring(2, cmd.lastIndexOf(' ')).toInt();
    int speed = cmd.substring(cmd.lastIndexOf(' ') + 1).toInt();
    if (axis=='X') moveMotor(X_STEP,X_DIR,X_EN,steps,speed);
    if (axis=='Y') moveMotor(Y_STEP,Y_DIR,Y_EN,steps,speed);
    if (axis=='Z') moveMotor(Z_STEP,Z_DIR,Z_EN,steps,speed);
    if (axis=='E') moveMotor(E_STEP,E_DIR,E_EN,steps,speed);
    Serial.println("OK");
  }
  else if (cmd == "T?") {
    Serial.print("Hotend: "); Serial.print(readTemp(TH_0)); Serial.print(" C | ");
    Serial.print("Bed: "); Serial.print(readTemp(TH_1)); Serial.println(" C");
  }
  else if (cmd == "HB ON") { digitalWrite(BED_PIN, HIGH); Serial.println("HeatBed ON"); }
  else if (cmd == "HB OFF") { digitalWrite(BED_PIN, LOW); Serial.println("HeatBed OFF"); }
  else Serial.println("Unknown — HELP");
}

void moveMotor(int stepPin, int dirPin, int enPin, long steps, int speed) {
  digitalWrite(enPin, LOW);
  digitalWrite(dirPin, (steps>=0)?HIGH:LOW);
  for(long i=0; i<abs(steps); i++){
    digitalWrite(stepPin,HIGH);
    delayMicroseconds(speed);
    digitalWrite(stepPin,LOW);
    delayMicroseconds(speed);
  }
  digitalWrite(enPin, HIGH);
}

float readTemp(int pin) {
  int raw = analogRead(pin);
  float v = raw * (5.0 / 1023.0);
  float r = (v * R_SERIE) / (5.0 - v);
  float invT = 1/T0_K + (1.0/BETA) * log(r/R0);
  return (1/invT) - 273.15;
}
