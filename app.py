"""Mosaic Photo-Editor - Streamlit entry point.

Run it with the app icon (launcher.py opens it in its own window), or for development:
    streamlit run app.py
"""
import os
import threading
import time
from pathlib import Path

import streamlit as st
from PIL import Image

from mosaic import APP_NAME, __version__, ai, history
from mosaic.ui.batch import batch_page
from mosaic.ui.common import session_id, styles
from mosaic.ui.sidebar import settings_panel
from mosaic.ui.upload import upload_page

ASSETS = Path(__file__).parent / "assets"


@st.cache_resource(show_spinner=False)
def _page_icon():
    try:
        return Image.open(ASSETS / "AppIcon.icns").convert("RGBA").resize((64, 64))
    except Exception:
        return None


@st.cache_resource(show_spinner=False)
def _startup():
    history.prune()
    if os.environ.get("MOSAIC_AI_PRELOAD", "1") != "0":
        ai.warm_up_async()                              # AI models load in the background, ready when needed
    idle = int(os.environ.get("MOSAIC_IDLE_EXIT", "0") or 0)
    if idle:
        threading.Thread(target=_idle_watchdog, args=(idle,), daemon=True).start()
    return True


def _idle_watchdog(idle_s: int):
    """Browser mode: stop the app when no browser tab has been connected for idle_s seconds."""
    from streamlit.runtime import Runtime
    last_seen = time.time()
    while True:
        time.sleep(15)
        try:
            n = Runtime.instance()._session_mgr.num_active_sessions()
        except Exception:
            return                                      # Streamlit internals changed: just keep running
        if n:
            last_seen = time.time()
        elif time.time() - last_seen > idle_s:
            os._exit(0)


st.set_page_config(page_title=APP_NAME, page_icon=_page_icon(), layout="wide")
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))
styles()
_startup()
session_id()

if st.session_state.get("_quitting"):
    st.info(f"{APP_NAME} has been closed. You can close this tab.")
    st.stop()

pages = [
    st.Page(upload_page, title="Upload", url_path="upload", default=True),
    st.Page(batch_page, title="Batch", url_path="batch"),
]
nav = st.navigation(pages, position="top")

with st.sidebar:
    settings_panel(nav.url_path)
    st.caption(f"{APP_NAME} {__version__}")

nav.run()
