"""Thai for the result lines of the Background, Dock and Bootup sections (msg.bg.*, msg.dock.*, msg.boot.*).

Forge Settings shows what forge-appearance-background, forge-appearance-dock, forge-appearance-boot and the root
helper forge-boot-apply print, and translates each line with tr() (forgei18n.I18N_JS). These tests run that same
runtime in node on real lines copied from the programs (with sample values filled in), and check the tables.
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
I18N = os.path.join(ROOT, "files/system/usr/share/forge/i18n")
PREFIXES = ("msg.bg.", "msg.dock.", "msg.boot.")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

sys.path.insert(0, LIB)

HINT_EN = ("If your panel and desktop are gone, open a terminal (Ctrl+Alt+T) and run: systemctl --user start "
           "plasma-plasmashell.service (or log out and back in), then apply again")
HINT_TH = ("ถ้าแผงและเดสก์ท็อปหายไป ให้เปิดเทอร์มินัล (Ctrl+Alt+T) แล้วรัน: systemctl --user start "
           "plasma-plasmashell.service (หรือออกจากระบบแล้วเข้าใหม่) จากนั้นนำไปใช้อีกครั้ง")

# (English line as the program prints it, the Thai that Forge Settings should show)
BACKGROUND = [
    ("Background applied: picture on all screens.", "ใช้พื้นหลังแล้ว: รูปภาพบนทุกจอ"),
    ("Background applied: slideshow on screen DP-1.", "ใช้พื้นหลังแล้ว: สไลด์โชว์บนจอ DP-1"),
    ("Background applied: solid color on all screens.", "ใช้พื้นหลังแล้ว: สีพื้นบนทุกจอ"),
    ("Background applied: gradient on screen HDMI-A-1.", "ใช้พื้นหลังแล้ว: ไล่สีบนจอ HDMI-A-1"),
    ("Background applied: Wallpaper Engine: Ocean Waves on screen DP-2.",
     "ใช้พื้นหลังแล้ว: Wallpaper Engine: Ocean Waves บนจอ DP-2"),
    ("Background applied: Wallpaper Engine: Neon City on all screens.",
     "ใช้พื้นหลังแล้ว: Wallpaper Engine: Neon City บนทุกจอ"),
    ("Background applied: video on all screens.\nThe crash guard runs in the background (without systemd-run).",
     "ใช้พื้นหลังแล้ว: วิดีโอบนทุกจอ\nตัวป้องกันการล่มทำงานอยู่เบื้องหลัง (โดยไม่ใช้ systemd-run)"),
    ("Background applied: video on screen DP-1.\n"
     "Note: last time the desktop crashed with that background, so Forge switched back.\n"
     "The crash guard is off: plasmashell is not running as a systemd service here.",
     "ใช้พื้นหลังแล้ว: วิดีโอบนจอ DP-1\n"
     "หมายเหตุ: ครั้งก่อนเดสก์ท็อปล่มเพราะพื้นหลังนั้น Forge จึงเปลี่ยนกลับให้\n"
     "ตัวป้องกันการล่มปิดอยู่: ที่นี่ plasmashell ไม่ได้ทำงานเป็นบริการของ systemd"),
    ("Nothing changed: choose a picture first.", "ไม่มีอะไรเปลี่ยน: เลือกรูปภาพก่อน"),
    ("Nothing changed: choose a Wallpaper Engine wallpaper first.",
     "ไม่มีอะไรเปลี่ยน: เลือกภาพพื้นหลังของ Wallpaper Engine ก่อน"),
    ("Not applied: unknown choice for fill. Open Forge Settings and save the Background section again.",
     "ยังไม่ได้นำไปใช้: ไม่รู้จักตัวเลือกของ fill เปิด Forge Settings แล้วบันทึกส่วนพื้นหลังอีกครั้ง"),
    ("Not applied: that file is not a video.", "ยังไม่ได้นำไปใช้: ไฟล์นั้นไม่ใช่วิดีโอ"),
    ("Not applied: scene wallpapers can crash the desktop. Turn on 'Allow scene wallpapers' first.",
     "ยังไม่ได้นำไปใช้: ภาพพื้นหลังแบบฉากอาจทำให้เดสก์ท็อปล่ม เปิด 'อนุญาตภาพพื้นหลังแบบฉาก' ก่อน"),
    ("Not applied: Plasma did not accept the change (org.kde.Error: something broke).",
     "ยังไม่ได้นำไปใช้: Plasma ไม่ยอมรับการเปลี่ยนแปลง (org.kde.Error: something broke)"),
    ("Not applied: Plasma did not accept the change.", "ยังไม่ได้นำไปใช้: Plasma ไม่ยอมรับการเปลี่ยนแปลง"),
    ("Not applied: /home/u/.local/share/forge/appearance/generated is not a private folder of yours, so nothing "
     "was changed.",
     "ยังไม่ได้นำไปใช้: /home/u/.local/share/forge/appearance/generated ไม่ใช่โฟลเดอร์ส่วนตัวของคุณ "
     "จึงไม่ได้เปลี่ยนอะไร"),
    ("Not applied: the desktop is locked (widgets are locked). Unlock widgets, then try again.",
     "ยังไม่ได้นำไปใช้: เดสก์ท็อปถูกล็อกอยู่ (วิดเจ็ตถูกล็อก) ปลดล็อกวิดเจ็ตแล้วลองใหม่"),
    ("Not applied: no screen called DP-3 is connected. Check the connector name.",
     "ยังไม่ได้นำไปใช้: ไม่มีจอชื่อ DP-3 ต่ออยู่ ตรวจชื่อช่องต่ออีกครั้ง"),
    ("Not applied: Plasma gave no answer.", "ยังไม่ได้นำไปใช้: Plasma ไม่ตอบกลับ"),
    ("Not applied: Forge is restarting the desktop after a crash. Try again in a minute.",
     "ยังไม่ได้นำไปใช้: Forge กำลังเริ่มเดสก์ท็อปใหม่หลังจากล่ม ลองใหม่ในอีกหนึ่งนาที"),
    ("Not applied: the input has no Background values.", "ยังไม่ได้นำไปใช้: ข้อมูลที่ส่งมาไม่มีค่าของส่วนพื้นหลัง"),
    # Notes under the lists (options)
    ("Showing the first 200 wallpapers from your Steam library.", "แสดงภาพพื้นหลัง 200 ภาพแรกจากคลัง Steam ของคุณ"),
    ("12 wallpapers found in your Steam library.", "พบภาพพื้นหลัง 12 ภาพในคลัง Steam ของคุณ"),
    ("Wallpaper Engine support is not included in this build.", "รุ่นนี้ไม่มีการรองรับ Wallpaper Engine"),
    ("No connected screens found. Type the connector name.", "ไม่พบจอที่ต่ออยู่ พิมพ์ชื่อช่องต่อเอง"),
]

DOCK = [
    ("Dock: Floating clean applied to 1 Plasma panel.", "Dock: ใช้แบบลอยเรียบกับแผง Plasma 1 แผงแล้ว"),
    ("Dock: Floating clean applied to 2 Plasma panels.", "Dock: ใช้แบบลอยเรียบกับแผง Plasma 2 แผงแล้ว"),
    ("Dock: Krema started with your settings; your Plasma panel now hides until you touch its edge.",
     "Dock: เริ่ม Krema ด้วยการตั้งค่าของคุณแล้ว และแผง Plasma ของคุณจะซ่อนจนกว่าเมาส์จะแตะขอบของมัน"),
    ("Dock: Krema started with your settings; your Plasma panel is left as it is.",
     "Dock: เริ่ม Krema ด้วยการตั้งค่าของคุณแล้ว และแผง Plasma ของคุณคงไว้ตามเดิม"),
    ("Dock: your Plasma panel is back to how it was before Forge changed it.",
     "Dock: แผง Plasma ของคุณกลับเป็นแบบเดิมก่อนที่ Forge จะเปลี่ยนแล้ว"),
    ("Dock: Forge has not changed your Plasma panel, so there is nothing to restore.",
     "Dock: Forge ยังไม่เคยเปลี่ยนแผง Plasma ของคุณ จึงไม่มีอะไรต้องคืนค่า"),
    ("Dock: Plasma is not running (no plasmashell in this session), so nothing was changed. Apply again from "
     "inside a Plasma session.",
     "Dock: Plasma ไม่ได้ทำงานอยู่ (ไม่มี plasmashell ในเซสชันนี้) จึงไม่ได้เปลี่ยนอะไร "
     "นำไปใช้อีกครั้งจากในเซสชัน Plasma"),
    ("Dock: Plasma refused the panel change (dbus-send did not answer within 20 seconds). If your widgets are "
     "locked, unlock them and apply again.",
     "Dock: Plasma ไม่ยอมเปลี่ยนแผง (dbus-send ไม่ตอบภายใน 20 วินาที) ถ้าวิดเจ็ตถูกล็อกอยู่ "
     "ให้ปลดล็อกแล้วนำไปใช้อีกครั้ง"),
    ("Dock: plasmashell could not be stopped for the backup: Failed to stop unit. " + HINT_EN + ".",
     "Dock: หยุด plasmashell เพื่อสำรองข้อมูลไม่ได้: Failed to stop unit " + HINT_TH),
    ("Dock: the backup is done, but your panel and desktop (plasmashell) did not come back. " + HINT_EN + ".",
     "Dock: สำรองข้อมูลเสร็จแล้ว แต่แผงและเดสก์ท็อปของคุณ (plasmashell) ไม่กลับมา " + HINT_TH),
    ("Dock: the dock backup in /home/u/.local/share/forge/appearance/dock-backup/first is damaged, so nothing was "
     "changed. Rename or remove that folder (for example: mv /home/u/.local/share/forge/appearance/dock-backup/first "
     "/home/u/.local/share/forge/appearance/dock-backup/first.broken) and apply again; Forge then backs up your "
     "panel as it is now.",
     "Dock: ข้อมูลสำรองของ Dock ใน /home/u/.local/share/forge/appearance/dock-backup/first เสียหาย "
     "จึงไม่ได้เปลี่ยนอะไร เปลี่ยนชื่อหรือลบโฟลเดอร์นั้น (เช่น mv "
     "/home/u/.local/share/forge/appearance/dock-backup/first "
     "/home/u/.local/share/forge/appearance/dock-backup/first.broken) แล้วนำไปใช้อีกครั้ง "
     "จากนั้น Forge จะสำรองแผงของคุณตามที่เป็นอยู่ตอนนี้"),
    ("Dock: /home/u/.config/kremarc is a link to /home/u/dots/kremarc, which does not exist, so Forge left it "
     "alone. Fix or remove the link and apply again.",
     "Dock: /home/u/.config/kremarc เป็นลิงก์ไปยัง /home/u/dots/kremarc ซึ่งไม่มีอยู่ Forge จึงไม่ได้แตะต้อง "
     "แก้หรือลบลิงก์นั้นแล้วนำไปใช้อีกครั้ง"),
    ("Dock: Krema is not installed in this Forge OS image, so the Animated style can't run. Nothing was changed. "
     "Choose Floating clean, or see README (Dock styles) to add Krema.",
     "Dock: อิมเมจ Forge OS นี้ไม่ได้ติดตั้ง Krema แบบเคลื่อนไหวจึงใช้ไม่ได้ ไม่มีอะไรเปลี่ยน เลือกแบบลอยเรียบ "
     "หรือดู README (Dock styles) เพื่อเพิ่ม Krema"),
    ("Dock: the desktop answered too slowly, so Forge stopped before running systemctl. Apply again in a moment.",
     "Dock: เดสก์ท็อปตอบช้าเกินไป Forge จึงหยุดก่อนรัน systemctl นำไปใช้อีกครั้งในอีกสักครู่"),
    ("Dock: systemd-run is not installed, so nothing was changed.",
     "Dock: systemd-run ไม่ได้ติดตั้งไว้ จึงไม่ได้เปลี่ยนอะไร"),
    ("Dock: a file could not be written (Permission denied).", "Dock: เขียนไฟล์ไม่ได้ (Permission denied)"),
    ("Dock: could not read the settings (expected JSON on stdin), so nothing was changed.",
     "Dock: อ่านการตั้งค่าไม่ได้ (ต้องเป็น JSON ทาง stdin) จึงไม่ได้เปลี่ยนอะไร"),
]

BOOT = [
    ("Desktop splash: applied. You see it at your next login.\n"
     "Lock screen: applied. You see it the next time the screen locks.\n"
     "Asking for an admin password to change the boot splash and login screen.",
     "หน้าจอโหลดเดสก์ท็อป: นำไปใช้แล้ว จะเห็นเมื่อเข้าสู่ระบบครั้งถัดไป\n"
     "หน้าจอล็อก: นำไปใช้แล้ว จะเห็นเมื่อหน้าจอล็อกครั้งถัดไป\n"
     "กำลังขอรหัสผ่านผู้ดูแลระบบเพื่อเปลี่ยนหน้าจอตอนเปิดเครื่องและหน้าจอเข้าสู่ระบบ"),
    ("Desktop splash: back to Plasma's own.\nLock screen: back to Plasma's own.",
     "หน้าจอโหลดเดสก์ท็อป: กลับเป็นแบบของ Plasma แล้ว\nหน้าจอล็อก: กลับเป็นแบบของ Plasma แล้ว"),
    ("Asking for an admin password to change the login screen.",
     "กำลังขอรหัสผ่านผู้ดูแลระบบเพื่อเปลี่ยนหน้าจอเข้าสู่ระบบ"),
    ("Lock screen: the desktop background has no picture, video or colour yet.",
     "หน้าจอล็อก: พื้นหลังเดสก์ท็อปยังไม่มีรูปภาพ วิดีโอ หรือสี"),
    ("Boot splash: 45 of 90 frames kept to stay under 8 MB. The timing stays the same, the animation is less smooth.",
     "หน้าจอตอนเปิดเครื่อง: เก็บไว้ 45 จาก 90 เฟรมเพื่อให้ไม่เกิน 8 MB จังหวะเวลาเท่าเดิม แต่การเคลื่อนไหวจะลื่นน้อยลง"),
    ("Boot splash: not changed, the picture is too large (9000 x 9000 pixels; at most 64 megapixels).",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน รูปภาพใหญ่เกินไป (9000 x 9000 พิกเซล ไม่เกิน 64 ล้านพิกเซล)"),
    ("Boot splash: not changed, converting took too long. Try a shorter or smaller file.",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน การแปลงไฟล์ใช้เวลานานเกินไป ลองใช้ไฟล์ที่สั้นลงหรือเล็กลง"),
    # "{tool} could not start ({why})" is the Theme section's entry: it is reused here.
    ("Boot splash: not changed, ffmpeg could not start (No such file or directory).",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน ffmpeg เริ่มทำงานไม่ได้ (No such file or directory)"),
    ("Login screen: not changed, the login screen plays MP4, WebM, MKV or MOV videos. Convert this one first.",
     "หน้าจอเข้าสู่ระบบ: ไม่ได้เปลี่ยน หน้าจอเข้าสู่ระบบเล่นวิดีโอ MP4, WebM, MKV หรือ MOV แปลงวิดีโอนี้ก่อน"),
    ("Login screen: not supported with this login manager. It works only with Plasma Login Manager.",
     "หน้าจอเข้าสู่ระบบ: ไม่รองรับ ตัวจัดการการเข้าสู่ระบบนี้ ใช้ได้กับ Plasma Login Manager เท่านั้น"),
    ("Login screen: not supported with sddm. It works only with Plasma Login Manager.",
     "หน้าจอเข้าสู่ระบบ: ไม่รองรับ sddm ใช้ได้กับ Plasma Login Manager เท่านั้น"),
    ("Bootup: could not remember what was changed (/home/u/.local/state/forge is not a folder of yours (a link?), "
     "so nothing was changed).",
     "การเริ่มเครื่อง: จดจำสิ่งที่เปลี่ยนไปไม่ได้ (/home/u/.local/state/forge ไม่ใช่โฟลเดอร์ของคุณ "
     "(เป็นลิงก์หรือเปล่า) จึงไม่ได้เปลี่ยนอะไร)"),
    ("/home/u/.config/ksplashrc is a link, so it was left alone", "/home/u/.config/ksplashrc เป็นลิงก์ จึงไม่ได้แตะต้อง"),
    ("Desktop splash: /home/u/.local/share/plasma is a link, so nothing was changed.",
     "หน้าจอโหลดเดสก์ท็อป: /home/u/.local/share/plasma เป็นลิงก์ จึงไม่ได้เปลี่ยนอะไร"),
    ("Boot splash and login screen: not changed (they need the admin password).",
     "หน้าจอตอนเปิดเครื่องและหน้าจอเข้าสู่ระบบ: ไม่ได้เปลี่ยน (ต้องใช้รหัสผ่านผู้ดูแลระบบ)"),
    ("Bootup: not changed, the admin password was not given.",
     "การเริ่มเครื่อง: ไม่ได้เปลี่ยน ไม่ได้ใส่รหัสผ่านผู้ดูแลระบบ"),
    ("Bootup: could not ask for the admin password (Permission denied).",
     "การเริ่มเครื่อง: ขอรหัสผ่านผู้ดูแลระบบไม่ได้ (Permission denied)"),
    ("Bootup: nothing to change.", "การเริ่มเครื่อง: ไม่มีอะไรต้องเปลี่ยน"),
    ("Bootup: the settings could not be read.", "การเริ่มเครื่อง: อ่านการตั้งค่าไม่ได้"),
]

# forge-boot-apply (as root): forge-appearance-boot passes its lines through.
BOOT_HELPER = [
    ("Boot splash: ready. You see it after the next restart (also at shutdown and for the disk password).\n"
     "Login screen: background set. You see it the next time the login screen shows.",
     "หน้าจอตอนเปิดเครื่อง: พร้อมแล้ว จะเห็นหลังรีสตาร์ตครั้งถัดไป (รวมถึงตอนปิดเครื่องและตอนถามรหัสผ่านดิสก์)\n"
     "หน้าจอเข้าสู่ระบบ: ตั้งพื้นหลังแล้ว จะเห็นเมื่อหน้าจอเข้าสู่ระบบแสดงครั้งถัดไป"),
    ("Boot splash: not changed: Plymouth's two-step plugin is missing, so a custom boot splash can't work",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน: ไม่มีปลั๊กอิน two-step ของ Plymouth "
     "หน้าจอตอนเปิดเครื่องแบบกำหนดเองจึงใช้ไม่ได้"),
    ("Boot splash: not changed: /usr/share/plymouth/themes/spinner/lock.png is missing. The disk password box needs "
     "it, so the custom boot splash stays off",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน: /usr/share/plymouth/themes/spinner/lock.png หายไป "
     "ช่องใส่รหัสผ่านดิสก์ต้องใช้ไฟล์นี้ หน้าจอตอนเปิดเครื่องแบบกำหนดเองจึงยังปิดอยู่"),
    ("Boot splash: not changed: /etc/plymouth/plymouthd.conf is not a regular file (a link?), so the boot splash "
     "was not changed",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน: /etc/plymouth/plymouthd.conf ไม่ใช่ไฟล์ธรรมดา (เป็นลิงก์หรือเปล่า) "
     "จึงไม่ได้เปลี่ยนหน้าจอตอนเปิดเครื่อง"),
    ("Boot splash: not changed: could not read the boot image file list (rpm-ostree initramfs-etc), so nothing was "
     "changed",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน: อ่านรายการไฟล์ของอิมเมจบูต (rpm-ostree initramfs-etc) ไม่ได้ "
     "จึงไม่ได้เปลี่ยนอะไร"),
    ("Login screen: not changed: /var/lib/forge-boot is not a folder that only root can change (a link or something "
     "else), so nothing was changed",
     "หน้าจอเข้าสู่ระบบ: ไม่ได้เปลี่ยน: /var/lib/forge-boot ไม่ใช่โฟลเดอร์ที่มีแต่ root เปลี่ยนได้ "
     "(เป็นลิงก์หรืออย่างอื่น) จึงไม่ได้เปลี่ยนอะไร"),
    ("Login screen: not changed: /etc/plasmalogin.conf could not be opened safely (Too many levels of symbolic "
     "links): is it a link?",
     "หน้าจอเข้าสู่ระบบ: ไม่ได้เปลี่ยน: /etc/plasmalogin.conf เปิดอย่างปลอดภัยไม่ได้ "
     "(Too many levels of symbolic links) เป็นลิงก์หรือเปล่า"),
    ("Login screen: not supported with no display manager. It works only with Plasma Login Manager.",
     "หน้าจอเข้าสู่ระบบ: ไม่รองรับ ระบบที่ไม่มีตัวจัดการการแสดงผล ใช้ได้กับ Plasma Login Manager เท่านั้น"),
    ("Login screen: a video needs the Smart Video Wallpaper plugin, which is not installed. Choose a picture instead.",
     "หน้าจอเข้าสู่ระบบ: วิดีโอต้องใช้ปลั๊กอิน Smart Video Wallpaper ซึ่งยังไม่ได้ติดตั้ง เลือกรูปภาพแทน"),
    ("Nothing was changed: another bootup change is still running. Try again in a minute.",
     "ไม่ได้เปลี่ยนอะไร: การเปลี่ยนการเริ่มเครื่องอีกรายการยังทำงานอยู่ ลองใหม่ในอีกหนึ่งนาที"),
    ("Boot splash: not changed. rpm-ostree said: rpm-ostree took longer than 600 seconds",
     "หน้าจอตอนเปิดเครื่อง: ไม่ได้เปลี่ยน rpm-ostree แจ้งว่า: rpm-ostree ใช้เวลานานกว่า 600 วินาที"),
    ("Boot splash: still on. rpm-ostree said: no details",
     "หน้าจอตอนเปิดเครื่อง: ยังเปิดอยู่ rpm-ostree แจ้งว่า: ไม่มีรายละเอียด"),
    ("Boot splash: rpm-ostree reported a problem (error: Transaction in progress), but the files may be in use, so "
     "they were left in place. Try again in a minute.",
     "หน้าจอตอนเปิดเครื่อง: rpm-ostree แจ้งปัญหา (error: Transaction in progress) แต่ไฟล์อาจกำลังถูกใช้อยู่ "
     "จึงคงไว้ที่เดิม ลองใหม่ในอีกหนึ่งนาที"),
    ("Boot splash: /etc/forge-boot/plymouth/themes/forge-custom is still in the boot image list (someone else added "
     "it), so its files stay.",
     "หน้าจอตอนเปิดเครื่อง: /etc/forge-boot/plymouth/themes/forge-custom ยังอยู่ในรายการของอิมเมจบูต "
     "(มีคนอื่นเพิ่มไว้) จึงเก็บไฟล์ของมันไว้"),
    ("Boot splash: stopped by an unexpected problem (KeyError). What was already changed is recorded, so turning it "
     "off still undoes it.",
     "หน้าจอตอนเปิดเครื่อง: หยุดเพราะปัญหาที่ไม่คาดคิด (KeyError) สิ่งที่เปลี่ยนไปแล้วถูกบันทึกไว้ "
     "การปิดจึงยังย้อนคืนได้"),
    ("Login screen: already the default.\nBoot splash: back to Bazzite's own after the next restart.\n"
     "Warning: /etc/crypttab (USB key unlock) was missing from the boot image list. Forge added it back.",
     "หน้าจอเข้าสู่ระบบ: เป็นค่าเริ่มต้นอยู่แล้ว\nหน้าจอตอนเปิดเครื่อง: จะกลับเป็นแบบของ Bazzite หลังรีสตาร์ตครั้งถัดไป\n"
     "คำเตือน: /etc/crypttab (การปลดล็อกด้วยกุญแจ USB) หายไปจากรายการของอิมเมจบูต Forge เพิ่มกลับให้แล้ว"),
    ("IMPORTANT: /etc/crypttab (USB key unlock) is missing from the boot image list. Before you restart, run: "
     "sudo rpm-ostree initramfs-etc --track=/etc/crypttab",
     "สำคัญ: /etc/crypttab (การปลดล็อกด้วยกุญแจ USB) หายไปจากรายการของอิมเมจบูต ก่อนรีสตาร์ต ให้รัน: "
     "sudo rpm-ostree initramfs-etc --track=/etc/crypttab"),
]

# Lines that are not from these programs, or only look like them: they must not be taken by the new entries.
UNRELATED = [
    ("Dock", "Dock"),
    ("Not applied", "Not applied"),
    ("Nothing changed", "Nothing changed"),
    ("Dock: no full stop at the end", "Dock: no full stop at the end"),
    ("Background applied: picture on all screens", "Background applied: picture on all screens"),
    ("Not applied: bad request.", "ยังไม่ได้นำไปใช้: คำขอไม่ถูกต้อง"),
    ("Theme: colors set to Breeze.", "ธีม: ตั้งสีเป็น Breeze แล้ว"),
    ("Theme: Colors: not changed, the font Comic is not installed on this PC.",
     "ธีม: สี: ไม่ได้เปลี่ยน ไม่ได้ติดตั้งฟอนต์ Comic ในพีซีเครื่องนี้"),
]


def node_path():
    return shutil.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)


def load(lang):
    with open(os.path.join(I18N, lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


def ours(strings):
    return {k: v for k, v in strings.items() if k.startswith(PREFIXES)}


class Runtime:
    """tr() exactly as the pages run it: forgei18n.I18N_JS in node, fed with page_json()."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.node = node_path()
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(tmp, "config"), "FORGE_I18N_DIR": I18N}):
            import forgei18n
            self.i = importlib.reload(forgei18n)
            self.th_json = self.i.page_json("th")
            self.en_json = self.i.page_json("en")
        self.script = os.path.join(tmp, "tr.js")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write("const fs = require('fs');\n"
                    "const data = fs.readFileSync(process.argv[2], 'utf8');\n"
                    "global.document = {documentElement: {}, getElementById: () => ({textContent: data}), "
                    "querySelector: () => null};\n" + self.i.I18N_JS +
                    "\nconst lines = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
                    "console.log(JSON.stringify(lines.map(l => tr(l))));\n")

    def tr(self, data_json, lines):
        data = os.path.join(self.tmp, "data.json")
        inp = os.path.join(self.tmp, "lines.json")
        with open(data, "w", encoding="utf-8") as f:
            f.write(data_json)
        with open(inp, "w", encoding="utf-8") as f:
            json.dump(lines, f, ensure_ascii=False)
        r = subprocess.run([self.node, self.script, data, inp], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout)


class TranslateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not node_path():
            raise unittest.SkipTest("node is needed")
        cls.tmp = tempfile.mkdtemp()
        cls.rt = Runtime(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, pairs):
        got = self.rt.tr(self.rt.th_json, [en for en, _ in pairs])
        for (en, want), out in zip(pairs, got):
            self.assertEqual(out, want, en)

    def test_background_lines(self):
        self.check(BACKGROUND)

    def test_dock_lines(self):
        self.check(DOCK)

    def test_bootup_lines(self):
        self.check(BOOT)

    def test_boot_helper_lines(self):
        self.check(BOOT_HELPER)

    def test_unrelated_lines_are_not_taken(self):
        self.check(UNRELATED)

    def test_english_stays_english(self):
        lines = [en for en, _ in BACKGROUND + DOCK + BOOT + BOOT_HELPER]
        self.assertEqual(self.rt.tr(self.rt.en_json, lines), lines)

    def test_each_entry_wins_for_its_own_line(self):
        """Every msg.bg/dock/boot entry, filled with plain values, comes out as its own Thai: no other entry
        (older or new, longer or shorter) takes the line first."""
        en, th = load("en")["strings"], load("th")["strings"]
        keys = sorted(ours(en))
        lines, wants = [], []
        for k in keys:
            values = {n: f"V{i}x" for i, n in enumerate(dict.fromkeys(PLACEHOLDER.findall(en[k])))}
            fill = lambda s: PLACEHOLDER.sub(lambda m: values[m.group(1)], s)   # noqa: E731
            lines.append(fill(en[k]))
            wants.append(fill(th[k]))
        for k, line, want, out in zip(keys, lines, wants, self.rt.tr(self.rt.th_json, lines)):
            self.assertEqual(out, want, f"{k}: {line}")

    def test_older_entries_translate_as_before(self):
        """The new entries change nothing for lines the older msg.* entries already translated."""
        en, th = load("en")["strings"], load("th")["strings"]
        lines = []
        for k, v in sorted(en.items()):
            if k.startswith("msg.") and not k.startswith(PREFIXES):
                names = dict.fromkeys(PLACEHOLDER.findall(v))
                lines.append(PLACEHOLDER.sub(lambda m: "V%dx" % list(names).index(m.group(1)), v))
        without = {"lang": "th", "pref": "auto", "names": {}, "strings": {k: v for k, v in th.items()
                                                                          if not k.startswith(PREFIXES)},
                   "fallback": {k: v for k, v in en.items() if not k.startswith(PREFIXES)}}
        before = self.rt.tr(json.dumps(without, ensure_ascii=False), lines)
        after = self.rt.tr(self.rt.th_json, lines)
        for line, b, a in zip(lines, before, after):
            self.assertEqual(a, b, line)


class TableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.en, cls.th = load("en"), load("th")

    def test_there_are_entries_for_each_section(self):
        for p in PREFIXES:
            self.assertGreater(sum(1 for k in self.en["strings"] if k.startswith(p)), 10, p)

    def test_every_entry_is_in_both_tables_with_the_same_placeholders(self):
        en, th = ours(self.en["strings"]), ours(self.th["strings"])
        self.assertEqual(sorted(set(en) ^ set(th)), [])
        for k in en:
            self.assertEqual(sorted(PLACEHOLDER.findall(en[k])), sorted(PLACEHOLDER.findall(th[k])), k)

    def test_thai_is_thai(self):
        for k, v in ours(self.th["strings"]).items():
            if k != "msg.dock.line":          # "Dock: {reason}": the section is called Dock in Thai too
                self.assertRegex(v, r"[฀-๿]", k)

    def test_no_em_dash_or_double_hyphen(self):
        for t in (self.en, self.th):
            for k, v in ours(t["strings"]).items():
                self.assertNotIn(chr(0x2014), v, k)
                self.assertNotRegex(v, r"--(?![a-z])", k)     # a command line option is fine

    def test_files_keep_their_layout(self):
        """{"lang", "name", "strings"} with the keys sorted and one entry per line."""
        for lang, t in (("en", self.en), ("th", self.th)):
            with open(os.path.join(I18N, lang + ".json"), encoding="utf-8") as f:
                text = f.read()
            want = '{"lang": %s, "name": %s, "strings": {\n' % (json.dumps(t["lang"], ensure_ascii=False),
                                                               json.dumps(t["name"], ensure_ascii=False))
            want += ",\n".join("  %s: %s" % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False))
                               for k, v in sorted(t["strings"].items())) + "\n}}\n"
            self.assertEqual(text, want, lang)
            self.assertEqual(list(t), ["lang", "name", "strings"])


if __name__ == "__main__":
    unittest.main()
