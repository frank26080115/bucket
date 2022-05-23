#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math, glob, queue
import threading, queue, socket

import bucketapp, bucketutils, bucketlogger

logger = bucketlogger.getLogger()

COPYLIST_FILENAME = "copylist.txt"

COPIERMODE_NONE = 0
COPIERMODE_MAIN2REDUN = 1
COPIERMODE_REDUN2MAIN = 2
COPIERMODE_BOTH       = 3

COPIERSTATE_IDLE  = 0
COPIERSTATE_CALC  = 1
COPIERSTATE_COPY  = 2
COPIERSTATE_DONE  = 3
COPIERSTATE_FULL  = 4
COPIERSTATE_ERROR = -1
COPIERSTATE_RESTART = -2
COPIERSTATE_CANCELED = -3

restricted_names = [
    os.path.sep + COPYLIST_FILENAME,
    os.path.sep + bucketapp.CONFIG_FILE_NAME,
    ".washere",
    ".lock",
    "SONYCARD.IND",
]

restricted_dirs = [
    "delete",
    "AVF_INFO",
    "System Volume Information",
]

class BucketCopier:
    def __init__(self, app):
        self.app = app
        self.priority_queue = queue.Queue()
        self.reset_all()

    def start(self, mode):
        self.total_files  = 0
        self.total_size   = 0
        self.done_files   = 0
        self.done_size    = 0
        self.file_remain  = 0
        self.file_totsize = 0
        self.activity_time = None
        self.start_time    = None
        self.speed_calc    = None
        self.error = ""
        self.mode = mode
        if self.state != COPIERSTATE_IDLE and self.mode != COPIERMODE_NONE:
            self.state = COPIERSTATE_RESTART
        elif self.mode != COPIERMODE_NONE:
            self.state = COPIERSTATE_CALC
        if self.copy_thread is None and (self.mode != COPIERMODE_NONE or self.priority_queue.empty() == False):
            self.copy_thread = threading.Thread(target=self.copy_worker, daemon=True)
            self.copy_thread.start()

    def reset_all(self):
        self.state = COPIERSTATE_IDLE
        self.mode = COPIERMODE_NONE
        self.calc_thread = None
        self.copy_thread = None
        self.interrupted = False
        self.paused      = False
        self.activity_time = None
        self.speed_calc    = None
        self.start(COPIERMODE_NONE)

    def calculate(self):
        self.total_files  = 0
        self.total_size   = 0
        self.done_files   = 0
        self.done_size    = 0
        self.interrupted  = False
        self.paused       = False
        self.file_remain  = 0
        self.file_totsize = 0
        self.speed_calc   = None

        copylistfilenpath = os.path.join(self.app.disks[0], COPYLIST_FILENAME)
        # if this task list exists, remove it
        if os.path.isfile(copylistfilenpath):
            try:
                os.remove(copylistfilenpath)
            except Exception as ex:
                logger.error("Copy-thread error deleting task list \"" + copylistfilenpath + "\", exception: " + str(ex))
                pass

        for origdisk in self.app.disks:
            time.sleep(0) # thread yield

            if self.mode == COPIERMODE_REDUN2MAIN and origdisk == self.app.disks[0]:
                # main disk cannot be origin disk under redundant-to-main copying
                continue

            for origroot, origdirs, origfiles in os.walk(origdisk, topdown=True): # for all files
                for srcfile in origfiles: # for all files
                    # thread management
                    time.sleep(0)
                    while self.paused:
                        time.sleep(1)
                    if self.interrupted:
                        self.state = COPIERSTATE_CANCELED
                        return

                    if self.state == COPIERSTATE_RESTART:
                        # this should force the thread worker to loop around and restart the calculate function
                        self.state = COPIERSTATE_CALC
                        return

                    # some special files shouldn't be copied
                    acceptable = True
                    for unacceptable in restricted_names:
                        if srcfile.lower().endswith(unacceptable.lower()):
                            acceptable = False
                            break
                    for unacceptable in restricted_dirs:
                        if (os.path.sep + unacceptable + os.path.sep).lower() in origroot.lower() or origroot.lower().endswith(os.path.sep + unacceptable.lower()):
                            acceptable = False
                            break
                    if acceptable == False:
                        continue

                    for destdisk in self.app.disks:
                        # for all other disks (and obviously don't copy to itself)
                        if origdisk == destdisk:
                            continue

                        if self.mode == COPIERMODE_REDUN2MAIN and destdisk != self.app.disks[0]:
                            # main disk cannot be destination disk under redundant-to-main copying
                            continue

                        origfilepath = os.path.abspath(os.path.join(origroot, srcfile))
                        srcsize = os.path.getsize(origfilepath)
                        pathtail = origfilepath[len(origdisk):]
                        is_cam_file = False
                        # construct destination file path with new mount point
                        if bucketutils.is_camera_file(origfilepath) and bucketutils.is_disk_camera(origdisk):
                            # the file path looks like a camera file structure
                            dtstroverride = None
                            if self.app.has_date is None: # we haven't gotten a time from FTP transfer, but since this is a SD card, the timestamp can be used
                                dtstroverride = datetime.date.fromtimestamp(os.path.getmtime(origfilepath)).strftime("%y%m%d")
                            destfilepath = bucketutils.rename_camera_file_path(origfilepath, self.app.cfg_get_bucketname(), destdisk, dateoverride = dtstroverride)
                            is_cam_file = True
                        else:
                            # looks like a generic file, or the file has already been renamed
                            destfilepath = os.path.join(destdisk, pathtail)

                        if srcsize > 0 and (os.path.isfile(destfilepath) == False or (os.path.getsize(destfilepath) <= srcsize // 2 or (self.mode != COPIERMODE_BOTH and os.path.getsize(destfilepath) != srcsize))):
                            # passed overwrite rules
                            # enqueue the copy task into the task list
                            cmd = origfilepath + ";" + destfilepath
                            if is_cam_file:
                                cmd += ";cam"
                            with open(copylistfilenpath, "a") as copylistfile:
                                copylistfile.write("\n" + cmd)
                                self.total_files += 1
                                self.total_size  += srcsize

            if self.mode == COPIERMODE_MAIN2REDUN and origdisk == self.app.disks[0]:
                # main disk cannot be origin disk under main-to-redundant copying
                break

        if self.total_files > 0 and self.total_size > 0:
            # something to do!
            self.state = COPIERSTATE_COPY
            self.start_time = time.monotonic()
            if self.copy_thread is None:
                self.copy_thread = threading.Thread(target=self.copy_worker, daemon=True)
                self.copy_thread.start()
        else:
            # nothing to do
            self.state = COPIERSTATE_DONE

    def enqueue_copy(self, cmd):
        self.priority_queue.put(cmd)
        if self.copy_thread is None:
            self.copy_thread = threading.Thread(target=self.copy_worker, daemon=True)
            self.copy_thread.start()

    def copy_one_file(self, cmd, highprior = False):
        try:
            # parse the command
            line = cmd.strip()
            if ";" not in line:
                return
            cmdparts = line.split(';')
            if len(cmdparts) < 2:
                return
            if os.path.isfile(cmdparts[0]) == False or len(cmdparts[1]) <= 0:
                return
            src = cmdparts[0]
            dst = cmdparts[1]
            is_cam_file = True if len(cmdparts) > 2 and cmdparts[2] == "cam" else False

            os.makedirs(os.path.dirname(dst), exist_ok=True)
            fsize = os.path.getsize(src)
            set_date = os.path.getmtime(src)

            # check overwrite rules
            if os.path.isfile(dst):
                existing_date = os.path.getmtime(dst)
                if os.path.getsize(dst) == fsize and set_date is not None and set_date != 0 and set_date == existing_date:
                    if highprior == False:
                        self.done_files += 1
                        self.done_size += fsize
                    return

            # check for free space on disk and alert if required
            total, free = bucketutils.get_disk_stats(dst)
            if free < ((fsize / 1024 / 1024) + bucketapp.LOW_SPACE_THRESH_MB):
                self.state = COPIERSTATE_FULL
                self.app.on_disk_full()
                return

            self.app.pause_thumbnail_generation()
            self.file_totsize = fsize
            self.file_remain  = self.file_totsize
            self.activity_time = time.monotonic()
            self.app.cpu_highfreq()
            start_time = time.monotonic()
            try:
                with open(src, "rb") as fin:
                    with open(dst, "wb") as fout:
                        # copy from input to output in chunks, so the GUI may show updates
                        while self.file_remain > 0:
                            self.copy_last_act = time.monotonic()
                            rlen = min(1024 * 100, self.file_remain)
                            bytes = fin.read(rlen)
                            self.activity_time = time.monotonic()
                            if not bytes or len(bytes) <= 0:
                                self.file_remain = 0
                                break
                            fout.write(bytes)
                            self.file_remain -= rlen
                            if highprior == False:
                                self.done_size += rlen
                            if len(bytes) < rlen:
                                self.file_remain = 0
                                break
                            time.sleep(0) # yield thread

                # set the modified time for the file
                try:
                    os.utime(dst, (set_date, set_date))
                except Exception as ex:
                    logger.error("Copy file error while setting timestamp for file \"" + dst + "\", exception: " + str(ex))
                    pass

                if highprior == False: # immediate redundant copy vs background copy
                    self.done_files += 1
                    if is_cam_file:
                        self.app.thumbnail_queue.put(dst)

                # calculate transfer speed
                time_per_file = time.monotonic() - start_time
                if time_per_file > 0:
                    speed = self.file_totsize / time_per_file
                    # use a low-pass-filter on the speed figure
                    if self.speed_calc is None:
                        self.speed_calc = speed
                    else:
                        self.speed_calc = (speed * 0.25) + (self.speed_calc * 0.75)

            except Exception as ex:
                # check the mount point of the destination file
                if bucketutils.is_still_mounted(src) == False:
                    self.state = COPIERSTATE_ERROR
                    self.app.on_copy_error()
                elif bucketutils.is_still_mounted(dst) == False:
                    self.state = COPIERSTATE_ERROR
                    self.app.on_copy_error()
                raise ex

            self.file_totsize = 0
            self.file_remain = 0
        except Exception as ex2:
            logger.error("Copy file error, \"" + cmd + "\", exception: " + str(ex2))
            if os.name == "nt":
                raise ex2

    def copy_worker(self):
        try:
            while True:
                try:
                    time.sleep(1) # thread yield
                    self.app.try_get_time()
                    self.copy_dowork()
                except Exception as ex2:
                    logger.error("Copy thread inner exception: " + str(ex2))
                    if os.name == "nt":
                        raise ex2
        except Exception as ex1:
            logger.error("Copy thread outer exception: " + str(ex1))
            if os.name == "nt":
                raise ex1
        self.copy_thread = None

    def copy_dowork(self):
        is_highpriority = False
        if self.priority_queue.empty() == False:
            line = self.priority_queue.get()
            is_highpriority = True
            self.copy_one_file(line, highprior=is_highpriority)
            # we'll also check the priority_queue when the low priority task list is opened
            if self.interrupted:
                self.state = COPIERSTATE_CANCELED
                return
            while self.paused:
                time.sleep(1)
            return

        if self.state == COPIERSTATE_RESTART:
            self.state = COPIERSTATE_CALC

        if self.state == COPIERSTATE_CALC:
            self.calculate()
            # this will never exit with state still being CALC (unless another thread changes it)
            time.sleep(1)
            return

        if self.mode == COPIERMODE_NONE or self.state == COPIERSTATE_IDLE or self.state == COPIERSTATE_DONE:
            self.app.generate_next_thumbnail()
            time.sleep(1)
            return

        copylistfilenpath = os.path.join(self.app.disks[0], COPYLIST_FILENAME)
        if os.path.isfile(copylistfilenpath) == False:
            self.app.generate_next_thumbnail()
            time.sleep(1)
            return

        with open(copylistfilenpath, "r") as copylistfile:
            while True:
                try:
                    time.sleep(0) # thread yield
                    if self.state == COPIERSTATE_RESTART:
                        self.state = COPIERSTATE_CALC
                        return
                    is_highpriority = False
                    if self.priority_queue.empty() == False:
                        line = self.priority_queue.get()
                        is_highpriority = True
                    else:
                        line = copylistfile.readline()
                        if not line:
                            break
                        if self.interrupted:
                            self.state = COPIERSTATE_CANCELED
                            break
                        while self.paused:
                            time.sleep(1)
                    self.copy_one_file(line, highprior=is_highpriority)
                except Exception as ex1:
                    logger.error("Copy thread error while reading cmd list: " + str(ex1))
                    if os.name == "nt":
                        raise ex1

        # the list has been finished, we can resume thumbnail generation
        self.app.generate_next_thumbnail()

        if self.mode != COPIERMODE_NONE and self.state == COPIERSTATE_COPY:
            self.state = COPIERSTATE_DONE
            time.sleep(1)

    def get_status(self):
        if self.state == COPIERSTATE_IDLE or self.state == COPIERSTATE_CALC or self.state == COPIERSTATE_RESTART:
            return self.state, self.is_busy(), 0, "0MB", "00:00"
        elif self.state == COPIERSTATE_DONE:
            return self.state, self.is_busy(), 100, "0MB", "00:00"
        elif self.state == COPIERSTATE_COPY:
            percentage = round(self.done_size * 1000 / self.total_size) / 10
            timestr = "??:??"
            bremain = self.total_size - self.done_size
            sizestr = bucketutils.get_size_string(bremain)
            speed = None
            if self.speed_calc is not None:
                speed = self.speed_calc
                tremain = bremain / self.speed_calc
                timestr = bucketutils.get_time_string(round(tremain))
            elif self.start_time is not None:
                timeelapsed = time.monotonic() - self.start_time
                if timeelapsed > 0:
                    speed = self.done_size / timeelapsed
            if speed is not None and speed > 0:
                tremain = bremain / speed
                timestr = bucketutils.get_time_string(round(tremain))
            return self.state, self.is_busy(), percentage, sizestr, timestr
        else:
            return self.state, self.is_busy(), 0, "0MB", "00:00"

    def is_busy(self):
        if self.activity_time is not None:
            if (time.monotonic() - self.activity_time) < 3:
                return True
        if self.priority_queue.empty() == False:
            return True
        else:
            if self.copy_thread is None:
                self.copy_thread = threading.Thread(target=self.copy_worker, daemon=True)
                self.copy_thread.start()
        if self.file_remain > 0 and self.file_totsize > 0:
            return True
        return False

    def is_off(self):
        return self.state == COPIERSTATE_IDLE or self.state == COPIERSTATE_DONE or self.state == COPIERSTATE_CANCELED or self.state == COPIERSTATE_ERROR or self.state == COPIERSTATE_FULL or self.mode == COPIERMODE_NONE

    def user_cancel(self):
        self.interrupted = True
        if self.copy_thread is not None:
            timeout = 0
            while self.is_off() == False and timeout < 100:
                time.sleep(1) # thread yield
                timeout += 1
