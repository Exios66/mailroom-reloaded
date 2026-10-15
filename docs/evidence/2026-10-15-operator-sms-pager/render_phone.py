"""Render pager texts (evidence/messages.json) as an SMS thread in a phone frame (HTML)."""
import html
import json
import sys

src, out, title, picks = sys.argv[1], sys.argv[2], sys.argv[3], [int(x) for x in sys.argv[4].split(",")]
with open(src) as fh:
    msgs = json.load(fh)["messages"]
bubbles = []
for i in picks:
    m = msgs[i]
    bubbles.append(f'<div class="ts">Today {html.escape(m["at"].rsplit(":", 1)[0] + " " + m["at"].split()[-1])}</div>'
                   f'<div class="b">{html.escape(m["body"])}</div>')
clock = msgs[picks[-1]]["at"].split(":")[0] + ":" + msgs[picks[-1]]["at"].split(":")[1]
page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#e9ecef;font-family:-apple-system,"Segoe UI","Noto Sans",Helvetica,Arial,"Noto Color Emoji",sans-serif}}
.wrap{{padding:28px;display:flex;flex-direction:column;align-items:center;gap:14px}}
.cap{{font-size:15px;color:#333;font-weight:600;max-width:420px;text-align:center}}
.phone{{width:390px;background:#000;border-radius:54px;padding:14px;box-shadow:0 18px 40px rgba(0,0,0,.25)}}
.screen{{background:#fff;border-radius:42px;overflow:hidden}}
.status{{display:flex;justify-content:space-between;padding:14px 30px 4px;font-size:15px;font-weight:600}}
.hdr{{text-align:center;padding:6px 0 10px;border-bottom:1px solid #ddd;background:#f7f7f7}}
.av{{width:46px;height:46px;border-radius:50%;background:linear-gradient(#8e9aaf,#6c7a92);color:#fff;
 display:flex;align-items:center;justify-content:center;margin:0 auto 4px;font-weight:700;font-size:18px}}
.nm{{font-size:13px;color:#111}} .sub{{font-size:11px;color:#888}}
.thread{{padding:10px 14px 22px;display:flex;flex-direction:column;gap:6px}}
.ts{{text-align:center;font-size:11px;color:#8e8e93;margin-top:8px}}
.b{{align-self:flex-start;max-width:86%;background:#e9e9eb;color:#000;border-radius:18px;padding:9px 13px;
 font-size:15px;line-height:1.32;white-space:pre-wrap;word-wrap:break-word}}
.bar{{border-top:1px solid #eee;padding:10px 14px 18px;color:#bbb;font-size:14px}}
</style></head><body><div class="wrap"><div class="cap">{html.escape(title)}</div>
<div class="phone"><div class="screen"><div class="status"><span>{clock}</span><span>●●●● 5G ▮</span></div>
<div class="hdr"><div class="av">M</div><div class="nm">Mailroom Pager</div><div class="sub">Text Message · SMS</div></div>
<div class="thread">{''.join(bubbles)}</div><div class="bar">Text Message</div></div></div></div></body></html>"""
with open(out, "w") as fh:
    fh.write(page)
