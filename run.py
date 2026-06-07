# standard script structure
import os
import base64
import gc
from gitalertmanager import AlertManager
from gexProcessor import GexProcessor

def processmain():
    alertMgr = AlertManager()
    gxprocessor = GexProcessor()
    image_buffer = gxprocessor.processrequest()
    alertMgr.send_photo_alert(image_buffer)
    chart_image_base64 = base64.b64encode(image_buffer.getvalue()).decode('utf-8')
    image_buffer.close()
    print("done")
    del image_buffer,chart_image_base64, alertMgr, gxprocessor
    gc.collect()
    
if __name__ == '__main__':
    spy_data = processmain()
    
