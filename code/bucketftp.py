#!/usr/bin/env python3

import os

from bucketio import *
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler, DTPHandler
from pyftpdlib.servers import FTPServer, ThreadedFTPServer
from pyftpdlib.filesystems import AbstractedFS, FilesystemError

bucket_app = None

def register_bucket_app(app):
    global bucket_app
    bucket_app = app

class BucketFtpHandler(FTPHandler):

"""
Mostly just event handlers
"""

    def on_connect(self):
        """Called when client connects, *before* sending the initial
        220 reply.
        """
        global bucket_app
        pass

    def on_disconnect(self):
        """Called when connection is closed."""
        global bucket_app
        bucket_app.on_nonactivity()
        pass

    def on_login(self, username):
        """Called on user login."""
        global bucket_app
        pass

    def on_login_failed(self, username, password):
        """Called on failed login attempt.
        At this point client might have already been disconnected if it
        failed too many times.
        """
        global bucket_app
        pass

    def on_logout(self, username):
        """Called when user "cleanly" logs out due to QUIT or USER
        issued twice (re-login). This is not called if the connection
        is simply closed by client.
        """
        global bucket_app
        pass

    def on_file_sent(self, file):
        """Called every time a file has been successfully sent.
        "file" is the absolute name of the file just being sent.
        """
        global bucket_app
        pass

    def on_file_received(self, file):
        """Called every time a file has been successfully received.
        "file" is the absolute name of the file just being received.
        """
        global bucket_app
        bucket_app.on_nonactivity()
        bucket_app.on_file_received(path_cvt_fsyspath_thatexists(file))
        pass

    def on_incomplete_file_sent(self, file):
        """Called every time a file has not been entirely sent.
        (e.g. ABOR during transfer or client disconnected).
        "file" is the absolute name of that file.
        """
        global bucket_app
        pass

    def on_incomplete_file_received(self, file):
        """Called every time a file has not been entirely received
        (e.g. ABOR during transfer or client disconnected).
        "file" is the absolute name of that file.
        """
        global bucket_app
        bucket_app.on_nonactivity()
        paths = path_cvt_fsyspaths(file)
        for p in paths:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except:
                pass
        pass

class BucketFtpFilesystem(AbstractedFS):

    def ftp2fs(self, ftppath):
        global bucket_app
        x = AbstractedFS.ftp2fs(ftppath)
        bucket_root = bucket_app.get_root()
        if bucket_root is None:
            return "/tmp"
        if x[0] != '/':
            x = bucket_root + x
        x = path_cvt_fsyspath(x)
        return x

    def fs2ftp(self, fspath):
        assert isinstance(fspath, unicode), fspath
        fspath = path_cvt_virtualpath(fspath)
        bucketroot = bucket_app.get_root()
        if bucket_root is None:
            bucketroot = "/tmp" # uhhhh no place to put the file, the file write will fail if disk space is not available
        if os.path.isabs(fspath):
            p = os.path.normpath(fspath)
        else:
            p = os.path.normpath(os.path.join(bucketroot, fspath))
        if not self.validpath(p):
            return u('/')
        p = p.replace(os.sep, "/")
        p = p[len(bucketroot):]
        if not p.startswith('/'):
            p = '/' + p
        return p

    def open(self, filename, mode):
        assert isinstance(filename, unicode), filename
        if bucket_app.still_has_space() == False:
            raise FilesystemError("Out of disk space")
        filename = path_cvt_fsyspath(filename)
        head, tail = os.path.split(filename)
        os.makedirs(head, exist_ok=True)
        return open(filename, mode)

    def listdir(self, path):
        assert isinstance(path, unicode), path
        lst = os.listdir(path)
        i = 0
        # convert all the names from date-coded to non-date-coded before returning it to the FTP client
        while i < len(lst):
            if "." in lst:
                lst[i] = path_cvt_virtualname(lst[i])
            i += 1
        return lst

    def listdirinfo(self, path):
        return self.listdir(path)

    def remove(self, path):
        # do not delete any files
        pass

    def chmod(self, path, mode):
        assert isinstance(path, unicode), path
        if not hasattr(os, 'chmod'):
            raise NotImplementedError
        os.chmod(path_cvt_fsyspath(path), mode)

    def stat(self, path):
        return os.stat(path_cvt_fsyspath(path))

    def utime(self, path, timeval):
        return os.utime(path_cvt_fsyspath(path), (timeval, timeval))

    if hasattr(os, 'lstat'):
        def lstat(self, path):
            """Like stat but does not follow symbolic links."""
            # on python 2 we might also get bytes from os.lisdir()
            # assert isinstance(path, unicode), path
            return os.lstat(path_cvt_fsyspath(path))
    else:
        lstat = stat

    def isfile(self, path):
        assert isinstance(path, unicode), path
        return os.path.isfile(path_cvt_fsyspath(path))

    def isdir(self, path):
        assert isinstance(path, unicode), path
        return os.path.isdir(path_cvt_fsyspath(path))

    def getsize(self, path):
        assert isinstance(path, unicode), path
        return os.path.getsize(path_cvt_fsyspath(path))

    def getmtime(self, path):
        assert isinstance(path, unicode), path
        return os.path.getmtime(path_cvt_fsyspath(path))

    def realpath(self, path):
        assert isinstance(path, unicode), path
        return os.path.realpath(path_cvt_fsyspath(path))

    def lexists(self, path):
        assert isinstance(path, unicode), path
        return os.path.lexists(path_cvt_fsyspath(path))

class BucketDtpHandler(ThrottledDTPHandler):

"""
Using the ThrottledDTPHandler skeleton so we can get a signal for every packet transferred
"""

    def use_sendfile(self):
        return False

    def recv(self, buffer_size):
        global bucket_app
        chunk = DTPHandler.recv(self, buffer_size)
        bucket_app.on_activity()
        return chunk

    def send(self, data):
        global bucket_app
        num_sent = DTPHandler.send(self, data)
        bucket_app.on_activity()
        return num_sent

def path_is_image_file(path):
    global bucket_app
    x = os.path.basename(path).lower()

    # check for the file name prefix, on Sony cameras, default is "DSC"
    prf = bucket_app.cfg_get_prefix().lower()
    if x.startswith(prf) == False:
        return False, "", "", "", ""

    # check if it's an image file by examining its file name extension
    extlist = bucket_app.cfg_get_extensions().lower()
    usedext = None
    for ext in extlist:
        if x.endswith("." + ext.lower()):
            usedext = path[-len(ext):]
            break
    if usedext is None:
        return False, "", "", ""

    # extract the part of the name that's not the prefix and not the extension
    y = x[len(prf):]
    y = y[:-(1 + len(usedext))]

    if y.isnumeric() == False: # this must be a number string to be considered a valid file from the camera
        return False, "", "", "", ""

    if len(y) >= 11: # the name is long enough to contain a date
        return True, y, y[0:6], y[-5:], usedext
    else:
        return True, y, "", y, usedext

# return the name of a file without the date code, even if it has a date code
def path_cvt_virtualname(path):
    global bucket_app
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    head, tail = os.path.split(path)
    if isimg:
        prf = bucket_app.cfg_get_prefix()
        s1 = tail[0:len(prf)]
        s2 = filenumber
        s3 = "." + fileext
        t2 = s1 + s2 + s3
        return t2
    else:
        return tail

# return the path of a file without the date code, even if it has a date code
# warning: does not care where the root dir is
def path_cvt_virtualpath(path):
    global bucket_app
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    if isimg:
        head, tail = os.path.split(path)
        return os.path.join(head, path_cvt_virtualname(path))
    else:
        return path

# return the name of a file with the date code
def path_cvt_fsysname(path):
    global bucket_app
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    head, tail = os.path.split(path)
    if isimg and len(filedatecode) <= 0:
        prf = bucket_app.cfg_get_prefix()
        s1 = tail[0:len(prf)]
        s2 = bucket_app.get_date_str()
        s3 = tail[len(prf):]
        t2 = s1 + s2 + s3
        return t2
    else:
        return tail

# return the path of a file with the date code
# warning: does not care where the root dir is, assumed that ftp2fs added it
def path_cvt_fsyspath(path):
    global bucket_app
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    if isimg and len(filedatecode) <= 0:
        head, tail = os.path.split(path)
        return os.path.join(head, path_cvt_fsysname(path))
    else:
        return path

# returns a list of potential paths of a file, one version with the date code, and another without the date code
# warning: does not handle mount points
def path_cvt_fsyspaths(path):
    global bucket_app
    paths = []
    isimg, filename, filedatecode, filenumber, fileext = path_is_image_file(path)
    if isimg and len(filedatecode) <= 0:
        head, tail = os.path.split(path)
        npath = os.path.join(head, path_cvt_fsysname(path))
        if npath not in paths:
            paths.append(npath)
    if isimg and len(filedatecode) > 0:
        head, tail = os.path.split(path)
        npath = os.path.join(head, path_cvt_virtualname(path))
        if npath not in paths:
            paths.append(npath)
    if path not in paths:
        paths.append(path)
    return paths

# goes through the results from path_cvt_fsyspaths and picks the first one that actually exists
def path_cvt_fsyspath_thatexists(path):
    if os.path.isfile(path):
        return path
    paths = path_cvt_fsyspaths(path)
    for p in paths:
        if os.path.isfile(p):
            return p
    return None

def start_ftp_server(app = None):
    global bucket_app
    if app is not None:
        bucket_app = app

    authorizer = DummyAuthorizer()
    if bucket_app is None:
        authorizer.add_user('user', '12345', os.getcwd(), perm='elradfmwMT')
    else:
        authorizer.add_user(bucket_app.cfg_get_ftpusername(), bucket_app.cfg_get_ftppassword(), os.getcwd(), perm='elradfmwMT')

    ftp_handler               = BucketFtpHandler
    ftp_handler.authorizer    = authorizer
    ftp_handler.dtp_handler   = BucketDtpHandler
    ftp_handler.abstracted_fs = BucketFtpFilesystem

    port = 2121
    if bucket_app is not None:
        port = bucket_app.cfg_get_ftpport()

    server = ThreadedFTPServer(('', port), ftp_handler)
    if bucket_app is not None:
        bucket_app.server = server
    server.serve_forever()
    return server

def main():
    return 0

if __name__ == "__main__":
    main()
