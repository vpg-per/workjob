import io
from typing import Optional

from gexProcessor import GexProcessor
from gitalertmanager import AlertManager

# Streamlit strips the numeric prefix from pages/2_Sector_Performance.py,
# so the page is served at /Sector_Performance (same pattern as Volume_History).
SECTOR_URL = "https://optionexp.streamlit.app/~/+/Sector_Performance"


class SectorProcessor(GexProcessor):

    def capture_sectorchart(self) -> Optional[io.BytesIO]:
        """Returns the chart PNG as a rewound BytesIO, or None if capture failed."""
        return self._capture_chart_from_url(SECTOR_URL)

    def process_sectorrequest(self) -> Optional[io.BytesIO]:
        img_buf = self.capture_sectorchart()
        alertMgr = AlertManager()
        alertMgr.send_photo_alert(img_buf)
        print("done")


if __name__ == "__main__":
    SectorProcessor().process_sectorrequest()
    
