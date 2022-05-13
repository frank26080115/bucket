#!/usr/bin/env python3

import os, sys, time, datetime, shutil, subprocess, signal, random, math, glob
import threading, queue, socket

from PIL import Image, ImageOps, ImageDraw, ImageFont, ExifTags

from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urlparse import urlparse

from pyftpdlib.log import logger, config_logging, debug

bucket_app = None

thumb_queue_lowpriority = Queue.queue()
thumb_queue_highpriority = Queue.queue()
thumb_queue_busy = False
thumb_gen_thread = None

class BucketHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global bucket_app
        query = urlparse(self.path).query
        query_components = dict(qc.split("=") for qc in query.split("&"))
        if bucket_app is not None:
            dir = os.path.join(bucket_app.get_root(), bucket_app.cfg_get_bucketname())
        else:
            dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
        subfolders = [ f.path for f in os.scandir(dir) if f.is_dir() ]
        subfolders.sort(reverse=True)
        if self.path == "/" or self.path == "/index" or self.path == "/index.htm" or self.path == "/index.html":
            # root page
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            if os.path.isfile(get_webfilepath("index.htm")):
                with open(get_webfilepath("index.htm"), "r") as f:
                    self.wfile.write(bytes(f.read(), "utf-8"))
            else:
                self.wfile.write(bytes("<html><head><title>Bucket Home</title></head><body>\r\n", "utf-8"))
            # serve up a simple clickable list of folders
            for d in subfolders:
                dd = os.path.basename(d)
                self.wfile.write(bytes("<br /><a href=\"/" + dd + ".htm\">" + dd + "</a><br />\r\n", "utf-8"))
            self.wfile.write(bytes("</body></html>", "utf-8"))
            return

        # check if requesting a subfolder
        for d in subfolders:
            dd = os.path.basename(d)
            virname = "/" + dd + ".htm"
            if self.path == virname:
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                if os.path.isfile(get_webfilepath("dirpage.htm")):
                    with open(get_webfilepath("dirpage.htm"), "r") as f:
                        self.wfile.write(bytes(f.read(), "utf-8"))
                else:
                    self.wfile.write(bytes("<html><head><title>Bucket " + dd + "</title></head><body>\r\n<h1>" + d + "</h1><br />", "utf-8"))

                # find all of the files, including ones already marked for keeping or deleting
                files = [ f.path for f in os.scandir(d) if f.is_file() ]
                files_kept = [] if os.path.isdir(os.path.join(d, "keep"  )) else [ f.path for f in os.scandir(d) if f.is_file(os.path.join(d, "keep"  )) ]
                files_del  = [] if os.path.isdir(os.path.join(d, "delete")) else [ f.path for f in os.scandir(d) if f.is_file(os.path.join(d, "delete")) ]
                allfiles = files + files_kept + files_del

                # sorting by name will keep them all in order
                def sort_basename(n):
                    return os.path.basename(n)
                allfiles.sort(key=sort_basename)

                if len(allfiles) <= 0:
                    self.wfile.write(bytes("no files in " + dd + "<br />\r\n", "utf-8"))
                else:
                    self.wfile.write(bytes("<div id=\"file_list\" style=\"display: none;\">\r\n"), "utf-8"))

                    # we'll need the thumbnails pretty soon
                    for f in files:
                        enqueue_thumb_generation(i, important=False)

                    # output a list of the files, the javascript can deal with it later
                    for f in allfiles:
                        ff = os.path.basename(f)
                        fn, fe = os.path.splitext(ff)
                        p = dd + "/" + ff
                        if fe.lower() == ".jpg" or fe.lower() == ".arw":
                            self.wfile.write(bytes("<a href=\"" + p + "\">" + p + "</a><br />\r\n"), "utf-8"))
                    self.wfile.write(bytes("</div>"), "utf-8"))
                self.wfile.write(bytes("</body></html>", "utf-8"))
                return

        if self.path.startswith("/") and (self.path.endswith(".css") or self.path.endswith(".js")):
            p = get_webfilepath("." + self.path)
            if os.path.isfile(p) == False:
                self.send_response(404)
                return
            self.send_response(200)
            ct = "text/css" if self.path.endswith(".css") else "text/javascript"
            self.send_header("Content-type", ct)
            self.end_headers()
            with open(p, "r") as f:
                self.wfile.write(bytes(f.read(), "utf-8"))
            return

        if self.path.startswith("/") and (self.path.endswith(".jpg") or self.path.endswith(".jpeg")):
            p = os.path.join(dir, self.path[1:].replace("/", os.path.sep))

            if "thumb" in p or ".zoomed." in p or ".preview." in p:
                orignames = get_original_names(p)
                # we need the thumbnails immediately, generate them with high priority now
                # if they exist already, this will almost immediately finish
                for i in orignames:
                    enqueue_thumb_generation(i, important=True)
                thumbgen_wait()

            if (os.path.sep + "thumbs" + os.path.sep) in p and os.path.isfile(p) == False:
                # file not there might mean the type is wrong, so swap the type
                if ".preview." in p:
                    p = p.replace(".preview.", ".thumb.")
                elif ".thumb." in p:
                    p = p.replace(".thumb.", ".preview.")

            if os.path.isfile(p) == False:
                # maybe it's in a keep or delete folder? multi clients might be out of sync with the file system
                head, tail = os.path.split(p)
                p2 = os.path.join(head, "keep", tail)
                if os.path.isfile(p2):
                    p = p2
                else:
                    p2 = os.path.join(head, "delete", tail)
                    if os.path.isfile(p2):
                        p = p2
                    else:
                        self.send_error(404)
                        return

            # we are ready to serve the JPG now
            self.send_response(200)
            self.send_header("Content-type", "image/jpg")
            self.end_headers()

            sz = os.path.getsize(p)
            rem = sz
            with open(p, "rb") as fin:
                while rem > 0:
                    rlen = min(1024 * 100, rem)
                    bytes = fin.read(rlen)
                    if not bytes or len(bytes) <= 0:
                        break
                    self.wfile.write(bytes)
                    rem -= rlen
                    if len(bytes) < rlen:
                        break
            return

        # handle command
        if self.path.startswith("/keepfile"):
            keepname = query_components["file"]
            keep_file(keepname)
            return

        # handle command
        if self.path.startswith("/deletefile"):
            deletename = query_components["file"]
            keep_file(deletename, dirword="delete")
            return

def get_webfilepath(fname):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)

def get_server(port = 8000):
    return ThreadingHTTPServer(('', port), BucketHttpHandler)

def get_original_names(thumbpath):
    pnodots = p[0:p.index('.')]
    head, tail = os.path.split(pnodots)
    if head.endswith("thumbs"):
        head = head[0:-7]
    p1 = os.path.join(head, tail + ".ARW")
    p2 = os.path.join(head, tail + ".JPG")
    return [p1, p2]

def get_kept_name(fpath, dirname = "keep"):
    if (os.path.sep + dirname + os.path.sep) in fpath:
        return fpath
    head, tail = os.path.split(fpath)
    return os.path.join(head, dirname, tail)

def keep_file(vpath, dirword = "keep"):
    global bucket_app
    fpath = vpath.replace("/", os.path.sep)
    if bucket_app is not None:
        dir = os.path.join(bucket_app.get_root(), bucket_app.cfg_get_bucketname())
    else:
        dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
    fpath2 = os.path.join(dir, fpath)
    kpath = get_kept_name(fpath2, dirname = dirword)
    os.makedirs(os.path.dirname(kpath), exist_ok=True)
    if os.path.isfile(fpath2) and os.path.isfile(kpath) == False:
        os.rename(fpath2, kpath)
    if bucket_app is not None:
        if len(bucket_app.disks) > 1:
            for disk in bucket_app.disks:
                if disk == bucket_app.disks[0]:
                    continue
                try:
                    dir = os.path.join(disk, bucket_app.cfg_get_bucketname())
                    fpath2 = os.path.join(dir, fpath)
                    kpath = get_kept_name(fpath2, dirname = dirword)
                    os.makedirs(os.path.dirname(kpath), exist_ok=True)
                    if os.path.isfile(fpath2) and os.path.isfile(kpath) == False:
                        os.rename(fpath2, kpath)
                except Exception as ex2:
                    logger.error("Error in keep_file for another drive(\"" + disk + "\", \"" + vpath + "\"): " + str(ex2))
                    if os.name == "nt":
                        raise ex2

def generate_thumbnail(filepath, skip_if_exists=True):
    thumbpath   = get_thumbname(filepath)
    previewpath = get_thumbname(filepath, filetail="preview")
    zoomedpath  = get_thumbname(filepath, filetail="zoomed")

    if filepath.lower().endswith(".arw"):
        if skip_if_exists == False or os.path.isfile(previewpath) == False:
            extract_jpg_preview(filepath)
        jpgpath = filepath[0:-4] + ".JPG"
        if os.path.isfile(jpgpath) and skip_if_exists:
            generate_thumbnail(jpgpath, skip_if_exists=skip_if_exists)
        return

    if filepath.lower().endswith(".jpg"):
        if skip_if_exists == False or os.path.isfile(zoomedpath) == False:
            generate_zoomnail(filepath, skip_if_exists=skip_if_exists)
        arwpath = filepath[0:-4] + ".ARW"
        generate_thumbnail(arwpath, skip_if_exists=skip_if_exists)
        if skip_if_exists == False or (os.path.isfile(thumbpath) == False and os.path.isfile(previewpath) == False):
            # only do this image rescaling if the raw file did not provide a faster embedded preview
            img = Image.open(filepath)
            try:
                img = ImageOps.exif_transpose(img)
            except:
                pass
            img.thumbnail((1616, 1620), Image.ANTIALIAS)
            os.makedirs(os.path.dirname(thumbpath), exist_ok=True)
            img.save(thumbpath, "JPEG")

def enqueue_thumb_generation(filepath, important=False):
    global thumb_queue_lowpriority
    global thumb_queue_highpriority
    global thumb_queue_busy
    global thumb_gen_thread
    if important:
        thumb_queue_highpriority.put(filepath)
        thumb_queue_busy = True
    else:
        thumb_queue_lowpriority.put(filepath)
    if thumb_gen_thread is None:
        thumb_gen_thread = threading.Thread(target=thumbgen_worker, daemon=True)
        thumb_gen_thread.start()

def thumbgen_worker():
    global thumb_queue_lowpriority
    global thumb_queue_highpriority
    global thumb_queue_busy
    global thumb_gen_thread
    try:
        while True:
            try:
                was_high = False
                x = None
                if thumb_queue_highpriority.empty() == False:
                    x = thumb_queue_highpriority.get()
                    was_high = True
                    thumb_queue_busy = True
                elif thumb_queue_lowpriority.empty() == False:
                    x = thumb_queue_lowpriority.get()

                if x is not None:
                    generate_thumbnail(x)
                    if was_high and thumb_queue_highpriority.empty():
                        thumb_queue_busy = False
                    time.sleep(0)
                else:
                    time.sleep(1)

                if thumb_queue_lowpriority.empty() and thumb_queue_highpriority.empty():
                    break
            except Exception as ex2:
                logger.error("Thumb generation thread inner exception: " + str(ex2))
                if os.name == "nt":
                    raise ex2
        thumb_gen_thread = None
        thumb_queue_busy = False
    except Exception as ex1:
        logger.error("Thumb generation thread outer exception: " + str(ex1))
        thumb_gen_thread = None
        if os.name == "nt":
            raise ex1

def thumbgen_wait(t = 0.001):
    global thumb_queue_highpriority
    global thumb_queue_busy
    global thumb_gen_thread
    if thumb_gen_thread is None and thumb_queue_highpriority.empty() == False:
        enqueue_thumb_generation(thumb_queue_highpriority.get(), important=True)
    while thumb_queue_busy:
        time.sleep(t)

def thumbgen_clear():
    global thumb_queue_lowpriority
    remainder = []
    while thumb_queue_lowpriority.empty() == False:
        remainder.append(thumb_queue_lowpriority.get())
    return remainder

def generate_zoomnail(filepath, skip_if_exists=True):
    zoomedpath = get_thumbname(filepath, filetail="zoomed")
    if skip_if_exists and os.path.isfile(zoomedpath):
        return

    exif = get_image_exif(filepath)
    focus_points = get_image_focus_point(exif)
    if focus_points is None:
        return
    img = Image.open(filepath)
    try:
        img = ImageOps.exif_transpose(img)
    except:
        pass
    width, height = img.size

    sz = (1616, 1080)
    if height > width:
        focus_points = [focus_points[1], focus_points[0], focus_points[3], focus_points[2]]
        sz = (sz[1], sz[0])
    sz2 = (int(round(sz[0]/2)), int(round(sz[1]/2)))
    pt = (int(round(width * focus_points[2] / focus_points[0])), int(round(height * focus_points[3] / focus_points[1])))
    box = (pt[0] - sz2[0], pt[1] - sz2[1], pt[0] + sz2[0], pt[1] + sz2[1])
    while box[0] < 0 and box[2] < (width - 1):
        box[0] += 1
        box[2] += 1
    while box[2] > (width - 1) and box[0] > 0:
        box[0] -= 1
        box[2] -= 1
    while box[0] < 0:
        box[0] += 1
    while box[1] < 0 and box[3] < (height - 1):
        box[1] += 1
        box[3] += 1
    while box[3] > (height - 1) and box[1] > 0:
        box[1] -= 1
        box[3] -= 1
    while box[1] < 0:
        box[1] += 1
    cropped = img.crop((box[0], box[1], box[2], box[3]))
    os.makedirs(os.path.dirname(zoomedpath), exist_ok=True)
    cropped.save(zoomedpath, "JPEG")

def get_thumbname(origfile, thumbdir = "thumbs", filetail = "thumb"):
    dir, fname = os.path.split(origfile)
    fnamenoext, fext = os.path.splitext(fname)
    thumbdir = os.path.join(dir, thumbdir)
    thumbname = fnamenoext + "." + filetail + ".jpg"
    return os.path.join(thumbdir, thumbname)

def get_image_focus_point(exiftxt):
    lines = exiftxt.split('\n')
    for line in lines:
        if line.lower().startswith("Focus Location".lower()):
            parts = line.split(' ')
            if len(parts) > 6:
                numstrs = parts[-4:]
                if numstrs[0].isnumeric() and numstrs[1].isnumeric() and numstrs[2].isnumeric() and numstrs[3].isnumeric():
                    return [int(numstrs[0]), int(numstrs[1]), int(numstrs[2]), int(numstrs[3])]
    return None

def extract_jpg_preview(filepath):
    # use exiftool to extract embedded preview out of raw file
    # this should be faster than using PIL to resize a full JPG
    subprocess.run([find_exiftool(), "-b", filepath, "-PreviewImage", "-w", ".pv.jpg"], capture_output=True, text=True).stdout
    pvpath = filepath[0:-4] + ".pv.jpg"
    if os.path.isfile(pvpath) == False:
        return None
    thumbpath = get_thumbname(filepath, filetail = "preview")
    os.makedirs(os.path.dirname(thumbpath), exist_ok=True)
    os.rename(pvpath, thumbpath)
    return thumbpath

def get_image_exif(filepath):
    # this will just spit out a huge chunk of text from exiftool
    return subprocess.run([find_exiftool(), filepath], capture_output=True, text=True).stdout

def find_exiftool():
    g = glob.glob("*ExifTool*/exiftool", recursive=True)
    g.sort(reverse=True)
    if len(g) > 0:
        return g[0]
    g = glob.glob("*ExifTool*.tar.gz")
    if len(g) <= 0:
        return
    g.sort(reverse=True)
    tarname = g[0]
    os.system("tar -xvf " + tarname)
    return find_exiftool()

def register_app(app):
    global bucket_app
    bucket_app = app

def main():
    config_logging()
    if len(sys.argv) > 1:
        print("test image processing " + sys.argv[1])
        generate_thumbnail(sys.argv[1])
    else:
        print("starting http server")
        server = get_server()
        server.serve_forever()
    return 0

if __name__ == "__main__":
    main()