/*
  Simple pH analog monitor (A15 / T0 sur RAMPS)
  Calcule une estimation de pH à partir de la tension lue (formule linéaire à calibrer).
*/

static const uint32_t BAUDRATE = 9600;
static const uint8_t PH_PIN = A9; // AUX-2

void setup()
{
  Serial.begin(BAUDRATE);
  pinMode(PH_PIN, INPUT);
  Serial.println("PH monitor ready (A15)");
}

void loop()
{
  int sensorValue = analogRead(PH_PIN);
  float voltage = sensorValue * (5.0f / 1023.0f);
  float pH_value = 3.5f * voltage + 0.0f; // Ajuster coefficients via calibration

  Serial.print("RAW=");
  Serial.print(sensorValue);
  Serial.print(";V=");
  Serial.print(voltage, 3);
  Serial.print(";pH=");
  Serial.println(pH_value, 2);

  delay(1000);
}
