#!/usr/bin/env python3
"""A virtual KM switch for use with QEMU input-linux"""

import time
import sys
import threading
from select import select

import evdev

from .ecodes import * # pylint: disable=wildcard-import,unused-wildcard-import

# pylint: disable=multiple-statements

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

    MOUSE_CAP = {
        EV_KEY: [BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA, BTN_FORWARD],
        EV_REL: [REL_X, REL_Y, REL_WHEEL, REL_HWHEEL],
    }

    def __init__(self, hw_kbd, hw_mouse, name, notify_key=None):
        self.kbd = evdev.UInput.from_device(hw_kbd, name=f'{name}-virt-kbd')
        self.mouse = evdev.UInput(VirtualInputGroup.MOUSE_CAP, name=f'{name}-virt-mouse')
        self.notify_key = notify_key

        # active keys and mouse buttons
        self.active_keys = set()

        self.mouse_move_x = 0
        self.mouse_move_y = 0

    # key events
    def write_key(self, key, value):
        """Emit a key event"""
        if value == 1:
            self.active_keys.add(key)
        elif value == 0:
            try: self.active_keys.remove(key)
            except KeyError: pass
        self.kbd.write(EV_KEY, key, value)
        self.kbd.syn()

    def press_and_release_key(self, key):
        """Simulate a key press and release"""
        self.kbd.write(EV_KEY, key, 1)
        self.kbd.syn()
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
        if value == 1:
            self.active_keys.add(button)
        elif value == 0:
            try: self.active_keys.remove(button)
            except KeyError: pass
        self.mouse.write(EV_KEY, button, value)
        self.mouse.syn()

    def scroll_mouse(self, code, value):
        """Emit a mouse scroll event"""
        self.mouse.write(EV_REL, code, value)
        self.mouse.syn()


class VirtualKMSwitch(object): # pylint: disable=too-many-instance-attributes
    """Grabs a hardware keyboard and a mouse and redirects their input events
    to virtual input devices."""
    def __init__(self, keyboard, mouse):
        # event sources
        self.hw_kbd = [evdev.InputDevice(k) for k in (keyboard if isinstance(keyboard, list) else [keyboard])]
        self.hw_mouse = [evdev.InputDevice(m) for m in (mouse if isinstance(mouse, list) else [mouse])]
        self.hw_by_fd = {dev.fd: dev for dev in self.hw_kbd + self.hw_mouse}
        self.hw_hotkey = None
        # event destinations
        self.virt_group_by_hotkey = {}
        self.active_virt_group = None
        # special
        self.callbacks_by_key = dict()
        self.noswitch_modifier = None
        self.noswitch_toggle = None
        self.noswitch = False

    def add_virtual_device_group(self, hotkey, name, notify_key=None):
        """Add a virtual keyboard and a mouse that are activated with `hotkey`."""
        self.virt_group_by_hotkey[hotkey] = VirtualInputGroup(
            self.hw_kbd[0], self.hw_mouse[0], name, notify_key=notify_key)

    def add_callback_key(self, keycode, callback):
        """A key that triggers a callback"""
        if keycode not in self.callbacks_by_key:
            self.callbacks_by_key[keycode] = callbacks = []
        callbacks.append(callback)

    def set_noswitch_modifier(self, keycode):
        """When holding this key, switch hotkeys will be sent to the virtual device"""
        self.noswitch_modifier = keycode

    def set_noswitch_toggle(self, keycode):
        """Toggles switch hotkey functionality"""
        self.noswitch_toggle = keycode

    def set_active(self, hotkey):
        """Activate a virtual device or restore hw controls"""
        if hotkey < 0:
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
                if disconnected_device in self.hw_kbd:
                    self.hw_kbd[self.hw_kbd.index(disconnected_device)] = device
                else:
                    self.hw_mouse[self.hw_mouse.index(disconnected_device)] = device
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
                    self.hw_kbd[0].set_led(LED_SCROLLL, self.noswitch)
                return
            # let switch keys through in noswitch mode
            elif self._is_noswitch():
                pass
            # switch key pressed. start redirecting input to a virtual device
            elif event.code in self.virt_group_by_hotkey:
                if event.value == 1:
                    # activate the new virtual device and notify about it
                    self.set_active(event.code)
                    notify_key = self.virt_group_by_hotkey[event.code].notify_key
                    if notify_key:
                        for virt_group in self.virt_group_by_hotkey.values():
                            virt_group.press_and_release_key(notify_key)
                return
            # hw hotkey pressed. stop redirecting input to virtual devices
            elif event.code == self.hw_hotkey:
                if event.value == 0:
                    self.set_active(-1)
                return

        # else/pass:
        # the event triggers a callback
        if (event.code in self.callbacks_by_key and
                # don't trigger callbacks in noswitch mode except for the modifier
                not (event.code != self.noswitch_modifier and self._is_noswitch())):
            for callback in self.callbacks_by_key[event.code]:
                callback(event)
            return

        self.route_event(event)

    def route_event(self, event):
        """Route an event to the active virtual input group while making sure that
        keys are properly released from the previous one"""
        if self.active_virt_group is None:
            return

        def _is_mouse_btn(keycode):
            # BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA
            return 272 <= keycode <= 276

        # key or mouse button event
        if event.type == EV_KEY:
            # don't send keys unreleased from previous virtual device to a new device
            unreleased_keys = set()
            for hotkey in self.virt_group_by_hotkey:
                if hotkey == self.active_virt_group:
                    continue
                unreleased_keys |= self.virt_group_by_hotkey[hotkey].active_keys
            # route event to recipient(s)
            for hotkey in self.virt_group_by_hotkey:
                virt_group = self.virt_group_by_hotkey[hotkey]
                if ((hotkey == self.active_virt_group and event.code not in unreleased_keys) or
                        (event.code in virt_group.active_keys and event.value == 0)):
                    # mouse button event
                    if _is_mouse_btn(event.code):
                        virt_group.write_mouse_button(event.code, event.value)
                    # key event
                    else:
                        virt_group.write_key(event.code, event.value)
        # mouse move or wheel event
        elif event.type == EV_REL:
            virt_group = self.virt_group_by_hotkey[self.active_virt_group]
            if event.code in {REL_X, REL_Y}:
                virt_group.queue_mouse_move(event.code, event.value)
            elif event.code in {REL_WHEEL, REL_HWHEEL}:
                virt_group.scroll_mouse(event.code, event.value)

    def _is_noswitch(self):
        return self.noswitch or self.noswitch_modifier in self.hw_kbd[0].active_keys()
