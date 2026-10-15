# 🐟 This Fish Does Not Exist

*A Journal of Spurious Fish.*

An Arduino UNO Q app that draws procedurally generated fish stroke by stroke on a 7.5" e-paper display, laid out as textbook pages with scientific descriptions written by an on-device LLM.

A new fish is generated at regular time intervals using the [fishdraw](https://github.com/LingDong-/fishdraw) algorithm and drawn on a Waveshare 7.5" e-paper panel (800×480) through the Arduino e-Paper Shield. The UNO Q's Linux side generates the fish, rasterizes it with PIL and streams only the changed bytes to the MCU, so fast partial refreshes can reveal the drawing stroke by stroke if desired. The [LLM Brick](https://github.com/arduino/app-bricks-py/tree/main/src/arduino/app_bricks/llm) can then prompted with the characteristics of the fish to generate a scientific description of the fish.

## Layouts

- **plate**: landscape specimen plate with the fish's name and discovery date.
- **figure**: landscape textbook figure with a classification block.
- **page**: portrait textbook page. A local Gemma 3 1B model (via the App Lab LLM brick) writes an invented scientific description from the fish's characteristics, and it is typed out word by word. A template fallback is used if the LLM is unavailable.

A web UI (port 7000) lets you change the layout, generation interval, draw duration, timezone and other settings.

## Hardware

- Arduino UNO Q
- Waveshare 7.5" e-paper (800×480, UC8179, marked `075BN-T7-D2`)
- Arduino e-Paper Shield (SPI config on D13/D12/D11, resistor on 0.47R/B)

Wiring: BUSY → D7, RST → D8, DC → D9, CS → D10, DIN → D11, CLK → D13, VCC → 3.3V, GND → GND.

## Running

Open the folder in Arduino App Lab and run it. Python dependencies are in `python/requirements.txt`, sketch libraries in `sketch/sketch.yaml`.

## Credits

Fish generation is ported from [fishdraw](https://github.com/LingDong-/fishdraw) by LingDong-.
