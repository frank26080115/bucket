#!/usr/bin/env python3

import os, sys, time, datetime, shutil, signal, random, math
import threading, queue, socket
import psutil

from PIL import Image, ImageDraw, ImageFont, ExifTags

import bucketapp, bucketio, bucketftp, bucketcopy, bucketlogger, bucketutils

logger = bucketlogger.getLogger()
bucket_app = None

MENUITEM_BACK         = 0
MENUITEM_CLEARWARN    = 1
MENUITEM_CLEARSESSION = 2
MENUITEM_SHUTDOWN     = 3
MENUITEM_EJECT        = 4
MENUITEM_CLONE        = 5
MENUITEM_LOSTFILES    = 6
MENUITEM_NETINFO      = 7
MENUITEM_QRCODES      = 8

class BucketMenu:
    def __init__(self, app):
        global bucket_app
        self.app = app
        bucket_app = app
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
        # dynamically generate the menu based on what items should be visible
        if self.app.alarm_reason != 0:
            items.append(["CLEAR WARNS", MENUITEM_CLEARWARN])
        items.append(["CLEAR SESSION", MENUITEM_CLEARSESSION])
        items.append(["SHUTDOWN", MENUITEM_SHUTDOWN])
        if len(self.app.disks) > 0:
            items.append(["EJECT DISK", MENUITEM_EJECT])
            if len(self.app.disks) > 1:
                tail = "[OFF]"
                if self.app.copier.state == bucketcopy.COPIERSTATE_DONE:
                    tail = "[DONE]"
                elif self.app.copier.mode == bucketcopy.COPIERMODE_MAIN2REDUN:
                    tail = "[M>R]"
                elif self.app.copier.mode == bucketcopy.COPIERMODE_BOTH:
                    tail = "[M<->R]"
                elif self.app.copier.mode == bucketcopy.COPIERMODE_REDUN2MAIN:
                    tail = "[R>M]"
                items.append(["CLONE DISKS " + tail, MENUITEM_CLONE])
        if len(self.app.session_lost_list) > 0:
            items.append(["LOST FILES", MENUITEM_LOSTFILES])
        items.append(["NET INFO", MENUITEM_NETINFO])
        items.append(["NET QR CODES", MENUITEM_QRCODES])

        # there is a chance that the menu changed while still visible
        # ideally we keep the user's cursor on the same item
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

        # implement an automatic timeout to go back out of the menu
        if (time.monotonic() - self.timeout_time) > 10:
            self.reset_state()
            self.app.ux_screen = bucketapp.UXSCREEN_MAIN
            return

        self.app.hwio.oled_blankimage()
        (font_width, font_height) = self.app.font.getsize("X")
        pad = 1 if os.name == "nt" else 0
        # draw all menu items and indicate the selected one with a cursor
        i = 0
        while i < len(items):
            s = (">" if i == self.selected_idx else "") + items[i][0]
            y = (font_height + bucketapp.UX_LINESPACE) * i
            self.app.hwio.imagedraw.text((pad, pad+y), s, font=self.app.font, fill=255)
            i += 1

        self.selected_item = items[self.selected_idx][1]
        self.last_selected_item = self.selected_item
        # display the correct options and do the right actions according to which item is selected
        if self.selected_item == MENUITEM_BACK:
            self.draw_bottom_texts(mid="BACK")
            if btn_popped == 2:
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN
        elif self.selected_item == MENUITEM_CLEARWARN:
            self.draw_bottom_texts(mid="CLEAR")
            if btn_popped == 2:
                self.app.reset_alarm()
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN
        elif self.selected_item == MENUITEM_CLEARSESSION:
            self.draw_bottom_texts(left="CLEAR", right="CLR LOST")
            if btn_popped == 1:
                self.app.reset_stats()
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN
            elif btn_popped == 3:
                self.app.reset_lost()
                self.reset_state()
                self.app.ux_screen = bucketapp.UXSCREEN_MAIN
        elif self.selected_item == MENUITEM_SHUTDOWN:
            self.draw_bottom_texts(left="SHUTDN", right="REBOOT")
            if btn_popped == 1:
                self.app.hwio.oled_blankimage()
                self.draw_bottom_texts(left="HALTING...")
                self.app.hwio.oled_show()
                time.sleep(1)
                if os.name != "nt":
                    bucketutils.run_cmdline_read("sudo shutdown -h now")
                    while True:
                        time.sleep(1)
                else:
                    sys.exit()
            elif btn_popped == 3:
                self.app.hwio.oled_blankimage()
                self.draw_bottom_texts(left="REBOOTING...")
                self.app.hwio.oled_show()
                time.sleep(1)
                if os.name != "nt":
                    bucketutils.run_cmdline_read("sudo reboot")
                    while True:
                        time.sleep(1)
                else:
                    sys.exit()
            elif btn_popped == 2:
                # hidden option
                self.draw_bottom_texts(mid=("%d" % os.getpid()))
                self.hold_screen(btn_popped)
                
        elif self.selected_item == MENUITEM_EJECT:
            if len(self.app.disks) > 1:
                self.draw_bottom_texts(left="EJ-MAIN", right="-OTHER")
                if btn_popped == 1:
                    bucketapp.disk_unmount_start(self.app.disks[0])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
                elif btn_popped == 3:
                    bucketapp.disks_unmount(self.app.disks[1:])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
            elif len(self.app.disks) == 1:
                self.draw_bottom_texts(mid="EJECT")
                if btn_popped == 2:
                    bucketapp.disk_unmount_start(self.app.disks[0])
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
        elif self.selected_item == MENUITEM_CLONE:
            if self.app.copier.is_off():
                is_camera = False
                if bucketutils.is_disk_camera(self.app.disks[1]):
                    self.draw_bottom_texts(right="CAM>MAIN")
                    is_camera = True
                else:
                    self.draw_bottom_texts(left="M>R", mid="M<->R", right="R>M")
                if btn_popped == 1 and is_camera == False:
                    self.app.copier.start(bucketcopy.COPIERMODE_MAIN2REDUN)
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
                if btn_popped == 2 and is_camera == False:
                    self.app.copier.start(bucketcopy.COPIERMODE_BOTH)
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
                if btn_popped == 3:
                    self.app.copier.start(bucketcopy.COPIERMODE_REDUN2MAIN)
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
            else:
                self.draw_bottom_texts(mid="OFF")
                if btn_popped == 2:
                    self.app.hwio.oled_blankimage()
                    self.draw_bottom_texts(left="STOPPING COPY...")
                    self.app.hwio.oled_show()
                    self.app.copier.user_cancel()
                    self.app.copier.start(bucketcopy.COPIERMODE_NONE)
                    self.reset_state()
                    self.app.ux_screen = bucketapp.UXSCREEN_MAIN
        elif self.selected_item == MENUITEM_LOSTFILES:
            self.draw_bottom_texts(mid="SHOW")
            if btn_popped == 2:
                self.timeout_time = time.monotonic()
                self.app.hwio.oled_blankimage()
                self.show_lost_files()
                self.hold_screen(btn_popped)
        elif self.selected_item == MENUITEM_NETINFO:
            self.draw_bottom_texts(left="INFO",right="CLIENTS")
            if btn_popped == 1 or btn_popped == 3:
                self.timeout_time = time.monotonic()
                self.app.hwio.oled_blankimage()
                if btn_popped == 1:
                    self.show_ftp_info()
                elif btn_popped == 3:
                    self.show_wifi_clients()
                self.hold_screen(btn_popped)
        elif self.selected_item == MENUITEM_QRCODES:
            self.draw_bottom_texts(left="WIFI",right="URL")
            if btn_popped == 1:
                self.show_qr_code_wifi()
            elif btn_popped == 3:
                self.show_qr_code_url()
            elif btn_popped == 2:
                # hidden demo
                self.show_demo_screen()
        #self.app.hwio.oled_show()

    def draw_bottom_texts(self, left="", mid="", right="", yoffset = 0):
        pad = 1 if os.name == "nt" else 0
        (font_width, font_height) = self.app.font.getsize("X")
        y = bucketio.OLED_HEIGHT - font_height + yoffset - 1
        if len(left) > 0:
            self.app.hwio.imagedraw.text((pad, pad+y), left, font=self.app.font, fill=255)
        if len(mid) > 0:
            (font_width, font_height) = self.app.font.getsize(mid)
            x = round((bucketio.OLED_WIDTH / 2) - (font_width / 2))
            self.app.hwio.imagedraw.text((pad+x, pad+y), mid, font=self.app.font, fill=255)
        if len(right) > 0:
            (font_width, font_height) = self.app.font.getsize(right)
            x = round((bucketio.OLED_WIDTH) - (font_width))
            self.app.hwio.imagedraw.text((pad+x, pad+y), right, font=self.app.font, fill=255)

    def hold_screen(self, btn, min_t = 3):
        self.app.hwio.oled_show()
        time.sleep(min_t)
        while self.app.hwio.is_btn_held(btn):
            self.timeout_time = time.monotonic()
            self.app.hwio.oled_show()

    def show_lost_files(self):
        pad = 1 if os.name == "nt" else 0
        s = "LOST:"
        oldstr = s
        (font_width, font_height) = self.app.font.getsize(s)
        i = 0
        y = 0
        tot = len(self.app.session_lost_list)
        while i <= tot and y < (bucketio.OLED_HEIGHT - bucketapp.UX_LINESPACE - font_height):
            if i < tot:
                s += " %d" % (self.app.session_lost_list[i])
            s = s.strip()
            if len(s) > 0:
                (font_width, font_height) = self.app.font.getsize(s)
                if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    self.app.hwio.imagedraw.text((pad, pad+y), oldstr, font=self.app.font, fill=255)
                    y += font_height + bucketapp.UX_LINESPACE
                    if i < tot:
                        s = "%d" % (self.app.session_lost_list[i])
                    else:
                        s = ""
                    oldstr = s
                else:
                    oldstr = s
                    self.app.hwio.imagedraw.text((pad, pad+y), oldstr, font=self.app.font, fill=255)
            i += 1

    def show_ftp_info(self):
        txtlist = []
        txtlist.append("WIFI SSID:")
        txtlist.append(bucketutils.get_wifi_ssid())
        txtlist.append("IP ADDR:")
        txtlist.append(bucketutils.get_wifi_ip())
        txtlist.append("FTP/HTTP PORT:")
        txtlist.append("%u / %u" % (self.app.cfg_get_ftpport(), self.app.cfg_get_httpport()))
        txtlist.append("FTP USERNAME:")
        txtlist.append(self.app.cfg_get_ftpusername())
        txtlist.append("WIFI/FTP PASSWORD:")
        txtlist.append(self.app.cfg_get_ftppassword())
        pad = 1 if os.name == "nt" else 0
        y = 0
        (font_width, font_height) = self.app.font.getsize('X')
        for t in txtlist:
            self.app.hwio.imagedraw.text((pad, pad+y), t, font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE

    def show_wifi_clients(self):
        clients = bucketutils.get_wifi_clients()
        pad = 1 if os.name == "nt" else 0
        (font_width, font_height) = self.app.font.getsize('X')
        y = 0
        for c in clients:
            self.app.hwio.imagedraw.text((pad, pad+y), c, font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
        if len(clients) <= 0:
            self.app.hwio.imagedraw.text((pad, pad+y), "NONE", font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE

    def show_qr_code_wifi(self):
        self.show_qr_code("WIFI:S:%s;T:WPA;P:%s;;" % (bucketutils.get_wifi_ssid(), self.app.cfg_get_ftppassword()))

    def show_qr_code_url(self):
        self.show_qr_code("http://%s:%u/" % (bucketutils.get_wifi_ip(), self.app.cfg_get_httpport()))

    def show_qr_code(self, x):
        try:
            import qrcode
            box_sz = 4
            while box_sz >= 1:
                qr = qrcode.QRCode(version=1, box_size=box_sz, border=1) # box size = 2 seems to fit the screen
                qr.add_data(x)
                qr.make(fit = True)
                img = qr.make_image(fill_color="white", back_color="black")
                imgbw = img.convert('1')
                width, height = imgbw.size
                if height <= bucketio.OLED_HEIGHT:
                    break
                box_sz -= 1
            self.app.hwio.oled_blankimage()
            self.app.hwio.imagedraw.bitmap((int(round((bucketio.OLED_WIDTH - width) / 2)), int(round((bucketio.OLED_HEIGHT - height) / 2))), imgbw, fill=255)
            self.app.hwio.oled_show()
        except Exception as ex:
            logger.error("ERROR: while generating QR code: " + str(ex))
            pad = 1 if os.name == "nt" else 0
            y = 0
            (font_width, font_height) = self.app.font.getsize('X')
            self.app.hwio.imagedraw.text((pad, pad+y), "QR code", font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
            self.app.hwio.imagedraw.text((pad, pad+y), "generation", font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
            self.app.hwio.imagedraw.text((pad, pad+y), "failed", font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
            self.app.hwio.imagedraw.text((pad, pad+y), "DATA:", font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
            self.app.hwio.imagedraw.text((pad, pad+y), x, font=self.app.font, fill=255)
            y += font_height + bucketapp.UX_LINESPACE
            self.app.hwio.oled_show()
        while True:
            self.timeout_time = time.monotonic()
            btn_popped = self.app.hwio.pop_button()
            if btn_popped != 0:
                return
            time.sleep(0.1)
            self.app.hwio.oled_show()

    def show_demo_screen(self):
        while True:
            self.timeout_time = time.monotonic()
            self.app.ux_frame(demo = True)
            time.sleep(0.1)
            if self.app.hwio.pop_button() != 0:
                return
