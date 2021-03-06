#!/usr/bin/env python3

import os, sys, time, datetime, shutil, signal, random, math, glob, queue
import threading, queue, socket

import bucketapp, bucketutils, bucketcopy, bucketlogger

from PIL import Image, ImageOps, ImageDraw, ImageFont, ExifTags

from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import urllib, urllib.parse

logger = bucketlogger.getLogger()

bucket_app = None

class BucketHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global bucket_app

        query = urllib.parse.urlparse(self.path).query
        query_components = dict()
        try:
            query_components = dict(qc.split("=") for qc in query.split("&"))
        except Exception as ex:
            if "2 is required" in str(ex):
                pass
            else:
                logger.error("HTTP query_components error: " + str(ex))

        if bucket_app is not None:
            self.ftmgr = bucket_app.ftmgr
            dir = os.path.join(bucket_app.get_root(), bucket_app.cfg_get_bucketname())
        else:
            dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")

        if not hasattr(self, 'ftmgr'):
            self.ftmgr = BucketWebFeatureManager()

        subfolders = [ f.path for f in os.scandir(dir) if f.is_dir() ]
        subfolders.sort(reverse=True)
        if self.path == "/" or self.path == "/index" or self.path == "/index.htm" or self.path == "/index.html":
            # root page
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            if os.path.isfile(get_webfilepath("index.htm")):
                with open(get_webfilepath("index.htm"), "r") as f:
                    x = f.read()
                    if "<!--test-->" in x:
                        x = x[:x.index("<!--test-->")]
                    self.wfile.write(bytes(x, "utf-8"))
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

                def writeout(s, hfile, wfile):
                    hfile.write(s)
                    wfile.write(bytes(s, "utf-8"))

                with open(os.path.join(dir, dd) + ".htm", "w") as htmlfile:

                    if os.path.isfile(get_webfilepath("dirpage.htm")):
                        with open(get_webfilepath("dirpage.htm"), "r") as f:
                            x = f.read()
                            if "<!--test-->" in x:
                                x = x[:x.index("<!--test-->")]
                            writeout(x, htmlfile, self.wfile)
                            writeout("<div id=\"title_holder\" style=\"display: none;\">" + dd + "</div>\r\n", htmlfile, self.wfile)
                    else:
                        writeout("<html><head><title>Bucket " + dd + "</title></head><body>\r\n<h1>" + d + "</h1><br />", htmlfile, self.wfile)

                    # find all of the files, including ones already marked for keeping or deleting
                    files = [ f.path for f in os.scandir(d) if f.is_file() ]
                    files_kept = [] if not os.path.isdir(os.path.join(d, "keep"  )) else [ f.path for f in os.scandir(os.path.join(d, "keep"  ))]
                    files_del  = [] if not os.path.isdir(os.path.join(d, "delete")) else [ f.path for f in os.scandir(os.path.join(d, "delete"))]
                    allfiles = files + files_kept + files_del

                    # sorting by name will keep them all in order
                    def sort_basename(n):
                        return os.path.basename(n)
                    allfiles.sort(key=sort_basename)

                    if len(allfiles) <= 0:
                        writeout("no files in " + dd + "<br />\r\n", htmlfile, self.wfile)
                    else:
                        writeout("<div id=\"file_list\" style=\"display: none;\">\r\n", htmlfile, self.wfile)

                        # we'll need the thumbnails pretty soon
                        for f in files:
                            self.ftmgr.enqueue_thumb_generation(f, important=False)

                        # output a list of the files, the javascript can deal with it later
                        for f in allfiles:
                            ff = os.path.basename(f)
                            fn, fe = os.path.splitext(ff)
                            p = dd + "/" + ff
                            if fe.lower() == ".jpg" or fe.lower() == ".arw":
                                classes = " imgkept" if f in files_kept else (" imgdeleted" if f in files_del else "")
                                writeout("<a href=\"" + p + "\" class=\"imgurl" + classes + "\">" + p + "</a><br />\r\n", htmlfile, self.wfile)
                        writeout("</div><div id=\"main_content\"></div><hr /><div id=\"foot_content\"></div>", htmlfile, self.wfile)
                    writeout("</body></html>", htmlfile, self.wfile)
                return

        if self.path.startswith("/") and (self.path.lower().endswith(".css") or self.path.lower().endswith(".js")):
            p = get_webfilepath("." + self.path)
            if os.path.isfile(p) == False:
                self.send_error(404)
                return
            self.send_response(200)
            ct = "text/css" if self.path.lower().endswith(".css") else "text/javascript"
            self.send_header("Content-type", ct)
            self.end_headers()
            with open(p, "r") as f:
                self.wfile.write(bytes(f.read(), "utf-8"))
            return

        if self.path.startswith("/") and (self.path.lower().endswith(".jpg") or self.path.lower().endswith(".jpeg")) and (self.path.startswith("/keepfile") == False and self.path.startswith("/deletefile") == False):
            p = os.path.join(dir, self.path[1:].replace("/", os.path.sep))

            if "thumb" in p or ".zoomed." in p or ".preview." in p:
                orignames = get_original_names(p)
                # we need the thumbnails immediately, generate them with high priority now
                # if they exist already, this will almost immediately finish
                for i in orignames:
                    self.ftmgr.enqueue_thumb_generation(i, important=True)
                self.ftmgr.thumbgen_wait()

            if (os.path.sep + "thumbs" + os.path.sep) in p and os.path.isfile(p) == False:
                # file not there might mean the type is wrong, so swap the type
                if ".preview." in p:
                    p = p.replace(".preview.", ".thumb.")
                elif ".thumb." in p:
                    p = p.replace(".thumb.", ".preview.")

            if os.path.isfile(p) == False:
                p = find_existing_image_file(p)
                if os.path.isfile(p) == False:
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
                    barr = fin.read(rlen)
                    if not barr or len(barr) <= 0:
                        break
                    self.wfile.write(barr)
                    rem -= rlen
                    if len(barr) < rlen:
                        break
            return

        # handle command
        if self.path.startswith("/keepfile"):
            keepname = urllib.parse.unquote(query_components["file"])
            keep_file(keepname)
            self.send_response(200)
            self.end_headers()
            return

        # handle command
        if self.path.startswith("/deletefile"):
            deletename = urllib.parse.unquote(query_components["file"])
            keep_file(deletename, dirword="delete")
            self.send_response(200)
            self.end_headers()
            return

        # handle command
        if self.path.startswith("/actuallydelete"):
            deletename = urllib.parse.unquote(query_components["dir"])
            dp = os.path.join(dir, deletename, "delete", "*")
            dg = glob.glob(dp)
            dc = 0
            if os.name == "nt":
                print("deleting \"%s\"" % dp)
                print(dg)
                dc = len(dg)
            else:
                for i in dg:
                    try:
                        os.remove(dg)
                        dc += 1
                    except:
                        pass
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(bytes("%u" % dc, "utf-8"))
            return

def get_webfilepath(fname):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)

def get_server(port = 8000):
    return ThreadingHTTPServer((bucketutils.get_wifi_ip(), port), BucketHttpHandler)

def get_original_names(thumbpath):
    pnodots = thumbpath[0:thumbpath.index('.')]
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

def keep_file(vpath, dirword = "keep", canrecurse = True, fromhttp = True):
    global bucket_app
    if bucket_app is not None:
        dir = os.path.join(bucket_app.get_root(), bucket_app.cfg_get_bucketname())
    else:
        dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
    if fromhttp:
        fpath = vpath.replace("/", os.path.sep)
        fpath2 = os.path.join(dir, fpath)
    else:
        fpath2 = vpath
        fpathtail = os.path.abspath(fpath2)[len(dir):]

    kpath = get_kept_name(fpath2, dirname = dirword)

    if os.path.isfile(fpath2) == False:
        fpath2 = find_existing_image_file(fpath2)

    if os.path.isfile(fpath2) and os.path.isfile(kpath) == False:
        os.makedirs(os.path.dirname(kpath), exist_ok=True)
        os.rename(fpath2, kpath)
    elif os.path.isfile(fpath2) and os.path.isfile(kpath) == True and os.getsize(kpath) <= 0:
        os.remove(kpath)
        os.rename(fpath2, kpath)

    if bucket_app is not None:
        if len(bucket_app.disks) > 1:
            for disk in bucket_app.disks:
                if disk == bucket_app.disks[0]:
                    continue
                try:
                    dir = os.path.join(disk, bucket_app.cfg_get_bucketname())
                    if fromhttp:
                        fpath2 = os.path.join(dir, fpath)
                    else:
                        fpath2 = os.path.join(dir, fpathtail)
                    kpath = get_kept_name(fpath2, dirname = dirword)
                    if os.path.isfile(fpath2) and os.path.isfile(kpath) == False:
                        os.makedirs(os.path.dirname(kpath), exist_ok=True)
                        os.rename(fpath2, kpath)
                    elif os.path.isfile(fpath2) and os.path.isfile(kpath) == True and os.getsize(kpath) <= 0:
                        os.remove(kpath)
                        os.rename(fpath2, kpath)
                    elif os.path.isfile(fpath2) == False and os.path.isfile(kpath) == False and dirword == "keep":
                        # there's no available file on the redundant disk to simply rename
                        # so we will do a full copy instead, from one disk to another
                        # this is slow so we do it in another thread
                        bucket_app.ftmgr.enqueue_keepcopy(fpath2, kpath)
                except Exception as ex2:
                    logger.error("Error in keep_file for another drive(\"" + disk + "\", \"" + vpath + "\"): " + str(ex2))
                    if os.name == "nt":
                        raise ex2
    if canrecurse:
        if vpath.lower().endswith(".jpg"):
            keep_file(vpath.replace(".JPG", ".ARW").replace(".jpg", ".arw"), dirword = dirword, canrecurse = False, fromhttp = fromhttp)
        elif vpath.lower().endswith(".arw"):
            keep_file(vpath.replace(".ARW", ".JPG").replace(".arw", ".jpg"), dirword = dirword, canrecurse = False, fromhttp = fromhttp)

def find_existing_image_file(fpath):
    kstr = os.path.sep + "keep" + os.path.sep
    dstr = os.path.sep + "delete" + os.path.sep
    if kstr in fpath:
        fpath3 = fpath.replace(kstr, os.path.sep)
        if os.path.isfile(fpath3):
            fpath = fpath3
        else:
            fpath3 = fpath.replace(kstr, dstr)
            if os.path.isfile(fpath3):
                fpath = fpath3
    elif dstr in fpath:
        fpath3 = fpath.replace(dstr, os.path.sep)
        if os.path.isfile(fpath3):
            fpath = fpath3
        else:
            fpath3 = fpath.replace(dstr, kstr)
            if os.path.isfile(fpath3):
                fpath = fpath3
    else:
        head, tail = os.path.split(fpath)
        fpath3 = os.path.join(head, "keep", tail)
        if os.path.isfile(fpath3):
            fpath = fpath3
        else:
            fpath3 = os.path.join(head, "delete", tail)
            if os.path.isfile(fpath3):
                fpath = fpath3
    return fpath

class BucketWebFeatureManager:
    def __init__(self, app = None):
        global bucket_app
        if app is None and bucket_app is not None:
            app = bucket_app
        self.app = app
        self.keepcopy_thread = None
        self.keepcopy_queue = queue.Queue()
        self.thumb_queue_lowpriority = queue.Queue()
        self.thumb_queue_highpriority = queue.Queue()
        self.thumb_queue_busy = False
        self.thumb_gen_thread = None
        self.engaged = True

    def enqueue_keepcopy(self, src, dest):
        if not self.engaged:
            return
        self.keepcopy_queue.put(src + ";" + dest)
        if self.keepcopy_thread is None:
            self.keepcopy_thread = threading.Thread(target=self.keepcopy_worker, daemon=True)
            self.keepcopy_thread.start()

    def keepcopy_worker(self):
        try:
            while True:
                try:
                    if self.keepcopy_queue.empty() == False:
                        x = self.keepcopy_queue.get()
                        pts = x.split(';')
                        if len(pts) != 2:
                            continue
                        src = pts[0].strip()
                        dst = pts[1].strip()
                        if os.path.isfile(src) == False:
                            continue
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(src, dst)
                    else:
                        time.sleep(1)
                        break
                except Exception as ex2:
                    logger.error("Keep-copy thread inner exception: " + str(ex2))
                    if os.name == "nt":
                        raise ex2
            self.keepcopy_thread = None
        except Exception as ex1:
            logger.error("Keep-copy thread outer exception: " + str(ex1))
            self.keepcopy_thread = None
            if os.name == "nt":
                raise ex1

    def enqueue_thumb_generation(self, filepath, important=False):
        if not self.engaged:
            return
        if important:
            self.thumb_queue_highpriority.put(filepath)
            self.thumb_queue_busy = True
        else:
            self.thumb_queue_lowpriority.put(filepath)
        if self.thumb_gen_thread is None:
            self.thumb_gen_thread = threading.Thread(target=self.thumbgen_worker, daemon=True)
            self.thumb_gen_thread.start()

    def thumbgen_worker(self):
        try:
            while True:
                time.sleep(0) # thread yield
                try:
                    was_high = False
                    x = None
                    if self.thumb_queue_highpriority.empty() == False:
                        x = self.thumb_queue_highpriority.get()
                        was_high = True
                        self.thumb_queue_busy = True
                    elif self.thumb_queue_lowpriority.empty() == False:
                        x = self.thumb_queue_lowpriority.get()

                    if x is not None:
                        if self.app is not None:
                            self.app.cpu_highfreq()
                        generate_thumbnail(x)
                        move_if_rated(x)
                        if was_high and self.thumb_queue_highpriority.empty():
                            self.thumb_queue_busy = False
                        time.sleep(0)
                    else:
                        time.sleep(1)

                    if self.thumb_queue_lowpriority.empty() and self.thumb_queue_highpriority.empty():
                        break
                except Exception as ex2:
                    logger.error("Thumb generation thread inner exception: " + str(ex2))
                    if os.name == "nt":
                        raise ex2
            self.thumb_gen_thread = None
            self.thumb_queue_busy = False
        except Exception as ex1:
            logger.error("Thumb generation thread outer exception: " + str(ex1))
            self.thumb_gen_thread = None
            self.thumb_queue_busy = False
            if os.name == "nt":
                raise ex1

    def thumbgen_wait(self, t = 0.1):
        if self.thumb_gen_thread is None and self.thumb_queue_highpriority.empty() == False:
            self.enqueue_thumb_generation(self.thumb_queue_highpriority.get(), important=True)
        while self.thumb_queue_busy:
            time.sleep(t)

    def thumbgen_clear(self):
        if not self.engaged:
            return []
        remainder = []
        while self.thumb_queue_lowpriority.empty() == False:
            remainder.append(self.thumb_queue_lowpriority.get())
        return remainder

    def thumbgen_is_busy(self):
        if not self.engaged:
            return False
        return self.thumb_queue_busy

def generate_thumbnail(filepath, skip_if_exists=True, allow_recurse=True):
    thumbpath   = get_thumbname(filepath)
    previewpath = get_thumbname(filepath, filetail="preview")
    zoomedpath  = get_thumbname(filepath, filetail="zoomed")

    if filepath.lower().endswith(".arw"):
        if skip_if_exists == False or os.path.isfile(previewpath) == False:
            extract_jpg_preview(filepath)
        jpgpath = filepath[0:-4] + ".JPG"
        if os.path.isfile(jpgpath) and skip_if_exists and allow_recurse:
            generate_thumbnail(jpgpath, skip_if_exists=skip_if_exists, allow_recurse=False)
        return

    if filepath.lower().endswith(".jpg"):
        if skip_if_exists == False or os.path.isfile(zoomedpath) == False:
            generate_zoomnail(filepath, skip_if_exists=skip_if_exists)
        arwpath = filepath[0:-4] + ".ARW"
        if allow_recurse:
            generate_thumbnail(arwpath, skip_if_exists=skip_if_exists, allow_recurse=False)
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

def move_if_rated(filepath):
    exif = get_image_exif(filepath)
    rating = get_image_rating(exif)
    if rating > 0:
        keep_file(filepath, fromhttp = False)

def generate_zoomnail(filepath, skip_if_exists=True):
    zoomedpath = get_thumbname(filepath, filetail="zoomed")
    if skip_if_exists and os.path.isfile(zoomedpath):
        return

    exif = get_image_exif(filepath)
    focus_points = get_image_focus_point(exif)
    if focus_points is None:
        # fake this to center of image, in case focus points are missing
        focus_points = [30, 20, 15, 10]
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
    box = [pt[0] - sz2[0], pt[1] - sz2[1], pt[0] + sz2[0], pt[1] + sz2[1]]
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

def get_image_rating(exiftxt):
    lines = exiftxt.split('\n')
    for line in lines:
        if line.lower().startswith("Rating".lower()):
            parts = line.split(' ')
            if len(parts) >= 3:
                ratingstr = parts[-1]
                if ratingstr.isnumeric():
                    return int(ratingstr)
    return 0

def extract_jpg_preview(filepath):
    # use exiftool to extract embedded preview out of raw file
    # this should be faster than using PIL to resize a full JPG
    bucketutils.run_cmdline_read(find_exiftool() + " -b " + filepath + " -PreviewImage -w .pv.jpg")
    pvpath = filepath[0:-4] + ".pv.jpg"
    if os.path.isfile(pvpath) == False:
        return None
    thumbpath = get_thumbname(filepath, filetail = "preview")
    os.makedirs(os.path.dirname(thumbpath), exist_ok=True)
    os.rename(pvpath, thumbpath)
    return thumbpath

def get_image_exif(filepath):
    # this will just spit out a huge chunk of text from exiftool
    return bucketutils.run_cmdline_read(find_exiftool() + " " + filepath)

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

def set_running_app(app):
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
