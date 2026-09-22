const int TRIG_PIN = 8;
const int ECHO_PIN = 12;
const unsigned long TIMEOUT_US = 30000UL; // ~5m round trip cap

void setup() {
  Serial.begin(9600);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
}

void loop() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH, TIMEOUT_US);

  if (duration == 0) {
    Serial.println(500); // no echo detected -> report an arbitrary "far" value
  } else {
    float distanceCm = duration / 58.0;
    Serial.println(distanceCm);
  }

  delay(100); // sensor needs at least ~60ms between pings
}