// Teensy 4.1 test-signal generator for the Saleae Logic 8.
//
// Every 10 ms it emits one burst of each protocol; PWM runs continuously.
//
//   Logic 8   Teensy pin   Signal
//   D0        1  (TX1)     UART 115200 8N1, "hello\n"
//   D1        2            PWM 1 kHz, 25 % duty
//   D2        19 (SCL)     I2C 100 kHz, write 0x42 <- 12 34 (no slave: address NACKed)
//   D3        18 (SDA)
//   D4        13 (SCK)     SPI mode 0, 1 MHz, A5 5A 01 80
//   D5        11 (MOSI)
//   D6        10 (CS)      active low
//   D7        6            square 100 kHz, 50 % duty
//   GND       GND
//
// USB serial prints this table on connect (and whenever a byte is received).
#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>

static const int PIN_PWM = 2, PIN_SQUARE = 6, PIN_CS = 10;
static const uint8_t SPI_DATA[] = {0xA5, 0x5A, 0x01, 0x80};

static void printPinout() {
  Serial.println("teensy-siggen: D0=pin1 UART 115200 'hello\\n' | D1=pin2 PWM 1kHz 25% |"
                 " D2=pin19 SCL D3=pin18 SDA (I2C 100k, addr 0x42 <- 12 34) |"
                 " D4=pin13 SCK D5=pin11 MOSI D6=pin10 CS (SPI mode0 1MHz A5 5A 01 80) |"
                 " D7=pin6 100kHz square | burst every 10 ms");
}

void setup() {
  Serial1.begin(115200);

  analogWriteResolution(8);
  analogWriteFrequency(PIN_PWM, 1000);
  analogWrite(PIN_PWM, 64);          // 25 %
  analogWriteFrequency(PIN_SQUARE, 100000);
  analogWrite(PIN_SQUARE, 128);      // 50 %

  Wire.begin();                      // Teensy 4 enables internal pull-ups on SDA/SCL
  Wire.setClock(100000);

  pinMode(PIN_CS, OUTPUT);
  digitalWriteFast(PIN_CS, HIGH);
  SPI.begin();
}

void loop() {
  static elapsedMillis tick;
  static bool wasConnected = false;

  if (Serial && !wasConnected) printPinout();
  wasConnected = Serial;
  if (Serial.available()) {
    while (Serial.available()) Serial.read();
    printPinout();
  }

  if (tick < 10) return;
  tick = 0;

  Serial1.print("hello\n");
  Serial1.flush();

  Wire.beginTransmission(0x42);
  Wire.write(0x12);
  Wire.write(0x34);
  Wire.endTransmission();

  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWriteFast(PIN_CS, LOW);
  for (uint8_t b : SPI_DATA) SPI.transfer(b);
  digitalWriteFast(PIN_CS, HIGH);
  SPI.endTransaction();
}
