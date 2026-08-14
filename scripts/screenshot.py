"""Regenerate docs/screenshot.png.

Real Qt platform rather than offscreen, because the headless platform has no
fonts and renders every label as tofu boxes.

Two deliberate safety choices:

* A throwaway profile with only the SDE copied in, so the shot carries no
  tokens, character names or asset locations. The previous screenshot showed
  a corp's structure list.
* w.grab(), which renders this widget tree and nothing else. Grabbing the
  desktop and cropping would include the title bar, but it captures whatever
  is actually frontmost -- on the first attempt that was a live EVE client,
  complete with fleet names and local chat.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time

real_cache = pathlib.Path(os.environ['LOCALAPPDATA']) / 'eve-strait' / 'cache'
profile = pathlib.Path(tempfile.mkdtemp())
(profile / 'eve-strait' / 'cache').mkdir(parents=True)
for csv in real_cache.glob('*.csv'):
    shutil.copy2(csv, profile / 'eve-strait' / 'cache' / csv.name)
os.environ['LOCALAPPDATA'] = str(profile)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, Qt
app = QApplication([])
from eve_strait.ui import theme
theme.apply_theme(app)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        QCoreApplication.processEvents(); time.sleep(0.005)

from eve_strait.ui.main_window import MainWindow
w = MainWindow()
w.resize(1660, 1000)
w.show()
for _ in range(300):
    pump(0.1)
    if w.map_view is not None: break
pump(1.5)

u = w.universe
idx = w.ship.ship_combo.findText('Ark', Qt.MatchFlag.MatchStartsWith)
if idx >= 0:
    w.ship.ship_combo.setCurrentIndex(idx)
# All fives. The defaults are 4s, which cap an Ark at 9.00 ly and turn the
# 9.23 ly leg to M-OEE8 into an out-of-range error in red.
for sp in (w.ship.sp_jdc, w.ship.sp_jdo, w.ship.sp_jfc, w.ship.sp_jf):
    sp.setValue(5)
pump(0.5)

w.route.add_system(u.by_name('Jita').id)
w.route.add_system(u.by_name('M-OEE8').id)
pump(1.2)
w.route._sections[0].set_expanded(True)
pump(0.3)
w.route.wp_list.setCurrentRow(1)
pump(1.2)

# Zoom to the leg: the default view opens on the whole of New Eden, which
# leaves the jump range circle a few pixels across.
mv = w.map_view
a, b = mv._pos[u.by_name('Jita').id], mv._pos[u.by_name('M-OEE8').id]
mv.resetTransform()
mv.scale(13.0, 13.0)
mv.centerOn((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
pump(1.5)

shot = w.grab()
out = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'screenshot.png'
shot.save(str(out))
print('saved', out, shot.width(), 'x', shot.height())
print('range:', w.ship.lbl_range.text(), '| totals:', w.route.totals.text()[:95])
w.close()
