#!/usr/bin/env python3
"""A virtual KM switch for use with QEMU input-linux"""

import time
import os
import re
import asyncio
import types

import evdev

import ecodes

KBD = '/dev/input/by-id/usb-04d9_USB_Keyboard-event-kbd'
MOUSE = '/dev/input/by-id/usb-Kingsis_Peripherals_ZOWIE_Gaming_mouse-event-mouse'

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
        accepted_types = {ecodes.EV_KEY, ecodes.EV_REL}
        async for event in device.async_read_loop():
            # ignore noise
            if event.type not in accepted_types:
                continue

            # toggle noswitch mode
            if event.type == ecodes.EV_KEY and event.code == self.noswitch_toggle:
                if event.value == 1:
                    self.noswitch = not self.noswitch
                    self.hw_kbd.set_led(ecodes.LED_SCROLLL, self.noswitch)
                continue
            # let switch keys through in noswitch mode
            elif self._is_noswitch():
                pass
            # switch key pressed. start redirecting input to a virtual device
            elif event.code in self.virt_group_by_hotkey:
                print(evdev.categorize(event))
                if event.value == 1:
                    # release keys from the current virtual device
                    virt_device = self.virt_group_by_hotkey[self.active_virt_group][original_fd]
                    for key in virt_device.device.active_keys():
                        virt_device.write(ecodes.EV_KEY, key, 0)
                        virt_device.syn()

                    # activate the new virtual device and notify about it
                    self.set_active(True, event.code)
                    notify_key = self.virt_group_by_hotkey[event.code].get('notify_key')
                    if notify_key:
                        self._simulate_keypress(notify_key, original_fd)
                continue
            # hw hotkey pressed. stop redirecting input to virtual devices
            elif event.code == self.hw_hotkey:
                if event.value == 0:
                    self.set_active(False)
                continue

            # else/pass:
            self._route_event(event, original_fd)

    def _route_event(self, event, original_fd):
        if self.active_virt_group is not None:
            # only remap in normal (not noswitch) mode
            if event.code in self.remaps and not self._is_noswitch():
                event.code = self.remaps[event.code]

            # select event recipients
            if event.code in self.broadcast_keys:
                virt_groups = self.virt_group_by_hotkey.values()
            else:
                virt_groups = [self.virt_group_by_hotkey[self.active_virt_group]]

            # route event to recipient(s) and call syn()
            for virt_group in virt_groups:
                virt_group[original_fd].write_event(event)
                virt_group[original_fd].syn()

    def _simulate_keypress(self, keycode, original_fd):
        # down
        key_down = types.SimpleNamespace(type=ecodes.EV_KEY, code=keycode, value=1)
        self._route_event(key_down, original_fd)
        # up
        key_up = types.SimpleNamespace(type=ecodes.EV_KEY, code=keycode, value=0)
        self._route_event(key_up, original_fd)

    def _is_noswitch(self):
        return self.noswitch or self.noswitch_modifier in self.hw_kbd.active_keys()

def main():
    """Initialize the KM switch and start it"""
    km_switch = VirtualKMSwitch(KBD, MOUSE)

    # map F1 and F2 to switching a virtual device and notify about the switch by
    # sending KEY_KP1 or KEY_KP2
    km_switch.add_virtual_device_group(ecodes.KEY_F1, 'windows', notify_key=ecodes.KEY_KP1)
    km_switch.add_virtual_device_group(ecodes.KEY_F2, 'linux', notify_key=ecodes.KEY_KP2)

    # broadcast VoIP key
    km_switch.add_broadcast_key(ecodes.KEY_MUHENKAN)
    # broadcast `notify_key`s
    km_switch.add_broadcast_key(ecodes.KEY_KP1)
    km_switch.add_broadcast_key(ecodes.KEY_KP2)
    # broadcast remapped key (normal mode only)
    km_switch.add_broadcast_key(ecodes.KEY_KP4)

    # set noswitch modifier and lock
    km_switch.set_noswitch_modifier(ecodes.KEY_MUHENKAN)
    km_switch.set_noswitch_toggle(ecodes.KEY_ESC)

    # remaps (normal mode only)
    km_switch.remaps[ecodes.KEY_F4] = ecodes.KEY_KP4

    km_switch.set_active(True, ecodes.KEY_F2)

    km_switch.start_loop()

if __name__ == '__main__':
    main()
