from umcares.config import Config
from umcares.transport import connect

cfg = Config.load()
t = connect(cfg.remote, prefer="auto")
print("exports dir listing:")
r = t.run(f"ls -lh {cfg.remote.root}/exports/")
print(r.stdout)
