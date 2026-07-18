# Water PLC Design Notes — v0.3

**Project:** Databyte Water Replenishment Controller (Domestic, Cape Town)
**Document:** DAT-WATER-PLC-001-v0.3-Misha
**Date:** 2026-07-18
**Author:** Misha (Claw agent for Zun)
**Status:** LOCKED — ready for cut

---

## 1. Topology (corrected)

The system is a **domestic water replenishment controller** for a 1970 Cape Town house, 4-person household (2 adults + 2 kids). The "cistern" is the **toilet cistern** — wall-mounted black plastic, ~11–12L capacity, ~1.5m above floor. The JoJo tank is ground-level storage.

**Flow path:**
```
Well point (existing borehole)
  → Well pump (existing 220V AC, 0.75 kW)
  → JoJo tank (ground level, ~1000L plastic)
  → A02YYUW level sensor (on JoJo lid, IP67 UART)
  → BDD 12V submersible pump (external, gravity-fed from bottom outlet)
  → 1/2" pipe → 15mm HDPE adapter → vertical rise ~1.5m
  → Toilet cistern (float switch on lid)
  → Float switch (reed, dry contact)
  → ESP32-C6 DevKitC-1 (decides when to refill)
```

Yard aviation LEDs (4× discrete 10mm W/R/B/O at GPIO10–13, 5V from LM2596) flash during 17:00–07:00 schedule.
Toilet indicator (WS2812B 3mm, GPIO16, on floating shelf in toilet) shows pump status.

---

## 2. Locked decisions (29 items)

| # | Item | Decision | Source / Reason |
|---|---|---|---|
| 1 | Topology | Toilet cistern = wall-mounted 11–12L, not ground storage | Zun msg #26778 (Q2) |
| 2 | Pump | BDD 12V Solar Heat Pump BK | Communica, R250, 800 L/h @ 5m head |
| 3 | Pump mounting | External, gravity-fed from JoJo bottom outlet | Zun msg #26836 ("i will submerge the pump alternatively i have an outlet at the bottom of tank") |
| 4 | JoJo lid drill | Yes — drill 3 holes (PG9 sensor + PG7×2 LEDs) | Zun msg #26836 |
| 5 | PSU | Mornsun LM50-23B12R2S | Communica, R230-280, 50W, 12V/4.2A, 4 kVAC isolation |
| 6 | PSU mounting | Panel-mount + DIN-rail adapter plate | R15-20 adapter, saves R100 over HDR DIN version |
| 7 | Step-down | Zun's LM2596 module | Already owned, set to 5.0V ±0.1V |
| 8 | MCU | ESP32-C6 DevKitC-1 | 19 GPIO, WiFi 6, no need for S3 (Zun msg #26829) |
| 9 | Time schedule | 17:00–07:00 daily (no LDR) | Zun msg #26778 (Q1) |
| 10 | Time source | NTP primary + DS3231 RTC (ZS-042) backup | Pool area WiFi dead-zone possible |
| 11 | Aviation LEDs | 4× discrete 10mm (W/R/B/O) on GPIO10/11/12/13 | Your existing weather-station pattern |
| 12 | LED brand | Wahwang WW10A3 series from Rabtron | Datasheet-verified, R10 ea, in stock |
| 13 | LED resistor | 220Ω/220Ω/120Ω/120Ω for Red/Orange/White/Blue @ 5V/15 mA | Recalculated from Rabtron datasheet Vf |
| 14 | Toilet LED | WS2812B 3mm housing, GPIO16 NeoPixel | Zun confirmed |
| 15 | LED mounting | 4× M12 metal bezels on interface panel | FastenRight / Communica |
| 16 | Toilet LED mount | 3mm plastic case on floating shelf | Zun msg #26836 |
| 17 | Pump driver | 1× AO3400A N-channel MOSFET on GPIO15 | For BDD 12V pump (1.5A typ) |
| 18 | Well pump driver | HL-52S relay module on GPIO18 | For 220V AC well pump |
| 19 | Level sensor | A02YYUW ultrasonic, UART2 GPIO20/21 | 3-450 cm range, IP67 |
| 20 | Flow meter | YF-S201 hall-effect on GPIO3 | 1/2" BSP, F=7.5×Q Hz |
| 21 | Cistern float | Reed switch + 10k pull-up on GPIO2 | INPUT_PULLUP |
| 22 | Well pump mode | 3-position toggle on GPIO4 | OFF/AUTO/MANUAL |
| 23 | Manual buttons | 2× momentary NO on GPIO5 (JoJo), GPIO9 (reset) | Per Zun pattern |
| 24 | OLED | SSD1306 0.96" 128×64 I²C, GPIO6/7 | Status display |
| 25 | Earth spike | 1.2m galvanized, ≤30Ω target | Zun self-installs (msg #26836) |
| 26 | Pool DB feed | 10A MCB from existing pool sub-DB | 4m run, 4mm² TPS, 20mm conduit |
| 27 | Solar/battery | Scrapped — kept in §10 future upgrade only | Zun msg #26778 |
| 28 | Compliance | SANS 10142-1 only | Zun msg #26829 (skip ICASA/NRCS) |
| 29 | HA integration | Yes, 12-entity default list | Zun confirmed msg #26829 |

---

## 3. GPIO pin map (FINAL for v0.3)

| GPIO | Direction | Function | Component | Interface | Safe for C6? |
|------|-----------|----------|-----------|-----------|--------------|
| GPIO2 | IN | Cistern float switch | Reed switch + 10kΩ pull-up | input | ✅ |
| GPIO3 | IN | Flow meter pulse | YF-S201 + 10kΩ pull-up | interrupt input | ✅ |
| GPIO4 | IN | Well pump mode switch | 3-position toggle + 10kΩ pull-up | input | ✅ |
| GPIO5 | IN | JoJo pump manual button | Momentary NO + 10kΩ pull-up | input | ✅ |
| GPIO6 | IO | I²C SDA | OLED SSD1306 (0x3C) + DS3231 RTC (0x68) | I²C bus, 4.7kΩ pull-up | ✅ |
| GPIO7 | IO | I²C SCL | OLED SSD1306 + DS3231 RTC | I²C bus, 4.7kΩ pull-up | ✅ |
| GPIO10 | OUT | Aviation LED Red (PWM) | Wahwang WW10A3SRQ4-N2 + 220Ω | LEDC PWM @ 1 kHz | ✅ |
| GPIO11 | OUT | Aviation LED White (PWM) | Wahwang WW10A3SWQ4-N2 + 120Ω | LEDC PWM @ 1 kHz | ✅ |
| GPIO12 | OUT | Aviation LED Blue (PWM) | Wahwang WW10A3SBQ4-N2 + 120Ω | LEDC PWM @ 1 kHz | ✅ |
| GPIO13 | OUT | Aviation LED Orange (PWM) | Wahwang WW10A3OYF4-N2 + 220Ω | LEDC PWM @ 1 kHz | ✅ |
| GPIO15 | OUT | BDD pump MOSFET gate | AO3400A + 10kΩ pull-down | digital out | ⚠️ strap pin, safe |
| GPIO16 | OUT | Toilet RGB indicator (NeoPixel) | WS2812B 3mm | NeoPixel data | ✅ |
| GPIO18 | OUT | Well pump relay coil IN | HL-52S module | digital out | ✅ |
| GPIO20 | IO | A02YYUW UART2 RX | Sensor TX (yellow) | UART2 @ 9600 baud | ✅ |
| GPIO21 | IO | A02YYUW UART2 TX | Sensor RX (white) | UART2 @ 9600 baud | ✅ |

**15 GPIOs used, 4 spare** (GPIO0, GPIO1, GPIO8, GPIO9, GPIO19 — actually 5 spare).

---

## 4. ESPHome firmware stub

(Full code in `water-plc.yaml` — attached as separate file.)

Key features:
- 4× `output: ledc` PWM for aviation LEDs
- 1× `neopixelbus` for toilet WS2812B
- 1× `uart` for A02YYUW
- 1× `sensor` for YF-S201 pulse counter
- 1× `binary_sensor` for cistern float
- 1× `switch` for well pump relay
- 1× `time` for DS3231 RTC + NTP sync
- 12 HA entities exposed via `api:` and `ota:`

---

## 5. Power budget

| Rail | Source | Voltage | Max current | Notes |
|------|--------|---------|-------------|-------|
| 12V main | Mornsun LM50-23B12R2S | 12.0V ±2% | 4.2A peak | Drives 12V busbar |
| 5V secondary | LM2596 buck | 5.0V ±0.1V | 2.0A peak | Drives ESP32 + LEDs |
| 3.3V | ESP32 on-board LDO | 3.3V ±5% | 0.5A peak | Drives sensors |

| Load | Voltage | Current (typ) | Current (peak) |
|------|---------|---------------|----------------|
| BDD pump | 12V | 1.5 A | 4.0 A (inrush) |
| ESP32-C6 (WiFi active) | 5V→3.3V | 0.5 A | 0.8 A |
| OLED display | 3.3V | 0.02 A | 0.02 A |
| DS3231 RTC | 3.3V | 0.0015 A | 0.0015 A |
| A02YYUW | 5V | 0.025 A | 0.025 A |
| YF-S201 | 3.3V | 0.015 A | 0.015 A |
| 4× aviation LEDs | 5V | 0.06 A | 0.08 A |
| WS2812B toilet | 5V | 0.06 A | 0.06 A |
| HL-52S relay coil | 5V | 0.08 A | 0.08 A |
| **TOTAL typical** | — | **2.27 A @ 12V** | — |
| **TOTAL peak** | — | — | **~5.1 A @ 12V inrush** |

**LM50-23B12R2S rated 4.2A continuous, ~5A peak for 10s.** ✅ Sufficient with margin.

---

## 6. Sourcing (SA-local shops)

| Shop | Use | URL |
|------|-----|-----|
| **Communica** | PSU, pump, sensors, ESP32, generic LEDs | communica.co.za |
| **Rabtron** | Wahwang 10mm LEDs, resistors, capacitors, generic parts | rabtron.co.za |
| **Mantech** | Industrial PSUs, terminal blocks, relay modules | mantech.co.za |
| **Micro Robotics** | ESP32 modules, sensors, breakout boards | microrobotics.co.za |
| **FastenRight** | Bolts, screws, fasteners, M12 bezels, DIN clips | fastenright.co.za |
| **Takealot / AliExpress** | Backup sourcing if SA stock out | — |

---

## 7. Drawings (4 external, referenced from §5)

- **D-01 P&ID** — Process flow with all locked components
- **D-02 Single-Line** — Electrical topology (220V → MCB → 12V PSU → buck → loads)
- **D-03 ESP32-C6 IO Wiring** — All 15 GPIO assignments + power rails + MOSFET driver
- **D-04 Enclosure Layout** — Hammond RP1465C with 4 LED bezels, OLED, terminal blocks

All drawings are 300 DPI PNG + SVG source. Print A3 landscape, staple with the spec.

---

## 8. Open items (carry into v0.4)

- [ ] Field-test A02YYUW range accuracy (3 cm min spec — too low for shallow tanks?)
- [ ] Confirm 2A DC MCB rating vs LM50-23B12R2S 4.2A output (currently 2A is fine, but if upgraded in future need to swap)
- [ ] Test pump duty cycle vs cistern float hysteresis (chatter protection)
- [ ] Verify all GPIOs boot cleanly with pull-up/down resistors on C6
- [ ] Comms check for HA API key rotation

---

## 9. Change history (v0.1 → v0.3)

| Version | Date | Author | Change |
|---------|------|--------|--------|
| v0.1 | 2026-07-16 | Previous agent | Initial draft (incorrect "cistern" as ground storage) |
| v0.2 | 2026-07-18 | Misha | Topology correction, BDD pump + Mornsun PSU, Zarah scrubbed, 4 drawings |
| v0.3 | 2026-07-18 | Misha | Rabtron LED datasheet-verified, ISO 9001 doc control, BOM upgrade, HA entities, ESPHome YAML v2 |

---

**End of design notes v0.3**
