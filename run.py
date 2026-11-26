import sys
import gc
import datetime as dt
from zoneinfo import ZoneInfo
from gitalertmanager import AlertManager
from gexProcessor import GexProcessor
from imageMerger import merge_images

def processmain():

    gxprocessor = GexProcessor()
    try:
        vh_buf, metadata = gxprocessor.process_volumehistoryrequest()
        if metadata:
            alertMgr = AlertManager()
            msg = f"PCR diff: {float(metadata['PCR_DIFF'])}, current: {float(metadata['PCR_CURRENT']):.4f}, prev: {float(metadata['PCR_LAST']):.4f}"
            print(msg)
            alertMgr.send_chart_alert(msg)
        print("done")

    finally:
        gc.collect()

if __name__ == '__main__':
    processmain()