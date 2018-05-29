#!/usr/bin/env python3
"""A virtual KM switch for use with QEMU input-linux"""

import time
import os
import re
import asyncio
import evdev

import ecodes

KBD = '/dev/input/by-id/usb-04d9_USB_Keyboard-event-kbd'
MOUSE = '/dev/input/by-id/usb-Kingsis_Peripherals_ZOWIE_Gaming_mouse-event-mouse'

def cur_time_components():
    """Current time as (seconds, microseconds)"""
    time_now = time.time()
    sec_now = int(time_now)
    usec_now = int((time_now - sec_now) * 10**6)
    return sec_now, usec_now

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

    def add_virtual_device_group(self, hotkey, base, notify_key=None):
        """Add a virtual keyboard and a mouse that are activated with `hotkey`."""
        # pylint: disable=multiple-statements
        virt_kbd = evdev.UInput.from_device(self.hw_kbd, name=f'{base}-virt-kbd')
        virt_mouse = evdev.UInput.from_device(self.hw_mouse, name=f'{base}-virt-mouse')
        for device in virt_kbd, virt_mouse:
            temp_path = os.path.join('/tmp', re.sub(r'[^a-z]', '_', device.name, re.I))
            with open(temp_path, 'w') as fh: # pylint: disable=invalid-name
                fh.write(device.device.fn)

        self.virt_group_by_hotkey[hotkey] = {
            self.hw_kbd.fd: virt_kbd,
            self.hw_mouse.fd: virt_mouse,
            'notify_key': notify_key,
        }

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
            for hw_fd in self.hw_by_fd:
                try: self.hw_by_fd[hw_fd].grab()
                except IOError: pass
            self.active_virt_group = hotkey

    def start_loop(self):
        """Start the virtual KM switch operation"""
        for device in self.hw_kbd, self.hw_mouse:
            asyncio.ensure_future(self._handle_events(device))

        loop = asyncio.get_event_loop()
        loop.run_forever()

    async def _handle_events(self, device):
        original_fd = device.fd
        original_fn = device.fn

        while True:
            if device:
                try:
                    await self._try_handle_events(device, original_fd)
                # device disconnected
                except OSError:
                    print(f'{device.fn} disconnected')
                    device.close()
                    device = None
            else:
                try:
                    device = evdev.InputDevice(original_fn)
                    self.hw_by_fd[original_fd] = device
                    print(f'{device.fn} reconnected')
                except FileNotFoundError:
                    time.sleep(1)

    async def _try_handle_events(self, device, original_fd):
        async for event in device.async_read_loop():
            # toggle hotkeys
            if event.type == ecodes.EV_KEY and event.code == self.noswitch_toggle:
                if event.value == 0:
                    self.noswitch = not self.noswitch
                    self.hw_kbd.set_led(ecodes.LED_SCROLLL, self.noswitch)
                continue
            # let switch key through
            elif self.noswitch or self.noswitch_modifier in self.hw_kbd.active_keys():
                pass
            # start redirecting input to a virtual device
            elif event.code in self.virt_group_by_hotkey:
                if event.value == 0:
                    self.set_active(True, event.code)
                    notify_key = self.virt_group_by_hotkey[event.code]['notify_key']
                    if notify_key:
                        self._simulate_keypress(notify_key, original_fd)
                continue
            # stop redirecting input to virtual devices
            elif event.code == self.hw_hotkey:
                if event.value == 0:
                    self.set_active(False)
                continue

            self._route_event(event, original_fd)

    def _route_event(self, event, original_fd, artificial=False):
        if self.active_virt_group is not None:
            if ((event.code in self.remaps) and
                    (not self.noswitch and
                     not self.noswitch_modifier in self.hw_kbd.active_keys())):
                event.code = self.remaps[event.code]

            if event.code in self.broadcast_keys:
                virt_groups = self.virt_group_by_hotkey.values()
            else:
                virt_groups = [self.virt_group_by_hotkey[self.active_virt_group]]

            for virt_group in virt_groups:
                if event.type == ecodes.SYN_REPORT:
                    virt_group[original_fd].syn()
                else:
                    virt_group[original_fd].write_event(event)
                    # workaround. could the bug be related to thread safety?
                    if artificial or event.code in self.broadcast_keys:
                        virt_group[original_fd].syn()

    def _simulate_keypress(self, keycode, original_fd):
        key_down = evdev.InputEvent(*cur_time_components(), ecodes.EV_KEY, keycode, 1)
        self._route_event(key_down, original_fd, artificial=True)
        key_up = evdev.InputEvent(*cur_time_components(), ecodes.EV_KEY, keycode, 0)
        self._route_event(key_up, original_fd, artificial=True)

def main():
    """Initialize the KM switch and start it"""
    km_switch = VirtualKMSwitch(KBD, MOUSE)

    # map F1 and F2 to switching a virtual device and notify about the switch by
    # sending KEY_KP1 or KEY_KP2
    km_switch.add_virtual_device_group(ecodes.KEY_F1, 'windows', notify_key=ecodes.KEY_KP1)
    km_switch.add_virtual_device_group(ecodes.KEY_F2, 'linux', notify_key=ecodes.KEY_KP2)

    km_switch.add_broadcast_key(ecodes.KEY_MUHENKAN)
    km_switch.add_broadcast_key(ecodes.KEY_KP1)
    km_switch.add_broadcast_key(ecodes.KEY_KP2)
    km_switch.add_broadcast_key(ecodes.KEY_KP4)
    km_switch.set_noswitch_modifier(ecodes.KEY_MUHENKAN)
    km_switch.set_noswitch_toggle(ecodes.KEY_ESC)

    km_switch.remaps[ecodes.KEY_F4] = ecodes.KEY_KP4

    km_switch.set_active(True, ecodes.KEY_F2)

    km_switch.start_loop()

if __name__ == '__main__':
    main()
