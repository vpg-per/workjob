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

def utc_naive_to_eastern_naive(utc_naive: dt.datetime) -> dt.datetime:
    return utc_naive.replace(tzinfo=dt.timezone.utc).astimezone(EASTERN_TZ).replace(tzinfo=None)


def should_process_volumehistory(argv) -> bool:
    if len(argv) > 1:
        return True

    now_eastern = dt.datetime.now(EASTERN_TZ).time()
    return now_eastern >= VOLUME_HISTORY_CUTOFF


def processmain():
    alertMgr = AlertManager()
    gxprocessor = GexProcessor()
    gex_image_buffer = gxprocessor.process_gexrequest()
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