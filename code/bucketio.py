#!/usr/bin/env python3

import sys, os, time, subprocess, queue, glob

is_embedded = True

try:
    import board, busio
    import RPi.GPIO as GPIO
    import gpiozero
    import adafruit_ssd1306
except:
    print("warning: bucket is not running on embedded platform")
    is_embedded = False
    import cv2, numpy

from PIL import Image, ImageDraw, ImageFont

import bucketutils

try:
    import bucketlogger
    logger = bucketlogger.getLogger()
except:
    pass

PIN_OLED_RESET = 17
PIN_OLED_CS    = 27
PIN_BUZZER     = 13
PIN_BTN_1      = 19
PIN_BTN_2      = 16
PIN_BTN_3      = 26
PIN_BTN_4      = 20
PIN_BTN_5      = 21

PIN_PULLUP_CONFIG = None
BUTTON_DEBOUNCE   = 0.01

BATT_VDIV_RLOWER      = 4.7  # resistor value of voltage divider
BATT_VDIV_RUPPER      = 15.0 # resistor value of voltage divider
BATT_VOLT_COMPENSATE  = 0.0  # there are diodes that will drop the voltage a bit
BATT_ADC_INTERNAL_REF = 2.048
BATT_VOLT_CALIBRATION_SCALE_M = 2.7027
BATT_VOLT_CALIBRATION_SCALE_B = -8.10811

OLED_WIDTH  = 102
OLED_HEIGHT = 64

I2CADDR_OLED = (0x78 >> 1)
I2CADDR_ADC  = (0xC8 >> 1)

class BucketIO:

    def __init__(self):
        self.is_sim = False
        self.buzzer_is_on = 0
        self.batt_raw  = [-1, -1]
        self.batt_volt = [-1, -1]
        self.batt_chg  = [-100, -100]
        self.button_queue = queue.Queue()
        self.adc_avail = False

    def hw_init(self):
        self.init_pins()
        self.init_buttons()
        self.init_adc()
        self.init_oled()
        self.oled_blankimage()

    def init_pins(self):
        self.i2c           = busio.I2C(board.SCL, board.SDA)
        self.pin_oledreset = gpiozero.DigitalOutputDevice(PIN_OLED_RESET, initial_value=False)
        self.pin_oledcs    = gpiozero.DigitalOutputDevice(PIN_OLED_CS   , initial_value=False)
        self.buzzer        = gpiozero.DigitalOutputDevice(PIN_BUZZER    , initial_value=False)

    def init_buttons(self):
        self.button_1 = gpiozero.Button(PIN_BTN_1, active_state = False, pull_up = PIN_PULLUP_CONFIG, bounce_time = BUTTON_DEBOUNCE)
        self.button_2 = gpiozero.Button(PIN_BTN_2, active_state = False, pull_up = PIN_PULLUP_CONFIG, bounce_time = BUTTON_DEBOUNCE)
        self.button_3 = gpiozero.Button(PIN_BTN_3, active_state = False, pull_up = PIN_PULLUP_CONFIG, bounce_time = BUTTON_DEBOUNCE)
        self.button_4 = gpiozero.Button(PIN_BTN_4, active_state = False, pull_up = PIN_PULLUP_CONFIG, bounce_time = BUTTON_DEBOUNCE)
        self.button_5 = gpiozero.Button(PIN_BTN_5, active_state = False, pull_up = PIN_PULLUP_CONFIG, bounce_time = BUTTON_DEBOUNCE)
        self.buttons = [self.button_1, self.button_2, self.button_3, self.button_4, self.button_5]
        self.button_1.when_pressed = self.on_pressed_1
        self.button_2.when_pressed = self.on_pressed_2
        self.button_3.when_pressed = self.on_pressed_3
        self.button_4.when_pressed = self.on_pressed_4
        self.button_5.when_pressed = self.on_pressed_5
        #self.button_4.when_held    = self.on_pressed_4
        #self.button_5.when_held    = self.on_pressed_5
        #self.button_4.hold_time    = 0.5
        #self.button_5.hold_time    = 0.5
        #self.button_4.hold_repeat  = True
        #self.button_5.hold_repeat  = True
        # note: gpiozero's button-hold detection is incredibly unreliable, do not use

    def init_oled(self, reinit = False):
        self.image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        self.imagedraw = ImageDraw.Draw(self.image)
        time.sleep(0.01)
        self.pin_oledreset.on()
        time.sleep(0.01)
        self.disp = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, self.i2c, addr = I2CADDR_OLED, external_vcc = False)
        self.disp.rotate(False) # this is required or else it's flipped by default
        if reinit == False:
            # testing shows that the initialization from the datasheet is worse
            # just stick with the Adafruit code
            return
        self.pin_oledreset.off()
        time.sleep(0.01)
        self.pin_oledreset.on()
        time.sleep(0.01)
        cmds = [
                0x40,             # Set Display start line
                0xA0,             # Bottom View no segment remap
                0xC0,             # Bottom View COM scan direction normal
                0x2E,             # StartColumnAddress
                0x8D, 0x95,       # Switch Charge Pump (9V)
                0x20, 0x02,       # Set Memory AddressMode
                0x81, 0xFF,       # Set Brightness
                0xD5, 0x40,       # Set Display Clock Divide
                0xD9, 0xF1,       # Set Precharge Period
                0xAD, 0x30,       # Set Internal Ref
                0x21, 0x0D, 0x72, # Set ColumnAddress
                0x22, 0x00, 0x3F, # Set PageAddress
                0xAF,             # Display on
            ]
        for c in cmds:
            self.disp.write_cmd(c)

    def oled_blankimage(self):
        self.imagedraw.rectangle((0, 0, OLED_WIDTH, OLED_HEIGHT), outline=0, fill=0)

    def oled_show(self):
        self.disp.image(self.image)
        self.disp.show()

    def init_adc(self):
        try:
            self.adc_setup_bytes = bytes([
                      0x80 # setup
                    | 0x40 # internal reference, auto-shutdown reference, AIN3 is input
                    | 0x00 # unipolar, input range 0-Vref
                    | 0x01 # prevent reset
                           # the rest is 0, use internal clock, unipolar mode
                ])
            self.i2c.writeto(I2CADDR_ADC, self.adc_setup_bytes)
            self.adc_avail = True
        except:
            self.adc_avail = False

    def batt_read(self):
        if self.adc_avail == False:
            return [-1, -1], [-1, -1], [-1, -1]
        self.i2c.writeto(I2CADDR_ADC, bytes([self.adc_setup_bytes[0], 0x01 + (0x01 << 1)])) # read up to chan 1, single ended
        result = bytearray(2)
        self.i2c.readfrom_into(I2CADDR_ADC, result)
        self.batt_raw[0] = result[0]
        self.batt_raw[1] = result[1]

        self.batt_volt[0] = adc_to_voltage(self.batt_raw[0])
        self.batt_volt[1] = adc_to_voltage(self.batt_raw[1])
        self.batt_chg [0] = voltage_to_charge(self.batt_volt[0])
        self.batt_chg [1] = voltage_to_charge(self.batt_volt[1])

        return self.batt_raw, self.batt_volt, self.batt_chg

    def buzzer_on(self):
        self.buzzer_is_on = time.monotonic()
        self.buzzer.on()

    def buzzer_off(self):
        self.buzzer_is_on = 0
        self.buzzer.off()

    def on_pressed_1(self):
        self.button_queue.put(1)
    def on_pressed_2(self):
        self.button_queue.put(2)
    def on_pressed_3(self):
        self.button_queue.put(3)
    def on_pressed_4(self):
        self.button_queue.put(4)
    def on_pressed_5(self):
        self.button_queue.put(5)

    def pop_button(self):
        if self.button_queue.empty():
            return 0
        return self.button_queue.get()

    def is_btn_held(self, num):
        return self.buttons[num].is_held

    def cpu_highfreq(self):
        cpu_highfreq()

    def cpu_lowfreq(self):
        cpu_lowfreq()

class BucketIO_Simulator:
    def __init__(self):
        self.is_sim = True
        self.button_queue = queue.Queue()
        self.image = Image.new("1", (OLED_WIDTH + 2, OLED_HEIGHT + 2))
        self.imagedraw = ImageDraw.Draw(self.image)
        self.oled_blankimage()

    def oled_blankimage(self):
        self.imagedraw.rectangle((0, 0, OLED_WIDTH + 2, OLED_HEIGHT + 2), outline=0, fill=0)

    def oled_show(self):
        npimg = numpy.array(self.image.convert('RGB'))
        cv2.imshow("img", npimg)
        k = cv2.waitKey(1)
        if k == ord('1'):
            self.button_queue.put(1)
        elif k == ord('2'):
            self.button_queue.put(2)
        elif k == ord('3'):
            self.button_queue.put(3)
        elif k == ord('4'):
            self.button_queue.put(4)
        elif k == ord('5'):
            self.button_queue.put(5)

    def batt_read(self):
        return [2048, 2048], [8.4, 8.4], [100, 100]

    def pop_button(self):
        if self.button_queue.empty():
            return 0
        return self.button_queue.get()

    def is_btn_held(self, num):
        return False

    def buzzer_on(self):
        pass

    def buzzer_off(self):
        pass

    def cpu_highfreq(self):
        pass

    def cpu_lowfreq(self):
        pass

def adc_to_voltage(x):
    if x < 0:
        return 0
    vbefore = x * BATT_ADC_INTERNAL_REF / (pow(2, 8) - 1)
    vafter = vbefore * (BATT_VDIV_RLOWER + BATT_VDIV_RUPPER) / BATT_VDIV_RLOWER

    vafter *= BATT_VOLT_CALIBRATION_SCALE_M
    vafter += BATT_VOLT_CALIBRATION_SCALE_B
    # 8.0 = 5.96 * m + b
    # 6.0 = 5.22 * m + b

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
    global is_embedded
    if is_embedded == False:
        return False
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

def cpu_userspace():
    g = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor", recursive=True)
    for cpu in g:
        os.system("sudo sh -c \"echo userspace > %s\"" % cpu)

def cpu_get_maxminfreq(word):
    if os.name == "nt":
        return 0
    maxfreq = 0
    minfreq = 999999999999
    g = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_" + word + "_freq", recursive=True)
    for cpu in g:
        str = subprocess.run(["cat", cpu], capture_output=True, text=True).stdout
        str = str.strip() if str is not None else ""
        if str.isnumeric():
            x = int(str)
            if x > maxfreq:
                maxfreq = x
            if x < minfreq:
                minfreq = x
    if word == "max":
        return maxfreq
    else:
        return minfreq

CPU_MAXFREQ = cpu_get_maxminfreq("max")
CPU_MINFREQ = cpu_get_maxminfreq("min")

def cpu_highfreq(freq_hz = CPU_MAXFREQ):
    g = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_setspeed", recursive=True)
    for cpu in g:
        os.system("sudo sh -c \"echo %d > %s\"" % (freq_hz, cpu))

def cpu_lowfreq(freq_hz = CPU_MINFREQ):
    g = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_setspeed", recursive=True)
    for cpu in g:
        os.system("sudo sh -c \"echo %d > %s\"" % (freq_hz, cpu))

def test_adc():
    bhw = BucketIO()
    bhw.init_pins()
    bhw.init_adc()
    print("Bucket IO initialized ADC")
    while True:
        raws, volts, percentages = bhw.batt_read()
        print("%d, %5d, %.2f, %.1f, %5d, %.2f, %.1f" % (round(time.monotonic()), raws[0], volts[0], percentages[0], raws[1], volts[1], percentages[1]))
        time.sleep(1)

def test_buttons():
    bhw = BucketIO()
    bhw.init_buttons()
    print("Bucket IO initialized buttons")
    while True:
        time.sleep(1)
        str = "%d, " % (round(time.monotonic()))
        if bhw.button_queue.empty():
            str += "none"
        while bhw.button_queue.empty() == False:
            str += "%d, " % bhw.button_queue.get()
        print(str)

def test_buzzer():
    bhw = BucketIO()
    bhw.init_pins()
    print("Bucket IO initialized buzzer")
    while True:
        time.sleep(1)
        bhw.buzzer_on()
        print("%d - buzz" % (round(time.monotonic())))
        time.sleep(0.2)
        bhw.buzzer_off()

def test_oled(t = 0):
    bhw = BucketIO()
    bhw.init_pins()
    bhw.init_oled()
    font = ImageFont.truetype("04b03mod.ttf", size = 8)
    (font_width, font_height) = font.getsize("X")
    while True:
        bhw.oled_blankimage()
        str = "Time: %.3f" % time.monotonic()
        bhw.imagedraw.text((2, 2), str, font=font, fill=255)
        bhw.oled_show()
        print(str)
        time.sleep(t) # this manages about 10 FPS

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "adc":
            test_adc()
        elif sys.argv[1] == "oled":
            test_oled()
        elif sys.argv[1] == "buzzer":
            test_buzzer()
        elif sys.argv[1] == "buttons":
            test_buttons()
        elif sys.argv[1] == "cpu":
            cpu_userspace()
            print("CPU frequencies range: %d - %d" % (CPU_MINFREQ, CPU_MAXFREQ))
        elif sys.argv[1] == "cpu_high":
            cpu_userspace()
            print("CPU frequencies range: %d - %d" % (CPU_MINFREQ, CPU_MAXFREQ))
            cpu_highfreq()
        elif sys.argv[1] == "cpu_low":
            cpu_userspace()
            print("CPU frequencies range: %d - %d" % (CPU_MINFREQ, CPU_MAXFREQ))
            cpu_lowfreq()
    else:
        bhw = BucketIO() # make an instance for running in terminal
