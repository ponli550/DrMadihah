from pathlib import Path
from umcares.config import Config
from umcares.transport import connect

cfg = Config.load()
t = connect(cfg.remote, prefer="auto")
remote = "/Users/irpan/Projects/DrMadihah/exports/UMCares_RU2025-T323A_v13.mp4"
local = Path(".umcares/check_v13.mp4").resolve()
if local.exists():
    local.unlink()
print(f"pulling {remote} -> {local}")
t.pull(remote, local)
print("pulled", local.stat().st_size)
