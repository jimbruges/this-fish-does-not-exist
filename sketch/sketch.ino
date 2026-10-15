/*
  This Fish Does Not Exist - MCU side.

  Waveshare 7.5" e-paper (800x480, UC8179, panel marked "075BN-T7-D2") on the
  official Arduino e-Paper Shield. The MPU (python/main.py) rasterises the
  fish with PIL and streams the 1-bit framebuffer here; this side just holds
  the buffer and drives refreshes. Keeping the raster on the MPU means the
  drawing detail isn't limited by MCU memory.

  Wiring: BUSY -> D7, RST -> D8, DC -> D9, CS -> D10, DIN(MOSI) -> D11,
  CLK(SCK) -> D13, VCC/PWR -> 3.3V, GND -> GND. Shield switches: SPI Config
  on D13/D12/D11, resistor on 0.47R/B.

  display.init(115200, true, 2, false) is load-bearing: the 2ms reset pulse is
  required by this shield's reset circuit. GxEPD2's default 10ms pulse leaves
  the panel completely unresponsive (every op then runs out its 10s BUSY
  timeout), which is what made this silent for a long time.

  Refreshes go through the driver (epd2.*) rather than Adafruit_GFX's paged
  buffer: we already have the full bitmap, so there's nothing to page over.
  The write/refresh/writeImageAgain sequences below mirror what GxEPD2_BW does
  internally for a panel with hasFastPartialUpdate.

  Framebuffer convention matches PIL mode "1" exactly - MSB-first rows,
  bit 1 = white - so Python can stream img.tobytes() with no conversion.
*/

#include <Arduino_RouterBridge.h>
#include <GxEPD2_BW.h>
#include <SPI.h>
#include <string.h>
#include <vector>

#define PIN_BUSY 7
#define PIN_RST 8
#define PIN_DC 9
#define PIN_CS 10

#define EPD_W 800
#define EPD_H 480
#define ROW_BYTES ((EPD_W + 7) / 8)
#define FB_SIZE (ROW_BYTES * EPD_H)

GxEPD2_BW<GxEPD2_750_T7, GxEPD2_750_T7::HEIGHT / 8> display(
    GxEPD2_750_T7(PIN_CS, PIN_DC, PIN_RST, PIN_BUSY));

static uint8_t fb[FB_SIZE];

static bool bridgeFbClear() {
  memset(fb, 0xFF, sizeof(fb));  // 0xFF = all white
  return true;
}

/* Python sends only the bytes that changed since the last frame, so a
   progressive reveal costs a few hundred bytes per step, not 48KB. */
static bool bridgeFbWrite(int offset, std::vector<uint8_t> data) {
  if (offset < 0 || offset + (int)data.size() > (int)sizeof(fb)) {
    Serial.println("epd_fb_write: bad offset");
    return false;
  }
  memcpy(fb + offset, data.data(), data.size());
  return true;
}

/* ---- raw controller access, for the partial-waveform override ----------
   GxEPD2's _writeCommand/_writeData are protected, so these mirror them
   exactly (same pins, same SPI mode and clock) to reach the UC8179's LUT
   registers directly. */
static const SPISettings kEpdSpi(4000000, MSBFIRST, SPI_MODE0);

static void epdCmd(uint8_t c) {
  SPI.beginTransaction(kEpdSpi);
  digitalWrite(PIN_DC, LOW);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(c);
  digitalWrite(PIN_CS, HIGH);
  digitalWrite(PIN_DC, HIGH);
  SPI.endTransaction();
}

static void epdData(const uint8_t* d, uint16_t n, uint16_t pad_zeros) {
  SPI.beginTransaction(kEpdSpi);
  digitalWrite(PIN_CS, LOW);
  for (uint16_t i = 0; i < n; i++) SPI.transfer(d[i]);
  for (uint16_t i = 0; i < pad_zeros; i++) SPI.transfer(0x00);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

/* Replace the partial-update waveform with a shorter one.

   GxEPD2_750_T7 ships T1=30, T2=5, T3=30, T4=5 - 70 frames at ~50Hz, which
   is exactly the ~1.6s partial refresh we measured. The LUT registers
   (0x20-0x25) are writable, and the frame counts are the only thing that
   changes here: the level bytes below are copied verbatim from the library's
   tables. Fewer frames = faster refresh, at the cost of some contrast and
   more residual ghosting - acceptable here because every period starts from
   a clean full refresh.

   Only valid once GxEPD2 has entered partial mode (its _Init_Part writes the
   default tables and would otherwise clobber these). */
static void applyFastPartialLut(uint8_t t1, uint8_t t2, uint8_t t3, uint8_t t4) {
  const uint8_t levels[6] = {0x00, 0x00, 0x5A, 0x84, 0x00, 0x00};  // LUTC,WW,KW,WK,KK,BD
  for (uint8_t i = 0; i < 6; i++) {
    uint8_t lut[6] = {levels[i], t1, t2, t3, t4, 1};
    epdCmd(0x20 + i);
    epdData(lut, sizeof(lut), 42 - sizeof(lut));
  }
}

/* Enter partial mode and install the fast waveform. Costs one default-speed
   partial refresh (the one that triggers GxEPD2's _Init_Part); every
   partial refresh after it runs at the shortened waveform. */
static bool bridgeFastPartial(int t1, int t2, int t3, int t4) {
  display.epd2.writeImagePart(fb, 0, 0, EPD_W, EPD_H, 0, 0, 8, 8);
  display.epd2.refresh(0, 0, 8, 8);
  applyFastPartialLut((uint8_t)t1, (uint8_t)t2, (uint8_t)t3, (uint8_t)t4);
  Serial.print("fast partial waveform: frames=");
  Serial.println(t1 + t2 + t3 + t4);
  return true;
}

/* Fast partial refresh of one region - no full-screen flash, so the fish
   appears to be drawn progressively. x/w should be multiples of 8.
   Only the changed window is pushed over SPI, not the whole framebuffer.
   (T7's writeImageAgain is a no-op - the controller copies new->old itself
   via the N2OCP bit - so there is no second write to make.) */
static bool bridgeRefreshWindow(int x, int y, int w, int h) {
  if (x < 0) x = 0;
  if (y < 0) y = 0;
  if (x + w > EPD_W) w = EPD_W - x;
  if (y + h > EPD_H) h = EPD_H - y;
  if (w <= 0 || h <= 0) return false;
  display.epd2.writeImagePart(fb, x, y, EPD_W, EPD_H, x, y, w, h);
  display.epd2.refresh(x, y, w, h);
  return true;
}

/* Full refresh - slower and flashes, but clears accumulated partial-update
   ghosting. Used once at the end of each fish. */
static bool bridgeRefreshFull() {
  display.epd2.writeImageForFullRefresh(fb, 0, 0, EPD_W, EPD_H);
  display.epd2.refresh(false);
  if (display.epd2.hasFastPartialUpdate) {
    display.epd2.writeImageAgain(fb, 0, 0, EPD_W, EPD_H);
  }
  display.epd2.powerOff();
  return true;
}

void setup() {
  Serial.begin(115200);
  Bridge.begin();
  Bridge.provide("epd_fb_clear", bridgeFbClear);
  Bridge.provide("epd_fb_write", bridgeFbWrite);
  Bridge.provide("epd_refresh_window", bridgeRefreshWindow);
  Bridge.provide("epd_refresh_full", bridgeRefreshFull);
  Bridge.provide("epd_fast_partial", bridgeFastPartial);

  memset(fb, 0xFF, sizeof(fb));

  display.init(115200, true, 2, false);  // 2ms reset pulse - required by this shield
  display.setRotation(0);
  display.epd2.writeImageForFullRefresh(fb, 0, 0, EPD_W, EPD_H);
  display.epd2.refresh(false);
  display.epd2.powerOff();

  Serial.println("This Fish Does Not Exist sketch ready");
}

void loop() {
  delay(10);
}
