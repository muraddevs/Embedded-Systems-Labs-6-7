/*
 *  HARDWARE WIRING
 * ─────────────────────────────────────────────────────────────
 *  Button P1      → Pin 5  (INPUT_PULLUP — other leg to GND)
 *  Button P2      → Pin 6  (INPUT_PULLUP — other leg to GND)
 *  Buzzer         → Pin 3  (active buzzer, HIGH = ON)
 *  Servo signal   → Pin 13 (5V + GND from Arduino)
 *  ULN2003 IN1    → Pin 8
 *  ULN2003 IN2    → Pin 9
 *  ULN2003 IN3    → Pin 10
 *  ULN2003 IN4    → Pin 11
 *  ULN2003 VCC    → 5V rail
 *  ULN2003 GND    → GND
 *
 *  SERIAL PROTOCOL (9600 baud)
 * ─────────────────────────────────────────────────────────────
 *  Python  → Arduino  :  "START\n"  |  "RESET\n"
 *  Arduino → Python   :  "GO"  |  "P1:<ms>"  |  "P2:<ms>"
 *                         "P1_FALSE"  |  "P2_FALSE"
 *                         "P1_MATCH"  |  "P2_MATCH"
 *                         "TIMEOUT"   |  "RESET_DONE"
 *
 
 
 * ============================================================
 */

#include <Servo.h>

// ── Pin definitions ──────────────────────────────────────────
const int BUTTON1   = 5;
const int BUTTON2   = 6;
const int BUZZER    = 3;
const int SERVO_PIN = 13;

// ── Stepper pins ─────────────────────────────────────────────
const int IN1 = 8;
const int IN2 = 9;
const int IN3 = 10;
const int IN4 = 11;

// ── Half-step sequence for 28BYJ-48 ─────────────────────────
// 8 steps per electrical cycle, gives smoother motion
// than full-step and more torque
int stepSequence[8][4] = {
    {1, 0, 0, 0},
    {1, 1, 0, 0},
    {0, 1, 0, 0},
    {0, 1, 1, 0},
    {0, 0, 1, 0},
    {0, 0, 1, 1},
    {0, 0, 0, 1},
    {1, 0, 0, 1}
};

int currentStep = 0;  // tracks current position in sequence

Servo servo;

// ── Servo angles ─────────────────────────────────────────────
const int SERVO_CENTER = 90;
const int SERVO_P1     = 20;    // pointer left  → P1 wins
const int SERVO_P2     = 160;   // pointer right → P2 wins

// ── Stepper increments ───────────────────────────────────────
const int STEP_INCREMENT = 256;   // one notch per round win
const int STEP_VICTORY   = 2048;  // full victory spin

// ── Reaction timeout ─────────────────────────────────────────
const unsigned long REACTION_TIMEOUT = 10000;

// ── Game state ───────────────────────────────────────────────
int p1Score    = 0;
int p2Score    = 0;
int stepperPos = 0;  // net steps from start, for reset

// ============================================================
//  MANUAL STEPPER CONTROL
//  clockwise=true  → P1 direction
//  clockwise=false → P2 direction
// ============================================================
void stepMotor(int steps, bool clockwise) {
    for (int i = 0; i < steps; i++) {
        if (clockwise) {
            currentStep = (currentStep + 1) % 8;
        } else {
            currentStep = (currentStep + 7) % 8;
        }
        digitalWrite(IN1, stepSequence[currentStep][0]);
        digitalWrite(IN2, stepSequence[currentStep][1]);
        digitalWrite(IN3, stepSequence[currentStep][2]);
        digitalWrite(IN4, stepSequence[currentStep][3]);
        delay(2);
    }
}

// ── Power off coils after move to prevent heat buildup ───────
void stepperOff() {
    digitalWrite(IN1, LOW);
    digitalWrite(IN2, LOW);
    digitalWrite(IN3, LOW);
    digitalWrite(IN4, LOW);
}

// ============================================================
void setup() {
    pinMode(BUTTON1, INPUT_PULLUP);
    pinMode(BUTTON2, INPUT_PULLUP);

    pinMode(BUZZER, OUTPUT);
    digitalWrite(BUZZER, LOW);   // active HIGH — LOW = OFF at start

    pinMode(IN1, OUTPUT);
    pinMode(IN2, OUTPUT);
    pinMode(IN3, OUTPUT);
    pinMode(IN4, OUTPUT);
    stepperOff();

    servo.attach(SERVO_PIN);
    servo.write(SERVO_CENTER);

    Serial.begin(9600);
}

// ============================================================
void loop() {
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();

        if (cmd == "START") {
            runRound();
        } else if (cmd == "RESET") {
            resetGame();
        }
    }
}

// ── Buzzer helper ────────────────────────────────────────────
void buzz(int ms) {
    digitalWrite(BUZZER, HIGH);  // ON
    delay(ms);
    digitalWrite(BUZZER, LOW);   // OFF
}

// ============================================================
//  MAIN ROUND LOGIC
// ============================================================
void runRound() {

    // Random wait: 1–20 seconds as per spec
    long waitMs = random(1000, 5001);
    unsigned long waitStart = millis();

    // Watch for false starts during countdown
    // INPUT_PULLUP: LOW = pressed, HIGH = not pressed
    while (millis() - waitStart < (unsigned long)waitMs) {
        if (digitalRead(BUTTON1) == LOW) {
            Serial.println("P1_FALSE");
            awardWin(2);
            return;
        }
        if (digitalRead(BUTTON2) == LOW) {
            Serial.println("P2_FALSE");
            awardWin(1);
            return;
        }
    }

    // Fire buzzer and signal Python
    buzz(300);
    Serial.println("GO");

    unsigned long reactionStart = millis();

    while (true) {

        // Timeout guard
        if (millis() - reactionStart > REACTION_TIMEOUT) {
            Serial.println("TIMEOUT");
            return;
        }

        if (digitalRead(BUTTON1) == LOW) {
            unsigned long t = millis() - reactionStart;
            Serial.print("P1:");
            Serial.println(t);
            awardWin(1);
            return;
        }

        if (digitalRead(BUTTON2) == LOW) {
            unsigned long t = millis() - reactionStart;
            Serial.print("P2:");
            Serial.println(t);
            awardWin(2);
            return;
        }
    }
}

// ============================================================
//  AWARD WIN — servo + stepper + match check
// ============================================================
void awardWin(int player) {

    if (player == 1) {
        p1Score++;
        servo.write(SERVO_P1);
        stepMotor(STEP_INCREMENT, true);   // P1 → clockwise
        stepperPos += STEP_INCREMENT;
        stepperOff();
    } else {
        p2Score++;
        servo.write(SERVO_P2);
        stepMotor(STEP_INCREMENT, false);  // P2 → counter-clockwise
        stepperPos -= STEP_INCREMENT;
        stepperOff();
    }

    delay(300);  // let servo settle

    // Send match result BEFORE victory spin so Python receives
    // the message without motor blocking serial
    if (p1Score >= 3) {
        Serial.println("P1_MATCH");
        delay(100);
        stepMotor(STEP_VICTORY, true);   // victory spin clockwise
        stepperOff();
    } else if (p2Score >= 3) {
        Serial.println("P2_MATCH");
        delay(100);
        stepMotor(STEP_VICTORY, true);   // victory spin clockwise
        stepperOff();
    }
}

// ============================================================
//  RESET — return all hardware to neutral state
// ============================================================
void resetGame() {
    p1Score = 0;
    p2Score = 0;

    servo.write(SERVO_CENTER);

    // Return stepper to starting position
    if (stepperPos > 0) {
        stepMotor(stepperPos, false);  // go back counter-clockwise
    } else if (stepperPos < 0) {
        stepMotor(-stepperPos, true);  // go back clockwise
    }
    stepperOff();
    stepperPos = 0;

    Serial.println("RESET_DONE");
}
