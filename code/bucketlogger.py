#!/usr/bin/env python3

import sys, os
import logging, pyftpdlib.log
import bucketapp

LOG_FILE_NAME = "bucket_log.?.log"

logger = logging.getLogger("bucket")

bucket_app = None

def reconfig(bapp = None, lvl = logging.ERROR):
    # every time a main disk is inserted, we need to reconfigure the logger to log into a new file
    # plus, if the time is obtained, we can add the date to the log file's name

    global logger
    global bucket_app
    if bapp is not None and bucket_app is None:
        bucket_app = bapp

    logger.handlers = []
    handler = logging.StreamHandler()
    formatter = pyftpdlib.log.LogFormatter()
    formatter.PREFIX = "[%(levelname)1.1s %(asctime)s]"
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(lvl)

    if bucket_app is None or len(bucket_app.disks) <= 0:
        return

    logpath = os.path.join(bucket_app.disks[0], bucket_app.cfg_get_bucketname(), LOG_FILE_NAME.replace("?", bucket_app.get_date_str()))
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    fhandler = logging.FileHandler(logpath)
    logger.addHandler(fhandler)

def getLogger():
    global logger
    return logger
