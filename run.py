import os
import sys
import base64
import gc
import datetime as dt
from gitalertmanager import AlertManager
from gexProcessor import GexProcessor
from zoneinfo import ZoneInfo

EASTERN_TZ = ZoneInfo("America/New_York")
VOLUME_HISTORY_CUTOFF = dt.time(10, 30)

def should_process_volumehistory(argv) -> bool:
    if len(argv) > 1:
        return True

    now_eastern = dt.datetime.now(EASTERN_TZ).time()
    return now_eastern >= VOLUME_HISTORY_CUTOFF

def should_send_gexalert() -> bool:
    now_eastern = dt.datetime.now(EASTERN_TZ).time()
    current_minute = now_eastern.minute
    return current_minute > 15 and current_minute < 30

def processmain():
    alertMgr = AlertManager()
    gxprocessor = GexProcessor()
    gex_image_buffer = gxprocessor.process_gexrequest()
    if should_send_gexalert():
        alertMgr.send_photo_alert(gex_image_buffer)
    gex_image_buffer.close()

    if should_process_volumehistory(sys.argv):
        vh_image_buffer = gxprocessor.process_volumehistoryrequest()
        alertMgr.send_photo_alert(vh_image_buffer)
        vh_image_buffer.close()
        del vh_image_buffer

    print("done")
    del gex_image_buffer, alertMgr, gxprocessor
    gc.collect()

if __name__ == '__main__':
    spy_data = processmain()