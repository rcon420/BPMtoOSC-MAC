#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.9.13 64-Bit
# WxPython: v4.1.1
#
# Autor: Leonhard Axtner (d00mfish) 2022
# Developed with VSCode using following Plugins:
#   Python, Pylance, GitLens, German language pack,Python Docstring Generator
#
# TODO:
# - Other output (Midi clock?)
# - Darkmode?

# Imports:
import sys
import random
from pathlib import Path
from subprocess import run, DEVNULL
from platform import system
from time import time
from threading import Thread, Event

# local
from sevensegment import SevenSegmentDisp
import beatfinder
import osc_client

import configparser
import pyaudio


import wx
import wx.lib.masked.ipaddrctrl as ipctrl
import wx.lib.agw.peakmeter as PM
import wx.adv
from PIL import Image as PILImage


CARD_ALPHA = 95  # white of the soft panel behind each group (0 = invisible, 255 = solid)


def _mix(colour, other, amount):
    return tuple(int(c + (o - c) * amount) for c, o in zip(colour, other))


class BeatLed(wx.Panel):
    """One of the four bar LEDs (own drawn, because native labels ignore background colours on macOS)"""

    def __init__(self, parent, colour=(50, 0, 0)):
        super().__init__(parent, wx.ID_ANY, size=(10, 26))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._colour = tuple(colour)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)

    def SetBackgroundColour(self, colour):
        if hasattr(colour, 'Red'):
            colour = (colour.Red(), colour.Green(), colour.Blue())
        self._colour = tuple(colour)[:3]
        self.Refresh()
        return True

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(*self._colour)))
        dc.Clear()
        dc.SetPen(wx.Pen(wx.Colour(*_mix(self._colour, (0, 0, 0), 0.5)), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(0, 0, w, h)


class SenderCtl:
    """Control object of one no-sync sender thread (every thread gets its own, so threads can never mix up signals)"""

    def __init__(self):
        self.wake = Event()   # set = wake the thread up now
        self.stop = False     # True = thread must end


SYSTEM_SOUND_NAME = "System sound (what you hear, e.g. Spotify)"


def resource_path(*parts) -> Path:
    """Path to bundled files (works from source and inside the PyInstaller .app)"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def default_conf_path() -> Path:
    """Config location per operating system"""
    os_name = system().lower()
    if os_name == "darwin":
        return Path.home() / "Library" / "Application Support" / "BPMtoOSC" / "lastsession.ini"
    if os_name == "windows":
        return Path.home() / "AppData" / "Roaming" / "BPMtoOSC" / "lastsession.ini"
    return Path.home() / ".config" / "BPMtoOSC" / "lastsession.ini"


def pil_to_bitmap(im, scale: float = 1.0):
    """PIL image -> wx.Bitmap (keeps transparency)"""
    im = im.convert('RGBA')
    w, h = im.size
    r, g, b, a = im.split()
    img = wx.Image(w, h)
    img.SetData(PILImage.merge('RGB', (r, g, b)).tobytes())
    img.SetAlpha(a.tobytes())
    bmp = wx.Bitmap(img)
    if scale != 1.0:
        try:
            bmp.SetScaleFactor(scale)
        except Exception:
            pass
    return bmp


def clean_ip(ip: str) -> str:
    """'127.  0.  0.  1' or '127.000.000.001' -> '127.0.0.1' (leading zeros can be read as octal)"""
    ip = ip.replace(" ", "")
    try:
        return ".".join(str(int(part)) for part in ip.split("."))
    except ValueError:
        return ip


def pad_ip(ip: str) -> str:
    """'127.0.0.1' -> '127.000.000.001' (format the IP input box expects)"""
    try:
        return ".".join(str(int(part)).zfill(3) for part in ip.replace(" ", "").split("."))
    except ValueError:
        return "127.000.000.001"


class Main_Frame(wx.Frame):
    '''Frame Class
    '''
    DEFAULT_WINDOW = (1103, 1253)   # size the window opens with (content area, in points); see [UI] in lastsession.ini
    DIGIT_SIZE = (60, 104)          # size of one digit of the BPM counters
    LOGO_SCALE = {"progresivie.png": 0.6}  # logos that should be shown smaller than the rest (1.0 = normal)
    LOGO_ORDER = ["fbi.png", "mossad.png", "sso.png", "vdd.png", "progresivie.png", "slesers.png",
                  "draugiem.png", "eklase.png"]
    sel_msg_frame = None
    sel_bus_frame = None
    CONF_PATH = default_conf_path()

    def __init__(self, parent=None):
        """Initialize Config Window
            - Reads config file and sets variables
            - Initializes GUI
        """
        # wx.Frame init
        style_dep = wx.DEFAULT_FRAME_STYLE ^ wx.MAXIMIZE_BOX  # ^ wx.RESIZE_BORDER
        super(Main_Frame, self).__init__(parent, title="BPMtoOSC RXv2 Mossad Spyware", style=style_dep)

        # Config Setup
        self.config = configparser.ConfigParser()

        # Audio Setup
        self.audio = pyaudio.PyAudio()
        self.audio_device_ids = []  # real PyAudio device index for every row in the dropdown

        # OSC Setup
        self.osc_client = None

        # BPM Setup
        self.beatfinder = None  # audio analysis instance
        self.send_bpm = 128  # sent bpm when sync is diasabled (used to hold last live or tap value)
        self.beat_divider = 1  # divides beat to get 1/2, 1/4
        self.sender = None  # SenderCtl of the running no-sync sender thread
        self.no_sync_send_thread = Thread(target=lambda: None, daemon=True)  # placeholder, replaced when sync is switched off
        self.last_sent_bpm = None  # last value sent by the no-sync mode

        self.last_tap = list()  # list of taps to determine bpm
        self.buttons_to_disable = list()  # list of buttons to disableon start/stop

        # Flags
        self.bpm_blink = False  # Blinking sevenseg background
        self.sync = True  # sync live bpm to send bpm
        self.running = False
        self.retrys = 3
        self.led_counter = 3
        self.last_level = -1
        self.last_tick = time()  # watchdog: time of the last timer tick (a big gap means the Mac was asleep)
        self.reconnect_at = 0.0  # earliest time of the next reconnect attempt
        self.reconnects = 0

        self.Read_LastSession_ini()
        self.CreateStatusBar(1)
        self.SetStatusText(self.HELP_IDLE)
        self.InitUI()

        self.Centre()  # centre window on screen

        # app icon (dock icon on macOS also when started from source)
        try:
            icon_file = resource_path("pics", "icon.png")
            if icon_file.exists():
                icon = wx.Icon(str(icon_file), wx.BITMAP_TYPE_PNG)
                self.SetIcon(icon)
                if system().lower() == "darwin":
                    self.dock_icon = wx.adv.TaskBarIcon(wx.adv.TBI_DOCK)
                    self.dock_icon.SetIcon(icon, "BPMtoOSC")
        except Exception as e:
            print("Could not set icon:", e)

# PEAK METER
        self.uv_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnUVTimer)

        self.uv_timer.Start(80)

    def OnUVTimer(self, event):
        now = time()
        gap, self.last_tick = now - self.last_tick, now
        if self.running:
            self.watchdog(gap, now)
        bf = self.beatfinder
        if self.running and bf is not None:
            levels = list(bf.level_queue)
            level = int(sum(levels) / len(levels)) if levels else 0
            if abs(level - self.last_level) >= 2 or (level == 0 and self.last_level != 0):  # only repaint on a visible change
                self.last_level = level
                self.peak_meter.SetData([level], 0, 1)

    def watchdog(self, gap, now):
        """Runs about 12 times a second: brings the audio back after sleep, a lost device or a stopped helper"""
        if now < self.reconnect_at:
            return
        bf = self.beatfinder
        if gap > 4:
            reason = "the computer woke up"   # this timer normally ticks every 80 ms
        elif bf is None:
            reason = "the audio is not running"
        else:
            reason = bf.problem()
        if reason:
            self.reconnect_audio(reason, now)

    def reconnect_audio(self, reason, now=None):
        """Closes the old audio input and opens it again (same device, found by name because device numbers can change)"""
        now = time() if now is None else now
        self.reconnect_at = now + 3.0   # if this attempt fails, the next one comes in 3 seconds
        print("Audio problem:", reason, "- reconnecting")
        self.SetStatusText("Audio problem (%s), reconnecting..." % reason)

        old, self.beatfinder = self.beatfinder, None
        if old is not None:
            closer = Thread(target=old.close, daemon=True)   # closing a dead device can hang, so never wait for it long
            closer.start()
            closer.join(2.0)

        try:
            wanted = self.config['AUDIO'].get('device_name', '')
            if wanted != SYSTEM_SOUND_NAME:
                try:   # refresh the device list: a headset or interface may have come back under a new number
                    self.audio.terminate()
                except Exception:
                    pass
                self.audio = pyaudio.PyAudio()
            self.on_button_reload(None)
            names = [self.audio_selection.GetString(n) for n in range(self.audio_selection.GetCount())]
            plain = [n.replace("DEFAULT: ", "") for n in names]
            if wanted not in plain:
                raise RuntimeError("input '%s' not found" % wanted)
            sel = plain.index(wanted)
            self.audio_selection.SetSelection(sel)
            self.beatfinder = beatfinder.BeatDetector(self.osc_client, self.audio_device_ids[sel], parent=self)
        except Exception as e:
            self.beatfinder = None
            print("Reconnect failed:", e)
            self.SetStatusText("Audio lost (%s). Trying again..." % e)
            return
        self.reconnects += 1
        self.reconnect_at = 0.0
        self.last_tick = time()
        self.SetStatusText("Audio reconnected (%s).  %s" % (reason, self.HELP_IDLE))

    def Read_LastSession_ini(self):
        """Reading / creating lastsession.ini (missing keys are filled with defaults)
        """
        defaults = {
            'OSC': {'IP': '127.0.0.1',
                    'PORT': '7000',
                    'RESYNC_BAR_ADRESS': '/composition/tempocontroller/resync',
                    'BPM_ADRESS': '/composition/tempocontroller/tempo',
                    'RESOLUME': 'True',
                    'GRANDMA3_MASTER': '',
                    'GRANDMA3_ID': '3.1',
                    'BEAT_OUTPUTS': 'True',
                    'LOCK': 'False',
                    'LOCK_PERCENT': '6',
                    'LOCK_SECONDS': '10',
                    'BPM_COUNT_ADDRESS': '/beat/count',
                    'BPM_ONE_ADDRESS': '/beat/one',
                    'BPM_TWO_ADDRESS': '/beat/two'},
            'AUDIO': {'device_name': '', 'engine': 'fast'},
            'UI': {'BACKGROUND_PERCENT': '60', 'WINDOW_WIDTH': '1103', 'WINDOW_HEIGHT': '1253'},
        }
        try:
            self.config.read(self.CONF_PATH)
        except Exception as e:
            print("Error reading config file, using defaults:", e)
            self.config = configparser.ConfigParser()

        for section, values in defaults.items():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for key, value in values.items():
                if not self.config.has_option(section, key):
                    self.config.set(section, key, value)

        # remember the grandMA3 master id even while another target is selected
        if self.config['OSC'].get('GRANDMA3_MASTER', '') != '':
            self.config['OSC']['GRANDMA3_ID'] = self.config['OSC']['GRANDMA3_MASTER']

    def InitUI(self):
        """Actual GUI Setup of main config Window: one simple vertical layout, normal macOS buttons.
        Every row has a minimum size, so nothing can be pushed outside the window."""

        panel = wx.Panel(self)
        self.panel = panel
        root = wx.BoxSizer(wx.VERTICAL)
        self.lock_countdown = ''

        # 7seg background blink
        self.bg_grey = wx.Colour(240, 240, 240)

        # background picture (pics/background.png), faded so the controls stay readable
        self.bg_source = None
        self.bg_cache = (None, None)  # ((width, height), bitmap)
        self.panel_cache = (None, None)  # (key, bitmap): picture + soft panels in one bitmap
        try:
            self.bg_opacity = max(0.0, min(1.0, float(self.config['UI'].get('BACKGROUND_PERCENT', '60')) / 100.0))
        except ValueError:
            self.bg_opacity = 0.6
        if resource_path("pics", "background.png").exists():
            self.bg_source = PILImage.open(resource_path("pics", "background.png")).convert('RGBA')
        # default window size from the config, but never bigger than the screen
        self.win_w, self.win_h = self.fit_to_screen(self.config_int('WINDOW_WIDTH', self.DEFAULT_WINDOW[0]),
                                                    self.config_int('WINDOW_HEIGHT', self.DEFAULT_WINDOW[1]))
        panel.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        panel.Bind(wx.EVT_PAINT, self.on_panel_paint)
        panel.Bind(wx.EVT_SIZE, lambda e: (panel.Refresh(), e.Skip()))
        self.bg_a = (self.bg_grey, (220, 220, 220))

        buttonfont = wx.Font(14, family=wx.DEFAULT, style=wx.NORMAL, weight=wx.BOLD)
        smallfont = wx.Font(12, family=wx.DEFAULT, style=wx.NORMAL, weight=wx.BOLD)

# ---------------------------------------------------------------- ROW 1: OSC address, port, target, ping
        self.ip_stuff_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, panel, label="OSC Adress and Port")

        self.text_ip = ipctrl.IpAddrCtrl(panel)
        self.text_ip.SetValue(pad_ip(self.config['OSC']['IP']))
        self.ip_stuff_sizer.Add(self.text_ip, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER, border=5)

        self.text_port = wx.SpinCtrl(panel)
        self.text_port.SetMax(65535)
        self.text_port.SetMin(0)
        self.text_port.SetValue(int(self.config['OSC']['PORT']))
        self.ip_stuff_sizer.Add(self.text_port, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER, border=5)

        # Text Connectionstatus (Reachable / Unreachable)
        self.text_connection = wx.StaticText(panel, label="", style=wx.ALIGN_RIGHT)
        self.text_connection.SetFont(wx.Font(12, family=wx.DEFAULT, style=wx.NORMAL, weight=wx.BOLD))
        self.text_connection.SetMinSize(wx.Size(80, -1))
        self.ip_stuff_sizer.Add(self.text_connection, 1, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER, border=5)

        # Target program: decides in which format the BPM is sent
        self.choice_target = wx.Choice(panel, choices=["Resolume", "grandMA3", "Raw BPM"])
        self.choice_target.SetSelection(self.target_index())
        self.Bind(wx.EVT_CHOICE, self.on_target_change, self.choice_target)
        self.ip_stuff_sizer.Add(self.choice_target, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER, border=5)

        self.button_ping = wx.Button(panel, wx.ID_ANY, label="Ping")
        self.ip_stuff_sizer.Add(self.button_ping, 0, wx.RIGHT | wx.LEFT | wx.ALIGN_CENTER, border=5)
        self.Bind(wx.EVT_BUTTON, self.on_button_ping, self.button_ping)

        root.Add(self.ip_stuff_sizer, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 10)

# ---------------------------------------------------------------- ROW 2: audio device + level meter
        audiosizer = wx.StaticBoxSizer(wx.HORIZONTAL, panel, label="Audio Input Device")

        self.audio_selection = wx.ComboBox(panel, style=wx.CB_READONLY)
        self.on_button_reload(None)  # fill combobox with devices and select the saved one
        self.audio_selection.SetMinSize(wx.Size(160, -1))  # long device names must not widen the window
        audiosizer.Add(self.audio_selection, 2, wx.EXPAND | wx.ALL, border=5)

        self.peak_meter = PM.PeakMeterCtrl(panel, -1, style=wx.SIMPLE_BORDER, agwStyle=PM.PM_HORIZONTAL)
        self.peak_meter.SetMeterBands(1, 48)
        self.peak_meter.SetFalloffEffect(False)
        self.peak_meter.SetBandsColour((150, 220, 150), (255, 255, 150), (220, 150, 150))
        self.peak_meter.SetRangeValue(20, 60, 80)
        self.peak_meter.SetMinSize(wx.Size(120, 22))
        audiosizer.Add(self.peak_meter, 2, wx.EXPAND | wx.ALL, border=5)

        root.Add(audiosizer, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 12)

# ---------------------------------------------------------------- ROW 3: the four bar LEDs
        beat_led_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.leds = [BeatLed(panel) for _ in range(4)]
        for led in self.leds:
            beat_led_sizer.Add(led, 1, wx.EXPAND | wx.RIGHT | wx.LEFT, border=5)
        root.Add(beat_led_sizer, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 14)
        root.AddStretchSpacer(1)

# ---------------------------------------------------------------- ROW 4: LIVE | SYNC 1/2 LOCK | SEND (takes the spare height)
        sevenseg_sizer = wx.BoxSizer(orient=wx.HORIZONTAL)

        self.live_disp = (SevenSegmentDisp(panel), SevenSegmentDisp(panel), SevenSegmentDisp(panel))
        self.send_disp = (SevenSegmentDisp(panel), SevenSegmentDisp(panel), SevenSegmentDisp(panel))

        bpm_box_live = wx.StaticBoxSizer(wx.HORIZONTAL, panel, label="LIVE")
        for digit in self.live_disp:
            digit.SetTilt(5)
            digit.SetMinSize(wx.Size(*self.DIGIT_SIZE))
            digit.SetMaxSize(wx.Size(*self.DIGIT_SIZE))
            digit.SetColours(segment_on=((0, 71, 77)), background=self.bg_grey, segment_off=(220, 220, 220))
            digit.SetGeometry(width=38, height=38, thickness=10, separation=2)
            digit.EnableDot(False)
            digit.EnableColon(False)
            digit.SetValue("-")
            bpm_box_live.Add(digit, 0, wx.DOWN, 5)

        bpm_box_sending = wx.StaticBoxSizer(wx.HORIZONTAL, panel, label="SEND")
        for digit in self.send_disp:
            digit.SetTilt(5)
            digit.SetMinSize(wx.Size(*self.DIGIT_SIZE))
            digit.SetMaxSize(wx.Size(*self.DIGIT_SIZE))
            digit.SetColours(segment_on=(85, 0, 0), background=self.bg_grey, segment_off=(220, 220, 220))
            digit.SetGeometry(width=38, height=38, thickness=10, separation=2)
            digit.EnableDot(False)
            digit.EnableColon(False)
            digit.SetValue("-")
            bpm_box_sending.Add(digit, 0, wx.DOWN, 5)

        # SYNC, 1/2 and LOCK in the middle (their height stays natural, the spare space goes above and below)
        self.sevenseg_button_sizer = wx.BoxSizer(orient=wx.VERTICAL)
        self.sevenseg_button_sizer.AddStretchSpacer(1)

        self.button_sync = wx.ToggleButton(panel, label='SYNC\nON')
        self.button_sync.SetValue(True)
        self.button_sync.SetFont(smallfont)
        self.button_sync.SetMinSize(wx.Size(84, -1))
        self.buttons_to_disable.append(self.button_sync)
        self.sevenseg_button_sizer.Add(self.button_sync, 0, wx.EXPAND | wx.ALL, 3)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_sync, self.button_sync)

        self.button_halftime = wx.ToggleButton(panel, label='1/2')
        self.button_halftime.SetFont(buttonfont)
        self.button_halftime.SetMinSize(wx.Size(84, -1))
        self.buttons_to_disable.append(self.button_halftime)
        self.sevenseg_button_sizer.Add(self.button_halftime, 0, wx.EXPAND | wx.ALL, 3)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_halftime, self.button_halftime)

        self.button_lock = wx.ToggleButton(panel, label='LOCK')
        self.button_lock.SetFont(smallfont)
        self.button_lock.SetMinSize(wx.Size(84, -1))
        self.button_lock.SetValue(self.config['OSC'].get('LOCK', 'False') == 'True')
        self.sevenseg_button_sizer.Add(self.button_lock, 0, wx.EXPAND | wx.ALL, 3)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_lock, self.button_lock)
        self.refresh_lock_label()

        self.sevenseg_button_sizer.AddStretchSpacer(1)

        # compact cluster in the middle: LIVE counter, SYNC column, SEND counter (the picture shows around it)
        sevenseg_sizer.AddStretchSpacer(1)
        sevenseg_sizer.Add(bpm_box_live, 0, wx.EXPAND)
        sevenseg_sizer.Add(self.sevenseg_button_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sevenseg_sizer.Add(bpm_box_sending, 0, wx.EXPAND)
        sevenseg_sizer.AddStretchSpacer(1)
        root.Add(sevenseg_sizer, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 8)
        root.AddStretchSpacer(1)

# ---------------------------------------------------------------- ROW 5: the button deck (original arrangement, compact)
#   |  RESYNC BAR  | -1 | +1 |
#   |              | /2 | x2 |
#   | START / STOP |   TAP   |
        button_sizer = wx.BoxSizer(orient=wx.VERTICAL)

        sub_button_sizer_top = wx.BoxSizer(orient=wx.HORIZONTAL)
        col_minus = wx.BoxSizer(orient=wx.VERTICAL)
        col_plus = wx.BoxSizer(orient=wx.VERTICAL)

        self.button_minus_one = wx.Button(panel, label='-1')
        self.button_half = wx.Button(panel, label='/2')
        self.button_plus_one = wx.Button(panel, label='+1')
        self.button_double = wx.Button(panel, label='x2')
        for button, column, handler in ((self.button_minus_one, col_minus, self.on_button_minus_one),
                                        (self.button_half, col_minus, self.on_button_half),
                                        (self.button_plus_one, col_plus, self.on_button_plus_one),
                                        (self.button_double, col_plus, self.on_button_double)):
            button.SetFont(buttonfont)
            button.SetMinSize(wx.Size(80, -1))   # fixed compact width, natural height
            self.buttons_to_disable.append(button)
            column.Add(button, 0, wx.EXPAND | wx.ALL, 3)
            self.Bind(wx.EVT_BUTTON, handler, button)

        self.button_resync = wx.Button(panel, label='RESYNC BAR')
        self.button_resync.SetFont(buttonfont)
        self.button_resync.SetMinSize(wx.Size(170, -1))
        self.buttons_to_disable.append(self.button_resync)
        sub_button_sizer_top.Add(self.button_resync, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        self.Bind(wx.EVT_BUTTON, self.on_button_resync, self.button_resync)

        sub_button_sizer_top.Add(col_minus, 0)
        sub_button_sizer_top.Add(col_plus, 0)

        sub_button_sizer_bot = wx.BoxSizer(orient=wx.HORIZONTAL)

        self.button_startstop = wx.ToggleButton(panel, label='START / STOP')
        self.button_startstop.SetFont(buttonfont)
        self.button_startstop.SetMinSize(wx.Size(170, -1))
        sub_button_sizer_bot.Add(self.button_startstop, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_startstop, self.button_startstop)

        self.button_tap = wx.Button(panel, label='TAP')
        self.button_tap.SetFont(buttonfont)
        self.button_tap.SetMinSize(wx.Size(166, -1))   # as wide as the -1/+1 columns together
        self.buttons_to_disable.append(self.button_tap)
        sub_button_sizer_bot.Add(self.button_tap, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        self.Bind(wx.EVT_BUTTON, self.on_button_tap, self.button_tap)

        button_sizer.Add(sub_button_sizer_top, 0, wx.ALIGN_CENTER_HORIZONTAL)
        button_sizer.Add(sub_button_sizer_bot, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 4)
        root.Add(button_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 12)
        root.AddStretchSpacer(1)

        self.add_tooltips()

# ---------------------------------------------------------------- ROW 6: all logos in one line
        banner = wx.BoxSizer(wx.HORIZONTAL)
        found = sorted(f.name for f in resource_path("pics").glob("*.png") if f.name not in ("icon.png", "background.png"))
        self.logo_names = [n for n in self.LOGO_ORDER if n in found] + [n for n in found if n not in self.LOGO_ORDER]
        aspects = {}
        for name in self.logo_names:
            with PILImage.open(resource_path("pics", name)) as im:
                aspects[name] = im.size[0] / im.size[1]
        gap = 8
        scale = {name: self.LOGO_SCALE.get(name, 1.0) for name in self.logo_names}
        avail = self.win_w - 24 - gap * max(0, len(self.logo_names) - 1)
        # all logos share one base height; a logo with a scale below 1.0 is shown smaller than the rest
        self.logo_height = int(min(70, avail / sum(aspects[n] * scale[n] for n in self.logo_names))) if aspects else 0
        self.logo_widths = []
        self.logo_sizes = []
        self.logo_ctrls = []
        for i, name in enumerate(self.logo_names):
            box_h = max(2, int(round(self.logo_height * scale[name])))
            box_w = max(2, int(box_h * aspects[name]))
            bmp, (bw, bh) = self.make_bitmap(name, box_w, box_h)
            self.logo_widths.append(bw)
            self.logo_sizes.append((bw, bh))
            if i > 0:
                banner.AddStretchSpacer(1)
            ctrl = wx.StaticBitmap(panel, bitmap=bmp)
            self.logo_ctrls.append(ctrl)
            banner.Add(ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(banner, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        for b in self.buttons_to_disable:
            b.Disable()

        # soft panels behind each group (painted in on_panel_paint, positions come from the real controls)
        WHITE = (255, 255, 255, CARD_ALPHA)
        DECK = (20, 20, 32, 105)  # darker glass behind the buttons, so the white buttons stand out
        self.card_groups = [
            ([self.ip_stuff_sizer.GetStaticBox()], WHITE),
            ([audiosizer.GetStaticBox()], WHITE),
            (self.leds, WHITE),
            ([bpm_box_live.GetStaticBox(), bpm_box_sending.GetStaticBox(), self.button_sync, self.button_lock], WHITE),
            ([self.button_minus_one, self.button_half, self.button_plus_one, self.button_double, self.button_resync,
              self.button_startstop, self.button_tap], DECK),
            (self.logo_ctrls, WHITE),
        ]

        # window: opens exactly as big as the background picture, never smaller than the layout needs
        self.Bind(wx.EVT_CLOSE, self.close)

        # menu bar with a Quit item: on macOS wx moves it into the app menu and gives it Cmd+Q
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_EXIT, "Quit\tCtrl+Q")
        menubar.Append(file_menu, "&File")
        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU, lambda e: self.close(e), id=wx.ID_EXIT)
        panel.SetSizer(root)
        try:
            need = root.CalcMin()
            min_w, min_h = int(need.width), int(need.height)
            self.SetMinClientSize(wx.Size(min_w, min_h))
            self.SetClientSize(wx.Size(max(self.win_w, min_w), max(self.win_h, min_h)))
        except Exception as e:
            print("Could not size the window:", e)
            self.SetSize(wx.Size(self.win_w, self.win_h))

    def on_button_plus_one(self, event):
        if self.sync:
            self.switch_sync(False)  # continues from the number shown in SEND (also when 1/2 was active)
        self.on_button_halftime(None, reset=True)
        if self.send_bpm < 499:
            self.send_bpm += 1
            self.update_bpm_display(self.send_bpm, send_to="send")
            self.send_current_bpm()

    def on_button_minus_one(self, event):
        if self.sync:
            self.switch_sync(False)  # continues from the number shown in SEND (also when 1/2 was active)
        self.on_button_halftime(None, reset=True)
        if self.send_bpm > 20:
            self.send_bpm -= 1
            self.update_bpm_display(self.send_bpm, send_to="send")
            self.send_current_bpm()

    def on_button_double(self, event):
        if self.sync:
            self.switch_sync(False)  # continues from the number shown in SEND (also when 1/2 was active)
        self.on_button_halftime(None, reset=True)
        if self.send_bpm*2 <= 500:
            self.send_bpm *= 2
            self.update_bpm_display(self.send_bpm, send_to="send")
            self.send_current_bpm()

    def on_button_half(self, event):
        if self.sync:
            self.switch_sync(False)  # continues from the number shown in SEND (also when 1/2 was active)
        self.on_button_halftime(None, reset=True)
        if self.send_bpm/2 >= 20:
            self.send_bpm = round(self.send_bpm/2)
            self.update_bpm_display(self.send_bpm, send_to="send")
            self.send_current_bpm()

    def on_button_halftime(self, event, reset=False):
        if reset:
            self.button_halftime.SetValue(False)

        self.beat_divider = 2 if self.button_halftime.GetValue() else 1

        # apply right away (do not wait until the music changes tempo)
        if not reset and self.running and self.sync:
            out = self.send_bpm // self.beat_divider
            self.send_osc_value(out)
            self.update_bpm_display(out, send_to="send")

    def on_button_lock(self, event):
        """LOCK: small tempo changes pass at once, big ones wait LOCK_SECONDS (see lastsession.ini)"""
        on = self.button_lock.GetValue()
        self.config['OSC']['LOCK'] = 'True' if on else 'False'
        if not on:
            self.lock_countdown = ''
        self.refresh_lock_label()
        if not on:
            if self.running and self.beatfinder is not None and self.sync:
                self.beatfinder.accept_live()   # lock released: follow the live tempo now

    def refresh_lock_label(self):
        """LOCK / LOCK ON, or the countdown while a big tempo change is waiting"""
        if self.lock_countdown:
            text = self.lock_countdown
        else:
            text = 'LOCK ON' if self.button_lock.GetValue() else 'LOCK'
        self.button_lock.SetLabel(text)

    def set_lock_status(self, text: str):
        """Countdown on the LOCK button while a big change is waiting (safe to call from any thread)"""
        self.lock_countdown = text or ''
        wx.CallAfter(self.refresh_lock_label)

    def send_osc_value(self, value):
        """Sends one bpm value to the OSC target (safe to call from any thread)"""
        client = self.osc_client
        if client is None:
            return
        cfg = self.config['OSC']
        client.send_osc(cfg['BPM_ADRESS'], value, map_to_resolume=cfg.get('RESOLUME', 'False'),
                        grandma3_master=cfg.get('GRANDMA3_MASTER', ''))

    def send_current_bpm(self):
        """Sends the value of the SEND display (used by +1, -1, x2, /2, TAP and the no-sync thread)"""
        self.last_sent_bpm = self.send_bpm
        self.send_osc_value(self.send_bpm)


    HELP_IDLE = "Hover over a button or display to see what it does."

    def show_help(self, text):
        """Instant one line help at the bottom of the window (native tooltips need about a second)"""
        self.SetStatusText(text or self.HELP_IDLE)

    def attach_help(self, widget, text):
        widget.Bind(wx.EVT_ENTER_WINDOW, lambda e, t=text: (self.show_help(t), e.Skip()))
        widget.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (self.show_help(""), e.Skip()))

    def add_tooltips(self):
        """Hover help for every control"""
        cfg = self.config['OSC']
        lock_pct = cfg.get('LOCK_PERCENT', '6')
        lock_sec = cfg.get('LOCK_SECONDS', '10')
        tips = [
            (self.text_ip, "IP address of the computer that receives the tempo (Resolume). 127.0.0.1 means this computer."),
            (self.text_port, "OSC port of the receiver. Resolume listens on 7000 by default."),
            (self.choice_target, "Which program receives the tempo: Resolume, grandMA3, or a plain BPM number (Raw BPM)."),
            (self.text_connection, "Result of the last ping."),
            (self.button_ping, "Checks that the IP address answers. It does not test OSC itself."),
            (self.audio_selection, "Audio input. Use your mixer or audio interface for a DJ deck, "
                                   "or System sound to test with YouTube or Spotify."),
            (self.peak_meter, "Input level. If it stays empty, the app hears nothing."),
            (self.button_sync, "SYNC on: SEND follows the music.\nSYNC off: SEND keeps its value, set it with +1, -1, x2, /2 or TAP.\n"
                               "Switching SYNC on again jumps to the live tempo."),
            (self.button_halftime, "Sends half of the live tempo (140 becomes 70). Only works while SYNC is on."),
            (self.button_lock, f"Big tempo changes (more than {lock_pct}%) are only sent after they stay for {lock_sec} seconds.\n"
                               "Small changes pass at once. Switch LOCK off to jump to the live tempo."),
            (self.button_plus_one, "Raise the sent tempo by 1 BPM. Switches SYNC off."),
            (self.button_minus_one, "Lower the sent tempo by 1 BPM. Switches SYNC off."),
            (self.button_double, "Double the sent tempo. Use it when the app reads half the real tempo. Switches SYNC off."),
            (self.button_half, "Halve the sent tempo. Use it when the app reads double the real tempo. Switches SYNC off."),
            (self.button_resync, "Press on the first beat of a bar: the next beat becomes beat 1 and the four LEDs restart. "
                                 "Also sends the resync command."),
            (self.button_startstop, "Start or stop listening to the audio input and sending OSC."),
            (self.button_tap, "Tap along with the beat (3 or more taps) to set the tempo by hand. Switches SYNC off."),
        ]
        for widget, text in tips:
            widget.SetToolTip(text)

        short = {
            self.text_ip: "IP address of the computer that receives the tempo (127.0.0.1 = this computer).",
            self.text_port: "OSC port of the receiver (Resolume listens on 7000).",
            self.choice_target: "Which program receives the tempo: Resolume, grandMA3 or a plain BPM number.",
            self.button_ping: "Check that the IP address answers (does not test OSC itself).",
            self.audio_selection: "Audio input: mixer or interface for a DJ deck, System sound for YouTube or Spotify.",
            self.peak_meter: "Input level. If it stays empty, the app hears nothing.",
            self.button_sync: "SYNC on: SEND follows the music. Off: SEND keeps its value (use +1, -1, x2, /2, TAP).",
            self.button_halftime: "Send half of the live tempo (140 becomes 70). Only while SYNC is on.",
            self.button_lock: f"LOCK: tempo changes over {lock_pct}% are sent after {lock_sec} seconds, small ones at once.",
            self.button_plus_one: "Raise the sent tempo by 1 BPM (switches SYNC off).",
            self.button_minus_one: "Lower the sent tempo by 1 BPM (switches SYNC off).",
            self.button_double: "Double the sent tempo, for when the app reads half (switches SYNC off).",
            self.button_half: "Halve the sent tempo, for when the app reads double (switches SYNC off).",
            self.button_resync: "Press on beat 1 of a bar: the LEDs restart and the next beat counts as 1.",
            self.button_startstop: "Start or stop listening to the audio input and sending OSC.",
            self.button_tap: "Tap along with the beat (3 or more taps) to set the tempo by hand (switches SYNC off).",
        }
        for widget, text in short.items():
            self.attach_help(widget, text)
        for digit in self.live_disp:
            digit.SetToolTip("LIVE: the tempo the app hears right now.")
            self.attach_help(digit, "LIVE: the tempo the app hears right now.")
        for digit in self.send_disp:
            digit.SetToolTip("SEND: the tempo that is sent to the receiving program.")
            self.attach_help(digit, "SEND: the tempo that is sent to the receiving program.")
        for led in self.leds:
            led.SetToolTip("The four beats of the bar. The bright one is the current beat.")
            self.attach_help(led, "The four beats of the bar (the bright one is the current beat).")

    def config_int(self, key, default):
        try:
            return int(self.config['UI'].get(key, default))
        except (ValueError, KeyError):
            return int(default)

    def fit_to_screen(self, w, h):
        """Makes sure the window does not open bigger than the screen it is on (title bar, status bar and dock allowed for)"""
        try:
            area = wx.Display(max(0, wx.Display.GetFromWindow(self))).GetClientArea()
            fit_w, fit_h = int(area.width) - 20, int(area.height) - 130
            if fit_w >= 600 and fit_h >= 560:   # only trust sane values
                return max(600, min(w, fit_w)), max(560, min(h, fit_h))
        except Exception:
            pass
        return w, h

    def target_index(self) -> int:
        """0 = Resolume, 1 = grandMA3, 2 = raw BPM (taken from the config)"""
        cfg = self.config['OSC']
        if cfg.get('RESOLUME', 'False') == 'True':
            return 0
        return 1 if cfg.get('GRANDMA3_MASTER', '') != '' else 2

    def on_target_change(self, event):
        """Switches the OSC format right away (also while running)"""
        cfg = self.config['OSC']
        sel = self.choice_target.GetSelection()
        if sel == 1:      # grandMA3: 'Master <id> At BPM <bpm>' command
            cfg['RESOLUME'] = 'False'
            cfg['GRANDMA3_MASTER'] = cfg.get('GRANDMA3_ID', '3.1') or '3.1'
        elif sel == 2:    # plain BPM number to BPM_ADRESS
            cfg['RESOLUME'] = 'False'
            cfg['GRANDMA3_MASTER'] = ''
        else:             # Resolume: (bpm - 20) / 480 to BPM_ADRESS
            cfg['RESOLUME'] = 'True'
            cfg['GRANDMA3_MASTER'] = ''
        # send the current value to the new target at once
        if self.running:
            self.send_osc_value(self.send_bpm // self.beat_divider if self.sync else self.send_bpm)

    def make_bitmap(self, name: str, box_w: int, box_h: int):
        """Loads pics/<name> scaled to fit into box_w x box_h (aspect ratio kept), at 2x for retina screens.
        Returns (bitmap, (logical width, logical height))"""
        im = PILImage.open(resource_path("pics", name)).convert('RGBA')
        w, h = im.size
        scale = min(box_w * 2 / w, box_h * 2 / h)
        nw, nh = max(2, round(w * scale)), max(2, round(h * scale))
        im = im.resize((nw, nh), PILImage.LANCZOS)
        return pil_to_bitmap(im, 2.0), (nw // 2, nh // 2)

    def on_panel_paint(self, event):
        """Paints the window background: one cached bitmap, so a repaint is a single fast copy"""
        dc = wx.PaintDC(self.panel)
        bmp = self.panel_background()
        if bmp is not None:
            dc.DrawBitmap(bmp, 0, 0, False)
        else:
            dc.SetBackground(wx.Brush(self.bg_grey))
            dc.Clear()

    def panel_background(self):
        """Background picture + soft white panels behind every group as ONE bitmap.
        Rebuilt only when the window size or the position of a group changes."""
        w, h = self.panel.GetClientSize()
        w, h = int(w), int(h)
        if w < 2 or h < 2:
            return None
        rects = self.card_rects()
        key = (w, h, tuple(rects))
        if self.panel_cache[0] == key:
            return self.panel_cache[1]
        bmp = wx.Bitmap(w, h)
        mdc = wx.MemoryDC(bmp)
        mdc.SetBackground(wx.Brush(self.bg_grey))
        mdc.Clear()
        picture = self.background_bitmap()
        if picture is not None:
            mdc.DrawBitmap(picture, 0, 0, True)
        if rects:
            gc = wx.GCDC(mdc)
            gc.SetPen(wx.TRANSPARENT_PEN)
            for x, y, rw, rh, colour in rects:
                gc.SetBrush(wx.Brush(wx.Colour(*colour)))
                gc.DrawRoundedRectangle(x, y, rw, rh, 10)
            del gc
        mdc.SelectObject(wx.NullBitmap)
        self.panel_cache = (key, bmp)
        return bmp

    def card_rects(self):
        """Rectangles (x, y, w, h, colour) for the soft panels: the box around each group of real controls"""
        rects = []
        for widgets, colour in getattr(self, 'card_groups', []):
            try:
                boxes = [w.GetRect() for w in widgets]
                x0 = min(r.x for r in boxes)
                y0 = min(r.y for r in boxes)
                x1 = max(r.x + r.width for r in boxes)
                y1 = max(r.y + r.height for r in boxes)
                if x1 > x0 and y1 > y0:
                    rects.append((x0 - 6, y0 - 6, x1 - x0 + 12, y1 - y0 + 12, colour))
            except Exception:
                pass
        return rects

    def background_bitmap(self):
        """Background picture scaled to cover the whole window (cached per window size)"""
        if self.bg_source is None:
            return None
        w, h = self.panel.GetClientSize()
        w, h = int(w), int(h)
        if w < 2 or h < 2:
            return None
        if self.bg_cache[0] != (w, h):
            sw, sh = self.bg_source.size
            scale = max(w / sw, h / sh)
            nw, nh = max(w, round(sw * scale)), max(h, round(sh * scale))
            im = self.bg_source.resize((nw, nh), PILImage.LANCZOS)
            left, top = (nw - w) // 2, (nh - h) // 2
            im = im.crop((left, top, left + w, top + h))
            r, g, b, a = im.split()
            a = a.point(lambda v: int(v * self.bg_opacity))
            im = PILImage.merge('RGBA', (r, g, b, a))
            self.bg_cache = ((w, h), pil_to_bitmap(im))
        return self.bg_cache[1]

    def on_button_reload(self, event):
        """Fills the dropdown with all input devices (real device index is kept in self.audio_device_ids).
        On macOS, install BlackHole to see the computer's internal sound here."""
        self.audio_selection.Clear()
        self.audio_device_ids = []

        try:
            default_index = int(self.audio.get_default_input_device_info()['index'])
        except Exception:
            default_index = None

        # macOS: capture the computer's own sound (Spotify, YouTube ...) without any extra driver
        if beatfinder.system_audio_helper() is not None:
            self.audio_selection.Append(SYSTEM_SOUND_NAME)
            self.audio_device_ids.append(beatfinder.SYSTEM_AUDIO)

        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if int(info.get('maxInputChannels', 0)) <= 0:
                continue
            name = info.get('name', f"Device {i}")
            if i == default_index:
                name = "DEFAULT: " + name
            self.audio_selection.Append(name)
            self.audio_device_ids.append(i)

        # pick: saved device > system sound > BlackHole > default input > first entry
        saved = self.config['AUDIO'].get('device_name', '')
        names = [self.audio_selection.GetString(n) for n in range(self.audio_selection.GetCount())]
        choice = 0
        for n, name in enumerate(names):
            if saved and name.replace("DEFAULT: ", "") == saved:
                choice = n
                break
        else:
            if beatfinder.SYSTEM_AUDIO in self.audio_device_ids:
                choice = self.audio_device_ids.index(beatfinder.SYSTEM_AUDIO)
            else:
                for n, name in enumerate(names):
                    if "blackhole" in name.lower():
                        choice = n
                        break
                else:
                    if default_index in self.audio_device_ids:
                        choice = self.audio_device_ids.index(default_index)

        if names:
            self.audio_selection.SetSelection(choice)

    def on_button_tap(self, event):
        """count time between taps and calculate bpm\n
        Args:
            event (ex.EVENT): unused
        """

        # calculate only after 2+ tabs
        if len(self.last_tap) > 1:

            # add time and timedelta to list
            self.last_tap.append((time(), time() - self.last_tap[-1][0]))

            # Clear if previous taps are too old
            if time() - self.last_tap[-2][0] > 1:
                self.last_tap = [self.last_tap[-1]]

            else:
                # disable sync if it was enabled
                # this also starts the osc sending thread
                if self.sync:
                    self.switch_sync(False)

                # Calculate the BPM, set variable for thread and display it
                self.send_bpm = round(60 / (sum([x[1] for x in self.last_tap[1:]]) / len(self.last_tap[1:])))
                self.update_bpm_display(self.send_bpm, send_to="send")
                self.send_current_bpm()

        elif len(self.last_tap) == 1:
            self.last_tap.append((time(), time() - self.last_tap[-1][0]))

        else:
            # if no taps yet, add first tap
            self.last_tap.append((time(), None))

    def on_button_sync(self, event):
        self.switch_sync(self.button_sync.GetValue())

    def on_button_resync(self, event):
        # reset bar animation
        self.next_led(reset=True)

        # restart the beat period of the no-sync sender thread (if it runs)
        if self.sender is not None and not self.sender.stop:
            self.sender.wake.set()

        if self.beatfinder:
            self.beatfinder.beat_counter = 0
            self.beatfinder.resync_bar()

    def on_button_startstop(self, event):
        """Starts and stops the audio and OSC stream

        Args:
            event (wx.EVENT): unused
        """
        if self.button_startstop.GetValue():
            print("Starting")

            sel = self.audio_selection.GetSelection()
            if sel == wx.NOT_FOUND or sel >= len(self.audio_device_ids):
                wx.MessageBox("No audio input device found.", "BPMtoOSC", wx.ICON_ERROR | wx.OK)
                self.button_startstop.SetValue(False)
                return

            # save input to config
            self.config['AUDIO']['device_name'] = self.audio_selection.GetString(sel).replace("DEFAULT: ", "")
            self.config['OSC']['IP'] = clean_ip(self.text_ip.GetValue())
            self.config['OSC']['PORT'] = str(self.text_port.GetValue())

            # create worker instances
            try:
                self.osc_client = osc_client.OSCclient(self.config['OSC']['IP'], int(self.config['OSC']['PORT']))
                self.running = True
                self.reconnect_at = 0.0
                self.beatfinder = beatfinder.BeatDetector(self.osc_client, self.audio_device_ids[sel], parent=self)
            except Exception as e:
                self.running = False
                self.beatfinder = None
                self.osc_client = None
                self.button_startstop.SetValue(False)
                wx.MessageBox(f"Could not start:\n{e}",
                              "BPMtoOSC", wx.ICON_ERROR | wx.OK)
                return

            # gui changes
            self.button_startstop.SetLabel("STOP")
            self.audio_selection.Disable()
            for b in self.buttons_to_disable:
                b.Enable()

        else:
            print("Stopping")
            self.stop_workers()

            # gui changes
            self.button_startstop.SetLabel("START / STOP")
            self.audio_selection.Enable()
            for b in self.buttons_to_disable:
                b.Disable()
            self.peak_meter.SetData([0], 0, 1)
            self.update_bpm_display("---", send_to="both")

    def stop_workers(self):
        """Stops audio stream, sender thread and OSC client"""
        self.running = False

        # restore sync (this also ends the no-sync sender thread)
        if not self.sync:
            self.switch_sync(True)
        self.stop_sender()

        if self.beatfinder is not None:
            self.beatfinder.close()
            self.beatfinder = None
        self.osc_client = None

    def on_button_ping(self, event):
        """Pings the IP in a background thread (the window stays responsive)"""
        ip = clean_ip(self.text_ip.GetValue())
        self.config['OSC']['IP'] = ip
        self.config['OSC']['PORT'] = str(self.text_port.GetValue())

        self.button_ping.Disable()
        self.text_connection.SetForegroundColour((120, 120, 120))
        self.text_connection.SetLabel("Pinging...")
        self.ip_stuff_sizer.Layout()

        def worker():
            os_name = system().lower()
            if os_name == 'darwin':
                command = ['/sbin/ping', '-c', '1', '-t', '2', ip]
            elif os_name == 'windows':
                command = ['ping', '-n', '1', '-w', '2000', ip]
            else:
                command = ['ping', '-c', '1', '-W', '2', ip]
            try:
                ok = run(command, stdout=DEVNULL, stderr=DEVNULL, timeout=6).returncode == 0
            except Exception as e:
                print("Ping error:", e)
                ok = False
            wx.CallAfter(self.ping_done, ok)

        Thread(target=worker, daemon=True).start()

    def ping_done(self, ok: bool):
        """Runs in the GUI thread after the ping finished"""
        if ok:
            self.text_connection.SetForegroundColour((0, 184, 0))
            self.text_connection.SetLabel("Reachable")
        else:
            self.text_connection.SetForegroundColour((220, 0, 0))
            self.text_connection.SetLabel("Unreachable")
        self.text_connection.Refresh()
        self.ip_stuff_sizer.Layout()
        self.button_ping.Enable()

    def stop_sender(self):
        """Ends the no-sync sender thread (if any)"""
        if self.sender is not None:
            self.sender.stop = True
            self.sender.wake.set()
            self.sender = None

    def switch_sync(self, state: bool):
        """Changes state of sync flag and button\n
        Starts thread to emit "send bpm" while sync is off

        Args:
            state (bool): State to switch to
        """
        #stop sync
        if not state and self.sync:
            self.sync = False
            self.button_sync.SetValue(False)
            self.button_sync.SetLabel('SYNC\nOFF')

            # the SEND value is the divided live bpm from now on
            self.send_bpm //= self.beat_divider
            self.last_sent_bpm = None  # forces the thread to send the current value once

            self.stop_sender()
            self.sender = SenderCtl()
            self.no_sync_send_thread = Thread(target=self.send_thread_when_no_sync, args=(self.sender,), daemon=True)
            self.no_sync_send_thread.start()

            self.button_halftime.Disable()

        #start sync
        elif state and not self.sync:
            self.stop_sender()
            self.sync = True
            self.button_sync.SetValue(True)
            self.button_sync.SetLabel('SYNC\nON')

            self.button_halftime.SetValue(True if self.beat_divider == 2 else False)
            self.button_halftime.Enable()

            # jump to the live bpm right away
            bf = self.beatfinder
            if self.running and bf is not None and bf.detected:
                bf.accept_live()
        else:
            print("Sync state already set to {}".format(state))

    def update_bpm_display(self, bpm, send_to: str = "both", Blink=False):
        """Iterates through digits and sets them accordinglly

        Args:
            bpm (int | str): bpm value to set
            send_to (str): wich display to update, can be "both", "live" or "send". Defaults to "both."
            Blink (bool, optional): wether the background should alternate color. Defaults to False.
        """
        # convert to array of chars

        def set_digits(bpm, send_to):

            bpm_digits = [d for d in str(bpm)]
            bpm_digits.reverse()

            def send_to_disp(disp):
                """actual update function

                Args:
                    disp (self.live_disp | self.send_disp): display to update
                """
                # set new background color
                new_bg = self.bg_a[::-1] if disp[0].GetColours()['background'] == self.bg_grey else self.bg_a
                
                for i, digit in enumerate(disp):

                    # set 0 in front if bpm has less than 3 digits
                    if len(bpm_digits) > 2:
                        digit.SetValue(bpm_digits[2-i])
                    else:
                        digit.SetValue(bpm_digits[2-i] if i > 0 else 0)

                    # blinking background
                    if Blink:
                        digit.SetColours(background=new_bg[0], segment_off=new_bg[1])


            # send to display
            if send_to == "both":
                send_to_disp(self.live_disp)
                send_to_disp(self.send_disp)
            elif send_to == "send":
                send_to_disp(self.send_disp)
            elif send_to == "live":
                send_to_disp(self.live_disp)

            self.bpm_blink = not (self.bpm_blink)

        # GUI may only be touched from the main thread (important on macOS)
        wx.CallAfter(set_digits, bpm, send_to)

    def next_led(self, reset=False, thread=True):

        def set_leds(rst):
            def set_background(led, color: tuple):
                # need to update label to see changes
                led.SetBackgroundColour(color)
                led.Refresh()
            if rst:
                set_background(self.leds[0], (200, 0, 0))
                set_background(self.leds[1], (50, 0, 0))
                set_background(self.leds[2], (50, 0, 0))
                set_background(self.leds[3], (50, 0, 0))
                if self.led_counter == 0:
                    self.led_counter = -1
                else:
                    self.led_counter = 3
            if self.led_counter < 3:
                set_background(self.leds[self.led_counter], (50, 0, 0))
                set_background(self.leds[self.led_counter+1], (200, 0, 0))
                self.led_counter += 1
            else:
                self.led_counter = 0
                set_background(self.leds[-1], (50, 0, 0))
                set_background(self.leds[self.led_counter], (200, 0, 0))

            # OSC bar position: same beat as the LEDs (follows 1/2, tap tempo and RESYNC BAR)
            self.send_beat_outputs(self.led_counter)

        # GUI may only be touched from the main thread (important on macOS)
        wx.CallAfter(set_leds, reset)

    def send_beat_outputs(self, position: int):
        """Sends the bar position (0 to 3) as OSC:
        count = 0,1,2,3   one = 1 on beat 1 only (else 0)   two = 1 on beats 1 and 3 (else 0)"""
        client = self.osc_client
        cfg = self.config['OSC']
        if client is None or not self.running or cfg.get('BEAT_OUTPUTS', 'True') != 'True':
            return
        position = max(0, min(3, int(position)))
        client.send_osc(cfg.get('BPM_COUNT_ADDRESS', '/beat/count'), position)
        client.send_osc(cfg.get('BPM_ONE_ADDRESS', '/beat/one'), 1 if position == 0 else 0)
        client.send_osc(cfg.get('BPM_TWO_ADDRESS', '/beat/two'), 1 if position in (0, 2) else 0)

    def send_thread_when_no_sync(self, ctl):
        """When sync is disabled, this thread runs the bar LEDs in the speed of the SEND display and
        sends the value of the SEND display whenever it changed (the buttons also send instantly).
        It ends when ctl.stop is set. ctl.wake restarts the beat period (RESYNC BAR).
        """
        while not ctl.stop:
            started = time()
            self.next_led()

            if self.last_sent_bpm != self.send_bpm:
                self.send_current_bpm()
                self.update_bpm_display(self.send_bpm, send_to="send", Blink=True)

            period = 60.0 / max(20, self.send_bpm)
            # efficient wait that can be interrupted at any time
            if ctl.wake.wait(max(0.0, period - (time() - started))):
                ctl.wake.clear()
                if ctl.stop:
                    return

    def close(self, event):  # save settings to ini and close down
        """Ask User if event can be vetoed (No force close event).
        """
        # no confirm question: Cmd+Q and the red button always quit at once
        try:
            self.uv_timer.Stop()
        except Exception:
            pass

        # save settings first, so nothing can block it
        try:
            self.CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self.CONF_PATH, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            print("Could not save config:", e)

        # stop audio and threads in the background, closing a device can hang
        worker = Thread(target=self.stop_workers, daemon=True)
        worker.start()
        worker.join(1.5)

        # end the process for sure, even if a thread or the audio helper is stuck
        try:
            self.Destroy()
        except Exception:
            pass
        import os, sys
        sys.stdout.flush()
        os._exit(0)


def main():
    app = wx.App(False)
    app.SetAppName("BPMtoOSC RXv2 Mossad Spyware")
    frame = Main_Frame(None)
    app.SetTopWindow(frame)
    frame.Show()
    app.MainLoop()


# debug and testing
if __name__ == '__main__':
    #import wx.lib.inspection
    # wx.lib.inspection.InspectionTool().Show()
    main()
