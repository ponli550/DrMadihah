import json
from umcares.config import Config
from umcares.transport import connect
from umcares.premiere import Premiere

cfg = Config.load()
t = connect(cfg.remote, prefer="auto")
p = Premiere(t, cfg.remote)

paths = [
    "/Users/irpan/Projects/DrMadihah/assets/cards/card_s7_statistik.mp4",
    "/Users/irpan/Projects/DrMadihah/assets/vo/s7_statistik.wav",
]

jsx = f"""
(function(){{
  try {{
    var targets = {json.dumps(paths)};
    var find = function(bin, target){{
      var kids = bin.children;
      if (!kids) return null;
      for (var i = 0; i < kids.numItems; i++){{
        var it = kids[i];
        var mp = "";
        try {{ mp = it.getMediaPath ? it.getMediaPath() : ""; }} catch(e){{}}
        if (mp && mp === target) return it;
        if (it.children && it.children.numItems > 0) {{
          var r = find(it, target);
          if (r) return r;
        }}
      }}
      return null;
    }};
    var out = {{}};
    for (var j = 0; j < targets.length; j++){{
      var target = targets[j];
      var it = find(app.project.rootItem, target);
      if (!it) {{
        out[target] = {{found: false}};
      }} else {{
        try {{ it.refreshMedia(); out[target] = {{found: true, refreshed: true}}; }}
        catch(e) {{ out[target] = {{found: true, refreshed: false, error: String(e)}}; }}
      }}
    }}
    return JSON.stringify(out);
  }} catch(e){{ return "ERR " + String(e); }}
}})()
"""
res = p.eval_jsx(jsx, timeout=60)
print(res)
