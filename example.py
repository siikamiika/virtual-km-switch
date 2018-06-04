#!/usr/bin/env python3
"""Example use for the module (currently in use)"""

import os
import threading
import socketserver

from virtual_km_switch import VirtualKMSwitch, ecodes

class Handler(socketserver.StreamRequestHandler):

    def handle(self):
        if self.rfile.readline(0x2000).strip() != self.server.auth:
            return
        data = self.rfile.readline().decode('utf-8').split()
        if data[0] == 'windows':
            self.server.km_switch.set_active(ecodes.KEY_F1)
            mouse_x = -1
        elif data[0] == 'linux':
            self.server.km_switch.set_active(ecodes.KEY_F2)
            mouse_x = 1

        # move mouse away from the edge
        virt_group = self.server.km_switch.virt_group_by_hotkey[
            self.server.km_switch.active_virt_group]
        virt_group.queue_mouse_move(ecodes.REL_X, mouse_x)
        virt_group.commit_mouse()

        current_y = int(data[1])
        if self.server.last_y != -1:
            # perform vertical mouse move in smaller parts
            y_left = current_y - self.server.last_y
            direction = -1 if y_left < 0 else 1
            part_size = direction * 100
            while direction * y_left > 0:
                next_y_left = y_left - part_size
                if direction * next_y_left > 0:
                    actual_part_size = part_size
                else:
                    actual_part_size = y_left
                virt_group.queue_mouse_move(ecodes.REL_Y, actual_part_size)
                virt_group.commit_mouse()
                y_left = next_y_left
        self.server.last_y = current_y

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):

    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.auth = kwargs.pop('auth')
        self.km_switch = kwargs.pop('km_switch')
        self.last_y = -1
        super().__init__(*args, **kwargs)

def main():
    kbd_path = '/dev/input/by-id/usb-04d9_USB_Keyboard-event-kbd'
    mouse_path = '/dev/input/by-id/usb-Kingsis_Peripherals_ZOWIE_Gaming_mouse-event-mouse'
    km_switch = VirtualKMSwitch(kbd_path, mouse_path)

    # map F1 and F2 to switching a virtual device and notify about the switch by
    # sending KEY_KP1 or KEY_KP2
    km_switch.add_virtual_device_group(ecodes.KEY_F1, 'windows', notify_key=ecodes.KEY_KP1)
    km_switch.add_virtual_device_group(ecodes.KEY_F2, 'linux', notify_key=ecodes.KEY_KP2)

    # special callbacks (normal mode only)
    # broadcast VoIP key
    def _handle_key_muhenkan(event):
        for virt_group in km_switch.virt_group_by_hotkey.values():
            virt_group.write_key(event.code, event.value)
    km_switch.add_callback_key(ecodes.KEY_MUHENKAN, _handle_key_muhenkan)
    # send clipboard key to windows only
    def _handle_key_compose(event):
        km_switch.virt_group_by_hotkey[ecodes.KEY_F1].write_key(event.code, event.value)
    km_switch.add_callback_key(ecodes.KEY_COMPOSE, _handle_key_compose)
    # if alt is active, send key as-is (alt+f4)
    # else, remap f4 to keypad 4 and broadcast it
    alt_f4_over = True
    def _handle_key_f4(event):
        nonlocal alt_f4_over
        if ((event.value == 1 and
             {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT} & set(km_switch.hw_kbd.active_keys())) or
                not alt_f4_over):
            if event.value == 0:
                alt_f4_over = True
            else:
                alt_f4_over = False
            virt_group = km_switch.virt_group_by_hotkey[km_switch.active_virt_group]
            virt_group.write_key(event.code, event.value)
        else:
            event.code = ecodes.KEY_KP4
            for virt_group in km_switch.virt_group_by_hotkey.values():
                virt_group.write_key(event.code, event.value)
    km_switch.add_callback_key(ecodes.KEY_F4, _handle_key_f4)

    # set noswitch modifier and lock
    km_switch.set_noswitch_modifier(ecodes.KEY_MUHENKAN)
    km_switch.set_noswitch_toggle(ecodes.KEY_ESC)

    # set linux active
    km_switch.set_active(ecodes.KEY_F2)

    # start loop
    bg_thread = threading.Thread(target=km_switch.start_loop)
    bg_thread.start()

    # server that listens to external switch requests at :9898
    with open(os.path.expanduser('~/.windows-hotkey-server'), 'rb') as f:
        auth = f.read().strip()
    server = Server(('', 9898), Handler, auth=auth, km_switch=km_switch)
    server.serve_forever()

if __name__ == '__main__':
    main()
