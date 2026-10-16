import logging
import logging.handlers
import os
from pathlib import Path

import uvicorn

debug = os.environ.get("HIDAGENT_DEBUG") == "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("hidagent").setLevel(logging.DEBUG if debug else logging.INFO)
if debug:  # keep the log of every command sent to the board, so a miss can be traced afterwards
    log_file = Path.home() / ".hidagent" / "hidagent.log"
    log_file.parent.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
uvicorn.run("hidagent.app:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8890)))
