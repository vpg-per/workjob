import requests
import time
import os
from dotenv import load_dotenv
from alertManager import AlertManager

load_dotenv()

def refresh_sector_chart():

    OWNER = os.getenv("GH_OWNER")
    REPO = os.getenv("GH_REPO")
    WORKFLOW_FILE = "run_analysis.yml"   # or the numeric workflow ID
    BRANCH = "main"
    GITHUB_TOKEN = os.getenv("GH_TOKEN")           # PAT with 'repo' scope (or fine-grained: actions:write)

    url = f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {
        "ref": BRANCH,
        "inputs": {
            "run_sector_chart": "true"   # must be a string, even though the yaml declares type: boolean
        },
    }

    resp = requests.post(url, headers=headers, json=payload)

    if resp.status_code == 204:
        print("Workflow dispatched successfully.")
    else:
        print(f"Failed ({resp.status_code}): {resp.text}")
    
    time.sleep(5)
    
    altMgr = AlertManager()
    altMgr.DeleteSectorChartFromDB()
    time.sleep(5)
    image_buffer = altMgr.GetSectorChartFromDB()
    for i in range(1, 10):
        if image_buffer is None:
           time.sleep(8)
           image_buffer = altMgr.GetSectorChartFromDB()
        else:
            break
            

    del altMgr
    return
	
	