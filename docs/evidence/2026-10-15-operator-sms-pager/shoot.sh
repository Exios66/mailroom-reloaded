#!/bin/bash
# shoot.sh DIR NAME — headless-Chrome screenshot (set CHROME to the browser binary) of DIR/NAME.html, cropped to content
set -e
"${CHROME:-chromium}" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=520,2600 --screenshot="$1/$2.full.png" "file://$1/$2.html" 2>/dev/null
python3 - "$1/$2" <<'PY'
import os, sys
from PIL import Image, ImageChops
b = sys.argv[1]
im = Image.open(b + ".full.png").convert("RGB")
bg = Image.new("RGB", im.size, im.getpixel((0, im.size[1] - 1)))
box = ImageChops.difference(im, bg).getbbox()
im.crop((0, 0, im.size[0], min(im.size[1], box[3] + 56))).save(b + ".png")
os.remove(b + ".full.png")
PY
