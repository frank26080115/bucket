#!/usr/bin/env python3

import sys, time, subprocess

import board, busio
import RPi.GPIO as GPIO
from gpiozero import Button

from PIL import Image, ImageDraw, ImageFont

PIN_OLED_RESET = 17
PIN_OLED_CS    = 27
PIN_BUZZER     = 13
PIN_BTN_1      = 19
PIN_BTN_2      = 16
PIN_BTN_3      = 26
PIN_BTN_4      = 20
PIN_BTN_5      = 40

PIN_PULLUP_CONFIG = False
BUTTON_DEBOUNCE   = 0.01

BATT_VDIV_RLOWER      = 4.7  # resistor value of voltage divider
BATT_VDIV_RUPPER      = 15.0 # resistor value of voltage divider
BATT_VOLT_COMPENSATE  = 0.4  # there are diodes that will drop the voltage a bit
BATT_ADC_INTERNAL_REF = 2.048

OLED_WIDTH  = 102
OLED_HEIGHT = 64

I2CADDR_OLED = (0x78 >> 1)
I2CADDR_ADC  = (0xC8 >> 1)

class BucketIO:

    def __init__(self):
        self.buzzer_on = 0
        self.batt_raw  = [-1, -1]
        self.batt_volt = [-1, -1]
        self.batt_chg  = [-100, -100]
        pass

    def hw_init(self):
        self.pin_oledreset = gpiozero.DigitalOutputDevice(PIN_OLED_RESET, initial_value=False)
        self.pin_oledcs    = gpiozero.DigitalOutputDevice(PIN_OLED_CS   , initial_value=False)
        self.buzzer   = gpiozero.Buzzer(PIN_BUZZER)
        self.button_1 = gpiozero.Button(PIN_BTN_1, pull_up = PIN_PULLUP_CONFIG, debounce_time = BUTTON_DEBOUNCE)
        self.button_2 = gpiozero.Button(PIN_BTN_2, pull_up = PIN_PULLUP_CONFIG, debounce_time = BUTTON_DEBOUNCE)
        self.button_3 = gpiozero.Button(PIN_BTN_3, pull_up = PIN_PULLUP_CONFIG, debounce_time = BUTTON_DEBOUNCE)
        self.button_4 = gpiozero.Button(PIN_BTN_4, pull_up = PIN_PULLUP_CONFIG, debounce_time = BUTTON_DEBOUNCE)
        self.button_5 = gpiozero.Button(PIN_BTN_5, pull_up = PIN_PULLUP_CONFIG, debounce_time = BUTTON_DEBOUNCE)
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.disp = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, self.i2c, addr = I2CADDR_OLED)
        self.init_adc()
        time.sleep(0.01)
        self.init_oled()
        self.oled_blankimage()

    def init_oled(self):
        self.pin_oledreset.on()
        time.sleep(0.01)
        cmds = [
                0x40,             # Set Display start line
                0xA0,             # Bottom View no segment remap
                0xC0,             # Bottom View COM scan direction normal
                0x2E,             # StartColumnAddress
                0x8D, 0x95,       # Switch Charge Pump (9V)
                0x20, 0x02,       # Set Memory AddressMode
                0x81, 0xFF,       # Set Brightness
                0xD5, 0x40,       # Set Display Clock Divide
                0xD9, 0xF1,       # Set Precharge Period
                0xAD, 0x30,       # Set Internal Ref
                0x21, 0x0D, 0x72, # Set ColumnAddress
                0x22, 0x00, 0x3F, # Set PageAddress
                0xAF,             # Display on
            ]
        for c in cmds:
            self.oled_txcmd(c)
        pass

    def oled_txcmd(self, c):
        self.i2c.writeto(I2CADDR_OLED, bytes([c]))

    def oled_blankimage(self):
        self.image = Image.new("1", (self.disp.width, self.disp.height))
        self.imagedraw = ImageDraw.Draw(self.image)
        self.imagedraw.rectangle((0, 0, self.disp.width, self.disp.height), outline=0, fill=0)

    def oled_show(self):
        self.disp.image(self.image)
        self.disp.show()

    def init_adc(self):
        self.i2c.writeto(I2CADDR_ADC, bytes([
                  0x80 # setup
                | 0x40 # internal reference, auto-shutdown reference, AIN3 is input
                       # the rest is 0, use internal clock, unipolar mode, reset config register
            ]))

    def batt_read(self):
        self.i2c.writeto(I2CADDR_ADC, bytes([0x60])) # read chan 0, single ended, no scanning
        result = bytearray(1)
        self.i2c.readfrom_into(I2CADDR_ADC, result)
        self.batt_raw[0] = result[0]
        self.i2c.writeto(I2CADDR_ADC, bytes([0x62])) # read chan 1, single ended, no scanning
        result = bytearray(1)
        self.i2c.readfrom_into(I2CADDR_ADC, result)
        self.batt_raw[1] = result[0]

        self.batt_volt[0] = adc_to_voltage(self.batt_raw[0])
        self.batt_volt[1] = adc_to_voltage(self.batt_raw[1])
        self.batt_chg [0] = voltage_to_charge(self.batt_volt[0])
        self.batt_chg [1] = voltage_to_charge(self.batt_volt[1])

        return self.batt_raw, self.batt_volt, self.batt_chg

def adc_to_voltage(x):
    if x < 0:
        return 0
    vbefore = x * BATT_ADC_INTERNAL_REF / pow(2, 8)
    vafter = vbefore * (BATT_VDIV_RLOWER + BATT_VDIV_RUPPER) / BATT_VDIV_RLOWER
    vafter += BATT_VOLT_COMPENSATE
    return vafter

def voltage_to_charge(x):
    x = x / 2 # per cell voltage
    tbl = [   # lookup table for voltage vs charge state
            [4.17 , 100],
            [4.0  , 85],
            [3.4  , 10],
            [3.15 , 0],
            [2.8  , -10],
            [0    , -100],
        ]
    last = None

    if x >= tbl[0][0]: # full battery
        return 100

    # do an interpolation between table entries
    for t in tbl:
        if x >= t[0]:
            return (((x - t[0]) * (last[1] - t[1])) / (last[0] - t[0])) + t[1]
        last = t

    return -100 # past last table element

def has_rtc():
    x = os.system("hwclock -rv >/dev/null")
    if x == 0:
        return True
    x = subprocess.run(["hwclock", "-rv"], capture_output=True)
    x = str(x).lower()
    if "using the rtc interface to the clock" in x:
        return True
    if "no usable clock" in x or "cannot access the hardware clock" in x:
        return False
    return False

def main():
    bhw = BucketIO()
    bhw.hw_init()
    bhw.oled_show()
    return 0

if __name__ == "__main__":
    main()