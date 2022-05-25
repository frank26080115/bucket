#!/usr/bin/env python3

import os, sys, time, datetime, glob, fnmatch

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler, DTPHandler
from pyftpdlib.servers import FTPServer
from pyftpdlib.filesystems import AbstractedFS, FilesystemError
from pyftpdlib._compat import unicode, u, PY3

import logging

import bucketapp, bucketio, bucketutils, bucketcopy, bucketlogger

logger = bucketlogger.getLogger()
logger2 = logging.getLogger('pyftpdlib')

bucket_app = None

class BucketFtpHandler(FTPHandler):
    """
    Mostly just event handlers
    """

    def on_connect(self):
        """Called when client connects, *before* sending the initial
        220 reply.
        """
        logger2.info("BucketFtpHandler - on_connect")

    def on_disconnect(self):
        global bucket_app
        """Called when connection is closed."""
        logger2.info("BucketFtpHandler - on_disconnect")
        if bucket_app is None:
            return
        bucket_app.on_nonactivity()

    def on_login(self, username):
        """Called on user login."""
        logger2.info("BucketFtpHandler - on_login: " + username)

    def on_login_failed(self, username, password):
        """Called on failed login attempt.
        At this point client might have already been disconnected if it
        failed too many times.
        """
        logger2.info("BucketFtpHandler - on_login_failed: " + username)

    def on_logout(self, username):
        """Called when user "cleanly" logs out due to QUIT or USER
        issued twice (re-login). This is not called if the connection
        is simply closed by client.
        """
        logger2.info("BucketFtpHandler - on_logout: " + username)

    def on_file_sent(self, file):
        """Called every time a file has been successfully sent.
        "file" is the absolute name of the file just being sent.
        """
        logger2.info("BucketFtpHandler - on_file_sent: " + file)

    def on_file_received(self, file):
        """Called every time a file has been successfully received.
        "file" is the absolute name of the file just being received.
        """
        global bucket_app
        logger2.info("BucketFtpHandler - on_file_received: " + file)
        if bucket_app is None:
            return
        bucket_app.on_nonactivity()
        bucket_app.on_file_received(file)

    def on_incomplete_file_sent(self, file):
        """Called every time a file has not been entirely sent.
        (e.g. ABOR during transfer or client disconnected).
        "file" is the absolute name of that file.
        """
        logger2.info("BucketFtpHandler - on_incomplete_file_sent: " + file)

    def on_incomplete_file_received(self, file):
        """Called every time a file has not been entirely received
        (e.g. ABOR during transfer or client disconnected).
        "file" is the absolute name of that file.
        """
        bucket_app
        logger2.info("BucketFtpHandler - on_incomplete_file_received: " + file)
        if bucket_app is None:
            return
        bucket_app.on_nonactivity()
        bucket_app.on_missed_file(file, forced = True)
        try:
            if os.path.isfile(file):
                os.remove(file)
            if os.path.isfile(file + ".washere"):
                os.remove(file + ".washere")
        except:
            pass

class BucketFtpFilesystem(AbstractedFS):

    def ftp2fs(self, ftppath):
        global bucket_app
        x = AbstractedFS.ftp2fs(self, ftppath)
        bucket_root = None
        if bucket_app is not None:
            bucket_root = bucket_app.get_root()
        if bucket_root is None:
            if os.name != "nt":
                return "/tmp"
            else:
                return "C:\\Sandbox"
        if x[0] != '/' and x[1] != ':':
            x = bucket_root.rstrip(os.path.sep) + os.path.sep + x
        return x

    def fs2ftp(self, fspath):
        global bucket_app
        assert isinstance(fspath, unicode), fspath
        bucket_root = None
        if bucket_app is not None:
            bucket_root = bucket_app.get_root()
        bucket_root = bucket_app.get_root()
        if bucket_root is None:
            # uhhhh no place to put the file, the file write will fail if disk space is not available
            if os.name != "nt":
                bucket_root = "/tmp"
            else:
                bucket_root = "C:\\Sandbox"
        if os.path.isabs(fspath):
            p = os.path.normpath(fspath)
        else:
            p = os.path.normpath(os.path.join(bucket_root, fspath))
        if not self.validpath(p):
            return u('/')
        p = p.replace(os.path.sep, "/")
        p = p[len(bucket_root):]
        if not p.startswith('/'):
            p = '/' + p
        return p

    def open(self, filename, mode):
        global bucket_app
        assert isinstance(filename, unicode), filename
        if bucket_app is not None:
            bucket_app.cpu_highfreq()
        if "w" in mode and (bucket_app is None or bucket_app.still_has_space() == False):
            raise FilesystemError("Out of disk space")
        if "w" in mode:
            bucket_app.on_before_open(filename)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        return open(filename, mode)

    def listdir(self, path):
        assert isinstance(path, unicode), path
        lst = os.listdir(path)
        i = 0
        # convert all the names from date-coded to non-date-coded before returning it to the FTP client
        while i < len(lst):
            if "." in lst:
                if lst[i].endswith(".washere"):
                    s = lst[i]
                    lst[i] = s[0:-8]
            i += 1
        return lst

    def listdirinfo(self, path):
        return self.listdir(path)

    def remove(self, path):
        if os.path.isfile(path + ".washere"):
            os.remove(path + ".washere")

    def chmod(self, path, mode):
        assert isinstance(path, unicode), path
        if not hasattr(os, 'chmod'):
            raise NotImplementedError
        if os.path.isfile(path + ".washere"):
            return os.chmod(read_washere_file(path), mode)
        return os.chmod(path, mode)

    def stat(self, path):
        return callfunc_for_washere(path, os.stat)

    def utime(self, path, timeval):
        if os.path.isfile(path + ".washere"):
            return os.utime(read_washere_file(path), (timeval, timeval))
        return os.utime(path, (timeval, timeval))

    if hasattr(os, 'lstat'):
        def lstat(self, path):
            """Like stat but does not follow symbolic links."""
            # on python 2 we might also get bytes from os.lisdir()
            # assert isinstance(path, unicode), path
            return callfunc_for_washere(path, os.lstat)
    else:
        lstat = stat

    def isfile(self, path):
        assert isinstance(path, unicode), path
        return callfunc_for_washere(path, os.path.isfile)

    def isdir(self, path):
        assert isinstance(path, unicode), path
        return os.path.isdir(path)

    def getsize(self, path):
        assert isinstance(path, unicode), path
        return callfunc_for_washere(path, os.path.getsize)

    def getmtime(self, path):
        assert isinstance(path, unicode), path
        return callfunc_for_washere(path, os.path.getmtime)

    def realpath(self, path):
        assert isinstance(path, unicode), path
        return callfunc_for_washere(path, os.path.realpath)

    def lexists(self, path):
        assert isinstance(path, unicode), path
        return callfunc_for_washere(path, os.path.lexists)

class BucketDtpHandler(ThrottledDTPHandler):
    """
    Using the ThrottledDTPHandler skeleton so we can get a signal for every packet transferred
    """

    def use_sendfile(self):
        return False

    def recv(self, buffer_size):
        global bucket_app
        chunk = DTPHandler.recv(self, buffer_size)
        if bucket_app is not None:
            bucket_app.on_activity()
        return chunk

    def send(self, data):
        global bucket_app
        num_sent = DTPHandler.send(self, data)
        if bucket_app is not None:
            bucket_app.on_activity()
        return num_sent

class BucketAuthorizer(DummyAuthorizer):
    def get_home_dir(self, username):
        global bucket_app
        if bucket_app is not None:
            return bucket_app.get_root()
        else:
            return DummyAuthorizer.get_home_dir(self, username)

def follow_washere(path):
    if os.path.isfile(path + ".washere"):
        npath = read_washere_file(path)
        if os.path.isfile(npath):
            return npath
        # if the file wasn't found, maybe the disk moved to another mount point?
        disks = bucketutils.get_mounted_disks()
        for disk in disks:
            maindir, fname = os.path.split(npath)
            dir3, dir2 = os.path.split(maindir)
            g = glob.glob(os.path.join(disk, "**") + os.path.sep + dir2 + os.path.sep + fname, recursive=True)
            if len(g) > 0:
                return g[0]
        return npath
    return path

def callfunc_for_washere(path, func):
    return func(follow_washere(path))

def read_washere_file(path):
    s = ""
    with open(path + ".washere", "r") as f:
        s = f.read().strip()
    return s

def start_ftp_server(running_app = None):
    global bucket_app
    if bucket_app is None and running_app is not None:
        bucket_app = running_app

    authorizer = BucketAuthorizer()
    username = "user"
    password = "1234567890"
    if bucket_app is not None:
        username = bucket_app.cfg_get_ftpusername()
        password = bucket_app.cfg_get_ftppassword()
    authorizer.add_user(username, password, os.getcwd(), perm='elradfmwMT')
    #authorizer.add_anonymous(os.getcwd())

    print("FTP username \"%s\" password \"%s\"" % (username, password))

    ftp_handler               = BucketFtpHandler
    ftp_handler.authorizer    = authorizer
    ftp_handler.dtp_handler   = BucketDtpHandler
    ftp_handler.abstracted_fs = BucketFtpFilesystem

    port = 2133
    if bucket_app is not None:
        port = bucket_app.cfg_get_ftpport()

    print("FTP port %d" % port)

    server = FTPServer((bucketutils.get_wifi_ip(), port), ftp_handler)
    if bucket_app is not None:
        print("running FTP server with bucket app")
        bucket_app.ftp_server = server
        bucket_app.ftp_start()
    else:
        print("running FTP server with serve_forever")
        server.serve_forever()

def main():
    start_ftp_server()
    return 0

if __name__ == "__main__":
    main()
