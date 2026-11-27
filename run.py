import sys
import gc
import datetime as dt
from zoneinfo import ZoneInfo
from gitalertmanager import AlertManager
from gexProcessor import GexProcessor
from imageMerger import merge_images

def processmain():

    gxprocessor = GexProcessor()

    vh_buf, metadata = gxprocessor.process_volumehistoryrequest()
    bFlag = False
    diff = 0
    pcr_circle = None
    if not metadata:                     
        bFlag = False
    else:
        diff = float(metadata["PCR_DIFF"])
        bFlag = diff >= 0.03 or diff <= -0.03
        if diff > 0:
            pcr_circle="🔴"
        else:
            pcr_circle="🟢"

    if bFlag and diff != 0 and pcr_circle is not None:
        alertMgr = AlertManager()
        msg = f"PCR diff: {pcr_circle} {float(metadata['PCR_DIFF'])}, curr: {float(metadata['PCR_CURRENT']):.4f}, prev: {float(metadata['PCR_LAST']):.4f}"
        print(msg)
        alertMgr.send_chart_alert(msg)
    
    print("done")
    gc.collect()

if __name__ == '__main__':
    processmain()