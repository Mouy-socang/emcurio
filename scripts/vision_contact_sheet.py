"""Derived API preview only; archived source photographs remain unchanged."""
import sys
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
out=Path(sys.argv[1]);paths=[Path(x) for x in sys.argv[2:]]
canvas=Image.new('RGB',(1200,600 if len(paths)<=2 else 1200),'white');draw=ImageDraw.Draw(canvas)
for i,p in enumerate(paths):
 with Image.open(p) as im:
  im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((580,560))
  x=(i%2)*600;y=(i//2)*600;canvas.paste(im,(x+(600-im.width)//2,y+35+(560-im.height)//2));draw.text((x+12,y+10),'im'+str(i+1),fill='black')
canvas.save(out,'JPEG',quality=85)
