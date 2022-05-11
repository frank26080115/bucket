#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math
import threading, queue, socket
import psutil

from PIL import Image, ImageDraw, ImageFont, ExifTags
from pyftpdlib.log import logger, config_logging, debug

import bucketapp, bucketio, bucketftp

bucket_app = None

MENUITEM_BACK      = 0
MENUITEM_CLEARWARN = 1
MENUITEM_SHUTDOWN  = 2
MENUITEM_EJECT     = 3
MENUITEM_CLONE     = 4
MENUITEM_LOSTFILES = 5
MENUITEM_FTPINFO   = 6

class BucketMenuItem:
    def __init__(self, menu, num, str,

class BucketMenu:
    def __init__(self, app):
        self.app = app
        self.reset_state()

    def reset_state(self):
        self.selected_idx = 0
        #self.last_selected_idx = 0
        self.selected_item = 0
        self.last_selected_item = 0
        self.timeout_time = time.monotonic()

    def run(self):
        if self.app.ux_screen != bucketapp.UXSCREEN_MENU:
            self.reset_state()
            return

        items = []
        items.append(["BACK", MENUITEM_BACK])
        if self.app.alarm_reasons != 0:
            items.append(["CLEAR WARNS", MENUITEM_CLEARWARN])
        items.append(["SHUTDOWN", MENUITEM_SHUTDOWN])
        if len(self.app.disk_list) > 0:
            items.append(["EJECT DISK", MENUITEM_EJECT])
            if len(self.app.disk_list) > 1:
                items.append(["CLONE DISKS", MENUITEM_CLONE])
        if len(self.app.session_lost_list) > 0:
            items.append(["LOST FILES", MENUITEM_LOSTFILES])
        items.append(["FTP INFO", MENUITEM_FTPINFO])

        sel_item = items[self.selected_idx][1]
        if sel_item != self.last_selected_item:
            self.timeout_time = time.monotonic()
            found = -1
            i = 0
            while i < len(items):
                if items[i][1] == self.last_selected_item:
                    found = i
                i += 1
            if found >= 0:
                self.selected_idx = found
                #self.last_selected_idx = found
            else:
                self.selected_idx = 0

        btn_popped = self.app.hwio.pop_button()
        if btn_popped != 0:
            self.timeout_time = time.monotonic()
        if btn_popped == 4:
            if self.selected_idx < (len(items) - 1):
                self.selected_idx += 1
        elif btn_popped == 5:
            if self.selected_idx > 0:
                self.selected_idx -= 1

        if (time.monotonic() - self.timeout_time) > 10:
            self.reset_state()
            self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
            return

        self.app.hwio.oled_blankimage()
        pad = 1 if os.name == "nt" else 0
        i = 0
        while i < len(items):
            str = (">" if i == self.selected_idx else "") + items[i][0]
            y = (font_height + UX_LINESPACE) * i
            self.hwio.imagedraw.text((pad, pad+y), str, font=self.app.font, fill=255)
            i += 1

        sel_item = items[self.selected_idx][1]
        if sel_item == MENUITEM_BACK:
            self.draw_bottom_texts(mid="BACK")
            if btn_popped == 2:
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
        elif sel_item == MENUITEM_CLEARWARN:
            self.draw_bottom_texts(mid="CLEAR")
            if btn_popped == 2:
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
        elif sel_item == MENUITEM_SHUTDOWN:
            self.draw_bottom_texts(left="SHUTDN", right="REBOOT")
            if btn_popped == 1:
                self.app.hwio.oled_blankimage()
                self.draw_bottom_texts(left="HALTING...")
                self.app.hwio.oled_show()
                time.sleep(1)
                os.popen("sudo halt")
                while True:
                    time.sleep(1)
            elif btn_popped == 3:
                self.app.hwio.oled_blankimage()
                self.draw_bottom_texts(left="REBOOTING...")
                self.app.hwio.oled_show()
                time.sleep(1)
                os.popen("sudo reboot")
                while True:
                    time.sleep(1)
        elif sel_item == MENUITEM_EJECT:
            if len(self.app.disk_list) > 1:
                self.draw_bottom_texts(left="EJ-MAIN", right="-OTHER")
                if btn_popped == 1:
                    self.app.eject_disk(self.app.disk_list[0:0])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
                elif btn_popped == 3:
                    self.app.eject_disk(self.app.disk_list[1:])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
            elif len(self.app.disk_list) == 1:
                self.draw_bottom_texts(mid="EJECT")
                if btn_popped == 2:
                    self.app.eject_disk(self.app.disk_list[0:0])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
        elif sel_item == MENUITEM_CLONE:
            if self.app.cloning_enaged:
                self.draw_bottom_texts(mid="ON=>OFF")
                if btn_popped == 2:
                    self.app.cloning_enaged = False
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
            else:
                self.draw_bottom_texts(mid="OFF=>ON")
                if btn_popped == 2:
                    self.app.cloning_enaged = True
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN:
        elif sel_item == MENUITEM_LOSTFILES or sel_item == MENUITEM_FTPINFO:
            self.draw_bottom_texts(mid="SHOW")
            if btn_popped == 2 or self.app.hwio.is_btn_held(2):
                self.timeout_time = time.monotonic()
                self.app.hwio.oled_blankimage()
                if sel_item == MENUITEM_LOSTFILES:
                    self.show_lost_files()
                elif sel_item == MENUITEM_FTPINFO:
                    self.show_ftp_info()
                self.app.hwio.oled_show()
                time.sleep(3)
        #self.app.hwio.oled_show()

    def draw_bottom_texts(self, left="", mid="", right="", yoffset = 0):
        pad = 1 if os.name == "nt" else 0
        (font_width, font_height) = self.app.font.getsize("X")
        y = bucketio.OLED_HEIGHT - font_height
        if len(left) > 0:
            self.hwio.imagedraw.text((pad, pad+y), left, font=self.app.font, fill=255)
        if len(mid) > 0:
            (font_width, font_height) = self.app.font.getsize(mid)
            x = round((OLED_WIDTH / 2) - (font_width / 2))
            self.hwio.imagedraw.text((pad+x, pad+y), mid, font=self.app.font, fill=255)
        if len(right) > 0:
            (font_width, font_height) = self.app.font.getsize(right)
            x = round((OLED_WIDTH) - (font_width))
            self.hwio.imagedraw.text((pad+x, pad+y), right, font=self.app.font, fill=255)

    def show_lost_files(self):
        pad = 1 if os.name == "nt" else 0
        str = "LOST:"
        oldstr = str
        (font_width, font_height) = self.app.font.getsize(str)
        i = 0
        y = 0
        tot = len(self.app.session_lost_list)
        while i <= tot and y < (bucketio.OLED_HEIGHT - UX_LINESPACE - font_height):
            if i < tot:
                str += " %d" % (self.app.session_lost_list[i])
            str = str.strip()
            if len(str) > 0:
                (font_width, font_height) = self.app.font.getsize(str)
                if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    self.hwio.imagedraw.text((pad, pad+y), oldstr, font=self.app.font, fill=255)
                    y += font_height + bucketapp.UX_LINESPACE
                    if i < tot:
                        str = "%d" % (self.app.session_lost_list[i])
                    else:
                        str = ""
                    oldstr = str
                else:
                    oldstr = str
                    self.hwio.imagedraw.text((pad, pad+y), oldstr, font=self.app.font, fill=255)
            i += 1

    def show_ftp_info(self):
        txtlist = []
        txtlist.append("IP ADDR:")
        txtlist.append(bucketapp.get_wifi_ip())
        txtlist.append("WIFI SSID:")
        txtlist.append(bucketapp.get_wifi_ssid())
        txtlist.append("FTP USERNAME:")
        txtlist.append(self.app.cfg_get_ftpusername())
        txtlist.append("FTP PASSWORD:")
        txtlist.append(self.app.cfg_get_ftppassword())
        pad = 1 if os.name == "nt" else 0
        y = 0
        for t in txtlist:
            self.hwio.imagedraw.text((pad, pad+y), t, font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE