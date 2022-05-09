#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random
import threading, queue

from bucketio import *

from PIL import Image, ExifTags

bucket_app = None

CONFIG_FILE_NAME    = "bucket_cfg.json"
LAST_TIME_FILE_NAME = "lasttime.txt"
LOW_SPACE_THRESH_MB = 200

class BucketApp:

    def __init__(self):
        global bucket_app
        bucket_app = self
        self.disks = []
        self.cfg = None
        self.server = None
        self.hwio = None
        self.copy_queue = queue.Queue()
        self.copy_filesize   = 0
        self.copy_fileremain = 0
        self.copy_thread = threading.Thread(target=self.copy_worker, daemon=True)
        self.copy_thread.start()
        self.cloning_enaged = False
        self.last_file = None
        self.last_file_date = None
        self.has_rtc = bucketio.has_rtc()
        self.start_monotonic_time = time.monotonic()
        self.session_first_number = None
        self.session_last_number  = None
        self.session_total_cnt    = 0
        self.session_lost_cnt     = 0
        self.session_lost_list    = []
        self.session_last_act     = None
        self.session_last_nonact  = None
        self.alarm_latch = False

    def reset_stats(self):
        self.session_first_number = None
        self.session_last_number  = None
        self.session_total_cnt    = 0
        self.session_lost_cnt     = 0
        self.session_lost_list    = []

    def update_disk_list(self):
        partitions = get_mounted_disks()
        if len(self.disks) <= 0:
            # on boot, go from no disks to having many disks
            # use the biggest disk as primary write target
            partitions.sort(reverse = True, key = disk_sort_func)
            for i in partitions:
                self.disks.append(i)
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
            for i in partitions:
                found = False
                for j in newlist:
                    if i == j:
                        found = True
                        break
                if not found:
                    newlist.append(i)
            self.disks = newlist

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

    def get_clock_str(self):
        return "CLK: 20" + self.get_date_str() + " +" + str(round(self.get_elapsed_secs())) + "s"

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

        if self.has_rtc == False:
            # we have no RTC so look for a last-time file on the any of the disks
            for d in disks:
                tstr = ""
                try:
                    path = os.path.join(d, LAST_TIME_FILE_NAME)
                    if os.path.isfile(path):
                        with open(path, "r") as timefile:
                            tstr = timefile.readline()
                            self.last_file_date = datetime.datetime.strptime(tstr, "%Y:%m:%d %H:%M:%S")
                        break
                except Exception as ex:
                    print("Failed to load last-time file at \"" + path + "\", exception: " + str(ex))

        # look on all disks for the config file
        for d in disks:
            path = os.path.join(d, CONFIG_FILE_NAME)
            if os.path.isfile(path):
                try:
                    with open(path, 'r') as f:
                        self.cfg = json.load(f) # loads a file as a dictionary
                    return
                except Exception as ex:
                    print("Failed to load JSON cfg file at \"" + path + "\", exception: " + str(ex))

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
        return self.cfg_get_genericstring("ftp_password", "123")

    def cfg_get_ftpport(self):
        return self.cfg_get_genericint("ftp_port", 2121)

    def on_file_received(self, file):
        self.last_file = file

        # if we have no RTC but we do have a date from the camera, then we write it to a file on the USB drive
        # this way, we can reboot the Pi and still have a sort-of-valid date without the camera
        if self.has_rtc == False and self.has_date is not None:
            try:
                mntpt = find_mount_point(file)
                timefilename = os.path.join(mntpt, LAST_TIME_FILE_NAME)
                with open(timefilename, "w") as timefile:
                    timefile.write(self.has_date.strftime("%Y:%m:%d %H:%M:%S"))
            except Exception as ex:
                print("Error writing last-time file, exception: " + str(ex))

        isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
        rawexts = self.cfg_get_extensions(key="raw_extensions", defval=["arw"]) # if raw file is not enabled on camera, then the cfg file should change this to jpg
        israw = False # ideally we only count statistics for raw files, otherwise this code can lose a raw file and not notify the user when the corresponding jpg file exists
        for re in rawexts:
            if re.lower() == fileext.lower():
                israw = True
                break
        if isimg and israw:
            # update the session statistics
            self.session_total_cnt += 1
            fnum = int(filenumber)
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
                    self.alarm_latch = True
                    i = 0
                    while i < diff:
                        lnum = fnum - i - 1
                        if lnum <= 0:
                            lnum += 10000
                        self.session_lost_list.append(lnum)
            self.session_last_number = fnum # update this after the delta check

        self.update_disk_list()
        if len(self.disks) <= 1:
            return # no other disk to copy to, give up

        for origdisk in self.disks:
            for destdisk in self.disks:
                if origdisk == destdisk:
                    continue # don't copy to the same disk as the origin
                if file.startswith(origdisk):
                    # enqueue the task
                    self.copy_queue.put(file + ";" + os.path.join(destdisk, file[len(origdisk) + 1:]))

    def clone_another(self):
        # a few checks to see if the system is busy, prioritize FTP transfers
        if self.cloning_enaged == False or len(self.disks) <= 1 or self.copy_queue.empty() == False:
            return
        tnow = time.monotonic()
        if self.session_last_act is not None:
            if (tnow - self.session_last_act) < 5:
                return
        if self.session_last_nonact is not None:
            if (tnow - self.session_last_nonact) < 5:
                return

        self.update_disk_list()

        for origdisk in self.disks:
            filelist = glob.glob(origdisk + os.path.sep + "**" + os.path.sep + "*", recursive=True)
            random.shuffle(filelist)
            for origfile in filelist:
                filetail = origfile[len(origdisk) + 1:]
                for destdisk in self.disks:
                    if origdisk == destdisk:
                        continue # don't copy to the same disk as the origin
                    destfile = os.path.join(destdisk, filetail)
                    if os.path.isfile(destfile) == False:
                        self.copy_queue.put(origfile + ";" + destfile) # enqueue the task
                        return # only do one
        time.sleep(2) # nothing to do!

    def copy_worker(self):
        try:
            while True:
                try:
                    # we don't have a valid time because we have no RTC, but we might've gotten a file from the camera
                    # if it is a image file with EXIF data, we can extract the current date from the image
                    if self.has_rtc == False:
                        if self.last_file is not None and self.has_date is None:
                            flower = self.last_file.lower()
                            if flower.endswith(".jpg") or flower.endswith(".jpeg"):
                                self.has_date = get_img_exif_date(self.last_file)
                                continue

                    # sleep if there's nothing to do
                    if self.copy_queue.empty():
                        self.copy_filesize   = 0
                        self.copy_fileremain = 0
                        time.sleep(2)
                        if self.cloning_enaged:
                            self.clone_another()
                        continue

                    itm = self.copy_queue.get()
                    itms = itm.split(';')
                    if os.path.isfile(itms[0]) == False:
                        continue # file is missing, weird, can't do anything so give up
                    sz = os.path.getsize(itms[0])
                    if free < sz + LOW_SPACE_THRESH_MB:
                        continue # no space for copying
                    self.copy_filesize   = sz
                    self.copy_fileremain = sz
                    total, free = get_disk_stats(itms[1])
                    with open(itms[0], "rb") as fin:
                        os.makedirs(os.path.dirname(itms[1]), exist_ok=True)
                        with open(itms[1], "wb") as fout:
                            # copy from input to output in chunks, so the GUI may show updates
                            while self.copy_fileremain > 0:
                                rlen = min(1024 * 10, self.copy_fileremain)
                                bytes = fin.read(rlen)
                                if not bytes or len(bytes) <= 0:
                                    self.copy_fileremain = 0
                                    break
                                fout.write(bytes)
                                self.copy_fileremain -= rlen
                                if len(bytes) < rlen:
                                    self.copy_fileremain = 0
                                    break
                                time.sleep(0) # yield thread
                    self.copy_filesize   = 0
                    self.copy_fileremain = 0
                except Exception as ex2:
                    print("Copy thread inner exception: " + str(ex2))
                    time.sleep(0.1)
        except Exception as ex1:
            print("Copy thread outer exception: " + str(ex1))
            self.copy_thread = None
            pass

    def ux_frame(self):
        tnow = time.monotonic()
        if (tnow - self.last_frame_time) < 0.2:
            time.sleep(0.02)
            return
        self.last_frame_time = tnow
        self.ux_frame_cnt += 1

def get_img_exif_date(file):
    if file is None:
        return None
    flower = file.lower()
    if flower.endswith(".jpg") == False and flower.endswith(".jpeg") == False:
        return None
    tval = ""
    try:
        img = Image.open(file)
        img_exif = img.getexif()
        for key, val in img_exif.items():
            if key in ExifTags.TAGS:
                if ExifTags.TAGS[key] == "DateTime" or ExifTags.TAGS[key] == "DateTimeOriginal":
                    tval = val
                    return datetime.datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
    except Exception as ex:
        estr = "Unable to parse EXIF date from file \"" + file + "\", "
        if len(tval) > 0:
            estr += " tag val: \"" + tval + "\", "
        print(estr + "exception: " + str(ex))
    return None

def get_mounted_disks():
    list = []
    partitions = psutil.disk_partitions()
    for p in partitions:
        if ((p.mountpoint.startswith("/mnt/") and len(p.mountpoint) > 5) or (p.mountpoint.startswith("/mount/") and len(p.mountpoint) > 7) or (p.mountpoint.startswith("/media/") and len(p.mountpoint) > 7)) and ("fat" in p.fstype):
            list.append(p.mountpoint)
    return p

def get_disk_stats(self, path):
    total = 0
    free = 0
    try:
        statvfs = os.statvfs(path)
        free = statvfs.f_frsize * statvfs.f_bfree
        total = statvfs.f_frsize * statvfs.f_blocks
    except:
        pass
    try:
        total, used, free = shutil.disk_usage(__file__)
    except:
        pass
    return total / 1024 / 1024, free / 1024 / 1024 # return in megabytes

def disk_sort_func(x):
    global bucket_app
    total, free = get_disk_stats(x)
    if bucket_app.cfg_disk_prefer_total_vs_free():
        return total
    else:
        return free

def disk_unmount(path):
    os.system("umount " + path)

def disk_unmount_start(path):
    command = "umount " + path
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process

def find_mount_point(path):
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path

def main():
    return 0

if __name__ == "__main__":
    main()
