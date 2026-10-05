# Generated hardware pinout

Generated from `electronics/pinmap.yaml`.
Do not edit these tables manually.

## GPIO

| Device | GPIO | Pico physical pin |
|---|---|---|
| HDG A / B (JP53) | GP0 / GP1 | 1 / 2 |
| ALT A / B (JP52) | GP2 / GP3 | 4 / 5 |
| VS A / B (JP51) | GP6 / GP7 | 9 / 10 |
| CRS A / B (JP50) | GP8 / GP9 | 11 / 12 |
| BARO A / B (JP49) | GP10 / GP11 | 14 / 15 |
| FLAPS A / B (JP48) | GP12 / GP13 | 16 / 17 |
| Input chain load | GP14 | 19 |
| Input chain clock | GP15 | 20 |
| Input chain data | GP16 | 21 |
| Output chain data | GP17 | 22 |
| Output chain clock | GP18 | 24 |
| Output chain latch | GP19 | 25 |
| LCD SDA / SCL | GP4 / GP5 | 6 / 7 |

## Active-low inputs

| Index | Control | Chip/input | Pin | Connector |
|---:|---|---|---:|---|
| 0 | AP | IC1 A | 11 | JP8 |
| 1 | FD | IC1 B | 12 | JP9 |
| 2 | HDG | IC1 C | 13 | JP10 |
| 3 | NAV | IC1 D | 14 | JP11 |
| 4 | APR | IC1 E | 3 | JP12 |
| 5 | ALT_SEL | IC1 F | 4 | JP13 |
| 6 | ALT | IC1 G | 5 | JP14 |
| 7 | VS | IC1 H | 6 | JP15 |
| 8 | YD | IC2 A | 11 | JP16 |
| 9 | CDI | IC2 B | 12 | JP17 |
| 10 | HDG_PUSH | IC2 C | 13 | JP18 |
| 11 | ALT_PUSH | IC2 D | 14 | JP19 |
| 12 | VS_PUSH | IC2 E | 3 | JP20 |
| 13 | CRS_PUSH | IC2 F | 4 | JP21 |
| 14 | BARO_PUSH | IC2 G | 5 | JP22 |
| 15 | GEAR | IC2 H | 6 | JP23 |

## Outputs (HIGH = lamp on)

| Index | Lamp | 595 output/pin | ULN input/output | Connector |
|---:|---|---|---|---|
| 0 | AP | U2 Q0 / 15 | IC3 8 / 11 | JP31 |
| 1 | FD | U2 Q1 / 1 | IC3 7 / 12 | JP30 |
| 2 | HDG | U2 Q2 / 2 | IC3 6 / 13 | JP29 |
| 3 | NAV | U2 Q3 / 3 | IC3 5 / 14 | JP28 |
| 4 | APR | U2 Q4 / 4 | IC3 4 / 15 | JP27 |
| 5 | ALT_SEL | U2 Q5 / 5 | IC3 3 / 16 | JP26 |
| 6 | ALT | U2 Q6 / 6 | IC3 2 / 17 | JP25 |
| 7 | VS | U2 Q7 / 7 | IC3 1 / 18 | JP24 |
| 8 | YD | U3 Q0 / 15 | IC4 8 / 11 | JP39 |
| 9 | GPS | U3 Q1 / 1 | IC4 7 / 12 | JP38 |
| 10 | VLOC | U3 Q2 / 2 | IC4 6 / 13 | JP37 |
| 11 | GS_CAP | U3 Q3 / 3 | IC4 5 / 14 | JP36 |
| 12 | GEAR_L | U3 Q4 / 4 | IC4 4 / 15 | JP35 |
| 13 | GEAR_N | U3 Q5 / 5 | IC4 3 / 16 | JP34 |
| 14 | GEAR_R | U3 Q6 / 6 | IC4 2 / 17 | JP33 |
| 15 | FLAPS_UP | U3 Q7 / 7 | IC4 1 / 18 | JP32 |
| 16 | FLAPS_1 | U4 Q0 / 15 | IC5 8 / 11 | JP47 |
| 17 | FLAPS_2 | U4 Q1 / 1 | IC5 7 / 12 | JP46 |
| 18 | FLAPS_FULL | U4 Q2 / 2 | IC5 6 / 13 | JP45 |
| 19 | SPARE_1 | U4 Q3 / 3 | IC5 5 / 14 | JP44 |
| 20 | SPARE_2 | U4 Q4 / 4 | IC5 4 / 15 | JP43 |
| 21 | SPARE_3 | U4 Q5 / 5 | IC5 3 / 16 | JP42 |
| 22 | SPARE_4 | U4 Q6 / 6 | IC5 2 / 17 | JP41 |
| 23 | SPARE_5 | U4 Q7 / 7 | IC5 1 / 18 | JP40 |

## Stock firmware device records

```text
8.0.1.0.HDG:
8.2.3.0.ALT:
8.6.7.0.VS:
8.8.9.0.CRS:
8.10.11.0.BARO:
8.12.13.0.FLAPS:
12.14.15.16.2.IN:
10.19.18.17.3.OUT:
7.39.20.4.LCD:
```

Configuration SHA-256: `5ff49a7cddd9cfffe42738498653f94e5658452a512bcebf90effee4e717e3b9`
