#!/usr/bin/env python3

import logging, pyftpdlib.log
import bucketapp

LOG_FILE_NAME = "bucket_log.?.log"

logger = logging.getLogger("bucket")

def reconfig(lvl = logging.ERROR):
    # every time a main disk is inserted, we need to reconfigure the logger to log into a new file
    # plus, if the time is obtained, we can add the date to the log file's name

    global logger
    app = bucketapp.bucket_app

    logger.handlers = []
    handler = logging.StreamHandler()
    formatter = pyftpdlib.log.LogFormatter()
    formatter.PREFIX = "[%(levelname)1.1s %(asctime)s]"
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(lvl)

    if app is None or len(app.disks) <= 0:
        return

    logpath = os.path.join(app.disks[0], app.cfg_get_bucketname(), LOG_FILE_NAME.replace("?", app.get_date_str()))
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    fhandler = logging.FileHandler(logpath)
    logger.addHandler(fhandler)

def getLogger():
    global logger
    return logger
