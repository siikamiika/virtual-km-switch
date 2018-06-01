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
        data = self.rfile.readline().decode('utf-8').strip()
        if data == 'windows':
            self.server.km_switch.set_active(ecodes.KEY_F1)
        elif data == 'linux':
            self.server.km_switch.set_active(ecodes.KEY_F2)

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):

    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.auth = kwargs.pop('auth')
        self.km_switch = kwargs.pop('km_switch')
        super().__init__(*args, **kwargs)

def main():
    kbd_path = '/dev/input/by-id/usb-04d9_USB_Keyboard-event-kbd'
    mouse_path = '/dev/input/by-id/usb-Kingsis_Peripherals_ZOWIE_Gaming_mouse-event-mouse'
    km_switch = VirtualKMSwitch(kbd_path, mouse_path)

    # map F1 and F2 to switching a virtual device and notify about the switch by
    # sending KEY_KP1 or KEY_KP2
    km_switch.add_virtual_device_group(ecodes.KEY_F1, 'windows', notify_key=ecodes.KEY_KP1)
    km_switch.add_virtual_device_group(ecodes.KEY_F2, 'linux', notify_key=ecodes.KEY_KP2)

    # broadcast VoIP key
    km_switch.add_broadcast_key(ecodes.KEY_MUHENKAN)
    # # broadcast `notify_key`s
    # km_switch.add_broadcast_key(ecodes.KEY_KP1)
    # km_switch.add_broadcast_key(ecodes.KEY_KP2)
    # broadcast remapped key (normal mode only)
    km_switch.add_broadcast_key(ecodes.KEY_KP4)

    # set noswitch modifier and lock
    km_switch.set_noswitch_modifier(ecodes.KEY_MUHENKAN)
    km_switch.set_noswitch_toggle(ecodes.KEY_ESC)

    # remaps (normal mode only)
    km_switch.remaps[ecodes.KEY_F4] = ecodes.KEY_KP4

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
