#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math, glob
import threading, queue, socket
import psutil

from PIL import Image, ImageDraw, ImageFont, ExifTags
import pyftpdlib.log

bucket_app = None

CONFIG_FILE_NAME    = "bucket_cfg.json"
LOW_SPACE_THRESH_MB = 200
LOW_BATT_THRESH     = 10

ALARMFLAG_DISKFULL   = 1
ALARMFLAG_LOSTFILE   = 2
ALARMFLAG_BATTLOW    = 4
ALARMFLAG_COPYERR    = 8

CLONING_OFF        = 0
CLONING_MAIN2REDUN = 1
CLONING_BOTH       = 2
CLONING_REDUN2MAIN = 3
CLONING_DONE       = -1

UX_LINESPACE = 0

UXSCREEN_MAIN = 0
UXSCREEN_MENU = 1

import bucketio, bucketmenu, bucketftp, bucketviewer, bucketcopy, bucketlogger, bucketutils

logger = bucketlogger.getLogger()

class BucketApp:
    def __init__(self, hwio = None):
        self.disks = []
        self.cfg = None
        self.ftp_server  = None
        self.ftp_thread  = None
        self.http_server = None
        self.http_thread = None
        self.hwio = hwio
        self.has_rtc = bucketio.has_rtc()
        self.has_date = None
        self.copier = bucketcopy.BucketCopier(self)
        self.last_file = None
        self.last_file_date = None
        self.start_monotonic_time = time.monotonic()
        self.session_last_act     = None
        self.session_last_nonact  = None
        self.session_last_5       = []
        self.alarm_reason = 0
        self.batt_lowest  = 100
        self.cpu_freq_high = False
        self.reset_stats()

        self.font = ImageFont.truetype("04b03mod.ttf", size = 8)
        self.font_has_lower = True
        self.last_frame_time = 0
        self.ux_frame_cnt    = 0
        self.ux_screen       = UXSCREEN_MAIN
        self.ux_menu         = bucketmenu.BucketMenu(self)

        self.thumbnail_queue = queue.Queue()
        self.ftmgr = bucketviewer.BucketWebFeatureManager()

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

    def reset_lost(self):
        self.session_lost_cnt     = 0
        self.session_lost_list    = []

    def reset_alarm(self):
        self.alarm_reason = 0
        self.hwio.buzzer_off()

    def update_disk_list(self):
        partitions = bucketutils.get_mounted_disks()
        if len(self.disks) <= 0:
            # on boot, go from no disks to having many disks
            # use the biggest disk as primary write target
            partitions.sort(reverse = True, key = disk_sort_func)
            for i in partitions:
                self.disks.append(i)
            bucketlogger.reconfig(bapp = self)
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
                bucketlogger.reconfig(bapp = self)

    def get_root(self):
        self.update_disk_list()
        if len(self.disks) > 0:
            return self.disks[0]
        return None

    def still_has_space(self):
        if len(self.disks) <= 0:
            return False
        total, free = bucketutils.get_disk_stats(self.disks[0])
        if free < LOW_SPACE_THRESH_MB:
            # another disk available?
            while i < len(self.disks):
                total2, free2 = bucketutils.get_disk_stats(self.disks[i])
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

    def cpu_highfreq(self):
        if self.cpu_freq_high == True:
            return
        self.cpu_freq_high = True
        self.hwio.cpu_highfreq()

    def cpu_lowfreq(self):
        if self.cpu_freq_high == False:
            return
        self.cpu_freq_high = False
        self.hwio.cpu_lowfreq()

    def cpu_choosefreq(self):
        busy = False
        if self.session_last_act is not None and (time.monotonic() - self.session_last_act) < 2:
            busy = True
        elif self.copier.is_busy() or self.ftmgr.thumbgen_is_busy():
            busy = True
        if busy == False:
            self.cpu_lowfreq()
        # all the other threads will automatically call cpu_highfreq
        # elif busy == True:
        #     self.cpu_highfreq()

    def load_cfg(self):
        import json

        # find all disks that may contain a config file
        disks = bucketutils.get_mounted_disks()
        disks.sort(reverse = True, key = disk_sort_func)
        if len(disks) <= 0:
            print("no disks to load config from")
            return

        # look on all disks for the config file
        for d in disks:
            path = os.path.join(d, CONFIG_FILE_NAME)
            if os.path.isfile(path):
                try:
                    print("trying to load json config from " + path)
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
                        bn = os.path.basename(dir.rstrip(os.path.sep))
                        if len(bn) > 6:
                            hdr = bn[-5:].strip()
                            if hdr.isnumeric():
                                self.last_file_date = datetime.datetime.strptime("202" + hdr, "%Y%m%d")
                                bucketlogger.reconfig(bapp = self)
                                print("date has been set, by config reader, to " + self.get_date_str())
                                break

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
        #return self.cfg_get_genericstring("ftp_password", "12345")
        return bucketutils.get_wifi_password()

    def cfg_get_ftpport(self):
        return self.cfg_get_genericint("ftp_port", 2133)

    def cfg_get_httpport(self):
        return self.cfg_get_genericint("http_port", 8000)

    def cfg_get_bucketname(self):
        return self.cfg_get_genericstring("bucket_name", "photos")

    def on_before_open(self, filepath):
        isimg, filename, filedatecode, filenumber, fileext = bucketutils.path_is_image_file(filepath)
        israw = bucketutils.ext_is_raw(fileext)
        if isimg and israw:
            if "_" in filenumber:
                filenumber = filenumber[0:filenumber.index('_')]
            fnum = int(filenumber)
            if self.session_first_number is None:
                self.session_first_number = fnum

        if self.has_rtc == False:
            if os.path.sep in filepath:
                pathparts = filepath.split(os.path.sep)
                if len(pathparts) >= 2:
                    dir = pathparts[-2]
                    if dir.isnumeric() and len(dir) == 8:
                        t =  datetime.datetime.strptime("202" + dir[3:], "%Y%m%d")
                        if self.last_file_date is None or t > self.last_file_date:
                            self.last_file_date = t

        # we are busying copying a file, so stop the low priority thumbnail generation thread
        self.pause_thumbnail_generation()

    def on_file_received(self, file):
        self.last_file = file
        self.try_get_time()

        isimg, filename, filedatecode, filenumber, fileext = bucketutils.path_is_image_file(file)
        israw = bucketutils.ext_is_raw(fileext) # ideally we only count statistics for raw files, otherwise this code can lose a raw file and not notify the user when the corresponding jpg file exists
        donotcount = False
        if len(filenumber) <= 0:
            fnum = None
        elif "_" in filenumber:
            donotcount = True
            fnum = int(filenumber.replace("_", ""))
        else:
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
            if self.session_last_number is not None and fnum is not None:
                # check if we skipped any file numbers, accounting for roll-over
                if fnum >= self.session_last_number:
                    diff = fnum - self.session_last_number
                else:
                    if fnum == 0 or fnum == 1:
                        diff = 1
                    elif fnum == (self.session_last_number - 1) or  fnum == (self.session_last_number - 2) or  fnum == (self.session_last_number - 3):
                        diff = 0
                    else:
                        fnum2 = fnum + 10000
                        diff = (fnum2 - self.session_last_number) % 10000

                if diff > 0: # difference = 1 is good, it means we counted up 1
                    diff -= 1
                if diff >= 5000:
                    donotcount = True
                if donotcount == False:
                    if diff > 0: # if we do lose a file, raise the alarm
                        j = 0
                        i = 0
                        while i < diff:
                            lnum = fnum - i - 1
                            if lnum <= 0:
                                lnum += 10000
                            if lnum not in self.session_last_5 and lnum not in self.session_lost_list:
                                self.session_lost_list.append(lnum)
                                self.session_lost_cnt += 1
                                j += 1
                            i += 1
                        if j > 0:
                            self.on_lost_file()

            if fnum is not None:
                lnum = fnum if fnum < 10000 else (fnum // 10)

                if len(self.session_last_5) >= 5:
                    self.session_last_5 = self.session_last_5[1:]
                self.session_last_number = lnum # update this after the delta check
                if lnum not in self.session_last_5:
                    self.session_last_5.append(lnum)

                if len(self.session_lost_list) > 0:
                    while fnum in self.session_lost_list:
                        self.session_lost_list.remove(fnum)
                        if len(self.session_lost_list) == 0:
                            self.session_lost_cnt = 0

        # the way the FTP code works is that when this callback is called, the file has not been renamed yet
        if isimg:
            npath = bucketutils.rename_camera_file_path(file, self.cfg_get_bucketname(), self.disks[0])
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
                        cmd = npath + ";" + os.path.join(destdisk, npath[len(origdisk):].strip(os.path.sep))
                        self.copier.enqueue_copy(cmd)

    def on_missed_file(self, file, forced = False):
        isimg, filename, filedatecode, filenumber, fileext = bucketutils.path_is_image_file(file)
        if isimg and filenumber.isnumeric():
            fnum = int(filenumber)
            if fnum not in self.session_lost_list and fnum not in self.session_last_5:
                self.session_lost_list.append(fnum)
                self.session_lost_cnt += 1
                self.on_lost_file(forced = forced)

    def on_disk_full(self):
        self.alarm_reason |= ALARMFLAG_DISKFULL

    def on_lost_file(self, forced = False):
        if forced:
            self.alarm_reason |= ALARMFLAG_LOSTFILE

    def on_copy_error(self):
        self.alarm_reason |= ALARMFLAG_COPYERR

    def try_get_time(self):
        # we don't have a valid time because we have no RTC, but we might've gotten a file from the camera
        # if it is a image file with EXIF data, we can extract the current date from the image
        if self.has_rtc == False:
            if self.last_file is not None and self.has_date is None:
                flower = self.last_file.lower()
                if flower.endswith(".jpg") or flower.endswith(".jpeg") or flower.endswith(".arw"):
                    self.has_date = bucketutils.get_img_exif_date(self.last_file)
                    if self.has_date is None: # prevent infinite loop from error
                        self.last_file = None
                    else:
                        bucketlogger.reconfig(bapp = self)
                        print("date has been set, by EXIF, to " + self.get_date_str())

    def generate_next_thumbnail(self):
        if self.thumbnail_queue.empty() == False:
            try:
                thumbme = self.thumbnail_queue.get()
                #self.ftmgr.generate_thumbnail(thumbme)
                self.ftmgr.enqueue_thumb_generation(thumbme, important=False)
            except Exception as ex:
                logger.error("Error generating thumbnail for \"" + thumbme + "\": " + str(ex))

    def pause_thumbnail_generation(self):
        thumb_later = self.ftmgr.thumbgen_clear()
        for tl in thumb_later:
            self.thumbnail_queue.put(tl)

    def session_is_busy(self, t=2):
        return self.session_last_act is not None and (time.monotonic() - self.session_last_act) < t

    def ux_frame(self, demo = False):
        tnow = time.monotonic()
        # run this every 1/5th of a second
        if (tnow - self.last_frame_time) < 0.2:
            time.sleep(0.02) # otherwise yield to another thread
            return
        self.last_frame_time = tnow
        self.ux_frame_cnt += 1

        if self.alarm_reason != 0:
            x = self.ux_frame_cnt % 5
            if x == 0 or x == 2:
                self.hwio.buzzer_on()
            else:
                self.hwio.buzzer_off()

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

        if self.ux_screen == UXSCREEN_MAIN or demo:
            if self.alarm_reason == 0:
                self.ux_show_clock(y, pad)
                y += font_height + UX_LINESPACE
                self.ux_show_batt(y, pad, (self.ux_frame_cnt % 20) < 10, demo = demo)
                y += font_height + UX_LINESPACE
            else:
                tmod = (self.ux_frame_cnt % 30)
                if tmod < 10:
                    self.ux_show_clock(y, pad)
                else:
                    self.ux_show_batt(y, pad, (tmod < 20), demo = demo)
                y += font_height + UX_LINESPACE
                self.ux_show_warnings(y, pad)
                y += font_height + UX_LINESPACE

            if demo and self.alarm_reason == 0:
                self.ux_show_warnings(y, pad, demo = True)
                y += font_height + UX_LINESPACE

            self.ux_show_wifi(y, pad)
            y += font_height + UX_LINESPACE

            self.ux_show_session(y, pad, demo = demo)
            y += font_height + UX_LINESPACE

            self.ux_show_disks(y, pad, demo = demo)
            y += font_height + UX_LINESPACE

            if len(self.session_lost_list) > 0 and not demo:
                self.ux_show_lost(y, pad)
                y += font_height + UX_LINESPACE

            if self.copier.state != bucketcopy.COPIERSTATE_IDLE and self.copier.state != bucketcopy.COPIERSTATE_CANCELED and self.copier.state != bucketcopy.COPIERSTATE_RESTART:
                self.ux_show_copystatus(y, pad)
                y += font_height + UX_LINESPACE
            elif demo:
                self.ux_show_copystatus(y, pad, demo = True)
                y += font_height + UX_LINESPACE
            else:
                if self.copier.is_busy() == False and self.session_is_busy(t=10) == False:
                    self.generate_next_thumbnail()

            if self.alarm_reason == 0 or True:
                self.ux_menu.draw_bottom_texts(left = "MENU")
            #else:
            #    self.ux_menu.draw_bottom_texts(left = "MENU", right = "CLR WARN")

            if self.hwio.pop_button() == 1:
                self.ux_screen = UXSCREEN_MENU
                self.ux_menu.reset_state()

            #if self.alarm_reason == 0 and self.hwio.pop_button() == 3:
            #    self.reset_alarm()

        elif self.ux_screen == UXSCREEN_MENU:
            self.ux_menu.run()

        self.hwio.oled_show()

    def ux_show_timesliced_texts(self, y, pad, prefix, textlist, period):
        tmod = self.ux_frame_cnt % (period * (len(textlist)))
        i = 0
        while i < len(textlist):
            if tmod < (period * (i + 1)):
                txt = textlist[i]
                if txt.startswith("PERCENTBAR "):
                    p = float(txt[txt.index(' ') + 1:].strip())
                    (font_width, font_height) = self.font.getsize(prefix)
                    font_width = (6 * 6) if font_width < 10 else font_width
                    x = pad + font_width + 1
                    w = bucketio.OLED_WIDTH - x - 1
                    wb = w * p / 100
                    self.hwio.imagedraw.rectangle((x, y + 2, bucketio.OLED_WIDTH - 1, y + font_height - 1), fill=None, outline=255)
                    self.hwio.imagedraw.rectangle((x, y + 2, x + wb, y + font_height - 1), fill=255, outline=255)
                    self.hwio.imagedraw.text((pad, pad+y), prefix, font=self.font, fill=255)
                else:
                    self.hwio.imagedraw.text((pad, pad+y), prefix + txt, font=self.font, fill=255)
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

    def ux_show_batt(self, y, pad, use_volts, demo = False):
        batt_raws, batt_volts, batt_chgs = self.hwio.batt_read()
        higher_batt = max(batt_chgs)
        if higher_batt < self.batt_lowest:
            if self.batt_lowest >= LOW_BATT_THRESH and higher_batt < LOW_BATT_THRESH:
                suppress = False
                # do not buzz if powered by USB
                if self.ux_frame_cnt < 10:
                    all_zero = True
                    for i in batt_volts:
                        if i > 0:
                            all_zero = False
                    if all_zero:
                        suppress = True
                if not suppress:
                    self.alarm_reason |= ALARMFLAG_BATTLOW
            self.batt_lowest = higher_batt
        s = "BATT: "
        if not demo:
            if use_volts:
                for i in batt_volts:
                    s += "%.2fV  " % i
            else:
                for i in batt_chgs:
                    s += "%4d%%  " % round(i)
        else:
            s += "6.32V   3%"
        self.hwio.imagedraw.text((pad, pad+y), s.rstrip(), font=self.font, fill=255)

    def ux_show_warnings(self, y, pad, demo = False):
        hdr = "WARN: "
        str2 = ""
        reason = self.alarm_reason if not demo else (ALARMFLAG_BATTLOW)
        if (reason & ALARMFLAG_DISKFULL) != 0:
            str2 += "FULL "
        if (reason & ALARMFLAG_BATTLOW) != 0:
            str2 += "BATT "
        if (reason & ALARMFLAG_LOSTFILE) != 0:
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

        ipstr = bucketutils.get_wifi_ip()
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

        ssid = bucketutils.get_wifi_ssid()
        if ssid is not None and len(ssid) > 0:
            (font_width, font_height) = self.font.getsize(hdr + ssid)
            if font_width < (bucketio.OLED_WIDTH - (pad * 2)):
                txtlist.append(ssid)
            else:
                while font_width > (bucketio.OLED_WIDTH - (pad * 2)):
                    ssid = ssid[0:-1]
                    (font_width, font_height) = self.font.getsize(hdr + ssid + "...")
                txtlist.append(ssid)

        if self.session_is_busy():
            if self.font_has_lower:
                i = math.floor(self.ux_frame_cnt / 2) % (len(hdr) - 2)
                c = hdr[i].lower()
                nstr = hdr[0:i] + c + hdr[i+1:]
                hdr = nstr
        elif self.session_last_act is not None:
            tsec = time.monotonic() - self.session_last_act
            if tsec <= 120:
                txtlist.append("-%dsec " % (round(tsec)))
            else:
                tmin = tsec / 60
                txtlist.append("-%.1fmin " % (tmin))

        clients = bucketutils.get_wifi_clients()
        clicnt = len(clients)
        #if clicnt == 0 and self.session_is_busy():
        #    clicnt = 1
        txtlist.append("%d clients" % clicnt)

        self.ux_show_timesliced_texts(y, pad, hdr, txtlist, 7)

    def ux_show_session(self, y, pad, demo = False):
        if self.session_first_number is None and not demo:
            self.hwio.imagedraw.text((pad, pad+y), "NEW SESSION", font=self.font, fill=255)
            return

        if demo:
            s = "Sess: 7235~7341"
            self.hwio.imagedraw.text((pad, pad+y), s, font=self.font, fill=255)
            return

        tmod = (self.ux_frame_cnt % (5 * 3))
        if tmod < (5 * 1):
            s = "SESS: %d~" % (self.session_first_number)
            if self.session_last_number is not None:
                s += "%d" % self.session_last_number
            else:
                s += "?"
        elif tmod < (5 * 2):
            s = "TOT: %d" % (self.session_total_cnt)
        elif tmod < (5 * 3):
            s = "LOST: %d" % (self.session_lost_cnt)
        if self.session_last_act is not None and (time.monotonic() - self.session_last_act) < 2:
            if self.font_has_lower:
                # animated busy indication by making the letters dance
                r = random.randint(0, 3)
                c = s[r]
                s = s[0:r] + c.lower() + s[r + 1:]
            else:
                # animated busy symbol
                tmod = self.ux_frame_cnt % (2 * 5)
                if tmod < (2 * 1):
                    s = ">" + s
                elif tmod < (2 * 2):
                    s = "=" + s
                elif tmod < (2 * 3):
                    s = "-" + s
                elif tmod < (2 * 4):
                    s = "=" + s
        (font_width, font_height) = self.font.getsize(s)
        if font_width > (bucketio.OLED_WIDTH - (pad * 2)):
            s = s.replace(" ", "")
        self.hwio.imagedraw.text((pad, pad+y), s.rstrip(), font=self.font, fill=255)

    def ux_show_disks(self, y, pad, demo = False):
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
                if disk_cnt > 1 or demo:
                    idc = "[M]: "
                else:
                    idc = ": "
            else:
                idc = "[%d]: " % disk_idx
            str1 = "DISK" + idc
            str2 = "FREE@@" + idc
            total, free = bucketutils.get_disk_stats(disk)
            is_copying = disk_idx > 0 and self.copier.is_busy() # indicate cloning is active
            disklabel = bucketutils.get_disk_label(disk)
            (font_width, font_height) = self.font.getsize(str1 + (">" if is_copying else "") + disklabel)
            if font_width > (bucketio.OLED_WIDTH - (pad * 2)) and os.path.sep in disklabel:
                # shorten the disk name to fit screen
                disklabel = disklabel.strip(os.path.sep)
                disklabel = disklabel[disklabel.index(os.path.sep):]
            txtlist.append(str1 + (">" if is_copying else "") + disklabel)
            txtlist.append(txtlist[-1])
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
        self.ux_show_timesliced_texts(y, pad, "", txtlist, 3 if not demo else 6)

    def ux_show_lost(self, y, pad):
        s = "LOST: %d" % self.session_lost_list[0]
        if len(self.session_lost_list) > 1:
            s += " ..."
        self.hwio.imagedraw.text((pad, pad+y), s.rstrip(), font=self.font, fill=255)

    def ux_show_copystatus(self, y, pad, demo = False):
        state, is_busy, percentage, sizestr, timestr = self.copier.get_status()
        if state == bucketcopy.COPIERSTATE_COPY or demo:
            txtlist = []
            if demo:
                percentage = 25.3  if (percentage < 20 or percentage > 80) else percentage
                sizestr = "756 MB" if state != bucketcopy.COPIERSTATE_COPY else sizestr
                timestr = "756 MB" if state != bucketcopy.COPIERSTATE_COPY else sizestr
            txtlist.append("PERCENTBAR %.1f" % percentage)
            txtlist.append("%.1f%%" % percentage)
            txtlist.append(sizestr)
            txtlist.append(timestr)
            fcnt = self.copier.total_files - self.copier.done_files
            fcnt = 654 if (demo and fcnt <= 0) else fcnt
            txtlist.append("%d F" % fcnt)
            self.ux_show_timesliced_texts(y, pad, "COPY: ", txtlist, 10)
        elif state == bucketcopy.COPIERSTATE_CALC or state == bucketcopy.COPIERSTATE_RESTART:
            self.hwio.imagedraw.text((pad, pad+y), "COPY STARTING", font=self.font, fill=255)
        elif state == bucketcopy.COPIERSTATE_DONE:
            txtlist = []
            txtlist.append("DONE")
            txtlist.append("%d F" % self.copier.total_files)
            self.ux_show_timesliced_texts(y, pad, "COPY: ", txtlist, 10)
        elif state == bucketcopy.COPIERSTATE_FULL:
            txtlist = []
            txtlist.append("DISK FULL")
            txtlist.append("%.1f%%" % percentage)
            txtlist.append("%d / %d" % (self.copier.done_files, self.copier.total_files))
            self.ux_show_timesliced_texts(y, pad, "COPY: ", txtlist, 10)
        elif state == bucketcopy.COPIERSTATE_ERROR:
            txtlist = []
            txtlist.append("ERROR")
            txtlist.append("%.1f%%" % percentage)
            txtlist.append("%d / %d" % (self.copier.done_files, self.copier.total_files))
            self.ux_show_timesliced_texts(y, pad, "COPY: ", txtlist, 10)
        

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
    total, free = bucketutils.get_disk_stats(x)
    if bucket_app is None or bucket_app.cfg_disk_prefer_total_vs_free():
        return total
    else:
        return free

def disk_unmount(path):
    if os.name == 'nt':
        return
    os.system("umount " + bucketutils.find_mount_point(path))

def disk_unmount_start(path):
    if os.name == 'nt':
        return
    command = "umount " + bucketutils.find_mount_point(path)
    bucketutils.run_cmdline_read(command)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process

def disks_unmount(disks):
    for i in disks:
        disk_unmount_start(i)

def main():
    global bucket_app
    pyftpdlib.log.config_logging()
    hwio = bucketio.BucketIO_Simulator() if os.name == "nt" else bucketio.BucketIO()
    hwio.hw_init()
    app = BucketApp(hwio = hwio)
    if bucket_app is None:
        bucket_app = app
    bucketutils.set_running_app(app)
    bucketviewer.set_running_app(app)
    app.load_cfg()
    bucketftp.start_ftp_server(app)
    app.http_server = bucketviewer.get_server(app.cfg_get_httpport())
    app.http_start()
    while True:
        app.ux_frame()
    return 0

if __name__ == "__main__":
    main()
