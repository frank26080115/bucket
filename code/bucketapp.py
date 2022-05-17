#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math, glob
import threading, queue, socket
import psutil

from PIL import Image, ImageDraw, ImageFont, ExifTags
import pyftpdlib.log

import bucketio, bucketmenu, bucketftp, bucketviewer

bucket_app = None

CONFIG_FILE_NAME    = "bucket_cfg.json"
LOW_SPACE_THRESH_MB = 200
LOW_BATT_THRESH     = 10

ALARMFLAG_DISKFULL   = 1
ALARMFLAG_LOSTFILE = 2
ALARMFLAG_BATTLOW    = 4

CLONING_OFF        = 0
CLONING_MAIN2REDUN = 1
CLONING_BOTH       = 2
CLONING_REDUN2MAIN = 3
CLONING_DONE       = -1

UX_LINESPACE = 0

UXSCREEN_MAIN = 0
UXSCREEN_MENU = 1

class BucketApp:
    def __init__(self, hwio = None):
        global bucket_app
        bucket_app = self
        self.disks = []
        self.cfg = None
        self.ftp_server  = None
        self.ftp_thread  = None
        self.http_server = None
        self.http_thread = None
        self.hwio = hwio
        self.has_rtc = bucketio.has_rtc()
        self.has_date = None
        self.cloning_enaged = CLONING_OFF
        self.last_file = None
        self.last_file_date = None
        self.start_monotonic_time = time.monotonic()
        self.session_last_act     = None
        self.session_last_nonact  = None
        self.alarm_reason = 0
        self.batt_lowest  = 100
        self.reset_stats()

        self.font = ImageFont.truetype("04b03mod.ttf", size = 8)
        self.font_has_lower = True
        self.last_frame_time = 0
        self.ux_frame_cnt    = 0
        self.ux_screen       = UXSCREEN_MAIN
        self.ux_menu         = bucketmenu.BucketMenu(self)

        self.thumbnail_queue = Queue.queue()

    def reset_stats(self):
        self.session_first_number = None
        self.session_last_number  = None
        self.session_total_cnt    = 0
        self.session_lost_cnt     = 0
        self.session_lost_list    = []
        self.both_file_types      = 0
        self.fsize_idx            = 0
        self.fsize_list           = [0] * 6
        self.fsize_avg            = 80 # start with a worse case estimate

    def reset_alarm(self):
        self.alarm_reason = 0
        self.hwio.buzzer_off()

    def update_disk_list(self):
        partitions = get_mounted_disks()
        if len(self.disks) <= 0:
            # on boot, go from no disks to having many disks
            # use the biggest disk as primary write target
            partitions.sort(reverse = True, key = disk_sort_func)
            for i in partitions:
                self.disks.append(i)
            bucketlogger.reconfig(self)
        else:
            # update the list in a way that preserves the previous order
            # this way, the top disk in the list is still the primary write target
            newlist = []
            for i in self.disks:
                found = False
                for j in partitions:
                    if i == j:
                        found = True
                        break
                if found:
                    newlist.append(i)
            found = False
            for i in partitions:
                found = False
                for j in newlist:
                    if i == j:
                        found = True
                        break
                if not found:
                    newlist.append(i)
            self.disks = newlist
            if not found:
                bucketlogger.reconfig(self)

    def get_root(self):
        self.update_disk_list()
        if len(self.disks) > 0:
            return self.disks[0]
        return None

    def still_has_space(self):
        if len(self.disks) <= 0:
            return False
        total, free = get_disk_stats(self.disks[0])
        if free < LOW_SPACE_THRESH_MB:
            # another disk available?
            while i < len(self.disks):
                total2, free2 = get_disk_stats(self.disks[i])
                if free2 >= LOW_SPACE_THRESH_MB: # free space on another disk?
                    disk0 = self.disks[i]
                    self.disks[i] = self.disks[0]
                    self.disk[0] = disk0
                    return True
                i += 1
            return False
        else:
            return True

    def get_datetime(self):
        if self.has_rtc or self.has_date is None:
            return datetime.datetime.now()
        elif self.has_date is None and self.last_file_date is not None:
            return self.last_file_date + datetime.timedelta(seconds = self.get_elapsed_secs())
        elif self.has_date is not None:
            return self.has_date + datetime.timedelta(seconds = self.get_elapsed_secs())

    def get_date_str(self):
        return self.get_datetime().strftime("%y%m%d")

    def get_elapsed_secs(self):
        return time.monotonic() - self.start_monotonic_time

    def on_activity(self):
        self.session_last_act = time.monotonic()

    def on_nonactivity(self):
        self.session_last_nonact = time.monotonic()

    def load_cfg(self):
        import json

        # find all disks that may contain a config file
        disks = get_mounted_disks()
        disks.sort(reverse = True, key = disk_sort_func)
        if len(disks) <= 0:
            return

        # look on all disks for the config file
        for d in disks:
            path = os.path.join(d, CONFIG_FILE_NAME)
            if os.path.isfile(path):
                try:
                    with open(path, 'r') as f:
                        self.cfg = json.load(f) # loads a file as a dictionary
                    return
                except Exception as ex:
                    logger.error("Failed to load JSON cfg file at \"" + path + "\", exception: " + str(ex))

        if self.has_rtc == False:
            # we have no RTC so look for a directory with the latest date code
            for d in disks:
                bucket_name = self.cfg_get_bucketname()
                bucket_dir = os.path.join(d, bucket_name)
                g = glob.glob(os.path.join(bucket_dir, "*") + os.path.sep, recursive = True)
                g.sort(reverse=True) # make sure the latest is on top
                for dir in g:
                    if os.path.isdir(dir) and "-" in dir:
                        hdr = dir[0:dir.index('-')]
                        if hdr.isnumeric() and len(hdr) == 6:
                            self.last_file_date = datetime.datetime.strptime("20" + hdr, "%Y%m%d")
                            bucketlogger.reconfig(self)

    def cfg_get_genericstring(self, key, defval):
        result = defval
        try:
            if self.cfg is None:
                return result
            if key not in self.cfg:
                return result
            result = str(self.cfg[key])
            return result
        except:
            return result
        finally:
            return result

    def cfg_get_genericbool(self, key, defval):
        result = defval
        try:
            if self.cfg is None:
                return result
            if key not in self.cfg:
                return result
            s = str(self.cfg[key]).strip().lower()
            if s == "true" or s == "yes" or s == "y":
                result = True
            elif s == "false" or s == "no" or s == "n":
                result = False
            elif s.isnumeric():
                if int(s) == 0:
                    result = False
                else:
                    result = True
            return result
        except:
            return result
        finally:
            return result

    def cfg_get_genericint(self, key, defval):
        result = defval
        try:
            if self.cfg is None:
                return result
            if key not in self.cfg:
                return result
            s = str(self.cfg[key]).strip().lower()
            if s.isnumeric():
                result = int(s)
            return result
        except:
            return result
        finally:
            return result

    def cfg_get_prefix(self):
        return self.cfg_get_genericstring("file_prefix", "DSC")

    def cfg_get_extensions(self, key = "file_extensions", defval = ['jpg', 'jpeg', 'arw', 'heif', 'hif']):
        result = defval
        try:
            if self.cfg is None:
                return result
            if key not in self.cfg:
                return result
            txt = str(self.cfg[key])
            # the cfg file will contain a list of acceptable image extensions in comma-separated format
            parts = txt.split(',')
            result2 = []
            for p in parts:
                p2 = p.strip()
                while p2.startswith('.'):
                    p2 = p2[1:].strip()
                while p2.endswith('.'):
                    p2 = p2[:-1].strip()
                if len(p2) > 0:
                    result2.append(p2)
            if len(result2) > 0:
                result = result2
            return result
        except:
            return result
        finally:
            return result

    def cfg_disk_prefer_total_vs_free(self):
        return self.cfg_get_genericbool("disk_prefer_total_vs_free", True)

    def cfg_get_ftpusername(self):
        return self.cfg_get_genericstring("ftp_username", "user")

    def cfg_get_ftppassword(self):
        return self.cfg_get_genericstring("ftp_password", "12345")

    def cfg_get_ftpport(self):
        return self.cfg_get_genericint("ftp_port", 2133)

    def cfg_get_bucketname(self):
        return self.cfg_get_genericstring("bucket_name", "photos")

    def on_before_open(self, filepath):
        isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(filepath)
        israw = ext_is_raw(fileext)
        if isimg and israw:
            fnum = int(filenumber)
            if self.session_first_number is None:
                self.session_first_number = fnum

        # we are busying copying a file, so stop the low priority thumbnail generation thread
        self.pause_thumbnail_generation()

    def on_file_received(self, file):
        self.last_file = file

        isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(file)
        israw = ext_is_raw(fileext) # ideally we only count statistics for raw files, otherwise this code can lose a raw file and not notify the user when the corresponding jpg file exists
        fnum = int(filenumber)

        # use consecutive identical numbers as an indicator that the camera is in raw+jpg mode
        if self.session_last_number is not None:
            if self.session_last_number == fnum and self.both_file_types < 10:
                self.both_file_types += 2
            elif self.both_file_types > 0:
                self.both_file_types -= 1

        if isimg:
            # use a running average list of the previous multiple file sizes
            fsize = math.ceil(os.path.getsize(file) / 1024 / 1024)
            if fsize > 0:
                self.fsize_list[self.fsize_idx] = fsize
                self.fsize_idx = (self.fsize_idx + 1) % len(self.fsize_list)
                if 0 not in self.fsize_list:
                    self.fsize_avg = math.ceil(sum(self.fsize_list) / len(self.fsize_list))
                    if self.both_file_types > 4:
                        self.fsize_avg *= 2

        if isimg and israw:
            # update the session statistics
            self.session_total_cnt += 1
            if self.session_first_number is None:
                self.session_first_number = fnum
            if self.session_last_number is not None:
                # check if we skipped any file numbers, accounting for roll-over
                fnum2 = fnum + 100000
                snum2 = self.session_last_number + 100000
                diff = (fnum2 - snum2) % 100000
                if diff >= 2 and (self.session_last_number == 9999 or self.session_last_number == 99999): # rollover scenario
                    diff -= 1
                if diff > 0: # difference = 1 is good, it means we counted up 1
                    diff -= 1
                self.session_lost_cnt += diff
                if diff > 0: # if we do lose a file, raise the alarm
                    self.on_lost_file()
                    i = 0
                    while i < diff:
                        lnum = fnum - i - 1
                        if lnum <= 0:
                            lnum += 10000
                        self.session_lost_list.append(lnum)
            self.session_last_number = fnum # update this after the delta check
            if len(self.session_lost_list) > 0:
                while fnum in self.session_lost_list:
                    self.session_lost_list.remove(fnum)

        # the way the FTP code works is that when this callback is called, the file has not been renamed yet
        if isimg:
            npath = rename_camera_file_path(file, self.cfg_get_bucketname(), self.disks[0])
            shutil.move(file, npath)
            # keep a record of the move so the FTP can still find it
            with open(file + ".washere", "w") as whf:
                whf.write(npath)
            self.last_file = npath
            self.thumbnail_queue.put(npath)

        self.update_disk_list()
        if len(self.disks) <= 1:
            return # no other disk to copy to, give up

        for origdisk in self.disks:
            for destdisk in self.disks:
                if origdisk == destdisk:
                    continue # don't copy to the same disk as the origin
                if file.startswith(origdisk):
                    # enqueue the task
                    self.copier.enqueue_copy(npath + ";" + os.path.join(destdisk, npath[len(origdisk):]))

    def on_missed_file(self, file):
        isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(file)
        if isimg and filenumber.isnumeric():
            fnum = int(filenumber)
            if fnum not in self.session_lost_list:
                self.session_lost_list.append(lnum)
            self.session_lost_cnt += 1
            self.on_lost_file()

    def on_disk_full(self):
        self.alarm_reason |= ALARMFLAG_DISKFULL
        self.hwio.buzzer_on()

    def on_lost_file(self):
        self.alarm_reason |= ALARMFLAG_LOSTFILE
        self.hwio.buzzer_on()

    def try_get_time(self):
        # we don't have a valid time because we have no RTC, but we might've gotten a file from the camera
        # if it is a image file with EXIF data, we can extract the current date from the image
        if self.has_rtc == False:
            if self.last_file is not None and self.has_date is None:
                flower = self.last_file.lower()
                if flower.endswith(".jpg") or flower.endswith(".jpeg") or flower.endswith(".arw"):
                    self.has_date = get_img_exif_date(self.last_file)
                    if self.has_date is None: # prevent infinite loop from error
                        self.last_file = None
                    else:
                        bucketlogger.reconfig(self)

    def generate_next_thumbnail(self):
        if self.thumbnail_queue.empty() == False:
            try:
                thumbme = self.thumbnail_queue.get()
                #bucketviewer.generate_thumbnail(thumbme)
                bucketviewer.enqueue_thumb_generation(thumbme, important=False)
            except Exception as ex:
                logger.error("Error generating thumbnail for \"" + thumbme + "\": " + str(ex))

    def pause_thumbnail_generation(self):
        thumb_later = bucketviewer.thumbgen_clear()
        for tl in thumb_later:
            self.thumbnail_queue.put(tl)

    def ux_frame(self):
        tnow = time.monotonic()
        # run this every 1/5th of a second
        if (tnow - self.last_frame_time) < 0.2:
            time.sleep(0.02) # otherwise yield to another thread
            return
        self.last_frame_time = tnow
        self.ux_frame_cnt += 1

        if self.ftp_server is not None and self.ftp_thread is None:
            logger.info("starting FTP thread")
            self.ftp_start()
        if self.http_server is not None and self.http_thread is None:
            logger.info("starting HTTP thread")
            self.ftp_start()

        pad = 1 if self.hwio.is_sim else 0
        y = 0
        (font_width, font_height) = self.font.getsize("X")
        self.hwio.oled_blankimage()

        if self.ux_screen == UXSCREEN_MAIN:
            if self.alarm_reason == 0:
                self.ux_show_clock(y, pad)
                y += font_height + UX_LINESPACE
                self.ux_show_batt(y, pad, (self.ux_frame_cnt % 20) < 10)
                y += font_height + UX_LINESPACE
            else:
                tmod = (self.ux_frame_cnt % 30)
                if tmod < 10:
                    self.ux_show_clock(y, pad)
                else:
                    self.ux_show_batt(y, pad, (tmod < 20))
                y += font_height + UX_LINESPACE
                self.ux_show_warnings(y, pad)
                y += font_height + UX_LINESPACE

            self.ux_show_wifi(y, pad)
            y += font_height + UX_LINESPACE

            self.ux_show_session(y, pad)
            y += font_height + UX_LINESPACE

            self.ux_show_disks(y, pad)
            y += font_height + UX_LINESPACE

            if len(self.session_lost_list) > 0:
                self.ux_show_lost(y, pad)
                y += font_height + UX_LINESPACE

            y = bucketio.OLED_HEIGHT - font_height
            self.hwio.imagedraw.text((pad, pad+y), "MENU", font=self.font, fill=255)
            if self.hwio.pop_button() == 1:
                self.ux_screen = UXSCREEN_MENU
                self.ux_menu.reset_state()
        elif self.ux_screen == UXSCREEN_MENU:
            self.ux_menu.run()

        self.hwio.oled_show()

    def ux_show_timesliced_texts(self, y, pad, prefix, textlist, period):
        tmod = self.ux_frame_cnt % (period * (len(textlist)))
        i = 0
        while i < len(textlist):
            if tmod < (period * (i + 1)):
                self.hwio.imagedraw.text((pad, pad+y), prefix + textlist[i], font=self.font, fill=255)
                return
            i += 1

    def ux_show_clock(self, y, pad):
        secstr = str(round(self.get_elapsed_secs()))
        clkstrhead = "CLK:  " + self.get_date_str() + " +"
        clkstr = clkstrhead + secstr + "s"
        (font_width, font_height) = self.font.getsize(clkstr)
        if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
            clkstr = clkstrhead + secstr
            (font_width, font_height) = self.font.getsize(clkstr)
            if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                clkstrhead = "CLK:" + self.get_date_str() + "+"
                clkstr = clkstrhead + secstr
                (font_width, font_height) = self.font.getsize(clkstr)
                if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    clkstrhead = "T:" + self.get_date_str() + "+"
                    clkstr = clkstrhead + secstr
                    (font_width, font_height) = self.font.getsize(clkstr)
                    while font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                        secstr = secstr[1:]
                        clkstr = clkstrhead + secstr
                        (font_width, font_height) = self.font.getsize(clkstr)
        self.hwio.imagedraw.text((pad, pad+y), clkstr, font=self.font, fill=255)

    def ux_show_batt(self, y, pad, use_volts):
        batt_raws, batt_volts, batt_chgs = self.hwio.batt_read()
        higher_batt = max(batt_chgs)
        if higher_batt < self.batt_lowest:
            if self.batt_lowest >= LOW_BATT_THRESH and higher_batt < LOW_BATT_THRESH:
                self.alarm_reason |= ALARMFLAG_BATTLOW
                self.hwio.buzzer_on()
            self.batt_lowest = higher_batt
        str = "BATT: "
        if use_volts:
            for i in batt_volts:
                str += "%.2fV  " % i
        else:
            for i in batt_chgs:
                str += "%4d%%  " % round(i)
        self.hwio.imagedraw.text((pad, pad+y), str.rstrip(), font=self.font, fill=255)

    def ux_show_warnings(self, y, pad):
        hdr = "WARN: "
        str2 = ""
        if (self.alarm_reason & ALARMFLAG_DISKFULL) != 0:
            str2 += "FULL "
        if (self.alarm_reason & ALARMFLAG_BATTLOW) != 0:
            str2 += "BATT "
        if (self.alarm_reason & ALARMFLAG_LOSTFILE) != 0:
            str2 += "LOST "
        str2 = str2.rstrip()
        (font_width, font_height) = self.font.getsize(hdr + str2)
        if font_width < (bucketio.OLED_WIDTH - (pad * 2)):
            self.hwio.imagedraw.text((pad, pad+y), hdr + str2, font=self.font, fill=255)
        else:
            parts = str2.split(' ')
            txtlist = []
            if len(parts) == 1:
                txtlist.append(parts[0])
            else:
                for w in parts:
                    txtlist.append(w + "...")
            self.ux_show_timesliced_texts(y, pad, hdr, txtlist, 4)

    def ux_show_wifi(self, y, pad):
        if self.ftp_server is None or self.ftp_thread is None:
            self.hwio.imagedraw.text((pad, pad+y), "FTP IS OFF", font=self.font, fill=255)
            return

        hdr = "WIFI: "
        txtlist = []

        ipstr = get_wifi_ip()
        nstr = hdr + ipstr
        (font_width, font_height) = self.font.getsize(nstr)
        if font_width < (bucketio.OLED_WIDTH - (pad * 2)):
            txtlist.append(ipstr)
        else:
            ipparts = ipstr.split('.')
            if (self.ux_frame_cnt % 10) < 5:
                txtlist.append(ipparts[0] + "." + ipparts[1] + "...")
            else:
                txtlist.append("..." + ipparts[2] + "." + ipparts[3])

        ssid = get_wifi_ssid()
        if ssid is not None and len(ssid) > 0:
            (font_width, font_height) = self.font.getsize(hdr + ssid)
            if font_width < (bucketio.OLED_WIDTH - (pad * 2)):
                txtlist.append(ssid)
            else:
                while font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    ssid = ssid[0:-1]
                    (font_width, font_height) = self.font.getsize(hdr + ssid + "...")
                txtlist.append(ssid)

        if self.session_last_act is not None and (time.monotonic() - self.session_last_act) < 2:
            if self.font_has_lower:
                i = math.floor(self.ux_frame_cnt / 2) % (len(hdr) - 2)
                c = hdr[i].lower()
                nstr = hdr[0:i] + c + hdr[i+1:]
                hdr = nstr
        elif self.session_last_act is not None:
            tsec = time.monotonic() - self.session_last_act
            if tsec <= 120:
                txtlist.append("-%d " % (round(tsec)))
            else:
                tmin = tsec / 60
                txtlist.append("-%.1fm " % (tmin))
        self.ux_show_timesliced_texts(y, pad, hdr, txtlist, 7)

    def ux_show_session(self, y, pad):
        if self.session_first_number is None:
            self.hwio.imagedraw.text((pad, pad+y), "NEW SESSION", font=self.font, fill=255)
            return
        tmod = (self.ux_frame_cnt % (5 * 3))
        if tmod < (5 * 1):
            str = "SESS: %d~" % (self.session_first_number)
            if self.session_last_number is not None:
                str += "%d" % self.session_last_number
            else:
                str += "?"
        elif tmod < (5 * 2):
            str = "TOT: %d" % (self.session_total_cnt)
        elif tmod < (5 * 3):
            str = "LOST: %d" % (self.session_lost_cnt)
        if (time.monotonic() - self.session_last_act) < 2:
            if self.font_has_lower:
                # animated busy indication by making the letters dance
                r = random.randint(0, 3)
                c = str[r]
                str = str[0:r] + c.lower() + str[r + 1:]
            else:
                # animated busy symbol
                tmod = self.ux_frame_cnt % (2 * 5)
                if tmod < (2 * 1):
                    str = ">" + str
                elif tmod < (2 * 2):
                    str = "=" + str
                elif tmod < (2 * 3):
                    str = "-" + str
                elif tmod < (2 * 4):
                    str = "=" + str
        (font_width, font_height) = self.font.getsize(str)
        if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
            str = str.replace(" ", "")
        self.hwio.imagedraw.text((pad, pad+y), str.rstrip(), font=self.font, fill=255)

    def ux_show_disks(self, y, pad):
        self.update_disk_list()
        if len(self.disks) <= 0:
            self.hwio.imagedraw.text((pad, pad+y), "NO DISK", font=self.font, fill=255)
            return
        txtlist = []
        disk_idx = 0
        disk_cnt = len(self.disks)
        # for all disks
        while disk_idx < disk_cnt:
            disk = self.disks[disk_idx]
            # show which disk
            if disk_idx == 0:
                if disk_cnt > 1:
                    idc = "[M]: "
                else:
                    idc = ": "
            else:
                idc = "[%d]: " % disk_idx
            str1 = "DISK" + idc
            str2 = "FREE@@" + idc
            total, free = get_disk_stats(disk)
            is_copying = disk_idx > 0 and self.copier.is_busy() # indicate cloning is active
            (font_width, font_height) = self.font.getsize(str1 + (">" if is_copying else "") + disk)
            if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                # shorten the disk name to fit screen
                disk = disk[1:]
                disk = disk[disk.index(os.path.sep):]
            txtlist.append(str1 + (">" if is_copying else "") + disk)
            if self.fsize_avg > 0:
                left = math.floor(free / self.fsize_avg) # calculate how many images can be saved
                str3 = ("%d" % (left)) + ("?" if 0 in self.fsize_list else "") # append unsure indicator if required
            else:
                str3 = "???"
            str3 = (">" if is_copying else "") + str3

            # here's a messy way of checking if the string will fit the screen
            # if it doesn't, then we shrink it until it does
            (font_width, font_height) = self.font.getsize(str1 + str3)
            if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                str1 = "DSK" + str1[4:].strip()
                str1 = "REM" + str2[4:].strip()
                (font_width, font_height) = self.font.getsize(str1 + str3)
                if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    if disk_idx == 0:
                        str1 = "DISK:"
                        str2 = "FREE:"
                    else:
                        if disk_cnt > 1:
                            str1 = "DSK%d:" % disk_idx
                            str2 = "REM%d:" % disk_idx
                        else:
                            str1 = "DSK:"
                            str2 = "REM:"
                    (font_width, font_height) = self.font.getsize(str1 + str3)
                    if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                        str3 = (">" if is_copying else "") + "99999999999999999" # in the end, we eventually stop being able to show big numbers, so we make the number look big with 9s
                        (font_width, font_height) = self.font.getsize(str1 + str3)
                        while font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                            str3 = str3[0:-1]
                            (font_width, font_height) = self.font.getsize(str1 + str3 + "+")
                        str3 += "+"
            txtlist.append(str1 + str3)
            txtlist.append(str2 + str3)
            # two similar entries occupies two consecutive time slots
            disk_idx += 1
        self.ux_show_timesliced_texts(y, pad, "", txtlist, 3)

    def ux_show_lost(self, y, pad):
        str = "LOST: %d" % self.session_lost_list[0]
        if len(self.session_lost_list) > 1:
            str += " ..."
        self.hwio.imagedraw.text((pad, pad+y), str.rstrip(), font=self.font, fill=255)

    def ftp_start(self):
        if self.ftp_server is None:
            return
        self.ftp_thread = threading.Thread(target=self.ftp_worker, daemon=True)
        self.ftp_thread.start()

    def ftp_worker(self):
        try:
            self.ftp_server.serve_forever()
        except Exception as ex1:
            logger.error("FTP thread exception: " + str(ex1))
            self.ftp_thread = None
            if os.name == "nt":
                raise ex1

    def http_start(self):
        if self.http_server is None:
            return
        self.http_thread = threading.Thread(target=self.http_worker, daemon=True)
        self.http_thread.start()

    def http_worker(self):
        try:
            self.http_server.serve_forever()
        except Exception as ex1:
            logger.error("HTTP thread exception: " + str(ex1))
            self.http_thread = None
            if os.name == "nt":
                raise ex1

def disk_sort_func(x):
    global bucket_app
    total, free = get_disk_stats(x)
    if bucket_app is None or bucket_app.cfg_disk_prefer_total_vs_free():
        return total
    else:
        return free

def disk_unmount(path):
    if os.name == 'nt':
        return
    os.system("umount " + find_mount_point(path))

def disk_unmount_start(path):
    if os.name == 'nt':
        return
    command = "umount " + find_mount_point(path)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process

def disks_unmount(disks):
    for i in disks:
        disk_unmount_start(i)

def main():
    pyftpdlib.log.config_logging()
    hwio = bucketio.BucketIO_Simulator() if os.name == "nt" else bucketio.BucketIO()
    app = BucketApp(hwio = hwio)
    bucketftp.start_ftp_server(app)
    while True:
        app.ux_frame()
    return 0

if __name__ == "__main__":
    main()
