#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math, glob
import threading, queue, socket
import psutil

def get_size_string(x):
    remMB = math.ceil(x / 1024 / 1024)
    remGB = x / 1024 / 1024 / 1024
    remTB = x / 1024 / 1024 / 1024 / 1024
    sizestr = "?MB"
    if remTB >= 1 or remGB >= 100:
        sizestr = "%dGB" % (math.ceil(remGB))
    elif remGB >= 10:
        sizestr = "%.1fGB" % (remGB)
    elif remGB >= 1:
        sizestr = "%.2fGB" % (remGB)
    else:
        sizestr = "%dMB" % (remMB)
    return sizestr

def get_time_string(totalsecs):
    minsremain = math.floor(totalsecs / 60)
    secsremain = totalsecs % 60
    if minsremain >= 100:
        hoursremain = math.floor(minsremain / 60)
        minsremain = minsremain % 60
        return "%d:%02d:%02d" % (hoursremain, minsremain, secsremain)
    else:
        return "%d:%02d" % (minsremain, secsremain)

def find_mount_point(path):
    abspath = os.path.abspath(path)
    x = abspath
    try:
        while not os.path.ismount(x):
            x = os.path.dirname(x)
        return x
    except:
        # probably unmounted
        return "/"

def is_still_mounted(path):
    app = bucketapp.bucket_app
    mntpt = find_mount_point(path)
    if mntpt is None or len(mntpt) <= 1:
        return False
    elif app is not None:
        app.update_disk_list()
        if mntpt not in app.disks or os.path.isdir(mntpt) == False:
            return False
    return True

def path_is_image_file(path):
    app = bucketapp.bucket_app
    x = os.path.basename(path).lower()

    # check for the file name prefix, on Sony cameras, default is "DSC"
    prf = app.cfg_get_prefix().lower()
    #if x.startswith(prf) == False:
    #    return False, "", "", "", ""
    # I disabled the exact match check for the header
    # the current requirement is that the first few characters must be alphabet
    # this allows for multiple cameras to connect to the same server, if they are configured with different headers
    xh = x[0:len(prf)]
    if xh.isalpha() == False:
        return False, "", "", "", ""

    # check if it's an image file by examining its file name extension
    extlist = app.cfg_get_extensions()
    usedext = None
    for ext in extlist:
        if x.endswith("." + ext.lower()):
            usedext = path[-len(ext):]
            break
    if usedext is None:
        return False, "", "", "", ""

    # extract the part of the name that's not the prefix and not the extension
    y = x[len(prf):]
    y = y[:-(1 + len(usedext))]

    if y.isnumeric() == False: # this must be a number string to be considered a valid file from the camera
        return False, "", "", "", ""

    if len(y) >= 11: # the name is long enough to contain a date
        return True, y, y[0:6], y[-5:], usedext
    else:
        return True, y, "", y, usedext

def is_camera_file(path):
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    if isimg == False or len(filedatecode) > 0:
        return False
    pathparts = path.split(os.path.sep)
    if pathparts[-3] == "DCIM" or pathparts[-2] == "DCIM":
        return True

def is_disk_camera(path):
    dcim = os.path.join(path, "DCIM")
    return os.path.isdir(dcim)

def rename_camera_file(path, datestroverride = None):
    app = bucketapp.bucket_app
    head, tail = os.path.split(path)
    prf = app.cfg_get_prefix() if app is not None or "DSC"
    # build the new file name with the date code
    s1 = tail[0:len(prf)]
    if datestroverride is None:
        s2 = app.get_date_str()
    else:
        s2 = datestroverride
    s3 = tail[len(prf):]
    nfname = s1 + s2 + s3
    # build the new dir name with the date code
    head2, tail2 = os.path.split(head)
    ndir = s2 + "-" + tail2
    return ndir, nfname

def rename_camera_file_path(path, bucketname, disk, dateoverride = None):
    bucketname = bucketname
    ndir, nfname = rename_camera_file(path, datestroverride = dateoverride)
    npath = os.path.join(disk, bucketname)
    npath = os.path.join(npath, ndir)
    os.makedirs(npath, exist_ok=True)
    npath = os.path.join(npath, nfname)
    return npath

def get_wifi_ip():
    #if os.name == 'nt':
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    return str(s.getsockname()[0])

def get_wifi_ssid():
    if os.name != "nt":
        try:
            ssid = os.popen("sudo iwgetid -r").read()
            ssid = ssid.strip()
            if len(ssid.strip()) > 0:
                return ssid
        except:
            pass
    else:
        return "Test SSID"
    try:
        r = subprocess.run(["netsh", "wlan", "show", "network"], capture_output=True, text=True).stdout
        ls = r.split("\n")
        ssids = [k for k in ls if 'SSID' in k]
        for ssid in ssids:
            s = ssid.strip()
            if len(s) > 0:
                return s
    except:
        pass
    return ""

def ext_is_raw(fileext):
    app = bucketapp.bucket_app
    rawexts = ["arw"]
    if app is not None:
        rawexts = app.cfg_get_extensions(key="raw_extensions", defval=["arw"]) # if raw file is not enabled on camera, then the cfg file should change this to jpg
    for re in rawexts:
        if re.lower() == fileext.lower():
            return True
    return False

def get_img_exif_date(file):
    if file is None:
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
        logger.error(estr + "exception: " + str(ex))

    # fall-back to method that uses the folder name for date
    try:
        head, tail = os.path.split(file)
        head, dir = os.path.split(head)
        if len(dir) >= 4:
            dirdatestr = dir[-5:]
            if dirdatestr.isnumeric():
                dirdatestr = "202" + dirdatestr
                return datetime.datetime.strptime(dirdatestr, "%Y%m%d")
    except Exception as ex:
        logger.error("Unable to extract date from dir name, exception: " + str(ex))
        if os.name == "nt":
            raise ex

    return None

def get_mounted_disks():
    list = []
    partitions = psutil.disk_partitions()
    for p in partitions:
        if ((p.mountpoint.startswith("/mnt/") and len(p.mountpoint) > 5) or (p.mountpoint.startswith("/mount/") and len(p.mountpoint) > 7) or (p.mountpoint.startswith("/media/") and len(p.mountpoint) > 7)) and ("fat" in p.fstype):
            t, f = get_disk_stats(p.mountpoint)
            if t > 0 and f > 0:
                list.append(p.mountpoint)
        elif len(p.mountpoint) == 3 and p.mountpoint[1] == ':' and p.mountpoint[0].isalpha() and p.mountpoint[0].isupper() and p.mountpoint[0] != 'C' and p.mountpoint[2] == os.path.sep:
            t, f = get_disk_stats(p.mountpoint)
            if t > 0 and f > 0:
                list.append(p.mountpoint)
    return list

def get_disk_stats(path):
    total = 0
    free = 0
    try:
        x = psutil.disk_usage(find_mount_point(path))
        total = x.total
        free = x.free
    except:
        pass
    if total <= 0:
        try:
            statvfs = os.statvfs(find_mount_point(path))
            free = statvfs.f_frsize * statvfs.f_bfree
            total = statvfs.f_frsize * statvfs.f_blocks
        except Exception as ex:
            if os.name == "nt":
                pass
            pass
    if total <= 0:
        try:
            total, used, free = shutil.disk_usage(find_mount_point(path))
        except:
            pass

    return total / 1024 / 1024, free / 1024 / 1024 # return in megabytes

def get_disk_label(path):
    try:
        mp = find_mount_point(path)
        res = mp
        lsblk = subprocess.run(["lsblk", "--output", "MOUNTPOINT,LABEL"], capture_output=True, text=True).stdout
        lines = lsblk.split('\n')
        for li in lines:
            limp = li[0:li.index(' ')]
            if limp == mp:
                lbl = li[len(mp) + 1:].strip()
                if len(lbl) > 0:
                    res = lbl
                    return res
        return res
    except Exception as ex:
        return None
