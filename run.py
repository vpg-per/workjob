import sys
import gc
import datetime as dt
from zoneinfo import ZoneInfo

from gitalertmanager import AlertManager
from gexProcessor import GexProcessor
from imageMerger import merge_images

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
    return 15 < current_minute < 30


def processmain():
    want_gex = should_send_gexalert()
    want_volume_history = should_process_volumehistory(sys.argv)

    if not (want_gex or want_volume_history):
        print("nothing to send")
        return

    gxprocessor = GexProcessor()
    buffers = []
    merged = None

    try:
        # Only launch a browser for charts we actually intend to send.
        if want_gex:
            gex_buf = gxprocessor.process_gexrequest()
            if gex_buf is not None:
                buffers.append(gex_buf)

        if want_volume_history:
            vh_buf = gxprocessor.process_volumehistoryrequest()
            if vh_buf is not None:
                buffers.append(vh_buf)

        if not buffers:
            print("no charts captured; nothing sent")
            return

        merged = merge_images(buffers, direction="horizontal")
        if merged is None:
            print("merge produced no image; nothing sent")
            return

        alertMgr = AlertManager()
        alertMgr.send_photo_alert(merged)
        print("done")

    finally:
        for buf in buffers:
            buf.close()
        if merged is not None:
            merged.close()
        gc.collect()


if __name__ == '__main__':
    processmain()