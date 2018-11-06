#!/usr/bin/env python3
"""Example use for the module (currently in use)"""

import os
import threading
import socketserver
import time

from virtual_km_switch import VirtualKMSwitch, create_key_event, ecodes

class Handler(socketserver.StreamRequestHandler):

    def handle(self):
        if self.rfile.readline(0x2000).strip() != self.server.auth:
            return
        data = self.rfile.readline().decode('utf-8').split()

        if self.server.km_switch.noswitch:
            return

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
    km_switch.add_callback((ecodes.EV_KEY, ecodes.KEY_MUHENKAN), _handle_key_muhenkan)
    # handle clipboard exchange at windows side
    def _handle_key_compose(event):
        # only on keydown
        if event.value != 1:
            return
        # send keys to this virtual input group
        windows_virt_group = km_switch.virt_group_by_hotkey[ecodes.KEY_F1]
        # if windows is active, send shift+menu, copying linux clipboard to windows
        if km_switch.active_virt_group == ecodes.KEY_F1:
            windows_virt_group.write_key(ecodes.KEY_LEFTSHIFT, 1)
            windows_virt_group.press_and_release_key(event.code)
            windows_virt_group.write_key(ecodes.KEY_LEFTSHIFT, 0)
        # if linux is active, send menu to windows, copying windows clipboard to linux
        else:
            windows_virt_group.press_and_release_key(event.code)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.KEY_COMPOSE), _handle_key_compose)
    # if alt is active, send key as-is (alt+f4)
    # else, remap f4 to keypad 4 and broadcast it
    alt_f4_over = True
    def _handle_key_f4(event):
        # not needed in noswitch mode
        if km_switch.noswitch:
            return km_switch.route_event(event)
        nonlocal alt_f4_over
        if ((event.value == 1 and
             {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT} & set(km_switch.hw_kbd[0].active_keys())) or
                not alt_f4_over):
            if event.value == 0:
                alt_f4_over = True
            else:
                alt_f4_over = False
            km_switch.route_event(event)
        else:
            event.code = ecodes.KEY_KP4
            for virt_group in km_switch.virt_group_by_hotkey.values():
                virt_group.write_key(event.code, event.value)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.KEY_F4), _handle_key_f4)
    # remap numpad numbers to regular number keys (numpad keycodes are used for notifications)
    def remap(from_code, to_code):
        """Simple keycode remap for the active virtual group"""
        def _handle_remap(event):
            event.code = to_code
            km_switch.route_event(event)
        km_switch.add_callback((ecodes.EV_KEY, from_code), _handle_remap)
    remap(ecodes.KEY_KP0, ecodes.KEY_0)
    remap(ecodes.KEY_KP1, ecodes.KEY_1)
    remap(ecodes.KEY_KP2, ecodes.KEY_2)
    remap(ecodes.KEY_KP3, ecodes.KEY_3)
    remap(ecodes.KEY_KP4, ecodes.KEY_4)
    remap(ecodes.KEY_KP5, ecodes.KEY_5)
    remap(ecodes.KEY_KP6, ecodes.KEY_6)
    remap(ecodes.KEY_KP7, ecodes.KEY_7)
    remap(ecodes.KEY_KP8, ecodes.KEY_8)
    remap(ecodes.KEY_KP9, ecodes.KEY_9)
    # make faulty mouse button less annoying
    btn_right = dict(hw=(None, 0), virt=None)
    def _handle_btn_right(event):
        _btn_right = (event.value, time.time())
        btn_right['hw'] = _btn_right
        if btn_right['virt'] == _btn_right[0]:
            return
        def _press_after_timeout():
            time.sleep(0.12)
            if btn_right['hw'] == _btn_right:
                km_switch.route_event(event)
                btn_right['virt'] = _btn_right[0]
        if event.value == 0:
            threading.Thread(target=_press_after_timeout).start()
        else:
            km_switch.route_event(event)
            btn_right['virt'] = _btn_right[0]
    km_switch.add_callback((ecodes.EV_KEY, ecodes.BTN_RIGHT), _handle_btn_right)
    # overload mouse side buttons
    btn_sideextra = {ecodes.BTN_SIDE: (None, 0), ecodes.BTN_EXTRA: (None, 0)}
    def _handle_btn_sideextra(event):
        # get previous event and store current
        prev = btn_sideextra[event.code]
        btn_sideextra[event.code] = (event, time.time())
        # cancel both if both have been pressed within the time frame
        other_code = next(c for c in btn_sideextra if c != event.code)
        other_event = btn_sideextra[other_code][0]
        btn_sideextra[other_code] = (other_event, 0)
        if other_event and other_event.value != 0:
            btn_sideextra[event.code] = (event, 0)
        # if the button was previously down, is now released, and it hasn't been too long
        if (prev[0] and prev[0].value == 1 and event.value == 0
                and time.time() - prev[1] < 0.15):
            km_switch.route_event(prev[0])
            km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.BTN_SIDE), _handle_btn_sideextra)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.BTN_EXTRA), _handle_btn_sideextra)
    # scroll by moving mouse when BTN_EXTRA is pressed
    def _handle_rel_xy(event):
        btn_extra = btn_sideextra[ecodes.BTN_EXTRA]
        if not btn_extra[0] or btn_extra[0].value == 0:
            pass
        elif time.time() - btn_extra[1] > 0.15:
            if event.code == ecodes.REL_X:
                event.code = ecodes.REL_HWHEEL
            elif event.code == ecodes.REL_Y:
                event.code = ecodes.REL_WHEEL
                event.value = -event.value
        km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_REL, ecodes.REL_X), _handle_rel_xy)
    km_switch.add_callback((ecodes.EV_REL, ecodes.REL_Y), _handle_rel_xy)
    # close tab on middle click when BTN_SIDE is pressed,
    # reopen tab with middle click when BTN_EXTRA is pressed
    def _handle_btn_middle(event):
        btn_side = btn_sideextra[ecodes.BTN_SIDE][0]
        btn_extra = btn_sideextra[ecodes.BTN_EXTRA][0]
        if btn_side and btn_side.value != 0:
            if event.value == 1:
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_W, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_W, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 0))
        elif btn_extra and btn_extra.value != 0:
            if event.value == 1:
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTSHIFT, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_T, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_T, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTSHIFT, 0))
        else:
            km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.BTN_MIDDLE), _handle_btn_middle)
    # enable horizontal scrolling when KEY_LEFTSHIFT is pressed,
    # switch between tabs when BTN_SIDE is pressed
    def _handle_rel_wheel(event):
        # tab switching
        btn_side = btn_sideextra[ecodes.BTN_SIDE][0]
        if btn_side and btn_side.value != 0:
            btn_sideextra[ecodes.BTN_SIDE] = (btn_side, 0)
            if event.value < 0:
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_TAB, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_TAB, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 0))
            else:
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTSHIFT, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_TAB, 1))
                km_switch.route_event(create_key_event(ecodes.KEY_TAB, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTCTRL, 0))
                km_switch.route_event(create_key_event(ecodes.KEY_LEFTSHIFT, 0))
            return
        # horizontal scrolling
        if {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT} & set(
            km_switch.hw_kbd[0].active_keys()
        ):
            event.code = ecodes.REL_HWHEEL
            # flip direction, down should be right and up left
            event.value = -event.value
        km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_REL, ecodes.REL_WHEEL), _handle_rel_wheel)


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
