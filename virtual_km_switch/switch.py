#!/usr/bin/env python3
"""A virtual KM switch for use with QEMU input-linux"""

import time
import os
import sys
import re
import threading
from select import select

import evdev

from .ecodes import * # pylint: disable=wildcard-import,unused-wildcard-import

TIMED_METHODS = {}

def timeit(method):
    """A decorator for measuring the time spent on running some frequently used method"""
    def _timed_method(*args, **kwargs):
        start = time.perf_counter()
        result = method(*args, **kwargs)
        end = time.perf_counter()

        if method.__name__ not in TIMED_METHODS:
            TIMED_METHODS[method.__name__] = []
        method_times = TIMED_METHODS[method.__name__]
        method_times.append(end - start)
        if len(method_times) == 1000:
            print(f'{method.__name__}: {sum(method_times) / len(method_times)}', file=sys.stderr)
            del method_times[:]

        return result

    return _timed_method

class VirtualInputGroup(object):
    """A uinput keyboard and mouse"""
    def __init__(self, hw_kbd, hw_mouse, name, notify_key=None):
        self.kbd = evdev.UInput.from_device(hw_kbd, name=f'{name}-virt-kbd')
        self.mouse = evdev.UInput.from_device(hw_mouse, name=f'{name}-virt-mouse')
        self.notify_key = notify_key

        self.mouse_move_x = 0
        self.mouse_move_y = 0

        # a hacky way to find these devices
        for device in self.kbd, self.mouse:
            temp_path = os.path.join('/tmp', re.sub(r'[^a-z]', '_', device.name, re.I))
            with open(temp_path, 'w') as fh: # pylint: disable=invalid-name
                fh.write(device.device.fn)

    # key events
    def write_key(self, key, value):
        """Emit a key event"""
        self.kbd.write(EV_KEY, key, value)
        self.kbd.syn()

    def press_and_release_key(self, key):
        """Simulate a key press and release"""
        self.kbd.write(EV_KEY, key, 1)
        self.kbd.syn()
        self.kbd.write(EV_KEY, key, 0)
        self.kbd.syn()

    def release_keys(self):
        """Release all keys that are active. Used before switching to another virtual input group"""
        for key in self.kbd.device.active_keys():
            self.kbd.write(EV_KEY, key, 0)
            self.kbd.syn()

    # mouse events
    def queue_mouse_move(self, code, value):
        """Used to combine many small events into an atomic mouse move"""
        if code == REL_X:
            self.mouse_move_x += value
        elif code == REL_Y:
            self.mouse_move_y += value

    def commit_mouse(self):
        """If the mouse has moved, emit a mouse move event"""
        syn = False
        if self.mouse_move_x:
            self.mouse.write(EV_REL, REL_X, self.mouse_move_x)
            syn = True
        if self.mouse_move_y:
            self.mouse.write(EV_REL, REL_Y, self.mouse_move_y)
            syn = True
        if syn:
            self.mouse.syn()
            self.mouse_move_x = self.mouse_move_y = 0

    def write_mouse_button(self, button, value):
        """Emit a mouse button event"""
        self.mouse.write(EV_KEY, button, value)
        self.mouse.syn()

    def scroll_mouse(self, value):
        """Emit a mouse scroll event"""
        self.mouse.write(EV_REL, REL_WHEEL, value)
        self.mouse.syn()


class VirtualKMSwitch(object): # pylint: disable=too-many-instance-attributes
    """Grabs a hardware keyboard and a mouse and redirects their input events
    to virtual input devices."""
    def __init__(self, keyboard, mouse):
        # event sources
        self.hw_kbd = evdev.InputDevice(keyboard)
        self.hw_mouse = evdev.InputDevice(mouse)
        self.hw_by_fd = {dev.fd: dev for dev in [self.hw_kbd, self.hw_mouse]}
        self.hw_hotkey = None
        # event destinations
        self.virt_group_by_hotkey = {}
        self.active_virt_group = None
        # special
        self.broadcast_keys = set()
        self.remaps = dict()
        self.noswitch_modifier = None
        self.noswitch_toggle = None
        self.noswitch = False

    def add_virtual_device_group(self, hotkey, name, notify_key=None):
        """Add a virtual keyboard and a mouse that are activated with `hotkey`."""
        self.virt_group_by_hotkey[hotkey] = VirtualInputGroup(
            self.hw_kbd, self.hw_mouse, name, notify_key=notify_key)

    def add_broadcast_key(self, keycode):
        """A key to be sent to every virtual device"""
        self.broadcast_keys.add(keycode)

    def set_noswitch_modifier(self, keycode):
        """When holding this key, switch hotkeys will be sent to the virtual device"""
        self.noswitch_modifier = keycode

    def set_noswitch_toggle(self, keycode):
        """Toggles switch hotkey functionality"""
        self.noswitch_toggle = keycode

    def set_active(self, active, hotkey=None):
        """Activate a virtual device or restore hw controls"""
        # pylint: disable=multiple-statements
        if not active:
            for hw_fd in self.hw_by_fd:
                try: self.hw_by_fd[hw_fd].ungrab()
                except IOError: pass
            self.active_virt_group = None
        else:
            if self.active_virt_group is None:
                for hw_fd in self.hw_by_fd:
                    try: self.hw_by_fd[hw_fd].grab()
                    except IOError: pass
            self.active_virt_group = hotkey

    def start_loop(self):
        """Start the virtual KM switch operation"""
        disconnected_fds = set()

        while True:
            try:
                # select readable devices from hw keyboard and mouse
                readable_fds, _, _ = select(set(self.hw_by_fd) - disconnected_fds, [], [])
                for readable_fd in readable_fds:
                    for event in self.hw_by_fd[readable_fd].read():
                        self._handle_event(event)
            # device disconnected
            except OSError:
                print(f'{self.hw_by_fd[readable_fd].fn} disconnected', file=sys.stderr)
                self.hw_by_fd[readable_fd].close()
                # don't try to read from this fd for now
                disconnected_fds.add(readable_fd)
                # try to reconnect in background
                reconnect_thread = threading.Thread(
                    target=self._reconnect_device, args=(readable_fd, disconnected_fds))
                reconnect_thread.start()

            # send a single mouse event consisting of multiple smaller ones
            for virt_group in self.virt_group_by_hotkey.values():
                virt_group.commit_mouse()

            # fixes some race condition or something
            time.sleep(0.005)

    def _reconnect_device(self, disconnected_fd, disconnected_fds):
        while True:
            try:
                disconnected_device = self.hw_by_fd[disconnected_fd]
                device = evdev.InputDevice(disconnected_device.fn)
                # grab device to avoid double events
                if self.active_virt_group is not None:
                    device.grab()
                # replace references to the disconnected device with the new one
                if disconnected_device is self.hw_kbd:
                    self.hw_kbd = device
                else:
                    self.hw_mouse = device
                del self.hw_by_fd[disconnected_fd]
                self.hw_by_fd[device.fd] = device
                # select from this device again
                disconnected_fds.remove(disconnected_fd)
                return print(f'{self.hw_by_fd[device.fd].fn} reconnected', file=sys.stderr)
            # device is not back yet, wait
            except (FileNotFoundError, OSError):
                time.sleep(1)

    def _handle_event(self, event):
        # ignore noise
        if event.type not in {EV_KEY, EV_REL}:
            return

        # key event
        if event.type == EV_KEY:
            # toggle noswitch mode
            if event.code == self.noswitch_toggle:
                if event.value == 1:
                    self.noswitch = not self.noswitch
                    self.hw_kbd.set_led(LED_SCROLLL, self.noswitch)
                return
            # let switch keys through in noswitch mode
            elif self._is_noswitch():
                pass
            # switch key pressed. start redirecting input to a virtual device
            elif event.code in self.virt_group_by_hotkey:
                if event.value == 1:
                    # release keys from the current virtual device
                    if self.active_virt_group is not None:
                        self.virt_group_by_hotkey[self.active_virt_group].release_keys()
                    # activate the new virtual device and notify about it
                    self.set_active(True, event.code)
                    notify_key = self.virt_group_by_hotkey[event.code].notify_key
                    if notify_key:
                        for virt_group in self.virt_group_by_hotkey.values():
                            virt_group.press_and_release_key(notify_key)
                return
            # hw hotkey pressed. stop redirecting input to virtual devices
            elif event.code == self.hw_hotkey:
                if event.value == 0:
                    self.set_active(False)
                return

        # else/pass:
        self._route_event(event)

    def _route_event(self, event):
        if self.active_virt_group is None:
            return

        def _is_mouse_btn(keycode):
            # BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA
            return 272 <= keycode <= 276

        # key event
        if event.type == EV_KEY and not _is_mouse_btn(event.code):
            # only remap in normal (not noswitch) mode
            if event.code in self.remaps and not self._is_noswitch():
                event.code = self.remaps[event.code]

            # select event recipients
            if event.code in self.broadcast_keys:
                virt_groups = self.virt_group_by_hotkey.values()
            else:
                virt_groups = [self.virt_group_by_hotkey[self.active_virt_group]]

            # route event to recipient(s)
            for virt_group in virt_groups:
                virt_group.write_key(event.code, event.value)
        # mouse button event
        elif event.type == EV_KEY and _is_mouse_btn(event.code):
            virt_group = self.virt_group_by_hotkey[self.active_virt_group]
            virt_group.write_mouse_button(event.code, event.value)
        # mouse move or wheel event
        elif event.type == EV_REL:
            virt_group = self.virt_group_by_hotkey[self.active_virt_group]
            if event.code in {REL_X, REL_Y}:
                virt_group.queue_mouse_move(event.code, event.value)
            elif event.code == REL_WHEEL:
                virt_group.scroll_mouse(event.value)

    def _is_noswitch(self):
        return self.noswitch or self.noswitch_modifier in self.hw_kbd.active_keys()