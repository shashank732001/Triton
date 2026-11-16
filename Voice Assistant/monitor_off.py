"""
Desktop-shortcut version of "go to sleep": switch the monitor off over DDC/CI and keep the
PC awake and unlocked. Stays running until the mouse/keyboard is touched, then switches the
monitor back on and exits.
"""

import time

from monitor import MonitorControl

time.sleep(0.7)     # so releasing the mouse click doesn't count as "input" straight away
mc = MonitorControl()
mc.off()
while mc.is_off:
    time.sleep(0.5)
